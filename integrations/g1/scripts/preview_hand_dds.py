#!/usr/bin/env python3
"""Preview the original Wuji mapping in BrainCo DDS units; JSON input/output only.

No DDS, serial, glove SDK, network connection or device controller is imported.
Use --demo for a reproducible synthetic exercise, or --binding + --input JSONL.
Recorded timestamps are evaluated only with explicit --replay-clock.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path('/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop')
CHANNELS = ['thumb_flex', 'thumb_opposition', 'index', 'middle', 'ring', 'little']
DDS_CHANNELS = ['Thumb', 'Thumb_aux', 'Index', 'Middle', 'Ring', 'Pinky']


class Refused(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def require(condition, code, detail):
    if not condition:
        raise Refused(code, detail)


def number(value, label):
    message = f'{label} must be a finite number, not bool'
    require(type(value) in (int, float), 'nonfinite_or_invalid_number', message)
    try:
        converted = float(value)
    except OverflowError as exc:
        raise Refused('nonfinite_or_invalid_number', message) from exc
    require(math.isfinite(converted), 'nonfinite_or_invalid_number', message)
    return converted


def vector(value, label, lower=None, upper=None):
    require(isinstance(value, list) and len(value) == 6,
            'invalid_six_channel_vector', f'{label} must have exactly six values')
    result = [number(v, f'{label}[{i}]') for i, v in enumerate(value)]
    if lower is not None:
        require(all(lower <= v <= upper for v in result),
                'out_of_range', f'{label} must be in [{lower}, {upper}]')
    return result


def strict_json(text):
    def invalid(value):
        raise ValueError(f'Non-finite JSON constant: {value}')
    return json.loads(text, parse_constant=invalid)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_artifact(reference, base, label):
    require(isinstance(reference, dict), 'missing_artifact', f'{label} metadata missing')
    name = reference.get('path')
    require(isinstance(name, str) and bool(name), 'missing_artifact', f'{label} path missing')
    path = Path(name).expanduser()
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise Refused('missing_artifact', f'{label}: {exc}') from exc
    require(reference.get('sha256') == digest(data), 'artifact_hash_mismatch',
            f'{label} file does not match its explicit SHA-256 binding')
    return path, data


def load_original(source=DEFAULT_SOURCE, lock=PROJECT / 'sources.lock.json'):
    """Only load the locked pure retarget module, avoiding either teleop package."""
    locked = strict_json(Path(lock).read_text())['hand_source']['sha256']
    source = Path(source).expanduser().resolve()
    for relative in ('teleop/retarget.py', 'settings.example.json'):
        require(digest((source / relative).read_bytes()) == locked[relative],
                'upstream_hash_mismatch', f'Original {relative} changed since sources.lock.json')
    path = source / 'teleop/retarget.py'
    spec = importlib.util.spec_from_file_location('_g1_original_hand_retarget', path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    settings = strict_json((source / 'settings.example.json').read_text())
    require(settings.get('motion_enabled') is False, 'unsafe_example_settings',
            'The original example settings must disable motion')
    require(list(module.CHANNELS) == CHANNELS, 'channel_order_changed', 'Original channel order changed')
    return module, settings


class HandDDSPreview:
    """One-side, stateful mapper. Rejections never contain a command candidate."""
    def __init__(self, binding, base, retarget, settings, *, allow_synthetic=False):
        require(isinstance(binding, dict), 'invalid_binding', 'Binding must be an object')
        self.binding = copy.deepcopy(binding)
        self.binding_id = digest(json.dumps(binding, sort_keys=True, separators=(',', ':'),
                                           allow_nan=False).encode())
        require(binding.get('schema_version') == 1, 'invalid_binding', 'Expected binding schema 1')
        self.synthetic = binding.get('kind') == 'synthetic_test_only'
        require(binding.get('kind') in ('operator_calibrated', 'synthetic_test_only'),
                'uncalibrated', 'Explicit calibrated-operator or synthetic-test binding required')
        require(not self.synthetic or allow_synthetic, 'synthetic_requires_demo',
                'Synthetic artifacts are allowed only in --demo')
        self.side = binding.get('side')
        require(self.side in ('left', 'right'), 'invalid_side', 'Binding side must be left or right')
        for field in ('glove_sn', 'sdk_user', 'clock_id', 'feedback_source_id'):
            require(isinstance(binding.get(field), str) and bool(binding[field].strip()),
                    'invalid_binding', f'Explicit {field} required')
        require(binding['sdk_user'].lower() != 'default', 'uncalibrated', 'Named SDK user required')
        require(binding.get('calibration_confirmed') is True, 'uncalibrated',
                'Operator/model and open-fist calibration must be explicitly confirmed')
        try:
            completed = datetime.fromisoformat(binding['calibrated_at'].replace('Z', '+00:00'))
            require(completed.tzinfo is not None, 'uncalibrated', 'calibrated_at requires a timezone')
        except (KeyError, AttributeError, ValueError) as exc:
            raise Refused('uncalibrated', 'A valid calibrated_at timestamp is required') from exc
        _, data = read_artifact(binding.get('calibration'), Path(base), 'finger calibration')
        cal = strict_json(data)
        require(cal.get('side') == self.side, 'calibration_side_mismatch', 'Calibration side mismatch')
        require(cal.get('channels') == CHANNELS, 'channel_order_mismatch', 'Calibration channel order mismatch')
        opened, closed = cal.get('open'), cal.get('closed')
        require(isinstance(opened, list) and isinstance(closed, list) and len(opened) == len(closed) == 6,
                'invalid_calibration', 'Calibration open/closed must each have six values')
        for i, (a, b) in enumerate(zip(opened, closed)):
            if i == 1 and a is None and b is None:
                continue
            a, b = number(a, f'calibration.open[{i}]'), number(b, f'calibration.closed[{i}]')
            require(abs(b - a) >= retarget.MIN_SPAN, 'invalid_calibration', 'Calibration span too small')
        model = binding.get('sdk_model', {})
        require(model.get('side') == self.side and model.get('sdk_user') == binding['sdk_user'],
                'model_identity_mismatch', 'SDK model evidence must match side and named user')
        require(model.get('kind') == binding['kind'], 'uncalibrated', 'Model provenance must match binding kind')
        model_path, model_data = read_artifact(model, Path(base), 'SDK hand model')
        require(model_path.name == f'{self.side}_hand.urdf', 'model_side_mismatch', 'Expected side-specific SDK model filename')
        try:
            root = ET.fromstring(model_data)
        except ET.ParseError as exc:
            raise Refused('invalid_model', 'SDK model is not valid XML') from exc
        require(root.tag == 'robot' and sum(j.get('type') != 'fixed' for j in root.findall('joint')) == 21,
                'invalid_model', 'Expected a robot URDF with 21 non-fixed hand joints')
        self.retarget, self.calibration = retarget, cal
        self.minimum = vector(settings['target_min'], 'target_min', 0, 1000)
        self.maximum = vector(settings['target_max'], 'target_max', 0, 1000)
        self.alpha = number(binding.get('alpha', .3), 'alpha')
        require(0 < self.alpha <= 1, 'invalid_alpha', 'alpha must be in (0,1]')
        self.max_step = number(binding.get('max_step_1000', int(settings['max_step'] * .8)), 'max_step_1000')
        require(self.max_step.is_integer() and 1 <= self.max_step <= int(settings['max_step'] * .8),
                'invalid_step_limit', 'Step must be an integer within the original 80% margin')
        self.glove_age = number(binding.get('max_glove_age_s', .2), 'max_glove_age_s')
        self.feedback_age = number(binding.get('max_feedback_age_s', .2), 'max_feedback_age_s')
        require(0 < self.glove_age <= .5 and 0 < self.feedback_age <= .5,
                'invalid_age_limit', 'Freshness limits must be in (0,0.5] seconds')
        # Match the project's reviewed service gate; zero is not a portable hold.
        self.speed = vector(binding.get('speed_normalized'), 'speed_normalized', .001, 1)
        self.legacy_duration_ms = settings['segment_ms']
        self.smooth = retarget.LowPass(self.alpha)
        self.clock_watermark = None
        self.last_now = self.last_glove_time = self.last_device_time = self.last_seq = None
        self.last_feedback_id = self.last_feedback_time = self.last_feedback_q = None

    def refused(self, code, detail):
        # No old target is republished; re-entry is still bounded by current measured state.
        self.smooth = self.retarget.LowPass(self.alpha)
        self.last_now = None
        return {'status': 'refused', 'reason': code, 'detail': detail,
                'binding_id': self.binding_id, 'hand_side': self.side,
                'synthetic': self.synthetic, 'hardware_output': False,
                'published': False, 'acknowledged': False}

    def preview(self, record, now):
        try:
            return self._preview(record, now)
        except (Refused, KeyError, TypeError, ValueError) as exc:
            return self.refused(exc.code if isinstance(exc, Refused) else 'malformed_record', str(exc))

    def _preview(self, record, now):
        require(isinstance(record, dict), 'malformed_record', 'Record must be an object')
        now = number(now, 'now')
        require(now >= 0, 'invalid_clock', 'Monotonic clock must be nonnegative')
        if self.clock_watermark is not None:
            require(now > self.clock_watermark, 'regressed_clock', 'Preview clock must strictly advance')
        self.clock_watermark = now
        require(record.get('binding_id') == self.binding_id, 'binding_mismatch', 'Record binding hash mismatch')
        require(record.get('clock_id') == self.binding['clock_id'], 'clock_mismatch', 'Input clock domain mismatch')
        require(record.get('sdk_model_sha256') == self.binding['sdk_model']['sha256'],
                'model_identity_mismatch', 'Input capture model evidence mismatch')
        glove, measured = record['glove'], record['measured']
        require(isinstance(glove, dict) and isinstance(measured, dict), 'malformed_record', 'Glove and measured objects required')
        require(glove.get('hand_side') == measured.get('hand_side') == self.side,
                'side_mismatch', 'Both input sides must match the binding')
        require(glove.get('sn') == self.binding['glove_sn'] and glove.get('sdk_user') == self.binding['sdk_user'],
                'glove_identity_mismatch', 'Glove SN or named SDK user mismatch')
        require(measured.get('topic') == f'rt/brainco/{self.side}/state'
                and measured.get('source_id') == self.binding['feedback_source_id'],
                'feedback_identity_mismatch', 'Feedback topic/source differs from commissioned binding')
        require(measured.get('channels') == DDS_CHANNELS, 'channel_order_mismatch', 'Feedback channel order mismatch')
        source_t = number(glove.get('source_t'), 'glove.source_t')
        received_t = number(measured.get('received_t'), 'measured.received_t')
        require(source_t >= 0 and 0 <= now - source_t <= self.glove_age,
                'stale_or_future_glove', 'Glove timestamp is stale, negative or in the future')
        require(received_t >= 0 and 0 <= now - received_t <= self.feedback_age,
                'stale_or_future_feedback', 'Measured feedback is stale, negative or in the future')
        seq, device_time, feedback_id = glove.get('seq'), glove.get('timestamp_us'), measured.get('sample_id')
        require(all(type(v) is int and v >= 0 for v in (seq, device_time, feedback_id)),
                'invalid_sample_identity', 'Sample IDs/timestamp_us must be nonnegative integers')
        require(device_time > 0, 'invalid_sample_identity', 'Device timestamp must be nonzero')
        if self.last_glove_time is not None:
            require(source_t > self.last_glove_time and device_time > self.last_device_time and seq > self.last_seq,
                    'replayed_or_regressed_glove', 'Glove timestamp/sequence did not advance')
        q = vector(measured.get('q_normalized'), 'measured.q_normalized', 0, 1)
        if self.last_feedback_id is not None:
            require(feedback_id >= self.last_feedback_id and received_t >= self.last_feedback_time,
                    'regressed_feedback', 'Feedback sequence or timestamp regressed')
            if feedback_id == self.last_feedback_id:
                require(received_t == self.last_feedback_time and q == self.last_feedback_q,
                        'rewritten_feedback', 'Same feedback sample cannot acquire a new timestamp/value')
            else:
                require(received_t > self.last_feedback_time, 'regressed_feedback', 'New feedback requires a new receipt timestamp')
        values = glove.get('joint_angles')
        require(isinstance(values, dict), 'invalid_glove_readings', 'Original joint_angles object required')
        for key, value in values.items():
            number(value, f'joint_angles.{key}')
        raw = self.retarget.readings(glove, self.side)
        require(raw is not None, 'invalid_glove_readings', 'Original readings() rejected this frame')
        if self.last_now is not None:
            require(now > self.last_now, 'regressed_clock', 'Preview clock must advance')
        dt = 1 / 120 if self.last_now is None else now - self.last_now
        goal = self.retarget.targets(self.smooth(self.retarget.closure(raw, self.calibration), dt=dt),
                                     self.minimum, self.maximum)
        measured_1000 = [v * 1000 for v in q]
        limited = self.retarget.step_limited(goal, measured_1000, self.max_step)
        require(all(lo <= value <= hi for lo, value, hi in zip(self.minimum, limited, self.maximum)),
                'step_envelope_conflict', 'No candidate within both the original envelope and current step limit')
        require(all(abs(value - actual) <= self.max_step + 1e-4 for value, actual in zip(limited, measured_1000)),
                'step_quantization_conflict', 'Original integer quantization exceeded the measured-state step bound')
        self.last_now, self.last_glove_time, self.last_device_time, self.last_seq = now, source_t, device_time, seq
        self.last_feedback_id, self.last_feedback_time, self.last_feedback_q = feedback_id, received_t, q
        return {'status': 'preview', 'binding_id': self.binding_id, 'hand_side': self.side,
                'synthetic': self.synthetic, 'hardware_output': False, 'published': False, 'acknowledged': False,
                'topic': f'rt/brainco/{self.side}/cmd', 'idl': 'unitree_go.msg.dds_.MotorCmds_',
                'channels': CHANNELS, 'dds_channels': DDS_CHANNELS,
                'glove_age_s': now - source_t, 'feedback_age_s': now - received_t,
                'measured_1000': measured_1000, 'mapped_goal_1000': goal,
                'step_limited_1000': limited, 'max_step_1000': self.max_step,
                'command_preview': {'cmds': [{'q': target / 1000, 'dq': speed}
                                            for target, speed in zip(limited, self.speed)]},
                'speed_semantics': 'normalized finger-speed parameter; not radians/second or duration',
                'legacy_segment_ms_not_translated': self.legacy_duration_ms}


def synthetic_fixture(directory):
    """Deliberately synthetic left artifacts, unrelated to either real hand model."""
    directory = Path(directory)
    calibration = {'side': 'left', 'channels': CHANNELS,
                   'open': [0, None, 0, 0, 0, 0], 'closed': [100, None, 100, 100, 100, 100]}
    cal_data = json.dumps(calibration).encode()
    model_data = ('<robot name="SYNTHETIC_TEST_ONLY">' + ''.join(
        f'<joint name="fixture_{i}" type="revolute"/>' for i in range(21)) + '</robot>').encode()
    (directory / 'finger-map.json').write_bytes(cal_data)
    (directory / 'left_hand.urdf').write_bytes(model_data)
    return {'schema_version': 1, 'kind': 'synthetic_test_only', 'side': 'left',
            'glove_sn': 'SYNTHETIC_LEFT_GLOVE', 'sdk_user': 'synthetic_operator',
            'clock_id': 'synthetic_monotonic', 'feedback_source_id': 'synthetic_left_hand',
            'calibration_confirmed': True, 'calibrated_at': '2000-01-01T00:00:00Z',
            'calibration': {'path': 'finger-map.json', 'sha256': digest(cal_data)},
            'sdk_model': {'path': 'left_hand.urdf', 'sha256': digest(model_data),
                          'side': 'left', 'sdk_user': 'synthetic_operator', 'kind': 'synthetic_test_only'},
            'speed_normalized': [.2] * 6, 'alpha': .3, 'max_step_1000': 80,
            'max_glove_age_s': .2, 'max_feedback_age_s': .2}


def synthetic_record(adapter, seq=1, now=100., amount=100., q=None):
    return {'binding_id': adapter.binding_id, 'clock_id': adapter.binding['clock_id'],
            'sdk_model_sha256': adapter.binding['sdk_model']['sha256'], 'now_monotonic_s': now,
            'glove': {'sn': adapter.binding['glove_sn'], 'hand_side': adapter.side,
                      'sdk_user': adapter.binding['sdk_user'], 'source_t': now - .01,
                      'seq': seq, 'timestamp_us': 1_000_000 + seq * 10_000,
                      'joint_angles': {key: amount for key in ('thumb', 'index', 'middle', 'ring', 'little')}},
            'measured': {'hand_side': adapter.side, 'source_id': adapter.binding['feedback_source_id'],
                         'topic': f'rt/brainco/{adapter.side}/state', 'channels': DDS_CHANNELS,
                         'sample_id': seq, 'received_t': now - .005,
                         'q_normalized': q if q is not None else [.5] * 6}}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', type=Path, default=DEFAULT_SOURCE)
    p.add_argument('--binding', type=Path, help='Side/SN/user/model/calibration evidence JSON')
    p.add_argument('--input', type=Path, help='JSONL snapshots; otherwise read stdin')
    p.add_argument('--replay-clock', action='store_true', help='Offline only: use each record now_monotonic_s')
    p.add_argument('--check-binding', action='store_true', help='Validate artifacts and print binding ID without reading input')
    p.add_argument('--demo', action='store_true', help='Finite synthetic left exercise, never real calibration')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.demo and (args.binding or args.input or args.replay_clock or args.check_binding):
        p.error('--demo is independent of --binding/--input/--replay-clock/--check-binding')
    if not args.demo and args.binding is None:
        p.error('--binding is required outside --demo')
    if args.replay_clock and args.input is None:
        p.error('--replay-clock requires an explicit recording file')
    if args.check_binding and (args.input or args.replay_clock):
        p.error('--check-binding does not consume input records')
    try:
        original, settings = load_original(args.source_dir)
        if args.demo:
            with tempfile.TemporaryDirectory(prefix='g1-hand-dds-synthetic-') as directory:
                binding = synthetic_fixture(directory)
                adapter = HandDDSPreview(binding, directory, original, settings, allow_synthetic=True)
                records = [synthetic_record(adapter, i + 1, 100 + i * .02, value)
                           for i, value in enumerate((0, 50, 100, 100, 0))]
                records[3]['measured']['received_t'] -= 1
                for record in records:
                    result = adapter.preview(record, record['now_monotonic_s'])
                    print(json.dumps(result, allow_nan=False))
                return 0
        binding = strict_json(args.binding.read_text())
        adapter = HandDDSPreview(binding, args.binding.resolve().parent, original, settings)
        if args.check_binding:
            print(json.dumps({'status': 'binding_valid', 'binding_id': adapter.binding_id,
                              'hand_side': adapter.side, 'glove_sn': binding['glove_sn'],
                              'sdk_user': binding['sdk_user'], 'sdk_model_sha256': binding['sdk_model']['sha256'],
                              'fresh_input_checked': False, 'hardware_output': False}, allow_nan=False))
            return 0
        stream = args.input.open() if args.input else sys.stdin
        refused = total = 0
        try:
            for line in stream:
                if not line.strip():
                    continue
                total += 1
                try:
                    record = strict_json(line)
                    now = record['now_monotonic_s'] if args.replay_clock else time.monotonic()
                    result = adapter.preview(record, now)
                except (ValueError, TypeError, KeyError) as exc:
                    result = adapter.refused('invalid_json_record', str(exc))
                refused += result['status'] == 'refused'
                result['offline_replay'] = args.replay_clock
                print(json.dumps(result, allow_nan=False), flush=True)
        finally:
            if args.input:
                stream.close()
        return 1 if refused or not total else 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(json.dumps({'status': 'configuration_refused', 'detail': str(exc),
                          'reason': exc.code if isinstance(exc, Refused) else type(exc).__name__,
                          'hardware_output': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
