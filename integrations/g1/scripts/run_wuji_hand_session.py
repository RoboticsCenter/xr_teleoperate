#!/usr/bin/env python3
"""Run the original Wuji reader through the existing BrainCo hand session.

Default is live preview (no command publisher). --demo is explicitly synthetic.
Real operation needs an existing binding and explicit DDS or Wi-Fi SSH feedback.
"""
import copy
import getpass
import importlib.util
import json
import math
import os
from pathlib import Path
from network_config import robot_host
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = load('_run_hand_live', 'live_hand_preview.py')
output = load('_run_hand_output', 'teleop_hand_session.py')


def parser():
    p = live.parser()
    p.description = __doc__
    p.add_argument('--demo', action='store_true', help='offline synthetic filter→mapper→session exercise')
    p.add_argument('--feedback-transport', choices=('dds', 'wifi-ssh'), default='dds',
                   help='wifi-ssh reads PC2 state without local DDS; preview only')
    p.add_argument('--enable-motion', action='store_true')
    p.add_argument('--exclusive-control-confirmed', action='store_true')
    p.add_argument('--feedback-identity-confirmed', action='store_true')
    p.add_argument('--command-service-confirmed', action='store_true')
    return p


def validate_mode(args):
    if args.demo and (args.enable_motion or args.binding or args._worker):
        raise ValueError('--demo cannot use motion, real binding, or worker mode')
    if args.feedback_transport == 'wifi-ssh':
        if args.enable_motion:
            raise ValueError('wifi-ssh has no hand command transport; motion is unavailable')
        if args.interface is not None or args.domain != 0:
            raise ValueError('wifi-ssh uses PC2 eth0/domain0; --interface and nonzero --domain are DDS-only')
    if args.enable_motion and not all((args.exclusive_control_confirmed,
                                      args.feedback_identity_confirmed, args.command_service_confirmed)):
        raise ValueError('Motion requires verified exclusive control, feedback identity and command service')


def wifi_feedback_source_id(side):
    return 'wifi-ssh:unitree@{};interface=eth0;domain=0;topic=rt/brainco/{}/state'.format(robot_host(),side)


def prepare(args):
    if args.feedback_transport == 'dds':
        return live.prepare(args)
    validate_mode(args)
    for field in ('side', 'sn', 'user', 'binding'):
        if not getattr(args, field):
            raise ValueError('Explicit --{} is required'.format(field))
    if args.user.lower() == 'default':
        raise ValueError('Named SDK user required')
    for field, low, high in (('seconds', .1, 60), ('idle_timeout', .1, 5), ('rate_hz', 1, 120)):
        value = getattr(args, field)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('{} must be in [{},{}]'.format(field, low, high))
    source = args.source_dir.expanduser().resolve()
    locked = live.mapping.strict_json((ROOT / 'sources.lock.json').read_text())
    if live.mapping.digest((source / 'wuji_bridge.py').read_bytes()) != locked['hand_source']['sha256']['wuji_bridge.py']:
        raise ValueError('Original bridge hash mismatch')
    retarget, settings = live.mapping.load_original(source)
    binding_path = args.binding.expanduser().resolve()
    binding = live.mapping.strict_json(binding_path.read_text())
    mapper = live.mapping.HandDDSPreview(binding, binding_path.parent, retarget, settings)
    if binding['kind'] != 'operator_calibrated':
        raise ValueError('Live session requires an operator_calibrated binding')
    if (binding['side'], binding['glove_sn'], binding['sdk_user']) != (args.side, args.sn, args.user):
        raise ValueError('CLI side/SN/user differ from binding')
    if binding['clock_id'] != live.clock_id():
        raise ValueError('binding.clock_id must match this capture host boot')
    if binding['feedback_source_id'] != wifi_feedback_source_id(args.side):
        raise ValueError('binding.feedback_source_id must be ' + wifi_feedback_source_id(args.side))
    return mapper, source


