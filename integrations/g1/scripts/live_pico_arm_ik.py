#!/usr/bin/env python3
"""Bounded right-arm IK preview from JSONL; no SDK, sockets or robot output.

One reference record followed by normalized pico_raw_probe sample records.
Live timestamps must use this host's monotonic clock. --offline-replay keeps
recorded timestamps and uses their event clock; it never establishes freshness.
"""
import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

import preview_arms as arms
from preview_pico_single_arm import single_target


MAX_LINE_BYTES = 65536
HYPOTHESIS_AXES = ((0., 0., -1.), (-1., 0., 0.), (0., 1., 0.))


class IKRejected(ValueError):
    def __init__(self, reason, details):
        super().__init__(reason)
        self.details = details


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid_' + name)
    return float(value)


def integer(value, name, minimum=1):
    if type(value) is not int or not minimum <= value <= 2**63-1:
        raise ValueError('invalid_' + name)
    return value


def vector(value, size, name):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError('invalid_' + name)
    return [number(x, name) for x in value]


def rotation(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError('invalid_axis_rotation')
    rows = [vector(row, 3, 'axis_rotation') for row in value]
    for i in range(3):
        for j in range(3):
            if abs(sum(rows[i][k] * rows[j][k] for k in range(3)) - (i == j)) > 1e-6:
                raise ValueError('invalid_axis_rotation')
    a, b, c = rows
    det = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(det - 1.) > 1e-6:
        raise ValueError('axis_rotation_must_be_proper')
    return rows


@dataclass(frozen=True)
class Limits:
    position_scale: float = .02
    translation_m: float = .01
    joint_excursion_rad: float = .03
    position_residual_m: float = .002
    rotation_residual_rad: float = .03
    sample_age_s: float = .10
    reference_capture_age_s: float = .10
    reference_duration_s: float = 60.
    sample_gap_s: float = .20
    tracker_jump_m: float = .10
    tracker_jump_rad: float = .50
    iterations: int = 2

    def validate(self):
        ceilings = {'position_scale': .02, 'translation_m': .01, 'joint_excursion_rad': .03,
                    'position_residual_m': .002, 'rotation_residual_rad': .03,
                    'sample_age_s': .2, 'reference_capture_age_s': .2,
                    'reference_duration_s': 60., 'sample_gap_s': .2,
                    'tracker_jump_m': .1, 'tracker_jump_rad': .5}
        for key, ceiling in ceilings.items():
            if not 0 < number(getattr(self, key), key) <= ceiling:
                raise ValueError('out_of_range_' + key)
        if type(self.iterations) is not int or not 1 <= self.iterations <= 8:
            raise ValueError('iterations_must_be_1_to_8')


class OfficialRightArmIK:
    """One audited model/optimizer; references are optimizer parameters.

    Reuses the official objective terms and smoothing with the reference posture
    and small joint window from preview_pico_single_arm. Model torque is ignored.
    """
    def __init__(self, limits=Limits(), source=arms.DEFAULT_SOURCE):
        import numpy as np
        import casadi
        limits.validate()
        self.np, self.limits = np, limits
        started = time.perf_counter()
        self.ik, self.versions = arms.load_ik(Path(source))
        self.anchor = self.ik.opti.parameter(14)
        self.ik.opti.minimize(50*self.ik.translational_cost + self.ik.rotation_cost
                             + .02*casadi.sumsqr(self.ik.var_q-self.anchor)
                             + .1*self.ik.smooth_cost)
        self.ik.opti.subject_to(self.ik.opti.bounded(
            self.anchor-limits.joint_excursion_rad, self.ik.var_q,
            self.anchor+limits.joint_excursion_rad))
        self.ik.opti.subject_to(self.ik.var_q[:7] == self.anchor[:7])
        # Compile IPOPT before announcing readiness; never let cold compilation
        # relabel an old reference or input packet as newly received.
        self.set_reference(arms.REFERENCE_Q)
        self.solve([0., 0., 0.])
        self.load_and_warmup_ms = (time.perf_counter()-started)*1000

    def set_reference(self, q):
        np = self.np
        q = np.asarray(vector(q, 14, 'q_rad'), dtype=float)
        model = self.ik.reduced_robot.model
        if np.any(q < model.lowerPositionLimit) or np.any(q > model.upperPositionLimit):
            raise ValueError('reference_outside_model_limits')
        self.reference = q.copy()
        self.last_q = q.copy()
        self.targets = arms.forward(self.ik, q)
        self.ik.opti.set_value(self.anchor, q)
        self.ik.init_data = q.copy()
        self.ik.smooth_filter = type(self.ik.smooth_filter)(np.array([.4, .3, .2, .1]), 14)
        self.ik.smooth_filter.add_data(q.copy())

    def solve(self, delta):
        np, ik = self.np, self.ik
        targets = [t.copy() for t in self.targets]
        # Reuse single_target's existing radial clipping and fixed-orientation
        # behavior. Invert its explicit axis hypothesis because delta is already
        # expressed in the caller's configured robot axes, in metres.
        unmap = np.asarray(HYPOTHESIS_AXES).T @ np.asarray(delta)
        targets[1], mapping = single_target(targets[1], np.zeros(3), unmap, 1., self.limits.translation_m)
        q = self.last_q.copy()
        states = []
        for _ in range(self.limits.iterations):
            q, _unused_model_torque = ik.solve_ik(*targets, q.copy(), np.zeros(14))
            stats = ik.opti.stats()
            states.append({'success': bool(stats.get('success')),
                           'return_status': stats.get('return_status', 'unknown')})
            if not stats.get('success'):
                raise IKRejected('ik_solver_failed:' + str(stats.get('return_status')),
                                 {'solver_iterations': states})
        q = np.asarray(q, dtype=float)
        if q.shape != (14,) or not np.isfinite(q).all():
            raise ValueError('ik_nonfinite_or_wrong_size')
        if np.max(np.abs(q[:7]-self.reference[:7])) > 1e-6:
            raise ValueError('ik_moved_fixed_left_arm')
        q[:7] = self.reference[:7]  # Remove equality-solver numerical epsilon.
        check = arms.evaluate(ik, q, targets, self.limits.position_residual_m,
                              self.limits.rotation_residual_rad)
        diagnostics = {'residuals': check['residuals'], 'joint_limits_ok': check['joint_limits_ok'],
                       'max_joint_excursion_rad': float(np.max(np.abs(q-self.reference))),
                       'bounded_delta_m': mapping['bounded_delta_m'],
                       'translation_clipped': mapping['was_clipped'], 'solver_iterations': states}
        if not check['joint_limits_ok'] or not check['residuals_ok']:
            raise IKRejected('ik_limits_or_residual_failed', diagnostics)
        if np.max(np.abs(q-self.reference)) > self.limits.joint_excursion_rad+1e-6:
            raise IKRejected('ik_reference_excursion_failed', diagnostics)
        self.last_q = q.copy()
        return {'q_rad': q.tolist(), 'residuals': check['residuals'],
                'solver_iterations': states, 'bounded_delta_m': mapping['bounded_delta_m'],
                'translation_clipped': mapping['was_clipped']}


class LivePicoArmIK:
    """Stateful preview gate; invalid data latches until a new reference ID."""
    def __init__(self, backend, limits=Limits(), *, clock_ns=time.monotonic_ns,
                 offline_replay=False):
        limits.validate()
        self.backend, self.limits, self.clock_ns = backend, limits, clock_ns
        self.offline_replay = bool(offline_replay)
        self.state = 'WAIT_REFERENCE'
        self.reference = None
        self.origin = self.previous_pose = None
        self.last_sequence = self.last_receipt = self.last_app_stamp = None
        self.device_id = None
        self.reference_ids = set()
        self.last_state_sequence = self.last_state_receipt = None

    def output(self, status, record=None, **extra):
        record = record if isinstance(record, dict) else {}
        receipt = record.get('local_monotonic_ns')
        result = {'type': 'arm_ik_preview', 'status': status, 'state': self.state,
                  'mode': 'offline_recorded_replay' if self.offline_replay else 'live_input_preview',
                  'accepted': False, 'motion_ready': False, 'commands_published': 0,
                  'robot_accessed': False, 'tracking_valid': None,
                  'source_packet_sequence': record.get('packet_sequence'),
                  'source_received_monotonic_ns': receipt,
                  'source_app_timestamp_ns': record.get('app_timestamp_ns'),
                  'sequence': record.get('packet_sequence'),
                  'received_t': receipt/1e9 if type(receipt) is int and 0 <= receipt <= 2**63-1 else None,
                  'processed_monotonic_ns': self.clock_ns()}
        if self.reference:
            result.update(reference_id=self.reference['reference_id'],
                          calibration_id=self.reference['axis_config']['id'],
                          axes_verified=self.reference['axis_config']['verified'],
                          right_sn=self.reference['right_sn'],
                          reference_received_monotonic_ns=self.reference['received_monotonic_ns'],
                          reference_time_provenance=self.reference.get('time_provenance'),
                          reference_source_clock_id=self.reference.get('source_clock_id'),
                          reference_age_upper_bound_s=self.reference.get('age_upper_bound_s'))
        result.update(extra)
        return result

    def reject(self, reason, record=None, details=None):
        self.state = 'LATCHED'
        return self.output('rejected', record, reason=str(reason), new_reference_required=True,
                           rejection_details=details)

    def age(self, stamp, now, limit, name):
        integer(stamp, name)
        if stamp > now or now-stamp > int(limit*1e9):
            raise ValueError('stale_or_future_' + name)

    def reference_record(self, record, now):
        ident = record.get('reference_id')
        if not isinstance(ident, str) or not ident.strip() or ident in self.reference_ids:
            raise ValueError('missing_or_reused_reference_id')
        self.age(record.get('received_monotonic_ns'), now,
                 self.limits.reference_capture_age_s, 'reference')
        state_seq = integer(record.get('state_sequence'), 'state_sequence', 0)
        if self.last_state_sequence is not None and (state_seq <= self.last_state_sequence
                or record['received_monotonic_ns'] <= self.last_state_receipt):
            raise ValueError('replayed_or_regressed_reference_state')
        if record.get('state_valid') is not True:
            raise ValueError('reference_state_not_valid')
        if record.get('clock_domain') != 'local_monotonic':
            raise ValueError('reference_clock_domain_must_be_local_monotonic')
        kind = 'recorded' if self.offline_replay else 'live'
        if record.get('reference_kind') != kind:
            raise ValueError('reference_kind_must_be_' + kind)
        sn = record.get('right_sn')
        if not isinstance(sn, str) or not sn.strip():
            raise ValueError('missing_right_sn')
        axes = record.get('axis_config')
        if not isinstance(axes, dict) or not isinstance(axes.get('id'), str) or not axes['id'].strip():
            raise ValueError('missing_explicit_axis_config_id')
        if axes.get('pose_unit') != 'm' or type(axes.get('verified')) is not bool:
            raise ValueError('axis_unit_or_verified_missing')
        rotation(axes.get('T_robot_axes_xr_axes'))
        q = vector(record.get('q_rad'), 14, 'q_rad')
        self.backend.set_reference(q)
        if not self.offline_replay:
            self.age(record['received_monotonic_ns'], self.clock_ns(),
                     self.limits.reference_capture_age_s, 'reference_after_setup')
        self.reference = json.loads(json.dumps(record, allow_nan=False))
        self.reference_ids.add(ident)
        self.last_state_sequence, self.last_state_receipt = state_seq, record['received_monotonic_ns']
        self.origin = self.previous_pose = None
        self.state = 'WAIT_ORIGIN'
        return self.output('reference_accepted', record,
                           reference_freshness='recorded_unknown_now' if self.offline_replay else 'checked_at_capture',
                           current_robot_feedback_checked=False)

    def sample_record(self, record, now):
        if not self.reference or self.state == 'LATCHED':
            raise ValueError('fresh_reference_required')
        if record.get('source') != 'native_json_callback' or record.get('snapshot_atomic') is not True:
            raise ValueError('atomic_native_packet_required')
        if record.get('issues') != []:
            raise ValueError('pico_packet_issues')
        seq = integer(record.get('packet_sequence'), 'packet_sequence')
        stamp = integer(record.get('local_monotonic_ns'), 'packet_receipt')
        app_stamp = integer(record.get('app_timestamp_ns'), 'app_timestamp_ns')
        self.age(stamp, now, self.limits.sample_age_s, 'pico_sample')
        if stamp < self.reference['received_monotonic_ns']:
            raise ValueError('pico_sample_predates_reference')
        if now-self.reference['received_monotonic_ns'] > self.limits.reference_duration_s*1e9:
            raise ValueError('reference_session_expired')
        if self.origin is None and stamp-self.reference['received_monotonic_ns'] > self.limits.sample_gap_s*1e9:
            raise ValueError('origin_too_late_after_reference')
        device = record.get('device_id')
        if not isinstance(device, str) or not device:
            raise ValueError('missing_device_id')
        if self.device_id is not None and device != self.device_id:
            raise ValueError('headset_changed_restart_required')
        if self.last_sequence is not None:
            if seq <= self.last_sequence or stamp <= self.last_receipt or app_stamp <= self.last_app_stamp:
                raise ValueError('replayed_or_regressed_pico_packet')
            if self.origin is not None and stamp-self.last_receipt > self.limits.sample_gap_s*1e9:
                raise ValueError('pico_stream_gap')
        trackers = record.get('trackers')
        count = record.get('motion_declared_count')
        if not isinstance(trackers, list) or type(count) is not int or not 1 <= count <= 3 or count != len(trackers):
            raise ValueError('invalid_normalized_motion_count')
        if not all(isinstance(t, dict) for t in trackers):
            raise ValueError('invalid_tracker_entry')
        serials = [t.get('serial_number') for t in trackers]
        if not all(isinstance(s, str) and s for s in serials) or len(set(serials)) != len(serials):
            raise ValueError('invalid_or_duplicate_tracker_sn')
        matches = [t for t in trackers if t['serial_number'] == self.reference['right_sn']]
        if len(matches) != 1:
            raise ValueError('right_tracker_missing')
        tracker = matches[0]
        if tracker.get('representation_ok') is not True:
            raise ValueError('invalid_tracker_representation')
        pose = vector(tracker.get('xyzw_pose'), 7, 'xyzw_pose')
        norm = math.sqrt(sum(x*x for x in pose[3:]))
        if not .99 <= norm <= 1.01:
            raise ValueError('invalid_quaternion_norm')
        confidence, valid = tracker.get('confidence'), tracker.get('tracking_valid')
        if valid is not None and type(valid) is not bool:
            raise ValueError('invalid_tracking_valid_type')
        if confidence is not None and (type(confidence) is not int or confidence not in (0, 1, 2, 3)):
            raise ValueError('invalid_tracking_confidence')
        if valid is False or confidence in (2, 3) or record.get('tracking_valid') is False:
            raise ValueError('tracking_explicitly_invalid')
        tracking_valid = True if valid is True and confidence in (0, 1) else None
        if self.previous_pose is not None:
            distance = math.dist(pose[:3], self.previous_pose[:3])
            old_norm = math.sqrt(sum(x*x for x in self.previous_pose[3:]))
            dot = abs(sum(a*b for a, b in zip(pose[3:], self.previous_pose[3:])))/(norm*old_norm)
            angle = 2*math.acos(min(1., dot))
            if distance > self.limits.tracker_jump_m or angle > self.limits.tracker_jump_rad:
                raise ValueError('tracker_pose_jump_new_reference_required')
        self.device_id, self.last_sequence = device, seq
        self.last_receipt, self.last_app_stamp = stamp, app_stamp
        first = self.origin is None
        if first:
            self.origin = pose[:3]
        raw = [pose[i]-self.origin[i] for i in range(3)]
        axes = self.reference['axis_config']['T_robot_axes_xr_axes']
        delta = [self.limits.position_scale*sum(row[i]*raw[i] for i in range(3)) for row in axes]
        start = time.perf_counter()
        if first:
            solved = {'q_rad': list(self.reference['q_rad']), 'bounded_delta_m': [0., 0., 0.],
                      'translation_clipped': False, 'solver_iterations': [],
                      'residuals': {s: {'position_m': 0., 'rotation_rad': 0.} for s in arms.SIDES}}
        else:
            try:
                solved = self.backend.solve(delta)
            except IKRejected as exc:
                exc.details['solve_ms'] = (time.perf_counter()-start)*1000
                raise
        elapsed = (time.perf_counter()-start)*1000
        # Validate even injected/test backends; no fallback q can become a target.
        q = vector(solved['q_rad'], 14, 'candidate_q')
        if any(abs(q[i]-self.reference['q_rad'][i]) > 1e-9 for i in range(7)):
            raise ValueError('candidate_moved_left_arm')
        if max(abs(a-b) for a, b in zip(q, self.reference['q_rad'])) > self.limits.joint_excursion_rad+1e-6:
            raise ValueError('candidate_reference_excursion')
        if not self.offline_replay:
            self.age(stamp, self.clock_ns(), self.limits.sample_age_s, 'pico_after_solve')
        self.previous_pose = pose
        self.state = 'PREVIEWING'
        return self.output('origin_captured' if first else 'candidate', record,
                           accepted=True, q_candidate_rad=q, tracking_valid=tracking_valid,
                           tracking_confidence=confidence, tracking_quality_known=tracking_valid is not None,
                           source_sensor_timestamp_ns=tracker.get('sensor_timestamp_ns'),
                           source_age_ms=None if self.offline_replay else (self.clock_ns()-stamp)/1e6,
                           raw_tracker_delta_m=raw, position_scale=self.limits.position_scale,
                           solve_ms=elapsed, orientation='held_at_robot_reference', left_arm='fixed',
                           current_robot_feedback_checked=False,
                           preview_only_reason='no_robot_output' if tracking_valid else 'tracking_quality_unknown',
                           **{k: v for k, v in solved.items() if k != 'q_rad'})

    def process(self, record):
        try:
            if not isinstance(record, dict):
                raise ValueError('record_must_be_object')
            now = self.clock_ns()
            if self.offline_replay:
                key = 'received_monotonic_ns' if record.get('type') == 'reference' else 'local_monotonic_ns'
                now = record.get(key, now)
                integer(now, 'replay_event_time')
            if record.get('type') == 'reference':
                return self.reference_record(record, now)
            if record.get('type') == 'sample':
                return self.sample_record(record, now)
            if record.get('type') == 'event' and record.get('event') in ('server_disconnected', 'device_missing'):
                raise ValueError('pico_' + record['event'])
            if record.get('type') in ('start', 'summary', 'event'):
                return self.output('metadata_ignored', record)
            raise ValueError('unsupported_record_type')
        except (ValueError, KeyError, TypeError, RuntimeError, OverflowError) as exc:
            return self.reject(exc, record, getattr(exc, 'details', None))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', type=Path, default=arms.DEFAULT_SOURCE)
    p.add_argument('--seconds', type=float, default=30., help='wall-clock input window after model warmup, <=3600')
    p.add_argument('--max-records', type=int, default=10000)
    p.add_argument('--offline-replay', action='store_true', help='saved event time; never live freshness')
    p.add_argument('--iterations', type=int, default=2, help='official solves per sample, 1..8')
    args = p.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600 or not 1 <= args.max_records <= 100000:
        p.error('seconds must be (0,3600], max-records 1..100000')
    # Native IPOPT and Python initialization chatter both stay off JSON stdout.
    output = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    emit = lambda obj: print(json.dumps(obj, allow_nan=False), file=output, flush=True)
    try:
        limits = Limits(iterations=args.iterations)
        backend = OfficialRightArmIK(limits, args.source_dir)
        gate = LivePicoArmIK(backend, limits, offline_replay=args.offline_replay)
        emit({'type': 'ready', 'mode': 'offline_recorded_replay' if args.offline_replay else 'live_input_preview',
              'load_and_warmup_ms': backend.load_and_warmup_ms, 'dependencies': backend.versions,
              'upstream_commit': arms.COMMIT, 'motion_ready': False, 'commands_published': 0})
        incoming = queue.Queue(maxsize=1)

        def reader():
            while True:
                line = sys.stdin.readline(MAX_LINE_BYTES+1)
                incoming.put(line)
                if not line or len(line) > MAX_LINE_BYTES:
                    return

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic()+args.seconds
        records = candidates = rejected = 0
        reason = 'max_records'
        while records < args.max_records:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                reason = 'time_limit'
                break
            try:
                line = incoming.get(timeout=remaining)
            except queue.Empty:
                reason = 'time_limit'
                break
            if not line:
                reason = 'eof'
                break
            records += 1
            if len(line) > MAX_LINE_BYTES:
                emit(gate.reject('jsonl_line_too_long'))
                rejected += 1
                reason = 'line_limit'
                break
            try:
                record = json.loads(line, parse_constant=lambda v: (_ for _ in ()).throw(ValueError('nonfinite_json')))
                result = gate.process(record)
            except (json.JSONDecodeError, ValueError) as exc:
                result = gate.reject('invalid_json:' + str(exc))
            emit(result)
            candidates += bool(result.get('accepted'))
            rejected += result['status'] == 'rejected'
        emit({'type': 'summary', 'records': records, 'candidates': candidates,
              'rejected': rejected, 'reason': reason, 'motion_ready': False, 'commands_published': 0})
        return 0 if candidates and not rejected else 1
    except (ValueError, RuntimeError, ImportError, OSError) as exc:
        emit({'type': 'error', 'error': str(exc), 'motion_ready': False, 'commands_published': 0})
        return 2
    except (KeyboardInterrupt, BrokenPipeError):
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
