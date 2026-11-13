#!/usr/bin/env python3
"""Read fresh XRoboToolkit callback packets, without robot/device commands.

Unlike cached SDK getters, one output sample comes from one received JSON packet.
Motion.len limits valid entries. Sensor tracking confidence remains unknown when
the headset client does not send it. Run without any other PCService SDK reader.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import time

LIBRARY = Path.home() / '.cache/unitree-g1-pico/pybind-source/vendor/linux_x86_64/libPXREARobotSDK.so'
LIBRARY_SHA256 = '2348e1b0bf7f05cd13d95d628f04237b6b9fc50c6008b82f9bf4b046fc9373e6'
EVENTS = {4: 'server_connected', 8: 'server_disconnected', 16: 'device_found',
          32: 'device_missing', 512: 'device_connected', 33554432: 'device_state_json'}


def pose(raw):
    values = [float(x) for x in raw.split(',')] if isinstance(raw, str) else []
    if len(values) != 7 or not all(math.isfinite(x) for x in values):
        raise ValueError('pose must contain seven finite comma-separated numbers')
    norm = math.sqrt(sum(x*x for x in values[3:]))
    if abs(norm - 1) > .03:
        raise ValueError('invalid quaternion norm')
    return values


def controller_enable(value, packet, control='trigger', threshold=.8):
    """Reported right-controller analog under an explicit receiver-side policy.

    The original client sends no feature-query success/connection flag. This is
    not a vendor device-valid or Tracker tracking-valid assertion. No controller
    value survives across frames; missing values are never interpreted as held.
    """
    issues = []
    app = value.get('appState')
    focus = app.get('focus') if isinstance(app, dict) else None
    if type(focus) is not bool:
        focus = None
        issues.append('missing_or_invalid_app_focus')
    mode = value.get('Input')
    if type(mode) is not int or mode not in (0, 1, 2):
        mode = None
        issues.append('missing_or_invalid_input_mode')
    controllers = value.get('Controller')
    right = controllers.get('right') if isinstance(controllers, dict) else None
    if not isinstance(right, dict):
        right = {}
        issues.append('missing_right_controller')
    analogs = {}
    for key in ('trigger', 'grip'):
        number = right.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1:
            analogs[key] = None
            issues.append('missing_or_invalid_right_' + key)
        else:
            analogs[key] = float(number)
    receipt = packet.get('local_monotonic_ns')
    sequence = packet.get('packet_sequence')
    receipt_ok = type(receipt) is int and receipt > 0
    if not receipt_ok:
        issues.append('missing_or_invalid_receiver_timestamp')
    selected = analogs[control]
    reported_pressed = None if selected is None else selected >= threshold
    blocked = (focus is False or mode in (0, 2) or bool(packet['issues']))
    if blocked:
        held, status = False, 'inhibited'
    elif focus is None or mode is None or reported_pressed is None or not receipt_ok:
        held, status = None, 'unavailable'
    else:
        held = reported_pressed
        status = 'held' if held else 'released'
    return {'source_id': 'pico_right_' + control, 'source': 'same_native_packet.Controller.right',
            'side': 'right', 'control': control, 'threshold': threshold,
            'policy_id': 'reported_right_' + control + '_threshold_focus_controller_mode_v1',
            'policy_origin': 'receiver_defined_not_vendor_deadman',
            'sequence': sequence, 'local_monotonic_ns': receipt,
            'received_t': receipt/1e9 if receipt_ok else None,
            'app_timestamp_ns': packet.get('app_timestamp_ns'),
            'app_focused': focus, 'input_mode': mode,
            'controller_input_active': None if mode is None else mode == 1,
            'trigger': analogs['trigger'], 'grip': analogs['grip'],
            'reported_pressed': reported_pressed, 'held': held, 'status': status,
            'packet_structure_ok': not bool(packet['issues']), 'issues': issues,
            'vendor_controller_valid': None, 'feature_query_success': None,
            'tracker_tracking_valid': None, 'receiver_age_checked': False}


class PacketDecoder:
    def __init__(self, enable_control='trigger', enable_threshold=.8):
        if enable_control not in ('trigger', 'grip'):
            raise ValueError('enable_control must be trigger or grip')
        if type(enable_threshold) not in (int, float) or not math.isfinite(enable_threshold) or not 0 < enable_threshold <= 1:
            raise ValueError('enable_threshold must be a finite number in (0,1]')
        self.enable_control, self.enable_threshold = enable_control, float(enable_threshold)
        self.previous = {}
        self.sequence = 0

    def finish(self, result, value):
        result['controller_enable'] = controller_enable(value, result, self.enable_control, self.enable_threshold)
        result['app_focused'] = result['controller_enable']['app_focused']
        result['input_mode'] = result['controller_enable']['input_mode']
        return result

    def reset(self, device_id=None):
        if device_id:
            self.previous.pop(device_id, None)
        else:
            self.previous.clear()

    def decode(self, raw, device_id, received_ns):
        self.sequence += 1
        result = {'type': 'sample', 'packet_sequence': self.sequence,
                  'device_id': device_id, 'local_monotonic_ns': received_ns,
                  'snapshot_atomic': True, 'trackers': [], 'issues': [],
                  'tracking_valid': None, 'motion_ready': False,
                  'commands_published': 0, 'source': 'native_json_callback'}
        outer = json.loads(raw)
        value = outer.get('value')
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError('packet has no tracking value object')
        result['payload_keys'] = sorted(value)
        stamp = value.get('timeStampNs')
        if type(stamp) is not int or stamp <= 0:
            result['issues'].append('missing_app_timestamp')
        elif device_id in self.previous and stamp <= self.previous[device_id]:
            result['issues'].append('repeated_or_regressed_app_timestamp')
        else:
            self.previous[device_id] = stamp
        result['app_timestamp_ns'] = stamp
        motion = value.get('Motion')
        if not isinstance(motion, dict):
            result['issues'].append('no_independent_motion_stream')
            return self.finish(result, value)
        count, joints = motion.get('len'), motion.get('joints')
        if type(count) is not int or not 0 <= count <= 3 or not isinstance(joints, list) or count > len(joints):
            result['issues'].append('invalid_motion_count')
            return self.finish(result, value)
        result['motion_declared_count'] = count
        result['motion_array_length'] = len(joints)
        # Upstream reuses the array. Entries after len may belong to old frames.
        for joint in joints[:count]:
            if not isinstance(joint, dict):
                result['issues'].append('invalid_tracker_entry')
                continue
            sn = joint.get('sn')
            if not isinstance(sn, str) or not sn.strip():
                result['issues'].append('missing_tracker_sn')
                continue
            try:
                parsed = pose(joint.get('p'))
            except (TypeError, ValueError, OverflowError):
                result['issues'].append('invalid_tracker_pose')
                continue
            confidence = joint.get('confidence')
            if type(confidence) is not int or confidence not in (0, 1, 2, 3):
                confidence = None
            result['trackers'].append({'serial_number': sn.strip(), 'xyzw_pose': parsed,
                                       'representation_ok': True, 'confidence': confidence,
                                       'tracking_valid': None if confidence is None else confidence in (0, 1),
                                       'sensor_timestamp_ns': None})
        serials = [t['serial_number'] for t in result['trackers']]
        if len(set(serials)) != len(serials):
            result['issues'].append('duplicate_tracker_sn')
        result['confidence_available'] = bool(serials) and all(t['confidence'] is not None for t in result['trackers'])
        result['two_tracker_packet'] = len(serials) == 2 and not result['issues']
        return self.finish(result, value)


class DeviceState(ctypes.Structure):
    _fields_ = [('device_id', ctypes.c_char * 32), ('state_json', ctypes.c_char * 16352)]


def worker(args):
    if hashlib.sha256(args.library.read_bytes()).hexdigest() != LIBRARY_SHA256:
        raise ValueError('native SDK library differs from the audited official binary')
    # Keep native C++ stdout away from JSON output, including during deinit.
    output = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    emit = lambda record: print(json.dumps(record, allow_nan=False), file=output, flush=True)
    events = queue.Queue(maxsize=256)
    dropped = [0]
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)

    @callback_type
    def callback(_context, event_type, status, pointer):
        received = time.monotonic_ns()
        try:
            if event_type == 33554432 and pointer:
                # Copy while the native callback owns the stack-allocated struct.
                state = DeviceState.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(DeviceState)))
                payload = {'device_id': bytes(state.device_id).decode('utf-8', 'strict'),
                           'raw': bytes(state.state_json).decode('utf-8', 'strict')}
            elif event_type in (16, 32, 512) and pointer:
                payload = {'device_id': ctypes.string_at(pointer).decode('utf-8', 'replace')}
            else:
                payload = {}
            events.put_nowait((event_type, status, received, payload))
        except (ValueError, UnicodeError, queue.Full):
            dropped[0] += 1

    library = ctypes.CDLL(str(args.library))
    library.PXREAInit.argtypes = [ctypes.c_void_p, callback_type, ctypes.c_uint]
    library.PXREAInit.restype = ctypes.c_int
    library.PXREADeinit.argtypes = []
    library.PXREADeinit.restype = ctypes.c_int
    decoder = PacketDecoder(args.enable_control, args.enable_threshold)
    frames = pairs = failures = matching = 0
    sns = set()
    raw_output = args.raw_output.open('w') if args.raw_output else None
    try:
        rc = library.PXREAInit(None, callback, sum(EVENTS))
        if rc != 0:
            raise RuntimeError('PXREAInit failed: %d' % rc)
        deadline = time.monotonic() + args.seconds
        emit({'type': 'start', 'mode': 'receive_only', 'library_sha256': LIBRARY_SHA256,
              'seconds': args.seconds, 'expected_trackers': args.expected_trackers,
              'controller_enable_control': args.enable_control, 'controller_enable_threshold': args.enable_threshold,
              'tracking_valid_requires_headset_metadata': True})
        while time.monotonic() < deadline:
            try:
                kind, status, received, payload = events.get(timeout=min(.1, max(.001, deadline-time.monotonic())))
            except queue.Empty:
                continue
            if kind != 33554432:
                if kind in (8, 32) or (kind == 512 and status != 0):
                    decoder.reset(payload.get('device_id'))
                emit({'type': 'event', 'event': EVENTS.get(kind, str(kind)), 'status': status,
                      'local_monotonic_ns': received, **payload})
                continue
            if raw_output:
                print(json.dumps({'local_monotonic_ns': received, **payload}), file=raw_output, flush=True)
            try:
                frame = decoder.decode(payload['raw'], payload['device_id'], received)
                frames += 1
                pairs += bool(frame.get('two_tracker_packet'))
                matching += len(frame['trackers']) == args.expected_trackers and not frame['issues']
                sns.update(t['serial_number'] for t in frame['trackers'])
                emit(frame)
            except (ValueError, TypeError, AttributeError, OverflowError) as exc:
                failures += 1
                emit({'type': 'rejected_packet', 'reason': str(exc), 'motion_ready': False})
        emit({'type': 'summary', 'received_tracking_packets': frames, 'two_tracker_packets': pairs,
              'expected_trackers': args.expected_trackers, 'matching_tracker_packets': matching,
              'tracker_sns_seen': sorted(sns), 'malformed_packets': failures,
              'dropped_callback_events': dropped[0], 'motion_ready': False, 'commands_published': 0})
    finally:
        library.PXREADeinit()
        if raw_output:
            raw_output.close()
        output.close()
    return 0 if matching and not failures and not dropped[0] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--expected-trackers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--enable-control', choices=('trigger', 'grip'), default='trigger',
                        help='right controller analog to normalize; no robot output')
    parser.add_argument('--enable-threshold', type=float, default=.8,
                        help='explicit analog threshold in (0,1], default .8')
    parser.add_argument('--library', type=Path, default=LIBRARY)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--raw-output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 120:
        parser.error('--seconds must be in (0,120]')
    if not math.isfinite(args.enable_threshold) or not 0 < args.enable_threshold <= 1:
        parser.error('--enable-threshold must be in (0,1]')
    if args.worker:
        return worker(args)
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
               '--seconds', str(args.seconds), '--library', str(args.library),
               '--enable-control', args.enable_control, '--enable-threshold', str(args.enable_threshold),
               '--expected-trackers', str(args.expected_trackers)]
    if args.raw_output:
        command += ['--raw-output', str(args.raw_output)]
    output = args.output.open('w') if args.output else sys.stdout
    try:
        # Native initialization/deinitialization may block; the worker is bounded.
        return subprocess.run(command, stdout=output, timeout=args.seconds+8).returncode
    except subprocess.TimeoutExpired:
        print(json.dumps({'type': 'error', 'reason': 'native_worker_timeout', 'motion_ready': False}), file=output)
        return 3
    finally:
        if args.output:
            output.close()


if __name__ == '__main__':
    raise SystemExit(main())