class WifiHandFeedbackReceiver:
    """Single-threaded polling: preserve remote sequence and conservative receipt.

    WifiFeedbackClient validates the SSH reply/clock and derives the RTT bound.
    A repeated remote sample never becomes a new sample here. No robot SDK or
    background reader is created by this adapter.
    """
    def __init__(self, mapper, client, clock=time.monotonic):
        self.mapper, self.client, self.clock = mapper, client, clock
        self.topic = 'rt/brainco/{}/state'.format(mapper.side)
        self.latest = self.source_record = self.source_clock = None
        self.last_poll = self.bound_at = self.bound_age = None
        self.error = None

    def accept(self, report):
        entry = report.get('streams', {}).get(self.topic)
        if entry is None or (entry.get('status') == 'missing_or_invalid' and entry.get('source') is None):
            if self.latest is not None:
                raise ValueError('previously present hand state is now missing')
            return
        if entry.get('status') != 'fresh_bound':
            raise ValueError('hand feedback is not fresh_bound: ' + str(entry.get('status')))
        source = entry['source']
        if source.get('error') or not isinstance(source.get('payload'), dict):
            raise ValueError('invalid remote hand payload')
        q = live.mapping.vector(source['payload'].get('q'), 'Wi-Fi hand q', 0, 1)
        live.mapping.vector(source['payload'].get('dq'), 'Wi-Fi hand dq')
        seq, estimate = entry.get('sequence'), entry.get('conservative_received_monotonic_ns')
        at = report.get('local_reply_received_ns')
        cid = report.get('source_clock_id')
        age = live.mapping.number(entry.get('age_upper_bound_s'), 'Wi-Fi age bound')
        if (type(seq) is not int or seq < 1 or source.get('sequence') != seq
                or type(estimate) is not int or estimate < 0 or type(at) is not int or at < estimate
                or not isinstance(cid, str) or not cid.startswith('linux-monotonic:')
                or not 0 <= age <= self.mapper.feedback_age):
            raise ValueError('invalid Wi-Fi hand sequence, timing, clock, or age bound')
        if self.source_clock is not None and cid != self.source_clock:
            raise ValueError('remote feedback clock changed')
        value = {'hand_side': self.mapper.side, 'source_id': self.mapper.binding['feedback_source_id'],
                 'topic': self.topic, 'channels': live.mapping.DDS_CHANNELS,
                 'sample_id': seq, 'received_t': estimate / 1e9, 'q_normalized': q,
                 'time_provenance': 'conservative_full_rtt_plus_remote_age',
                 'source_clock_id': cid, 'source_received_monotonic_ns': source.get('received_monotonic_ns')}
        if self.latest is not None:
            if seq < self.latest['sample_id']:
                raise ValueError('remote hand sequence regressed')
            if seq == self.latest['sample_id']:
                if value != self.latest or source != self.source_record:
                    raise ValueError('cached hand sample changed or was restamped')
            elif value['received_t'] <= self.latest['received_t']:
                raise ValueError('new conservative feedback receipt did not advance')
        self.latest, self.source_record, self.source_clock = value, copy.deepcopy(source), cid
        self.bound_at, self.bound_age = at / 1e9, age

    def snapshot(self):
        if self.error is not None:
            return None, self.error
        try:
            now = live.mapping.number(self.clock(), 'feedback local clock')
            if self.last_poll is not None and now < self.last_poll:
                raise ValueError('feedback local clock regressed')
            if self.last_poll is None or now - self.last_poll >= .05:
                self.last_poll = now
                self.accept(self.client.snapshot())
            now = live.mapping.number(self.clock(), 'feedback local clock')  # Recheck after blocking SSH.
            if self.latest is not None:
                bound = max(now - self.latest['received_t'], self.bound_age + now - self.bound_at)
                if now < self.bound_at or not 0 <= bound <= self.mapper.feedback_age:
                    raise ValueError('Wi-Fi hand feedback exceeded conservative age bound')
            return copy.deepcopy(self.latest), None
        except Exception as exc:
            self.error = '{}:{}'.format(type(exc).__name__, exc)
            return None, self.error


def create_wifi_client(seconds, max_age_s):
    from wifi_feedback_client import WifiFeedbackClient
    return WifiFeedbackClient(seconds=seconds, max_age_s=max_age_s)


def read_worker_password(stream):
    try:
        line = stream.readline(8193)
        record = json.loads(line)
        password = record['ssh_password']
        if (len(line) > 8192 or not line.endswith('\n') or set(record) != {'ssh_password'}
                or not isinstance(password, str) or not 0 < len(password) <= 1024
                or any(c in password for c in ('\n', '\r', '\0'))):
            raise ValueError()
        return password
    except (ValueError, KeyError, TypeError):
        raise ValueError('Invalid worker credential envelope') from None


def session_for(args, mapper, clock):
    return output.BrainCoHandSession(mapper, enable_motion=args.enable_motion,
        interface=args.interface, domain=args.domain, clock=clock,
        exclusive_control_confirmed=args.exclusive_control_confirmed,
        feedback_identity_confirmed=args.feedback_identity_confirmed,
        command_service_confirmed=args.command_service_confirmed)


