#!/usr/bin/env python3
"""Independent Wuji inputs and original Revo2 mapping; never publishes commands.

The unchanged source wuji_bridge runs in a child. Mapping imports only the pinned
retarget/config files, avoiding the unrelated G1 top-level `teleop` package.
No SDK initialization, connection, or process happens at construction.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import tomllib
import types
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop')
SERIALS = {'left': 'WG1JA06260822033', 'right': 'WG1KA06260821549'}
SDK_USER = 'brainco-teleop'
STALE_S = .5  # Original teleop_hand.py --stale-s default: hold, then resume.
BRIDGE_LAUNCHER = (
    "import os,sys,runpy; "
    "stream=os.fdopen(os.dup(1),'w',buffering=1); os.dup2(2,1); sys.stdout=stream; "
    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_original(source=SOURCE, root=ROOT):
    source, root = Path(source), Path(root)
    locked = json.loads((root/'sources.lock.json').read_text())['hand_source']['sha256']
    hashes = {}
    for name in ('wuji_bridge.py', 'teleop/retarget.py', 'teleop/config.py', 'teleop_hand.py'):
        hashes[name] = digest(source/name)
        if hashes[name] != locked[name]:
            raise ValueError('Original Wuji source hash changed: '+name)
    def load(name):
        module = types.ModuleType('_dashboard_wuji_'+Path(name).stem)
        # compile/exec reads exact verified bytes and never writes upstream pyc.
        module.__file__ = str(source/name)
        exec(compile((source/name).read_bytes(), str(source/name), 'exec'), module.__dict__)
        return module
    retarget, config = load('teleop/retarget.py'), load('teleop/config.py')
    original = json.loads((source/'settings.json').read_text())
    # Reuse commissioned mapping limits, not the original motion-enabled backend.
    settings = config.validate({k: original[k] for k in
        ('target_min', 'target_max', 'max_step', 'segment_ms')}, hardware=False)
    return retarget, settings, hashes


def reader_python():
    """Find the existing SDK installation by files, without importing its manager."""
    if importlib.util.find_spec('wuji_sdk') is not None:
        return sys.executable
    cache = Path.home()/'.cache/uv/archive-v0'
    for path in sorted(cache.glob('*/lib/python*/site-packages/wuji_sdk-2026.11.9.dist-info')):
        python = path.parents[3]/'bin/python'
        if python.is_file():
            return str(python)
    raise RuntimeError('No installed wuji-sdk 2026.11.9 Python environment found')


def sdk_assets(side, sdk_root=None):
    root = Path(sdk_root) if sdk_root is not None else Path.home()/'.wuji/sdk'
    result = {'sdk_user': None, 'sdk_user_id': None, 'model_path': None, 'model_available': False,
              'sdk_model_sha256': None, 'sdk_error': ''}
    try:
        registry = tomllib.loads((root/'users/registry.toml').read_text())
        user = next(u for u in registry['users'] if u['user_id'] == registry['current_user_id'])
        uid = user['user_id']
        if user.get('is_default') or not uid or Path(uid).name != uid:
            raise ValueError('Select a calibrated named Wuji SDK user')
        result['sdk_user'] = user['display_name']
        result['sdk_user_id'] = uid
        if user['display_name'] != SDK_USER:
            raise ValueError('Select Wuji SDK user '+SDK_USER)
        model = root/'users'/uid/'models'/f'{side}_hand.urdf'
        result['model_path'] = str(model)
        result['model_available'] = model.is_file()
        if model.is_file():
            result['sdk_model_sha256'] = digest(model)
        else:
            result['sdk_error'] = f'Missing personal {side}_hand.urdf; complete Wuji hand-model calibration'
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        result['sdk_error'] = str(exc)
    return result


def valid_calibration(value, side, retarget):
    if value.get('side') != side or value.get('channels') != list(retarget.CHANNELS):
        raise ValueError('Calibration side or channel order does not match '+side)
    opened, closed = value.get('open'), value.get('closed')
    if not isinstance(opened, list) or not isinstance(closed, list) or len(opened) != 6 or len(closed) != 6:
        raise ValueError('Calibration must contain six open and closed readings')
    for i, (a, b) in enumerate(zip(opened, closed)):
        if i == 1 and a is None and b is None:
            continue
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or abs(b-a) < retarget.MIN_SPAN):
            raise ValueError('Invalid calibration span for '+retarget.CHANNELS[i])
    return value


class GloveSide:
    def __init__(self, owner, side):
        self.owner, self.side, self.sn = owner, side, SERIALS[side]
        self.lock, self.operations = threading.RLock(), threading.RLock()
        self.stop_event = threading.Event()
        self.process = self.thread = self.lease = None
        self.frame = None
        self.frames = self.restarts = 0
        self.running, self.error = False, ''
        self.tail = deque(maxlen=4)
        self.smooth = owner.retarget.LowPass(.3)  # Original teleop_hand default.
        self.previous = owner.clock()-1/120.
        self.capture_state = None
        self.capture_thread = None
        self.capture_context = None
        self.capture_result = {'state': 'idle', 'message': ''}
        self.samples = {}
        self.capture_error = ''
        self.calibration = None
        self.calibration_pending = False
        self.input_session = uuid.uuid4().hex
        self.reader_binding = None
        self.legacy_binding = None
        self.invalidation = None
        self.invalidation_fault = ''
        self.invalidation_path = self.owner.root/'config'/f'hand-calibration-{side}.invalidated.json'
        self.refresh()

    def _binding(self):
        return {'side': self.side, 'sn': self.sn, **{key: self.assets.get(key) for key in
            ('sdk_user', 'sdk_user_id', 'sdk_model_sha256')}}

    def _context(self):
        return {'input_session': self.input_session, 'binding': self._binding(),
                'invalidation_id': self.invalidation.get('id') if self.invalidation else None}

    @staticmethod
    def _atomic_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('x') as stream:
                stream.write(json.dumps(value, indent=2, allow_nan=False)+'\n')
                stream.flush(); os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _backup(path):
        if not path.exists(): return None
        backup = path.with_name(path.stem+'-'+str(time.time_ns())+'.json')
        with backup.open('xb') as stream:
            stream.write(path.read_bytes()); stream.flush(); os.fsync(stream.fileno())
        return backup

    def invalidate_calibration(self):
        """Persist a model-change barrier without modifying any original file.

        A later complete open/fist save acknowledges this exact marker id in one
        atomic calibration-file replacement. Keeping the marker avoids a crash
        window between saving new calibration and deleting an invalidation file.
        """
        with self.lock:
            self.calibration = None
            self.invalidation_fault = 'Calibration invalidation is not yet saved; repeat open/fist calibration after saving the marker'
            self.calibration_error = self.invalidation_fault
            self._discard_samples_locked('Personal model recalibrated; capture open and fist again', 'error',
                                         invalidate_saved=True)
            self.assets = sdk_assets(self.side, self.owner.sdk_root)
            project = self.owner.root/'config'/f'hand-calibration-{self.side}.json'
            old = project if project.exists() else self.owner.source/'calibration.json' if self.side == 'right' else project
            try:
                project.parent.mkdir(parents=True, exist_ok=True)
                backup = self._backup(project)
                marker_backup = self._backup(self.invalidation_path)
                marker = {'schema': 'dashboard-hand-calibration-invalidation-v1', 'id': uuid.uuid4().hex,
                    'time': datetime.now(timezone.utc).isoformat(), 'reason': 'SDK personal model recalibrated',
                    **self._binding(), 'previous_calibration_path': str(old),
                    'previous_calibration_sha256': digest(old) if old.exists() else None,
                    'backup_path': str(backup) if backup else None,
                    'previous_marker_backup_path': str(marker_backup) if marker_backup else None}
                self._atomic_json(self.invalidation_path, marker)
                self.invalidation = marker
                self.invalidation_fault = ''
            except (OSError, ValueError) as exc:
                self.invalidation_fault = 'Could not persist calibration invalidation; this hand remains disabled: '+str(exc)
                self.calibration_error = self.invalidation_fault
                raise RuntimeError(self.invalidation_fault) from exc
        return self.refresh()

    def _cancel_locked(self, message, state='cancelled'):
        capture = self.capture_state
        if capture is not None:
            capture['cancel'].set()
            self.capture_state = None
            self.capture_result = {'state': state, 'pose': capture['pose'],
                'samples': len(capture['samples']), 'message': message}
            self.capture_error = message if state == 'error' else ''

    def _discard_samples_locked(self, message, state='error', *, invalidate_saved=False):
        """Discard a reader-bound unfinished pair and its obsolete success text.

        A persisted calibration stays saved when its reader stops. An open-only
        capture does not survive that reader session, so its status must say why
        the pending open pose was discarded instead of claiming it is available.
        """
        active = self.capture_state is not None
        self._cancel_locked(message, state)
        prior = self.capture_result
        if not active and (prior.get('state') == 'open_captured' or
                           invalidate_saved and prior.get('state') == 'saved'):
            pose = prior.get('pose', 'open')
            self.capture_result = {'state': state, 'pose': pose,
                'samples': len(self.samples.get(pose, [])) or prior.get('samples', 0), 'message': message,
                'discarded': True}
            self.capture_error = message if state == 'error' else ''
        self.samples.clear()
        self.capture_context = None

    def refresh(self):
        with self.lock:
            before = self._binding() if hasattr(self, 'assets') else None
            self.assets = sdk_assets(self.side, self.owner.sdk_root)
            if before is not None and before != self._binding():
                self._discard_samples_locked('Glove user or personal model changed; capture open again', 'error',
                                             invalidate_saved=True)
                if self.running:
                    self.frame = None
                    self.error = 'Personal model changed; stop and restart this glove input'
            project = self.owner.root/'config'/f'hand-calibration-{self.side}.json'
            path = project if project.exists() or self.side == 'left' else self.owner.source/'calibration.json'
            self.calibration_path, self.calibration_error = str(path), ''
            self.calibration_pending = False
            try:
                if self.invalidation_fault:
                    raise ValueError(self.invalidation_fault)
                try:
                    marker = json.loads(self.invalidation_path.read_text())
                except FileNotFoundError:
                    marker = None
                except (OSError, ValueError) as exc:
                    self.invalidation_fault = 'Cannot read calibration invalidation marker: '+str(exc)
                    raise ValueError(self.invalidation_fault) from exc
                if marker is not None and (not isinstance(marker, dict)
                        or marker.get('schema') != 'dashboard-hand-calibration-invalidation-v1'
                        or marker.get('side') != self.side
                        or not isinstance(marker.get('id'), str) or not marker['id']):
                    self.invalidation_fault = 'Invalid calibration invalidation marker; save a new invalidation before calibrating'
                    raise ValueError(self.invalidation_fault)
                self.invalidation = marker
                value = valid_calibration(json.loads(path.read_text()), self.side, self.owner.retarget)
                if marker is not None and (path != project or value.get('supersedes_invalidation') != marker['id']):
                    self.calibration_pending = True
                    raise ValueError('Personal model recalibrated; capture open and fist again')
                for key, expected in self._binding().items():
                    if key in value and value[key] != expected:
                        raise ValueError('Calibration identity changed: '+key+'; capture open and fist again')
                if path != project:
                    if self.legacy_binding is None: self.legacy_binding = self._binding()
                    if self.legacy_binding != self._binding():
                        raise ValueError('Personal hand model changed; repeat the original open/fist calibration')
                self.calibration = value
            except FileNotFoundError as exc:
                self.calibration = None
                self.calibration_pending = True
                self.calibration_error = 'Capture open and fist to create this hand calibration'
            except (OSError, ValueError, AttributeError) as exc:
                self.calibration, self.calibration_error = None, str(exc)
        return self.status()

    def status(self):
        with self.lock:
            age = None if self.frame is None else self.owner.clock()-self.frame['source_t']
            compatible = self.reader_binding is None or self.reader_binding == self._binding()
            live = bool(self.running and compatible and age is not None and 0 <= age <= STALE_S)
            capture = None if self.capture_state is None else {
                'pose': self.capture_state['pose'], 'samples': len(self.capture_state['samples']),
                'remaining_s': max(0., self.capture_state['end']-self.owner.clock()),
                'elapsed_s': max(0., self.owner.clock()-self.capture_state['start']),
                'progress': min(1., max(0., (self.owner.clock()-self.capture_state['start']) /
                    (self.capture_state['end']-self.capture_state['start'])))}
            mapping_ready = bool(self.calibration and self.assets['model_available'] and not self.assets['sdk_error'])
            preview = {'ready': False, 'input_live': live, 'channels': list(self.owner.retarget.CHANNELS),
                'closure': None, 'goals': None, 'target_min': list(self.owner.settings['target_min']),
                'target_max': list(self.owner.settings['target_max']), 'units': '0-1000',
                'filtered': False, 'published': False, 'error': ''}
            if live and mapping_ready and capture is None:
                raw = self.owner.retarget.readings(self.frame, self.side)
                if raw is not None:
                    preview['closure'] = self.owner.retarget.closure(raw, self.calibration)
                    preview['goals'] = self.owner.retarget.targets(preview['closure'],
                        self.owner.settings['target_min'], self.owner.settings['target_max'])
                    preview['ready'] = True
            if not preview['ready']:
                preview['error'] = ('Calibration capture in progress' if capture else
                    'Waiting for live glove input' if not live else
                    self.assets['sdk_error'] or self.calibration_error or 'Complete open/fist calibration')
            return dict(self.assets, side=self.side, sn=self.sn, running=self.running,
                connected=live, live=live, input_live=live, frames=self.frames, restarts=self.restarts,
                model_ready=bool(self.assets['model_available'] and not self.assets['sdk_error']),
                calibration_ready=self.calibration is not None,
                calibration_pending=bool(self.calibration_pending and self.calibration is None
                    and self.assets['model_available'] and not self.assets['sdk_error'] and not self.invalidation_fault),
                age_ms=None if age is None else round(age*1000, 1),
                angles=None if self.frame is None else dict(self.frame['joint_angles']),
                seq=None if self.frame is None else self.frame['seq'],
                mapping_ready=mapping_ready, input_session=self.input_session,
                reader_model_current=compatible,
                calibration_path=self.calibration_path, calibration_available=self.calibration is not None,
                calibration_invalidated=bool(self.invalidation_fault or (self.invalidation and
                    (not self.calibration or self.calibration.get('supersedes_invalidation') != self.invalidation['id']))),
                calibration_invalidation=self.invalidation,
                calibration_invalidation_error=self.invalidation_fault,
                calibration_origin='project' if Path(self.calibration_path).parent == self.owner.root/'config' else 'original_repository',
                calibration_binding_verified=bool(self.calibration and all(
                    self.calibration.get(k) == v for k, v in self._binding().items())),
                calibration_error=self.calibration_error, error=self.error, reader_log=list(self.tail),
                capture=capture, capture_error=self.capture_error,
                capture_result=dict(self.capture_result), capture_cancel_ready=capture is not None, preview=preview,
                captured_poses={key: len(value) for key, value in self.samples.items()},
                confidence_threshold=.5, source_stale_s=STALE_S, output_publisher=False)

    def accept(self, frame):
        """Only the owned, unchanged bridge's JSON output calls this method."""
        now = self.owner.clock()
        if (not isinstance(frame, dict) or frame.get('hand_side') != self.side
                or frame.get('sn') != self.sn or frame.get('sdk_user') != SDK_USER
                or type(frame.get('source_t')) not in (float, int)
                or not math.isfinite(frame['source_t']) or not 0 <= now-frame['source_t'] <= STALE_S
                or type(frame.get('seq')) is not int):
            return False
        raw = self.owner.retarget.readings(frame, self.side)
        if raw is None:
            return False
        with self.lock:
            if not self.running or (self.reader_binding is not None and self.reader_binding != self._binding()):
                return False
            if self.frame is not None and (frame['seq'], frame.get('timestamp_us')) == (
                    self.frame['seq'], self.frame.get('timestamp_us')):
                return False
            self.frame = dict(frame)
            self.frames += 1
            self.error = ''
            if self.capture_state is not None and now <= self.capture_state['end']:
                self.capture_state['samples'].append(raw)
        return True

    def start(self):
        with self.operations:
            if self.running:
                self.refresh()
                if self.reader_binding is not None and self.reader_binding != self._binding():
                    raise ValueError('Personal model changed; stop and restart this glove input')
                return self.status()
            if self.thread is not None:
                self.stop()
            self.refresh()
            if self.assets['sdk_user'] != SDK_USER:
                raise ValueError(self.assets['sdk_error'])
            # Missing a personal model is reported; read-only input may still be inspected.
            self.owner.verify_source()
            python = self.owner.python or reader_python()
            leases = self.owner.root/'diagnostics'
            leases.mkdir(parents=True, exist_ok=True)
            lease = (leases/f'dashboard-glove-{self.side}.lock').open('a')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                raise RuntimeError(f'Another dashboard reader owns the {self.side} glove') from None
            self.lease = lease
            self.stop_event.clear()
            with self.lock:
                self.frame, self.error, self.running = None, '', True
                self._discard_samples_locked('Glove input restarted; capture open again', 'error')
                self.input_session = uuid.uuid4().hex
                self.reader_binding = self._binding()
                self.smooth = self.owner.retarget.LowPass(.3)
                self.previous = self.owner.clock()-1/120.
            self.thread = threading.Thread(target=self._run, args=(python,), daemon=True)
            self.thread.start()
            return self.status()

    def _run(self, python):
        process = logger = None
        try:
            argv = [python, '-u', '-B', '-c', BRIDGE_LAUNCHER,
                str(self.owner.source/'wuji_bridge.py'), '--sn', self.sn,
                '--side', self.side, '--user', SDK_USER]
            if self.stop_event.is_set(): return
            process = self.owner.popen(argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors='replace', bufsize=1, cwd=self.owner.root, close_fds=True)
            with self.lock:
                self.process = process
            def logs():
                for line in process.stderr:
                    with self.lock: self.tail.append(line.strip()[:1000])
            logger = threading.Thread(target=logs, daemon=True)
            logger.start()
            for line in process.stdout:
                if self.stop_event.is_set(): break
                try: self.accept(json.loads(line))
                except (ValueError, TypeError, KeyError): continue
            with self.lock:
                self.error = '' if self.stop_event.is_set() else 'Original glove reader ended; this side holds. Click Check inputs to retry.'
        except OSError as exc:
            with self.lock: self.error = str(exc)
        finally:
            try:
                if process is not None: self._close_process(process, logger)
            finally:
                with self.lock:
                    self._discard_samples_locked('Glove input ended; restart input and capture open again', 'error')
                    self.process, self.running = None, False

    @staticmethod
    def _close_process(process, logger=None):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)  # Original bridge handles cleanup.
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)
        if logger is not None: logger.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None: stream.close()

    def stop(self):
        with self.operations:
            self.stop_event.set()
            with self.lock:
                self._discard_samples_locked('Glove input stopped; capture open again', 'cancelled')
                process = self.process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
            if self.thread:
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    with self.lock: process = self.process
                    if process is not None and process.poll() is None: process.kill()
                    self.thread.join(timeout=2)
                    if self.thread.is_alive():
                        raise RuntimeError('Original glove reader is still closing')
            with self.lock:
                self.running, self.frame, self.capture_state = False, None, None
            capture_thread = self.capture_thread
            if capture_thread is not None and capture_thread is not threading.current_thread():
                capture_thread.join(timeout=1)
            if self.lease:
                self.lease.close(); self.lease = None
            return self.status()

    def target(self, measured):
        with self.lock:
            status = self.status()
            if not status['live'] or not status['mapping_ready'] or self.capture_state is not None:
                return None
            if (not isinstance(measured, (list, tuple)) or len(measured) != 6
                    or any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1000 for x in measured)):
                return None
            # The hand reports integer 0..1000 registers through DDS float32.
            # Restore those register values before the original integer limiter;
            # e.g. 617 -> .61699998 -> 616.99998 must not become a step of 81.
            measured = [int(round(value)) for value in measured]
            now = self.owner.clock()
            dt, self.previous = now-self.previous, now
            raw = self.owner.retarget.readings(self.frame, self.side)
            fraction = self.smooth(self.owner.retarget.closure(raw, self.calibration), dt=dt)
            goal = self.owner.retarget.targets(fraction, self.owner.settings['target_min'], self.owner.settings['target_max'])
            target = self.owner.retarget.step_limited(goal, measured, int(self.owner.settings['max_step']*.8))
            return {'side': self.side, 'sn': self.sn, 'seq': self.frame['seq'],
                    'source_t': self.frame['source_t'], 'goal': goal, 'measured': list(measured),
                    'targets': target, 'q_normalized': [x/1000. for x in target],
                    'channels': list(self.owner.retarget.CHANNELS), 'published': False}

    def capture(self, pose, seconds=2.):
        if pose not in ('open', 'fist') or type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise ValueError('Choose open or fist and a finite capture duration up to 30 seconds')
        self.refresh()
        with self.lock:
            if self.invalidation_fault:
                raise ValueError(self.invalidation_fault)
            if (self.stop_event.is_set() or not self.status()['live']
                    or not self.assets['model_available'] or self.assets['sdk_error']):
                raise ValueError('Capture requires live original-filtered glove data and its personal SDK model')
            if self.capture_state is not None:
                raise ValueError('A calibration capture is already in progress')
            if pose == 'fist' and not self.samples.get('open'):
                raise ValueError('Capture the open hand first')
            if pose == 'fist' and self.capture_context != self._context():
                self._discard_samples_locked('Glove model or input session changed; capture open again')
                raise ValueError('Glove model or input session changed; capture open again')
            if pose == 'open':
                self.samples.clear()
                self.capture_context = self._context()
            self.capture_error = ''
            now = self.owner.clock()
            capture = {'pose': pose, 'start': now, 'end': now+seconds, 'samples': [],
                       'context': self._context(), 'cancel': threading.Event()}
            self.capture_state = capture
            self.capture_result = {'state': 'capturing', 'pose': pose, 'samples': 0, 'message': ''}
        def finish():
            if capture['cancel'].wait(seconds): return
            with self.lock:
                if self.capture_state is not capture: return
                try:
                    # Read files again: SDK model tools may replace the model while
                    # samples arrive. Never bind those old samples to the new hash.
                    self.refresh()
                    if self.capture_state is not capture: return
                    if capture['context'] != self._context() or self.capture_context != capture['context']:
                        self.samples.clear()
                        self.capture_context = None
                        raise ValueError('Glove model or input session changed; capture open again')
                    if not capture['samples']: raise ValueError('No valid glove frames during capture')
                    self.samples[pose] = capture['samples']
                    if pose == 'fist':
                        value = self.owner.retarget.calibrate(self.samples['open'], self.samples['fist'])
                        value.update(self._binding(), time=datetime.now(timezone.utc).isoformat(),
                            capture_session=self.input_session,
                            supersedes_invalidation=capture['context']['invalidation_id'],
                            sample_counts={key: len(v) for key, v in self.samples.items()},
                            calibration_method='original_retarget_median',
                            source_sha256=self.owner.hashes['teleop/retarget.py'])
                        valid_calibration(value, self.side, self.owner.retarget)
                        path = self.owner.root/'config'/f'hand-calibration-{self.side}.json'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        backup = self._backup(path)
                        self._atomic_json(path, value)
                        self.refresh()
                        if self.calibration is None:
                            raise ValueError(self.calibration_error)
                        self.smooth = self.owner.retarget.LowPass(.3)
                        self.capture_result = {'state': 'saved', 'pose': pose,
                            'samples': len(capture['samples']), 'message': 'Open/fist calibration saved',
                            'path': str(path), 'backup_path': str(backup) if backup else None}
                    else:
                        self.capture_result = {'state': 'open_captured', 'pose': pose,
                            'samples': len(capture['samples']), 'message': 'Open pose captured; capture a full fist next'}
                except (OSError, ValueError) as exc:
                    self.capture_error = str(exc)
                    self.capture_result = {'state': 'error', 'pose': pose,
                        'samples': len(capture['samples']), 'message': str(exc)}
                    self.samples.pop(pose, None)
                finally:
                    if self.capture_state is capture: self.capture_state = None
        self.capture_thread = threading.Thread(target=finish, daemon=True)
        self.capture_thread.start()
        return self.status()

    def cancel_capture(self):
        with self.lock:
            self._cancel_locked('Calibration capture cancelled; existing calibration unchanged')
        return self.status()


