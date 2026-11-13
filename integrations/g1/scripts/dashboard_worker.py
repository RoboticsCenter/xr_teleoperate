#!/usr/bin/env python3
"""G1 wrist teleoperation through normal-mode rt/arm_sdk arm blending.

Packet age, pose validity, session binding, hold/resume and geometry are upstream.
Stationary poses remain usable while their packets are fresh, as requested.
No ten-second policy or old G1 preview/nonce implementation is used here.
"""
import argparse
import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'vendor/xr_h2'
sys.path.insert(0, str(VENDOR))
from pico_tracker.geometry import IncrementalWrist, heading_basis, pose_valid
from pico_tracker.input import read_frame
from branch_arm_input import capture, live as arms_live, selected_sides
from arm_pose_source import effective_config, select_frame, validate_source
from arm_reference_frame import reference_frame, validate_reference_frame, reference_mapping
from network_config import robot_host

CONTROL_TOPIC = 'rt/arm_sdk'
CONTROL_MODE = 'normal_walking'


def remote_exit_reason(line):
    """Classify startup stderr without exposing SSH argv or credentials."""
    if 'unrecognized arguments:' in line and '--control-mode' in line:
        return 'G1 启动脚本版本不匹配：机器人端不支持 normal_walking 参数。'
    if 'ModuleNotFoundError:' in line or 'ImportError:' in line:
        return 'G1 控制程序缺少运行依赖；请查看本次 runtime.log。'
    if 'SyntaxError:' in line:
        return 'G1 控制脚本存在语法错误；请查看本次 runtime.log。'
    if 'No route to host' in line or 'Connection timed out' in line:
        return 'G1 网络连接中断；请检查机器人电源及 Wi-Fi。'
    return None


def require_arm_sdk(record, *, ready=False):
    """Reject old Debug adapters; never infer a control mode from missing data."""
    if (not isinstance(record, dict) or record.get('control_topic') != CONTROL_TOPIC or
            record.get('control_mode') != CONTROL_MODE):
        raise RuntimeError('G1 adapter is not verified for normal walking arm_sdk control; refusing arm commands.')
    if ready and (type(record.get('arm_weight')) not in (int,float) or record['arm_weight'] != 0.):
        raise RuntimeError('G1 LAUNCH did not report zero arm_sdk weight; refusing START.')
    return record


def input_live(frame, serial, session=None):
    """Single-wrist form of the project predicate; read_frame checks packet age."""
    return arms_live(frame, {'right': serial}, session)


def raw_input_record(frame, serials, input_source, reference):
    """Snapshot selected PICO-world input before the reference transform.

    This tracker-schema record can be passed back to reference_frame() using
    target_reference_frame. Absence of a pair map stays absent, distinguishing
    legacy fallback from an explicitly missing same-packet Head pairing.
    """
    fields = ('version', 'source', 'session', 'seq', 'head_pose', 'head_sample_mono',
              'sample_mono', 'received_mono', 'sample_mono_on_robot',
              'sample_at_unix_ns', 'packet_timestamp_ns', 'motion_timestamp_ns',
              'timestamp_source', 'tracker_sample_mono', 'controller_sample_mono',
              'controller_sample_mono_by_side', 'controller_validity_source')
    result = {name: deepcopy(frame[name]) for name in fields if name in frame}
    result.update(schema='g1-arm-raw-input-v1', arm_input_source=input_source,
                  arm_reference_frame='world', target_reference_frame=reference,
                  arm_reference_mapping=reference_mapping('world'),
                  target_reference_mapping=reference_mapping(reference),
                  serials=dict(serials),
                  trackers={sn: deepcopy(frame['trackers'][sn]) for sn in serials.values()
                            if sn in frame.get('trackers', {})})
    if 'head_pose_by_tracker' in frame:
        pairs = frame['head_pose_by_tracker']
        result['head_pose_by_tracker'] = ({sn: deepcopy(pairs[sn]) for sn in serials.values()
                                          if sn in pairs} if isinstance(pairs, dict) else {})
    return result


