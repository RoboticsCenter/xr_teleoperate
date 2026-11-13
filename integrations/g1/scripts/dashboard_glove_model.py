"""Asynchronous personal Wuji hand-model calibration; no robot control imports."""
from collections import deque
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET

CLI = Path('/home/jack-xiang/.local/opt/wuji-cli/2026.10.26/wuji')
SDK_USER = 'brainco-teleop'
SERIALS = dict(left='WG1JA06260822033', right='WG1KA06260821549')
POSES = {
    'pinch_index': '拇指尖与食指尖相碰，其他手指自然放松。',
    'pinch_middle': '拇指尖与中指尖相碰，其他手指自然放松。',
    'pinch_ring': '拇指尖与无名指尖相碰，其他手指自然放松。',
    'pinch_pinky': '拇指尖与小指尖相碰，其他手指自然放松。',
    'four_finger_bend_90': '四根手指弯曲约90度，拇指自然放松。',
    'flat_open': '手掌平展，所有手指伸直，保持手腕稳定。',
}

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def profile(sdk_root):
    users = Path(sdk_root)/'users'
    registry = tomllib.loads((users/'registry.toml').read_text())
    uid = registry['current_user_id']
    record = next(user for user in registry['users'] if user['user_id'] == uid)
    if not uid or Path(uid).name != uid or uid in ('.', '..') or record.get('is_default'):
        raise ValueError('请选择已命名的 Wuji SDK 用户 brainco-teleop。')
    if record.get('display_name') != SDK_USER:
        raise ValueError('当前 Wuji SDK 用户不是 brainco-teleop；未自动切换用户。')
    return dict(user_id=uid, name=SDK_USER, directory=users/uid)

def guidance(feedback):
    name, state = feedback.get('step_name', ''), feedback.get('state', '')
    instruction = POSES.get(name, '请完成 '+str(name).replace('_', ' ')+'，并保持稳定。')
    if state == 'waiting_movement':
        instruction = '先张开手掌，再完成当前姿态。'+instruction
    elif state == 'waiting_stable':
        instruction += ' 请保持不动。'+(' 当前手部移动过多。' if feedback.get('variance_ok') is False else '')
    elif state == 'collecting':
        instruction += ' 正在采样，请继续保持。'
    elif state == 'done':
        index, total = feedback.get('step_index'), feedback.get('step_total')
        instruction = ('姿态采集完成，等待模型求解和保存。' if type(index) is int and type(total) is int and index+1 >= total
                       else '当前姿态采集完成，请先张开手掌，再做下一步。')
    diagnostics = []
    for metric in feedback.get('metrics', []) if isinstance(feedback.get('metrics'), list) else []:
        if not isinstance(metric, dict):
            continue
        error = metric.get('error')
        if type(error) in (int, float):
            error = error*1000 if metric.get('unit') == 'm' else error
            if not math.isfinite(error) or error <= .0001:
                continue
        hint = metric.get('hint')
        if not isinstance(hint, str) or not hint.strip():
            continue
        group = str(metric.get('finger', ''))
        if metric.get('finger_b') is not None:
            group += '-'+str(metric['finger_b'])
        diagnostics.append((group, hint))
    messages = [(group+': ' if group else '')+hint for group, hint in sorted(diagnostics, key=lambda x: x[0])][:8]
    if feedback.get('constraints_ok') is False and not messages:
        messages = ['请调整当前姿态，等待 SDK 约束通过。']
    return instruction, messages

