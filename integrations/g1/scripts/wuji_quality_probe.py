#!/usr/bin/env python3
"""Bounded input-only Wuji diagnosis; never declares teleoperation ready."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from wuji_probe import DEFAULT_SOURCE, calibration_summary, user_summary


FINGERS = ('thumb', 'index', 'middle', 'ring', 'little')
IK_THRESHOLD = 0.5
EMF_THRESHOLD = 0.85


def duration(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 30:
        raise argparse.ArgumentTypeError('must be finite and greater than 0, at most 30 seconds')
    return value


def serial(value):
    if not value or value.strip() != value or any(c.isspace() for c in value):
        raise argparse.ArgumentTypeError('must be an exact, nonempty SN without whitespace')
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sn', required=True, type=serial)
    p.add_argument('--side', required=True, choices=('left', 'right'))
    p.add_argument('--seconds', type=duration, default=3.0,
                   help='sample window, 0 < seconds <= 30 (default 3)')
    p.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    p.add_argument('--user', help='optional required active user; never changes SDK user')
    p.add_argument('--output', type=Path, help='also save JSON here; SDK logs go to stderr')
    p.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    return p


def load_original_filter(source):
    path = source / 'wuji_bridge.py'
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    lock_path = Path(__file__).resolve().parents[1] / 'sources.lock.json'
    expected = json.loads(lock_path.read_text())['hand_source']['sha256']['wuji_bridge.py']
    if digest != expected:
        raise ValueError('Original wuji_bridge.py SHA256 differs from sources.lock.json; import refused')
    spec = importlib.util.spec_from_file_location('wuji_quality_original_bridge', path)
    module = importlib.util.module_from_spec(spec)
    # Execute exactly the verified bytes, without writing upstream __pycache__.
    exec(compile(data, str(path), 'exec'), module.__dict__)
    function = module.joint_readings
    if inspect.signature(function).parameters['min_confidence'].default != IK_THRESHOLD:
        raise ValueError('Original joint_readings default threshold is no longer 0.5')
    if tuple(module.FINGERS) != FINGERS:
        raise ValueError('Original finger order changed; diagnosis needs review')
    return function, {'path': str(path), 'sha256': digest,
                      'function': 'joint_readings(frame)', 'confidence_threshold': IK_THRESHOLD}


def discovery_summary(devices, sdk):
    return [{'sn': d.sn, 'address': d.address, 'transport': str(d.transport_type),
             'device_type': str(d.device_type),
             'verified_glove': d.device_type == sdk.DeviceType.WujiGlove} for d in devices]


def select_direct_glove(devices, sn):
    matches = [d for d in devices if d.get('sn') == sn]
    if len(matches) != 1:
        raise ValueError('Exact SN must appear exactly once in this scan; no connection attempted')
    chosen = matches[0]
    if chosen.get('verified_glove') is not True or chosen.get('transport') != 'UDP':
        raise ValueError('Only directly discovered UDP DeviceType.WujiGlove is allowed')
    address = chosen.get('address', '')
    try:
        host, port = address.split(':')
        ip = ipaddress.IPv4Address(host)
        valid = (str(ip) == host and port.isdecimal() and 1 <= int(port) <= 65535
                 and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback)
                 and host != '255.255.255.255')
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('Discovery address must be a direct unicast IPv4:port endpoint')
    return chosen


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def confidence_summary(rows, threshold):
    result = {}
    for index, name in enumerate(FINGERS):
        values = [row[index] for row in rows if row[index] is not None]
        result[name] = {
            'count': len(values), 'missing_or_nonfinite': len(rows) - len(values),
            'min': min(values) if values else None, 'max': max(values) if values else None,
            'mean': sum(values) / len(values) if values else None,
            'below_threshold': sum(v < threshold for v in values),
            'outside_0_1': sum(not 0 <= v <= 1 for v in values),
        }
    return result


class StreamStats:
    def __init__(self, field, threshold):
        self.field, self.threshold = field, threshold
        self.raw_frames = self.malformed_headers = self.malformed_shapes = 0
        self.nonfinite_angle_fingers = self.accepted = self.filter_errors = 0
        self.keys, self.rows, self.by_timestamp, self.ambiguous_timestamps = set(), [], {}, set()
        self.first = self.last = None

    def add(self, frame, now, original_filter=None):
        self.raw_frames += 1
        try:
            key = (frame.header.seq, frame.header.timestamp_us)
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in key):
                raise ValueError('invalid frame header')
        except (AttributeError, ValueError):
            self.malformed_headers += 1
            return
        if key in self.keys:
            return
        self.keys.add(key)
        self.first = now if self.first is None else self.first
        self.last = now
        fingers = getattr(frame, self.field, ())
        if len(fingers) != 5:
            self.malformed_shapes += 1
        row = []
        for i in range(5):
            value = getattr(fingers[i], 'confidence', None) if i < len(fingers) else None
            row.append(float(value) if finite_number(value) else None)
        self.rows.append(row)
        timestamp = key[1]
        if timestamp in self.by_timestamp:
            self.ambiguous_timestamps.add(timestamp)
        self.by_timestamp[timestamp] = row
        if original_filter is not None:
            for finger in fingers:
                angles = getattr(finger, 'angles', ())
                if len(angles) != 5:
                    self.malformed_shapes += 1
                if not all(finite_number(v) for v in angles):
                    self.nonfinite_angle_fingers += 1
            try:
                # Invoke the upstream function with its unchanged default threshold.
                if original_filter(frame) is not None:
                    self.accepted += 1
            except Exception:
                self.filter_errors += 1

    def summary(self, start, end):
        count = len(self.keys)
        return {'raw_frames': self.raw_frames, 'unique_frame_keys': count,
                'duplicate_frames': self.raw_frames - count - self.malformed_headers,
                'malformed_headers': self.malformed_headers,
                'malformed_shapes': self.malformed_shapes,
                'finger_frames_with_nonfinite_angles': self.nonfinite_angle_fingers,
                'observed_hz': count / (end - start),
                'first_receive_monotonic': self.first, 'last_receive_monotonic': self.last,
                'last_receive_age_seconds': end - self.last if self.last is not None else None,
                'confidence_threshold': self.threshold,
                'confidence_by_finger': confidence_summary(self.rows, self.threshold)}


def matched_summary(ik, emf):
    shared = ((set(ik.by_timestamp) & set(emf.by_timestamp))
              - ik.ambiguous_timestamps - emf.ambiguous_timestamps)
    paired_ik = [ik.by_timestamp[t] for t in sorted(shared)]
    paired_emf = [emf.by_timestamp[t] for t in sorted(shared)]
    return {'method': 'exact header.timestamp_us equality; no nearest-time interpolation',
            'status': 'matched' if shared else 'unavailable_no_exact_matches',
            'matched_timestamp_count': len(shared),
            'unmatched_ik_timestamps': len(set(ik.by_timestamp) - shared),
            'unmatched_emf_timestamps': len(set(emf.by_timestamp) - shared),
            'ambiguous_ik_timestamps': len(ik.ambiguous_timestamps),
            'ambiguous_emf_timestamps': len(emf.ambiguous_timestamps),
            'ik_confidence_threshold': IK_THRESHOLD, 'raw_emf_reference_threshold': EMF_THRESHOLD,
            'ik_confidence_by_finger': confidence_summary(paired_ik, IK_THRESHOLD),
            'raw_emf_confidence_by_finger': confidence_summary(paired_emf, EMF_THRESHOLD)}


def sample_streams(ik_subscription, emf_subscription, seconds, original_filter,
                   clock=time.monotonic, sleep=time.sleep):
    ik, emf = StreamStats('fingers', IK_THRESHOLD), StreamStats('poses', EMF_THRESHOLD)
    start = clock()
    emf_error = None
    while clock() - start < seconds:
        received = False
        for sub, stats, function in ((ik_subscription, ik, original_filter),
                                     (emf_subscription, emf, None)):
            if sub is None or (stats is emf and emf_error is not None):
                continue
            for _ in range(16):
                if clock() - start >= seconds:
                    break
                try:
                    frame = sub.recv()
                except Exception as exc:
                    if stats is ik:
                        raise
                    emf_error = {'type': type(exc).__name__, 'message': str(exc)}
                    break
                if frame is None:
                    break
                received = True
                stats.add(frame, clock(), function)
        if not received:
            sleep(0.0005)
    end = clock()
    ik_result = ik.summary(start, end)
    ik_result.update({'accepted_by_original_filter': ik.accepted,
                      'accepted_fraction': ik.accepted / len(ik.keys) if ik.keys else None,
                      'original_filter_errors': ik.filter_errors})
    result = {'duration_seconds': end - start, 'ik': ik_result,
              'raw_emf': emf.summary(start, end), 'timestamp_matching': matched_summary(ik, emf)}
    if emf_error:
        result['raw_emf']['error'] = emf_error
    return result


def base_report(args):
    return {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'mode': 'input_quality_only',
            'requested': {'sn': args.sn, 'side': args.side, 'seconds': args.seconds},
            'hardware_motion': False, 'calibration_writes': False, 'network_configuration_writes': False,
            'command_output': False, 'teleoperation_ready': False,
            'readiness_note': 'Short diagnosis does not verify wearer, calibration, or robot control readiness'}


def probe_sdk(sdk, args, home=None, sampler=sample_streams):
    report = base_report(args)
    manager, subscriptions = None, []
    try:
        original_filter, report['source_filter'] = load_original_filter(args.source)
        manager = sdk.SdkManager.instance()
        user = user_summary(manager.current_user())
        report['sdk_user'] = user
        if args.user is not None and user.get('display_name') != args.user:
            raise ValueError('Active SDK user differs from --user; no switch or connection attempted')
        report['calibration'] = calibration_summary(user, args.source, home)
        model = report['calibration'].get('sdk_models', {}).get(args.side, {})
        report['personal_model_available'] = user.get('is_default') is False and model.get('readable_xml', False)
        report['discovery'] = discovery_summary(manager.scan(), sdk)  # exactly one scan
        selected = select_direct_glove(report['discovery'], args.sn)
        glove = manager.connect(address=selected['address'], device_name='g1_wuji_quality_probe',
                                options=sdk.ConnectOptions(timeout_ms=1000, retry_count=1,
                                enable_bridge=False, auto_time_sync_interval_ms=None))
        if not isinstance(glove, sdk.WujiGlove):
            raise ValueError('Connected object is not WujiGlove; no subscription attempted')
        report['identity'] = {'sn': glove.sn().get(), 'hand_side': glove.hand_side().get(),
                              'version': glove.version().get()}
        if report['identity']['sn'] != args.sn or report['identity']['hand_side'] != args.side:
            raise ValueError('Read-back SN/side mismatch; no subscription attempted')
        ik_sub = glove.hand_joint_angles().subscribe()
        subscriptions.append(ik_sub)
        emf_sub = None
        try:
            emf_sub = glove.emf_poses().subscribe()
            subscriptions.append(emf_sub)
            report['raw_emf_subscription'] = {'status': 'subscribed'}
        except Exception as exc:
            report['raw_emf_subscription'] = {'status': 'unavailable', 'type': type(exc).__name__,
                                               'message': str(exc)}
        report['sample'] = sampler(ik_sub, emf_sub, args.seconds, original_filter)
        ik = report['sample']['ik']
        report['status'] = ('filter_error' if ik['original_filter_errors'] else
                            'sampled' if ik['unique_frame_keys'] else 'no_frames')
        if not report['personal_model_available']:
            report['model_note'] = 'No verified named-user model for requested side; diagnosis only'
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        cleanup_errors = []
        for subscription in reversed(subscriptions):
            try:
                subscription.close()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if manager is not None:
            try:
                manager.disconnect_all()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            report['cleanup_errors'] = cleanup_errors
            report['status'] = 'cleanup_failed'
        report['completed_utc'] = datetime.now(timezone.utc).isoformat()
    return report


def worker(args):
    saved_stdout = os.dup(1)
    try:
        sys.stdout.flush()
        os.dup2(2, 1)  # native SDK output must not corrupt JSON stdout
        try:
            import wuji_sdk
            report = probe_sdk(wuji_sdk, args)
            report['sdk_version'] = importlib.metadata.version('wuji-sdk')
        except Exception as exc:
            report = base_report(args)
            report.update(status='failed', error={'type': type(exc).__name__, 'message': str(exc)})
        sys.stdout.flush()
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if report['status'] == 'sampled' else 1


def run_bounded(args, popen=subprocess.Popen):
    command = [sys.executable, str(Path(__file__).resolve()), '--_worker',
               '--sn', args.sn, '--side', args.side, '--seconds', str(args.seconds),
               '--source', str(args.source)]
    if args.user is not None:
        command.extend(['--user', args.user])
    budget = args.seconds + 10.0
    process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    start_new_session=True)
    failure = None
    try:
        out, err = process.communicate(timeout=budget)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        failure = 'WorkerTimeout' if isinstance(exc, subprocess.TimeoutExpired) else 'Interrupted'
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = process.communicate()
    if err:
        print(err, end='' if err.endswith('\n') else '\n', file=sys.stderr)
    if failure:
        report = base_report(args)
        report.update(status='failed', error={'type': failure,
                      'message': 'Worker stopped; no readiness claim or automatic retry'})
    else:
        try:
            report = json.loads(out)
            if not isinstance(report, dict) or 'status' not in report:
                raise ValueError('missing worker status')
        except (ValueError, TypeError):
            report = base_report(args)
            report.update(status='failed', error={'type': 'InvalidWorkerResult',
                                                  'message': 'SDK worker did not return valid JSON'})
    report['worker_exit_code'] = process.returncode
    report['worker_timeout_seconds'] = budget
    if process.returncode != 0 and report.get('status') == 'sampled':
        report['status'] = 'worker_failed'
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    args.source = args.source.expanduser().resolve()
    if args._worker:
        return worker(args)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    report = run_bounded(args)
    encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['status'] == 'sampled' else 1


if __name__ == '__main__':
    raise SystemExit(main())