class JsonChild:
    def __init__(self, argv, log):
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True, bufsize=1)
        self.records = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                item = json.loads(line)
                if isinstance(item, dict): self.records.put(item)
            except ValueError:
                pass
        self.records.put({'type': 'closed'})

    def send(self, value):
        self.process.stdin.write(json.dumps(value, allow_nan=False) + '\n')
        self.process.stdin.flush()

    def get(self, timeout=30):
        item = self.records.get(timeout=timeout)
        if item.get('type') in ('closed', 'error'):
            raise RuntimeError('Child process: ' + str(item))
        return item

    def request(self, value):
        self.send(value)
        return self.get()

    def close(self):
        if self.process.poll() is None:
            try:
                self.send({'command': 'stop'})
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def ssh_handshake_error(transcript, *, timed_out=False):
    """Classify known SSH failures without returning transcripts or argv."""
    if isinstance(transcript, bytes):
        transcript = transcript.decode('utf-8', errors='replace')
    text = transcript.lower() if isinstance(transcript, str) else ''
    if any(reason in text for reason in ('host key verification failed',
            'remote host identification has changed', 'offending ed25519 key')):
        return 'G1 SSH identity could not be verified. Check the saved robot host key before retrying.'
    if any(reason in text for reason in ('permission denied', 'authentication failed',
            'too many authentication failures')):
        return 'G1 SSH authentication failed. Check the robot login credential.'
    if any(reason in text for reason in ('no route to host', 'network is unreachable')):
        return 'G1 SSH has no route to the robot. Check G1 power, Wi-Fi and its current address.'
    if 'connection refused' in text:
        return 'G1 SSH connection refused. Wait for the robot computer to finish booting.'
    if timed_out or 'timed out' in text:
        return 'G1 SSH connection timed out. Check G1 power, Wi-Fi and its current address.'
    return 'G1 SSH connection ended before authentication completed. Check G1 power and Wi-Fi.'