class GloveModelManager:
    def __init__(self, root, *, cli=CLI, sdk_root=None, before_start=None, on_finish=None,
                 simulation=False, popen=subprocess.Popen, cancel_grace_s=5.):
        self.root, self.cli = Path(root), str(cli)
        self.sdk_root = Path(sdk_root) if sdk_root is not None else Path.home()/'.wuji/sdk'
        self.before_start, self.on_finish = before_start, on_finish
        self.simulation, self._popen, self.cancel_grace_s = simulation, popen, cancel_grace_s
        self._simulation_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._active_side = self._thread = self._process = self._process_start = None
        self._cancel, self._continue = threading.Event(), threading.Event()
        self._closed = False
        self._states = {side: self._initial(side) for side in SERIALS}
        self._tails = {side: deque(maxlen=30) for side in SERIALS}

    def _initial(self, side):
        return dict(side=side, sn=SERIALS[side], sdk_user=SDK_USER, running=False,
            phase='idle', state='idle', completed=False, success=False, error=None,
            instruction='', diagnostics=[], progress_fraction=0., feedback={}, step_name=None,
            step_index=None, step_total=6, awaiting_continue=False, prompt_id=None,
            backup_path=None, backup_bundle=None, output_dir=None, model_path=None,
            model_sha256=None, model_verified=False, cancel_requested=False,
            exit_code=None, terminal_event=None, simulation=self.simulation,
            robot_output=False, started_at=None)

    @property
    def active(self):
        with self._lock:
            return self._active_side is not None

    def status(self, side=None):
        with self._lock:
            if side is None:
                side = self._active_side
                if side is None:
                    return dict(active=False, running=False, phase='idle', sides={s: self.status(s) for s in SERIALS})
            if side not in SERIALS:
                raise ValueError('unknown glove side')
            return dict(self._states[side], active=self._active_side == side,
                        active_side=self._active_side, log_tail=list(self._tails[side]))

    def _update(self, side, **values):
        with self._lock:
            self._states[side].update(values)

    def start(self, side):
        if side not in SERIALS:
            raise ValueError('unknown glove side')
        with self._lock:
            if self._closed or self._active_side is not None:
                raise RuntimeError('已有个人模型校准正在进行，或管理器已关闭。')
            self._active_side = side
            self._cancel.clear()
            self._continue.clear()
            self._states[side] = self._initial(side)
            self._tails[side].clear()
            self._states[side].update(running=True, phase='preparing', state='preparing',
                started_at=datetime.now(timezone.utc).isoformat(), instruction='正在停止手套读取并备份现有用户模型。')
            self._thread = threading.Thread(target=self._run, args=(side,), daemon=True, name='wuji-hand-model-'+side)
            self._thread.start()
            return self.status(side)

    def continue_calibration(self, side, prompt_id=None):
        with self._lock:
            if self._active_side != side:
                raise RuntimeError('该侧没有正在进行的个人模型校准。')
            current = self._states[side]
            if self.simulation and current['awaiting_continue']:
                if prompt_id is not None and prompt_id != current['prompt_id']:
                    raise ValueError('stale calibration prompt')
                self._continue.set()
            return self.status(side)

    def cancel(self, side=None):
        with self._lock:
            selected = self._active_side
            if selected is None:
                return self.status(side) if side else self.status()
            if side is not None and selected != side:
                raise RuntimeError('另一侧正在进行个人模型校准。')
            self._cancel.set()
            self._states[selected].update(cancel_requested=True, phase='cancelling', state='cancelling',
                                          instruction='正在取消 SDK 校准并释放手套。')
            return self.status(selected)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise InterruptedError('model calibration cancelled')

    def close(self):
        with self._lock:
            self._closed = True
            self.cancel()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self.cancel_grace_s+8)

    def _run(self, side):
        lease, readers = None, []
        success, failure = False, None
        try:
            directory = (self.root/'diagnostics'/'simulation-glove-model'/self._simulation_id
                         if self.simulation else self.root/'diagnostics')
            directory.mkdir(parents=True, exist_ok=True)
            lease = (directory/'dashboard-glove-model.lock').open('a+')
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.before_start:
                self.before_start(side)
            self._check_cancel()
            for hand in SERIALS:
                lock = (directory/f'dashboard-glove-{hand}.lock').open('a+')
                readers.append(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.simulation:
                for index, name in enumerate(POSES):
                    self._continue.clear()
                    self._update(side, phase='calibrating', state='simulation_pose', step_index=index,
                        step_name=name, progress_fraction=index/6., instruction='模拟：'+POSES[name],
                        awaiting_continue=True, prompt_id=str(index))
                    while not self._continue.wait(.05):
                        self._check_cancel()
                    self._check_cancel()
                success = True
            else:
                self._prepare_and_calibrate(side)
                success = True
        except InterruptedError:
            pass
        except Exception as exc:
            failure = type(exc).__name__+': '+str(exc)
        finally:
            self._update(side, phase='finishing', state='finishing', awaiting_continue=False)
            for lock in reversed(readers):
                lock.close()
            # The parent may restart a reader here; reader leases are already
            # released, but this model reservation remains active until return.
            if self._cancel.is_set():
                success = False
            try:
                if self.on_finish:
                    self.on_finish(side, success)
            except Exception as exc:
                failure = failure or ('refresh failed: '+str(exc))
                success = False
            if lease:
                lease.close()
            with self._lock:
                if self._cancel.is_set():
                    success = False
                cancelled = self._cancel.is_set() and not success
                self._states[side].update(running=False, completed=success, success=success,
                    phase='completed' if success else 'cancelled' if cancelled else 'error',
                    state='completed' if success else 'cancelled' if cancelled else 'error', error=failure,
                    progress_fraction=1. if success else self._states[side]['progress_fraction'],
                    instruction='模拟流程完成；未校准真实模型。' if success and self.simulation else
                        '个人手型模型已保存；下一步采集张开/握拳映射。' if success else
                        '校准已取消。' if cancelled else '个人模型校准未完成，请查看错误。')
                self._active_side = None

    def _accept(self, side, record):
        if record.get('schema_version') != 2 or record.get('calibration') != 'hand_model' or record.get('operation') != 'calibrate':
            return
        event = record.get('event')
        if event == 'progress' and isinstance(record.get('feedback'), dict):
            feedback = record['feedback']
            instruction, diagnostics = guidance(feedback)
            progress = feedback.get('progress')
            progress = min(1., max(0., progress)) if type(progress) in (float, int) and math.isfinite(progress) else None
            self._update(side, phase='calibrating', state=feedback.get('state', 'calibrating'),
                feedback=feedback, progress_fraction=progress, step_name=feedback.get('step_name'),
                step_index=feedback.get('step_index'), step_total=feedback.get('step_total', 6),
                instruction=instruction, diagnostics=diagnostics, awaiting_continue=False)
        elif event in ('result', 'error', 'cancelled'):
            with self._lock:
                if self._states[side]['terminal_event'] is not None:
                    raise ValueError('duplicate CLI terminal result')
                self._states[side]['terminal_event'] = record

    def _prepare_and_calibrate(self, side):
        before = profile(self.sdk_root)
        report = self._json_command(side, ['user', 'show', '--json'])
        if report.get('name') != SDK_USER or report.get('is_current') is not True:
            raise ValueError('CLI current profile differs from brainco-teleop')
        output = self.root/'calibrations'/('sdk-model-'+side+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
        output.mkdir(parents=True, exist_ok=False)
        backup = output/'files-before'
        backup.mkdir()
        shutil.copy2(self.sdk_root/'users/registry.toml', backup/'registry.toml')
        if before['directory'].is_dir():
            shutil.copytree(before['directory'], backup/before['user_id'])
        else:
            # A new named user may have no model directory until the first
            # successful calibration. The registry still needs a backup.
            (backup/before['user_id']).mkdir()
        hashes = {str(path.relative_to(backup)): sha(path) for path in backup.rglob('*') if path.is_file()}
        (output/'backup-manifest.json').write_text(json.dumps(hashes, indent=2)+'\n')
        model = before['directory']/'models'/f'{side}_hand.urdf'
        other = before['directory']/'models'/f'{"right" if side == "left" else "left"}_hand.urdf'
        other_hash = sha(other) if other.is_file() else None
        self._update(side, phase='backing_up', backup_path=str(backup), output_dir=str(output), model_path=str(model))
        if any(report.get(key, {}).get('calibrated') for key in ('left_hand', 'right_hand')) or report.get('tactile'):
            bundle = output/'sdk-before.zip'
            self._json_command(side, ['user', 'export', str(bundle), '--json'], timeout=30)
            if not bundle.is_file() or not bundle.stat().st_size:
                raise ValueError('SDK user export did not create a backup')
            self._update(side, backup_bundle=str(bundle))
        if profile(self.sdk_root)['user_id'] != before['user_id']:
            raise ValueError('SDK current user changed during backup')
        identity = self._json_command(side, ['get', 'hand_side', '--sn', SERIALS[side], '--json'])
        if not isinstance(identity.get('value'), str) or identity['value'].lower() != side:
            raise ValueError('selected glove handedness does not match '+side)
        self._check_cancel()
        if profile(self.sdk_root)['user_id'] != before['user_id']:
            raise ValueError('SDK current user changed before calibration')
        self._update(side, phase='connecting', state='connecting', instruction='正在连接指定手套，等待 SDK 姿态提示。')
        with (output/'cli.jsonl').open('w', buffering=1) as logfile:
            code, _ = self._command(side, ['calib', 'hand-model', '--sn', SERIALS[side], '--jsonl'],
                timeout=925, event_handler=lambda record: self._accept(side, record), logfile=logfile)
        self._update(side, exit_code=code)
        terminal = self.status(side)['terminal_event']
        if self._cancel.is_set() and (terminal is None or terminal.get('event') != 'result'):
            raise InterruptedError('calibration cancelled')
        if code != 0 or not terminal or terminal.get('event') != 'result' or terminal.get('status') != 'ok':
            detail = terminal.get('error') if terminal else 'CLI ended without a terminal result'
            raise RuntimeError('Wuji calibration did not succeed: '+str(detail))
        result = terminal.get('result', {})
        user = result.get('sdk_user')
        if isinstance(user, dict):
            user = user.get('name', user.get('display_name'))
        if user != SDK_USER or str(result.get('handedness', '')).lower() != side:
            raise ValueError('CLI result user/handedness differs from the requested binding')
        if profile(self.sdk_root)['user_id'] != before['user_id']:
            raise ValueError('SDK user changed during calibration; inspect the saved backup')
        if not model.is_file() or ET.parse(model).getroot().tag != 'robot':
            raise ValueError('CLI result has no valid expected-side URDF artifact')
        if (sha(other) if other.is_file() else None) != other_hash:
            raise ValueError('unselected hand model changed; inspect the saved backup')
        self._update(side, model_verified=True, model_sha256=sha(model))

    def _signal(self, process, signum):
        if process.poll() is None:
            try:
                identity = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                if identity == self._process_start:
                    os.killpg(process.pid, signum)
            except (FileNotFoundError, ProcessLookupError):
                pass

    def _command(self, side, arguments, *, timeout, event_handler=None, logfile=None):
        self._check_cancel()
        process = self._popen([self.cli, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', bufsize=1, start_new_session=True, close_fds=True)
        with self._lock:
            self._process = process
            try:
                self._process_start = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            except FileNotFoundError:
                self._process_start = None
        lines, records = [], queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    records.put(line)
            finally:
                records.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic()+timeout
        cancelled_at = terminated_at = None
        eof, captured, logged = False, 0, 0
        try:
            while not eof or process.poll() is None:
                now = time.monotonic()
                if (self._cancel.is_set() or now >= deadline) and cancelled_at is None:
                    self._signal(process, signal.SIGINT)
                    cancelled_at = now
                if cancelled_at is not None and terminated_at is None and now-cancelled_at >= self.cancel_grace_s:
                    self._signal(process, signal.SIGTERM)
                    terminated_at = now
                if terminated_at is not None and now-terminated_at >= 2:
                    self._signal(process, signal.SIGKILL)
                try:
                    line = records.get(timeout=.05)
                except queue.Empty:
                    continue
                if line is None:
                    eof = True
                    continue
                clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line).rstrip('\r\n')
                with self._lock:
                    self._tails[side].append(clean[:1600])
                if captured < 1_000_000:
                    lines.append(line)
                    captured += len(line)
                if logfile is not None and logged < 10_000_000:
                    logfile.write(clean[:20000]+'\n')
                    logfile.flush()
                    logged += len(clean[:20000])+1
                if event_handler:
                    try:
                        record = json.loads(clean, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        event_handler(record)
            process.wait(timeout=2)
            if time.monotonic() >= deadline and not self._cancel.is_set():
                raise TimeoutError('Wuji CLI operation exceeded its deadline')
            return process.returncode, ''.join(lines)
        finally:
            if process.poll() is None:
                self._signal(process, signal.SIGINT)
                try:
                    process.wait(timeout=self.cancel_grace_s)
                except subprocess.TimeoutExpired:
                    self._signal(process, signal.SIGKILL)
                    process.wait(timeout=2)
            reader.join(timeout=1)
            process.stdout.close()
            with self._lock:
                self._process = self._process_start = None

    def _json_command(self, side, arguments, timeout=20):
        code, text = self._command(side, arguments, timeout=timeout)
        self._check_cancel()
        if code != 0:
            raise RuntimeError('Wuji CLI failed ('+str(code)+'): '+text[-1200:].strip())
        decoder = json.JSONDecoder()
        candidates = []
        for match in re.finditer(r'\{', text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                if isinstance(value, dict):
                    candidates.append(value)
            except ValueError:
                pass
        # SDK diagnostics may contain JSON before the command's actual result.
        for value in reversed(candidates):
            if arguments[:2] == ['user', 'show'] and not ('name' in value and 'is_current' in value):
                continue
            if arguments[:2] == ['get', 'hand_side'] and 'value' not in value:
                continue
            return value
        raise ValueError('Wuji CLI returned no JSON object')