def collect(args, mapper, receiver, lines, emit, *, clock=time.monotonic,
            stopped=None, session_factory=session_for):
    start = last_glove = clock()
    previous = session = None
    accepted = previews = writes = 0
    reason = 'duration_complete'
    try:
        while clock() - start < args.seconds:
            if stopped is not None and stopped.is_set():
                reason = 'interrupted'
                break
            now = clock()
            if session is not None:
                fault = session.check_timeout()
                if fault:
                    emit({'type': 'hand_result', 'result': fault, 'published': False})
                    reason = fault['reason']
                    break
            measured, error = receiver.snapshot()
            if error:
                reason = 'invalid_feedback:' + error
                break
            if now - last_glove > args.idle_timeout:
                reason = 'no_accepted_original_reader_frames'
                break
            if now - (measured['received_t'] if measured else start) > args.idle_timeout:
                reason = 'feedback_timeout'
                break
            try:
                kind, value, received = lines.get(timeout=min(.02, max(.001, args.seconds - (now - start))))
            except queue.Empty:
                continue
            if kind != 'line':
                reason = 'original_reader_eof' if kind == 'eof' else 'original_reader_pipe_error'
                break
            now = clock()
            try:
                frame = live.mapping.strict_json(value)
                previous = live.validate_glove(frame, mapper, received, now, previous)
            except (ValueError, TypeError) as exc:
                reason = 'invalid_original_frame:' + str(exc)
                break
            accepted += 1
            last_glove = received
            measured, error = receiver.snapshot()
            if error:
                reason = 'invalid_feedback:' + error
                break
            if measured is None:
                emit({'type': 'waiting_for_feedback', 'input_quality_passed': True, 'published': False})
                continue
            now = clock()
            record = {'binding_id': mapper.binding_id, 'clock_id': mapper.binding['clock_id'],
                      'sdk_model_sha256': mapper.binding['sdk_model']['sha256'],
                      'now_monotonic_s': now, 'glove': frame, 'measured': measured}
            if session is None:
                # Startup/first feedback may take time; arm the 200 ms input-gap
                # gate only when an actual accepted frame and state are present.
                session = session_factory(args, mapper, clock)
            result = session.step(record, original_filter_passed=True)
            previews += result['status'] == 'preview'
            writes += result.get('published') is True
            emit({'type': 'hand_result', 'input': record, 'result': result,
                  'published': result.get('published') is True})
            if result['status'] == 'latched':
                reason = result['reason']
                break
    finally:
        if session is not None:
            emit({'type': 'hand_output_closed', 'result': session.close(), 'published': False})
    return {'type': 'summary', 'reason': reason, 'accepted_original_frames': accepted,
            'preview_count': previews, 'published_count': writes,
            'input_confidence_threshold': .5, 'allow_low_quality': False,
            'input_quality_passed': accepted > 0, 'physical_stop_confirmed': False,
            'published': False, 'ok': reason == 'duration_complete' and previews + writes > 0}