class RemoteArm:
    def __init__(self, password, log):
        import pexpect
        self.pexpect, self.log = pexpect, log
        package = io.BytesIO()
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(ROOT/'scripts/pc2_branch_arm.py', 'pc2_branch_arm.py')
            archive.writestr('teleop/__init__.py', '')
            archive.writestr('teleop/robot_control/__init__.py', '')
            for relative in ('teleop/robot_control/robot_arm.py', 'teleop/robot_control/dds_utils.py'):
                archive.write(VENDOR/relative, relative)
        encoded = base64.b64encode(package.getvalue()).decode()
        bootstrap = ("import base64,io,zipfile,tempfile,sys,runpy; "
            "d=tempfile.TemporaryDirectory(prefix='g1-branch-'); "
            f"zipfile.ZipFile(io.BytesIO(base64.b64decode({encoded!r}))).extractall(d.name); "
            "sys.path.insert(0,d.name); "
            "sys.argv=['pc2_branch_arm.py','--enable-motion','--control-mode','normal_walking','--interface','eth0','--vendor-root',d.name]; "
            "runpy.run_path(d.name+'/pc2_branch_arm.py',run_name='__main__')")
        remote = ' '.join(shlex.quote(x) for x in
            ['/home/unitree/miniconda3/envs/tv/bin/python','-u','-B','-c',bootstrap])
        args = ['-T', '-o','ConnectTimeout=5','-o','NumberOfPasswordPrompts=1',
            '-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
            '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=3',
            '-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=192.168.123.164',
            '-o','UserKnownHostsFile='+str(ROOT/'diagnostics/g1-known_hosts'),
            'unitree@'+robot_host(ROOT),remote]
        self.process = pexpect.spawn('ssh', args, echo=False, encoding='utf-8', timeout=30, maxread=65536)
        self.process.delaybeforesend = 0
        self.records = queue.Queue()
        self.latest = None
        self.final = None
        self.error = None
        self.startup_failure = None
        self.control_verified = False
        self._lock = threading.Lock()
        try:
            self.process.expect('[Pp]assword:', timeout=8)
            self.process.sendline(password)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            message = ssh_handshake_error(self.process.before, timed_out=isinstance(exc, pexpect.TIMEOUT))
            try:
                self.process.close(force=True)
            except Exception:
                pass
            # pexpect's exception string contains argv/bootstrap and may include
            # authentication transcripts. Only the fixed classification escapes.
            raise RuntimeError(message) from None
        except BaseException:
            self.process.close(force=True)
            raise
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                try: self.process.expect(r'\r?\n', timeout=1)
                except self.pexpect.TIMEOUT: continue
                line = self.process.before.strip()
                try: item = json.loads(line)
                except ValueError:
                    self.startup_failure = remote_exit_reason(line) or self.startup_failure
                    self.log.write(line+'\n'); self.log.flush(); continue
                if not isinstance(item, dict): continue
                if item.get('type') in ('ready', 'feedback', 'final'):
                    try: require_arm_sdk(item)
                    except RuntimeError as exc:
                        self.control_verified = False
                        self.error = {'type':'error','error':str(exc)}
                        self.records.put(self.error)
                        continue
                    if item.get('type') == 'final': self.final = item
                    else: self.latest = item
                elif item.get('type') == 'error': self.error = item
                if item.get('type') != 'feedback': self.records.put(item)
        except (self.pexpect.EOF, OSError, ValueError):
            self.records.put({'type':'closed'})

    def send(self, record):
        with self._lock:
            if not self.control_verified:
                raise RuntimeError('G1 arm_sdk control identity has not been verified; refusing arm commands.')
            require_arm_sdk(self.latest)
            self.process.sendline(json.dumps(record, allow_nan=False))

    def ready(self):
        while True:
            item = self.records.get(timeout=40)
            if item.get('type') == 'ready':
                require_arm_sdk(item,ready=True)
                if self.error:raise RuntimeError('G1 arm_sdk protocol failed during initialization.')
                self.control_verified = True
                return item
            if item.get('type') in ('closed','error','final'):
                if item.get('type') == 'closed':
                    raise RuntimeError(getattr(self,'startup_failure',None) or
                        'G1 远端控制进程在就绪前退出；请查看本次 runtime.log。')
                raise RuntimeError('G1 did not become ready: '+str(item))

    def start(self, stopping):
        """Do not publish target/following state before the remote START ack."""
        if stopping.is_set(): return False
        self.send({'command':'start'})
        deadline = time.monotonic()+15
        while not stopping.is_set():
            if self.error or self.final or not self.process.isalive():
                raise RuntimeError('G1 arm control ended before START completed.')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise RuntimeError('G1 arm_sdk START acknowledgement timed out.')
            try: item = self.records.get(timeout=min(.1,remaining))
            except queue.Empty: continue
            if item.get('type') == 'state' and item.get('stage') == 'following':
                require_arm_sdk(self.latest)
                return True
            if item.get('type') in ('closed','error','final'):
                raise RuntimeError('G1 arm control ended before START completed.')
        return False

    def close(self):
        try:
            if self.process.isalive() and self.control_verified:
                self.send({'command':'stop'})
                deadline = time.monotonic()+8
                while self.final is None and self.process.isalive() and time.monotonic()<deadline:
                    time.sleep(.05)
        finally:
            self.process.close(force=True)
        return self.final