class DashboardGloves:
    def __init__(self, root=ROOT, python=None, source_dir=SOURCE, *, sdk_root=None,
                 clock=time.monotonic, popen=subprocess.Popen):
        self.root, self.source = Path(root).resolve(), Path(source_dir).resolve()
        self.python, self.sdk_root, self.clock, self.popen = python, sdk_root, clock, popen
        self.retarget, self.settings, self.hashes = load_original(self.source, self.root)
        self.hands = {side: GloveSide(self, side) for side in SERIALS}

    def verify_source(self):
        for name, expected in self.hashes.items():
            if digest(self.source/name) != expected: raise ValueError('Original source changed: '+name)

    def status(self): return {side: hand.status() for side, hand in self.hands.items()}
    def refresh(self, side=None):
        if side is not None: return self.hands[side].refresh()
        return {name: hand.refresh() for name, hand in self.hands.items()}
    def start_input(self, side): return self.hands[side].start()
    def stop_input(self, side): return self.hands[side].stop()
    def target(self, side, measured_positions): return self.hands[side].target(measured_positions)
    def capture(self, side, pose, seconds=2.): return self.hands[side].capture(pose, seconds)
    def cancel_capture(self, side): return self.hands[side].cancel_capture()
    def invalidate_calibration(self, side): return self.hands[side].invalidate_calibration()
    def close(self):
        for hand in self.hands.values(): hand.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true', help='Read local metadata only; no SDK or devices')
    parser.add_argument('--serve', action='store_true', help='JSONL start_input/stop_input/target/capture/status/stop requests')
    args = parser.parse_args()
    manager = DashboardGloves()
    try:
        if args.status or not args.serve:
            print(json.dumps(manager.status(), ensure_ascii=False)); return
        for line in sys.stdin:
            try:
                command = json.loads(line)
                name, side = command['command'], command.get('side')
                if name == 'stop': break
                if name == 'status': value = manager.status()
                elif name == 'refresh': value = manager.refresh(side)
                elif name == 'start_input': value = manager.start_input(side)
                elif name == 'stop_input': value = manager.stop_input(side)
                elif name == 'target': value = manager.target(side, command.get('measured'))
                elif name == 'capture': value = manager.capture(side, command['pose'], command.get('seconds', 2.))
                elif name == 'cancel_capture': value = manager.cancel_capture(side)
                elif name == 'invalidate_calibration': value = manager.invalidate_calibration(side)
                else: raise ValueError('Unknown glove command')
                print(json.dumps({'type': 'gloves', 'result': value}, allow_nan=False), flush=True)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(json.dumps({'type': 'error', 'error': str(exc)}), flush=True)
    finally:
        manager.close()


if __name__ == '__main__': main()
