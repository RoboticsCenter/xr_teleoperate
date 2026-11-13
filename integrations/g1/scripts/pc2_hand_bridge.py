#!/usr/bin/env python3
"""Managed BrainCo DDS bridge; explicit open selects feedback or command mode.

The service is an isolated build of the audited feedback-first service, with
per-hand hold/resume. Import and construction never open DDS or serial ports.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

BASE = Path('/home/unitree/g1-integration/brainco-feedback-20261111')
HASHES = {
    'main.cpp': '144db3dfecf693ebf22de64f369a730a8a6fd9d2a32aca4e32f21fe8fc952d64',
    'include/command_gate.h': '0a3d502338a62d553e77d8f20ed70be4954fb9439dcf4acb2eb0e71331dfa33c',
    'include/param.h': '202b458851dbef1d3a9f21540e0a2b08a27fe83f7c17feb1bc208d7a9b9c546f',
    'idl-objects/MotorCmds_.cpp.o': '95b2531b1122ae2d0c4590f16fb3525f5fcfc23d8cea101aad53363bda4c86e8',
    'idl-objects/MotorStates_.cpp.o': 'ef3461dcfd0a8f36201c1e09fb45acefd1000eda2589ee0f83b3abbe6c9ba2d4',
    'lib/aarch64/libbc_stark_sdk.so': 'b1e2a4c9cd23bd0710a54e693cec5d0ccac08c93b6333834bfbce164c7e46391',
}
IDENTITIES = {
    'left': dict(port='/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if01-port0', slave_id=126, serial='BCXTL2204J2600021'),
    'right': dict(port='/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if00-port0', slave_id=127, serial='BCXTR2362J2500015'),
}
LOWER, UPPER = [.1, 0., 0., 0., 0., 0.], [.9, .95, .95, .95, .95, .95]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_service(patch, base=BASE):
    """Compile only. Does not execute a server, load its SDK, or open serial/DDS."""
    base, patch = Path(base), Path(patch)
    for relative, expected in HASHES.items():
        if sha(base / relative) != expected:
            raise RuntimeError('audited service dependency differs: ' + relative)
    key = sha(patch)[:16]
    build = base.parent / ('brainco-dashboard-' + key)
    build.mkdir(mode=0o700, exist_ok=True)
    with (build / 'build.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        binary, manifest = build / 'brainco_hand_server', build / 'build.json'
        if binary.is_file() and manifest.is_file():
            report = json.loads(manifest.read_text())
            if report.get('binary_sha256') == sha(binary) and report.get('patch_sha256') == sha(patch):
                return binary
        shutil.copy2(base / 'main.cpp', build / 'main.cpp')
        shutil.copytree(base / 'include', build / 'include', dirs_exist_ok=True)
        subprocess.run(['patch', '--batch', '-p1', '-i', str(patch.resolve())], cwd=build,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        command = ['g++', '-std=c++17', '-O2', '-DFMT_HEADER_ONLY', '-I'+str(base/'idl-overlay'),
            '-Iinclude', '-I/usr/local/include/ddscxx', 'main.cpp',
            str(base/'idl-objects/MotorCmds_.cpp.o'), str(base/'idl-objects/MotorStates_.cpp.o'),
            '-L/usr/local/lib', '-L'+str(base/'lib/aarch64'), '-Wl,-rpath,/usr/local/lib',
            '-Wl,-rpath,'+str(base/'lib/aarch64'), '-lunitree_sdk2', '-lddsc', '-lddscxx',
            '-lrt', '-lpthread', '-lboost_program_options', '-lyaml-cpp', '-lbc_stark_sdk',
            '-o', str(binary)]
        subprocess.run(command, cwd=build, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=120)
        manifest.write_text(json.dumps(dict(binary_sha256=sha(binary), patch_sha256=sha(patch),
                                           source_hashes=HASHES, hardware_started=False), indent=2)+'\n')
        return binary


class ManagedService:
    def __init__(self, patch, password='', interface='eth0', base=BASE):
        self.patch, self.password, self.interface, self.base = patch, password, interface, Path(base)
        self.process = self.lock = self.log = None

    def start(self, enable_commands=False, stopped=None):
        if type(enable_commands) is not bool:
            raise ValueError('enable_commands must be a bool')
        self.lock = (self.base.parent / 'brainco-dashboard.lock').open('a+')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / 'cmdline').read_bytes().split(b'\0')
                if any(Path(arg.decode(errors='replace')).name.startswith('brainco_hand_server') for arg in args if arg):
                    raise RuntimeError('an existing BrainCo service already owns the serial devices')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        binary = build_service(self.patch, self.base)
        if stopped is not None and stopped.is_set():
            return None
        authorized = subprocess.run(['sudo', '-S', '-p', '', '-v'], input=self.password+'\n',
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if authorized.returncode != 0:
            raise RuntimeError('sudo authentication failed')
        # fuser is read-only; a different serial owner is never stopped for us.
        ports = [v['port'] for v in IDENTITIES.values() if Path(v['port']).exists()]
        if not ports:
            raise RuntimeError('neither verified hand port is present')
        check = subprocess.run(['sudo', '-n', 'fuser', *ports], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if check.returncode not in (0, 1) or check.stdout.strip():
            raise RuntimeError('serial devices are occupied or occupancy check failed')
        if stopped is not None and stopped.is_set():
            return None
        self.log = (binary.parent / ('service-' + str(os.getpid()) + '.log')).open('w', buffering=1)
        command = ['sudo', '-S', '-p', '', 'env',
            'LD_LIBRARY_PATH='+str(self.base/'lib/aarch64')+':/usr/local/lib', str(binary), '-n', self.interface]
        if enable_commands:
            command.append('--enable-commands')
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
            text=True, start_new_session=True)
        self.process.stdin.write(self.password+'\n')
        self.process.stdin.close()
        return dict(pid=self.process.pid, command_mode=enable_commands, binary=str(binary),
                    binary_sha256=sha(binary), log=str(self.log.name), identities=IDENTITIES)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        code = None
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    # Signal only this live, owned process group. Root owns the
                    # serial worker, so do not depend on unprivileged kill rights.
                    subprocess.run(['sudo', '-S', '-p', '', 'kill', '-TERM', '--', '-'+str(self.process.pid)],
                        input=self.password+'\n', text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=3, check=True)
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        subprocess.run(['sudo', '-S', '-p', '', 'kill', '-KILL', '--', '-'+str(self.process.pid)],
                            input=self.password+'\n', text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=3, check=True)
                        self.process.wait(timeout=2)
                code = self.process.returncode
        finally:
            if self.log:
                self.log.close()
            if self.lock:
                self.lock.close()
            self.password = ''
        return code


def six(value, low=0., high=1.):
    if not isinstance(value, list) or len(value) != 6 or any(
            type(x) not in (int, float) or not math.isfinite(x) or not low <= x <= high for x in value):
        raise ValueError('expected six finite normalized channels')
    return [float(x) for x in value]


class DDSHands:
    def __init__(self, interface='eth0', *, enable_commands=False, max_step=.1, feedback_max_age_s=.1):
        self.interface, self.enabled = interface, enable_commands
        self.max_step, self.max_age = max_step, feedback_max_age_s
        self.lock, self.samples = threading.Lock(), {}
        self.subscribers, self.publishers = [], {}
        self.write_count = dict(left=0, right=0)
        self.faults = {}

    def open(self):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_, MotorCmds_
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
        self.publisher_factory, self.message_factory, self.motor_factory = ChannelPublisher, MotorCmds_, unitree_go_msg_dds__MotorCmd_
        ChannelFactoryInitialize(0, self.interface)
        for side in IDENTITIES:
            subscriber = ChannelSubscriber('rt/brainco/'+side+'/state', MotorStates_)
            self.subscribers.append(subscriber)
            subscriber.Init(lambda message, side=side: self.receive(side, message), 0)

    def receive(self, side, message):
        now = time.monotonic()
        try:
            motors = message.states
            q = six([float(m.q) for m in motors])
            dq = six([float(m.dq) for m in motors], -1., 1.)
            sample = dict(q=q, dq=dq, received=now, error=None)
        except Exception as exc:
            sample = dict(q=None, dq=None, received=now, error=type(exc).__name__+': '+str(exc))
        with self.lock:
            sample['sequence'] = self.samples.get(side, {}).get('sequence', 0)+1
            self.samples[side] = sample

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return {side: dict(sample, age_s=max(0., now-sample['received']),
                         fresh=sample['error'] is None and 0 <= now-sample['received'] <= self.max_age)
                    for side, sample in self.samples.items()}

    def send(self, side, q, speed):
        if not self.enabled:
            raise ValueError('command mode is not enabled')
        if side not in IDENTITIES:
            raise ValueError('unknown hand side')
        if side in self.faults:
            raise RuntimeError(self.faults[side])
        q, speed = six(q), six(speed, .001, 1.)
        if any(not low <= x <= high for x, low, high in zip(q, LOWER, UPPER)):
            raise ValueError('target outside original six-channel envelope')
        state = self.snapshot().get(side)
        if not state or not state['fresh']:
            raise ValueError(side+' feedback unavailable; hold previous target')
        if any(abs(goal-current) > self.max_step+1e-7 for goal, current in zip(q, state['q'])):
            raise ValueError('target exceeds original measured-position step bound')
        if side not in self.publishers:
            publisher = self.publisher_factory('rt/brainco/'+side+'/cmd', self.message_factory)
            self.publishers[side] = publisher
            publisher.Init()
        # DDS publisher initialization can block; use the current feedback again.
        current = self.snapshot().get(side)
        if not current or not current['fresh'] or any(abs(goal-actual) > self.max_step+1e-7 for goal, actual in zip(q, current['q'])):
            raise ValueError('feedback changed during publisher setup; hold previous target')
        message = self.message_factory(cmds=[])
        for position, velocity in zip(q, speed):
            motor = self.motor_factory()
            motor.q, motor.dq = position, velocity
            message.cmds.append(motor)
        try:
            if self.publishers[side].Write(message) is not True:
                raise RuntimeError('DDS hand write did not report success; delivery unknown')
        except Exception as exc:
            self.faults[side] = type(exc).__name__+': '+str(exc)
            raise RuntimeError(self.faults[side]) from None
        self.write_count[side] += 1
        return dict(published=True, side=side, dds_write_count=self.write_count[side], acknowledged=False)

    def close(self):
        errors = []
        for endpoint in [*self.publishers.values(), *self.subscribers]:
            try:
                endpoint.Close()
            except Exception as exc:
                errors.append(str(exc))
        return errors


def serve(args, *, stdin=None, emit=None, service_factory=ManagedService, dds_factory=DDSHands):
    stdin = sys.stdin if stdin is None else stdin
    emit = (lambda item: print(json.dumps(item, allow_nan=False), flush=True)) if emit is None else emit
    credential = json.loads(stdin.readline())
    password = credential.pop('sudo_password', '')
    commands, stopped = queue.Queue(), threading.Event()
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, lambda *_: stopped.set())
    def read():
        try:
            for line in stdin:
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        continue
                    commands.put(item)
                    if item.get('command') == 'stop':
                        stopped.set()
                        return
                except ValueError:
                    continue
        finally:
            stopped.set()
    threading.Thread(target=read, daemon=True).start()
    service = service_factory(args.patch, password, args.interface)
    dds = dds_factory(args.interface, enable_commands=args.enable_commands,
        max_step=args.max_step, feedback_max_age_s=args.feedback_max_age_s)
    error, service_info, stop_code = None, None, None
    try:
        if not stopped.is_set():
            dds.open()
            if not stopped.is_set():
                service_info = service.start(args.enable_commands, stopped=stopped)
                if service_info is not None:
                    emit(dict(type='ready', command_mode=args.enable_commands, service=service_info))
        while not stopped.is_set():
            if not service.alive():
                raise RuntimeError('owned BrainCo service exited')
            try:
                command = commands.get(timeout=.05)
            except queue.Empty:
                continue
            rid, action = command.get('request_id'), command.get('command')
            if action == 'snapshot':
                emit(dict(type='snapshot', request_id=rid, hands=dds.snapshot(),
                          dds_write_count=dds.write_count, command_mode=args.enable_commands))
            elif action == 'target' and not stopped.is_set():
                try:
                    result = dds.send(command.get('side'), command.get('q'), command.get('speed'))
                    emit(dict(type='target_result', request_id=rid, **result))
                except Exception as exc:
                    emit(dict(type='target_result', request_id=rid, published=False,
                              side=command.get('side'), error=str(exc),
                              delivery_unknown=not isinstance(exc, ValueError)))
            elif action != 'stop':
                emit(dict(type='input_warning', request_id=rid, error='unknown command'))
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)
    finally:
        stopped.set()
        errors = dds.close()
        try:
            stop_code = service.close()
            if stop_code not in (None, 0):
                errors.append('owned hand service exited with status '+str(stop_code))
        except Exception as exc:
            errors.append(type(exc).__name__+': '+str(exc))
        emit(dict(type='final', completed=not error and not errors, error=error,
            cleanup_errors=errors, dds_write_count=dds.write_count, service_exit_code=stop_code,
            actual_write_count=None if args.enable_commands and service_info else 0,
            physical_stop_verified=False, last_action_unknown=bool(args.enable_commands and service_info)))
    return 1 if error or errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--patch', type=Path, required=True)
    parser.add_argument('--interface', default='eth0')
    parser.add_argument('--enable-commands', action='store_true')
    parser.add_argument('--start-service', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--max-step', type=float, default=.1)
    parser.add_argument('--feedback-max-age-s', type=float, default=.1)
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_step) or not 0 < args.max_step <= .1 or not math.isfinite(args.feedback_max_age_s) or not 0 < args.feedback_max_age_s <= .1:
        parser.error('step/feedback limits must preserve the original maximums')
    if args.build_only:
        binary = build_service(args.patch)
        print(json.dumps(dict(binary=str(binary), sha256=sha(binary), hardware_started=False)))
        return 0
    if not args.start_service:
        parser.error('--start-service is required to open the hand service')
    protocol = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    try:
        return serve(args, emit=lambda item: print(json.dumps(item, allow_nan=False), file=protocol, flush=True))
    finally:
        protocol.close()


if __name__ == '__main__':
    raise SystemExit(main())