def worker(args, mapper, source, emit, password=None):
    validate_mode(args)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: stopped.set())
    manager = subscriber = process = lines = wifi_client = None
    try:
        import wuji_sdk
        manager = live.verify_sdk_context(mapper, wuji_sdk)
        if args.feedback_transport == 'wifi-ssh':
            if password is None:
                password = read_worker_password(sys.stdin)
            wifi_client = create_wifi_client(args.seconds + 10, mapper.feedback_age)
            emit({'type': 'wifi_feedback_opened', 'record': wifi_client.open(password), 'published': False})
            password = None
            receiver = WifiHandFeedbackReceiver(mapper, wifi_client)
        else:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
            receiver = live.FeedbackReceiver(mapper)
            ChannelFactoryInitialize(args.domain, args.interface)
            subscriber = ChannelSubscriber('rt/brainco/{}/state'.format(args.side), MotorStates_)
            subscriber.Init(receiver.receive, 0)
        command = [sys.executable, '-u', '-c', live.BRIDGE_LAUNCHER, str(source / 'wuji_bridge.py'),
                   '--sn', args.sn, '--side', args.side, '--user', args.user,
                   '--rate-hz', str(args.rate_hz), '--timeout-s', str(args.idle_timeout)]
        process = subprocess.Popen(command, cwd=source, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        lines = live.BridgeLines(process.stdout)
        emit({'type': 'session', 'mode': 'motion_enabled' if args.enable_motion else 'preview',
              'binding_id': mapper.binding_id, 'hand_side': args.side,
              'input_confidence_threshold': .5, 'allow_low_quality': False,
              'feedback_transport': args.feedback_transport,
              'feedback_source_id': mapper.binding['feedback_source_id'], 'published': False})
        report = collect(args, mapper, receiver, lines, emit, stopped=stopped)
    finally:
        # Close our own reader before native DDS shutdown; outer worker deadline
        # also covers an SDK/reader close that fails to return.
        try:
            if process is not None:
                live.stop_owned(process)
                if lines is not None:
                    lines.thread.join(timeout=1)
                process.stdout.close()
        finally:
            try:
                if subscriber is not None:
                    subscriber.Close()
            finally:
                try:
                    if wifi_client is not None:
                        wifi_client.close()
                finally:
                    if manager is not None:
                        manager.disconnect_all()
    emit(report)
    return 0 if report['ok'] else 2


def supervise(argv, seconds, emit, popen=subprocess.Popen, password=None):
    process = popen([sys.executable, '-u', str(Path(__file__).resolve())] + list(argv) + ['--_worker'],
                    stdin=subprocess.DEVNULL if password is None else subprocess.PIPE,
                    text=True, start_new_session=True,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    try:
        if password is not None:
            # Password is sent once over an anonymous pipe, never argv/env/logs.
            process.stdin.write(json.dumps({'ssh_password': password}) + '\n')
            process.stdin.flush()
            process.stdin.close()
            password = None
        return process.wait(timeout=seconds + 12)
    except subprocess.TimeoutExpired:
        emit({'type': 'summary', 'reason': 'worker_hard_timeout', 'ok': False,
              'published': False, 'physical_stop_confirmed': False})
        return 3
    finally:
        live.stop_owned(process, group=True)


def demo(source, emit):
    # Load exactly the original reviewed bytes and call the real quality filter.
    path = source / 'wuji_bridge.py'
    data = path.read_bytes()
    locked = live.mapping.strict_json((ROOT / 'sources.lock.json').read_text())
    if live.mapping.digest(data) != locked['hand_source']['sha256']['wuji_bridge.py']:
        raise ValueError('Original bridge hash mismatch')
    namespace = {'__name__': '_offline_original_bridge', '__file__': str(path)}
    exec(compile(data, str(path), 'exec'), namespace)
    retarget, settings = live.mapping.load_original(source)
    with tempfile.TemporaryDirectory(prefix='wuji-hand-session-synthetic-') as directory:
        binding = live.mapping.synthetic_fixture(directory)
        mapper = live.mapping.HandDDSPreview(binding, directory, retarget, settings, allow_synthetic=True)
        now = [100.]
        session = output.BrainCoHandSession(mapper, clock=lambda: now[0])
        for seq, confidence in enumerate((.9, .9, .49), 1):
            now[0] += .02
            raw = SimpleNamespace(fingers=[SimpleNamespace(angles=[.2] * 5, confidence=confidence)] * 5)
            readings = namespace['joint_readings'](raw)
            if readings is None:
                emit({'type': 'synthetic_filter_rejection', 'synthetic': True,
                      'input_quality_passed': False, 'confidence': confidence, 'published': False})
                continue
            record = live.mapping.synthetic_record(mapper, seq=seq, now=now[0])
            record['glove']['joint_angles'] = readings
            emit({'type': 'hand_result', 'synthetic': True,
                  'result': session.step(record, original_filter_passed=True), 'published': False})
        emit({'type': 'hand_output_closed', 'synthetic': True, 'result': session.close(), 'published': False})
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    stream = sys.stdout
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=stream, flush=True)
    def interrupted(_sig, _frame):
        raise KeyboardInterrupt
    try:
        validate_mode(args)
        if args.demo:
            return demo(args.source_dir.expanduser().resolve(), emit)
        if args.show_clock_id:
            emit({'clock_id': live.clock_id(), 'published': False})
            return 0
        mapper, source = prepare(args)
        if args.check_binding:
            emit({'binding_valid_locally': True, 'binding_id': mapper.binding_id,
                  'live_inputs_verified': False, 'published': False})
            return 0
        if args._worker:
            stream = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            return worker(args, mapper, source, emit)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, interrupted)
        password = getpass.getpass('PC2 SSH password: ') if args.feedback_transport == 'wifi-ssh' else None
        return supervise(argv, args.seconds, emit, password=password)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit({'type': 'refused', 'reason': '{}:{}'.format(type(exc).__name__, exc),
              'published': False, 'physical_stop_confirmed': False})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