def run(output, password, simulation=False, options=None):
    options = options or {}
    input_source = validate_source(options.get('arm_input_source','trackers'))
    reference = validate_reference_frame(options.get('arm_reference_frame', 'world'))
    output.mkdir(parents=True, exist_ok=True)
    commands, stopping = queue.Queue(), threading.Event()
    def controls():
        for line in sys.stdin:
            try:
                command = json.loads(line).get('command')
                commands.put(command)
                if command == 'stop': stopping.set()
            except ValueError: pass
        stopping.set()
    threading.Thread(target=controls, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    accepted, stage, active = 0, 'preparing', False
    control_verified = False
    def emit(**record):
        print(json.dumps(record, allow_nan=False), flush=True)
    def state(new_stage, **extra):
        nonlocal stage
        stage = new_stage
        emit(type='state', stage=stage, accepted_targets=accepted,
             control_topic=CONTROL_TOPIC, control_mode=CONTROL_MODE,
             control_verified=control_verified, arm_input_source=input_source,
             arm_reference_frame=reference, **extra)
    state(stage)
    if simulation:
        state('ready', actual_write_count=0)
        while not stopping.wait(.02):
            try: command = commands.get_nowait()
            except queue.Empty: continue
            if command == 'start': state('following', actual_write_count=0)
        emit(type='final', stage='stopped', completed=True, actual_write_count=0, last_action_unknown=False,
             control_topic=CONTROL_TOPIC,control_mode=CONTROL_MODE,control_verified=False,
             arm_input_source=input_source,arm_reference_frame=reference,simulation=True)
        return
    pico = ik = arm = None
    done, error, final = False, None, None
    remote_attempted = False
    with (output/'runtime.log').open('w', buffering=1) as log, (output/'targets.jsonl').open('w', buffering=1) as recording, tempfile.TemporaryDirectory(prefix='g1-pico-',dir='/dev/shm') as ram:
        frame_path = Path(options.get('tracker_frame_path') or Path(ram)/'latest.json')
        try:
            config = options.get('tracker_config') or json.loads((ROOT/'config/pico_tracker.json').read_text())
            config = effective_config(config,input_source)
            config['arm_reference_frame'] = reference
            sides = selected_sides(options.get('arms_mode','right'))
            serials = {side: config.get(side+'_sn','') for side in sides}
            if any(not sn for sn in serials.values()):
                raise ValueError('Choose a tracker for each selected arm before LAUNCH')
            if len(set(serials.values())) != len(serials):
                raise ValueError('Each wrist requires a different tracker serial')
            if not options.get('tracker_frame_path'):
                pico = JsonChild([str(Path.home()/'.cache/unitree-g1-pico/venv/bin/python'),'-u','-B',str(ROOT/'scripts/pico_arm_stream.py')],log)
                def frames():
                    while not stopping.is_set():
                        try: frame = pico.get(timeout=1)
                        except queue.Empty: continue
                        except RuntimeError: return
                        if 'trackers' not in frame: continue
                        # Both producer and consumer run on this laptop: no clock translation.
                        frame['received_mono'] = time.monotonic()
                        frame['sample_mono_on_robot'] = frame['sample_mono']
                        tmp = frame_path.with_suffix('.tmp')
                        try:
                            tmp.write_text(json.dumps(frame)); tmp.replace(frame_path)
                        except OSError: return
                threading.Thread(target=frames,daemon=True).start()
            # LAUNCH prepares the robot independently of the wearable input.
            # START captures real wrists/head when available; it must not invent
            # a neutral pose, but missing input does not terminate this session.
            if stopping.is_set(): return
            ik = JsonChild([str(Path.home()/'.cache/unitree-g1-arms/env/bin/python'),'-u','-B',str(ROOT/'scripts/branch_ik_worker.py')],log)
            ik.get(timeout=40)
            if stopping.is_set(): return
            remote_attempted = True
            arm = RemoteArm(password, log)
            require_arm_sdk(arm.ready(),ready=True)
            control_verified = True
            state('ready', message='LAUNCH prepared zero arm_sdk authority. START captures arm references and holds the measured waist posture with source gains; factory leg locomotion remains active.')
            captured = None
            last_seq, last_report = None, 0.
            while not stopping.is_set():
                tick = time.monotonic()
                if arm.error or arm.final or not arm.process.isalive(): raise RuntimeError('G1 transport closed: '+str(arm.error or arm.final))
                require_arm_sdk(arm.latest)
                try: command = commands.get_nowait()
                except queue.Empty: command = None
                if command == 'start' and not active:
                    source_frame = select_frame(read_frame(frame_path),input_source)
                    frame = reference_frame(source_frame,reference)
                    if not arms_live(frame, serials) or not pose_valid(frame.get('head_pose')):
                        state('ready', error=None, start_requested=False,
                              message='Robot ready; waiting for selected '+input_source+' and head input. Press START when available to capture neutral.')
                        continue
                    try:
                        measured = arm.latest
                        require_arm_sdk(measured)
                        poses = ik.request({'command':'fk','q':measured['q']})
                        captured = capture(frame, measured['q'], poses, config, sides)
                        if stopping.is_set() or not arm.start(stopping):break
                        active = True
                        recording.write(json.dumps(dict(captured.metadata(),arm_input_source=input_source,
                            raw_input=raw_input_record(source_frame,serials,input_source,reference)))+'\n')
                        state('following', error=None)
                    except ValueError as exc: state('ready', error=str(exc), start_requested=False)
                if active:
                    source_frame = select_frame(read_frame(frame_path),input_source)
                    frame = reference_frame(source_frame,reference)
                    live = arms_live(frame,serials,captured.session)
                    next_stage = 'following' if live else 'holding'
                    if next_stage != stage:
                        missing_source = input_source + (' or paired headset' if reference == 'headset' else '')
                        state(next_stage, message='Following '+', '.join(sides)+' '+input_source if live else 'Selected '+missing_source+' input unavailable; holding last arm target.')
                    if live and frame['seq'] != last_seq:
                        measured = arm.latest
                        require_arm_sdk(measured)
                        poses = ik.request({'command':'fk','q':measured['q']})
                        wrist_targets = captured.targets(frame, poses)
                        result = ik.request({'command':'solve', **wrist_targets, 'q':measured['q'],'dq':measured['dq']})
                        if stopping.is_set():break
                        arm.send({'command':'target','q':result['q'],'tau':result['tau']})
                        accepted += 1
                        last_seq = frame['seq']
                        recording.write(json.dumps({'seq':last_seq,'measured_q':measured['q'],'q':result['q'],'tau':result['tau'],
                            'arm_input_source':input_source,
                            'arm_reference_frame':reference,
                            'arm_reference_mapping':reference_mapping(reference),
                            'raw_input':raw_input_record(source_frame,serials,input_source,reference),
                            'input_age_ms':(tick-frame['sample_mono_on_robot'])*1000,
                            'sample_mono':frame['sample_mono_on_robot'], 'recorded_mono':time.monotonic(),
                            'trackers':{side:frame['trackers'][sn] for side,sn in serials.items()},
                            'head_pose':frame.get('head_pose'),'wrist_targets':wrist_targets})+'\n')
                if tick-last_report>1:
                    state(stage, actual_write_count=arm.latest.get('actual_write_count'), error=None)
                    last_report=tick
                stopping.wait(max(0., .01-(time.monotonic()-tick)))
            done = True
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            state('error',error=error)
        finally:
            stopping.set()
            if arm:
                state('stopping',message='Releasing arm_sdk blending weight; leaving the normal walking controller active.')
                try:
                    final=arm.close()
                    if final is not None:require_arm_sdk(final)
                except Exception as exc:
                    error=error or ('G1 cleanup: '+str(exc))
            for child in (pico,ik):
                if child:
                    try: child.close()
                    except Exception as exc:
                        error=error or ('Child cleanup: '+str(exc))
            summary=dict(type='final',stage='stopped' if done or not error else 'error',completed=bool(final and final.get('completed') and not error) if arm else not error,error=error or (final.get('error') if final else None),accepted_targets=accepted,actual_write_count=final.get('actual_write_count') if final else (None if remote_attempted else 0),last_action_unknown=final.get('last_action_unknown',True) if final else remote_attempted,physical_stop_verified=final.get('physical_stop_verified',False) if final else False)
            summary.update(control_topic=CONTROL_TOPIC,control_mode=CONTROL_MODE,
                arm_input_source=input_source,arm_reference_frame=reference,
                control_verified=control_verified,stop_behavior='release_arm_weight',
                arm_weight_released=bool(final and final.get('arm_weight_released')))
            (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
            emit(**summary)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--simulation',action='store_true')
    args=parser.parse_args()
    credentials=json.loads(sys.stdin.readline())
    run(args.output_dir,credentials.get('ssh_password',''),args.simulation,credentials.get('session_options'))
