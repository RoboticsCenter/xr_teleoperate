#!/usr/bin/env python3
"""Offline single-wrist position preview; no network or robot command output.

Axis/unit assumptions below are not physical calibration. Wrist orientation and
the other arm remain at the recorded G1 reference. All model torque is discarded.
"""
import argparse
import json
import math
from pathlib import Path
import preview_arms as arms


def recorded_arm_state(path):
    report = json.loads(path.read_text())
    report = report.get('probe', report)
    stream = report['streams']['rt/lowstate']
    if stream['status'] != 'fresh' or stream['invalid_count']:
        raise ValueError('robot recording was not fresh/valid when captured')
    sample = stream['latest_valid_sample']
    return sample['left_arm_q_rad'] + sample['right_arm_q_rad'], {
        'recorded_at_utc': report.get('finished_at_utc'), 'mode_machine': sample['mode_machine'],
        'mode_pr': sample['mode_pr'], 'current_robot_freshness': 'unknown: saved recording'}


def selected_packets(path, serial):
    selected = []
    for line in path.read_text().splitlines():
        sample = json.loads(line)
        if sample.get('type') != 'sample' or sample.get('source') != 'native_json_callback' or sample.get('issues'):
            continue
        matches = [t for t in sample.get('trackers', []) if t.get('serial_number') == serial]
        if len(matches) == 1 and sample.get('motion_declared_count') == len(sample['trackers']):
            selected.append(sample)
    if len(selected) < 2:
        raise ValueError('need two received non-replayed packets for selected SN')
    return selected


def single_target(reference, start, end, scale, bound):
    import numpy as np
    if not math.isfinite(scale) or not 0 < scale <= 1 or not math.isfinite(bound) or not 0 < bound <= .02:
        raise ValueError('scale must be (0,1], bound (0,0.02] metres')
    # Hypothesis for preview only: XR right/up/back -> robot forward/left/up.
    axes = np.array([[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]])
    delta = np.asarray(end, dtype=float) - np.asarray(start, dtype=float)
    if delta.shape != (3,) or not np.isfinite(delta).all():
        raise ValueError('position delta must be three finite values')
    proposed = axes @ delta * scale
    magnitude = float(np.linalg.norm(proposed))
    limited = proposed * min(1., bound / magnitude) if magnitude else proposed
    target = reference.copy()
    target[:3, 3] += limited
    return target, {'axis_hypothesis': axes.tolist(), 'raw_position_delta': delta.tolist(),
                    'scaled_delta_m': proposed.tolist(), 'bounded_delta_m': limited.tolist(),
                    'bound_m': bound, 'position_scale': scale, 'was_clipped': magnitude > bound,
                    'orientation': 'held at robot reference'}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pico-jsonl', required=True, type=Path)
    p.add_argument('--robot-state', required=True, type=Path)
    p.add_argument('--side', required=True, choices=('left', 'right'))
    p.add_argument('--tracker-sn', required=True)
    p.add_argument('--position-scale', type=float, default=.1)
    p.add_argument('--max-translation-m', type=float, default=.01)
    p.add_argument('--max-joint-excursion-rad', type=float, default=.03)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--svg', type=Path)
    args = p.parse_args(argv)
    try:
        import numpy as np
        packets = selected_packets(args.pico_jsonl, args.tracker_sn)
        p0 = arms.tracker_pose(packets[0], args.tracker_sn)[:3, 3]
        p1 = arms.tracker_pose(packets[-1], args.tracker_sn)[:3, 3]
        q_reference, state_metadata = recorded_arm_state(args.robot_state)
        q_reference = np.asarray(q_reference, dtype=float)
        ik, versions = arms.load_ik(arms.DEFAULT_SOURCE)
        model = ik.reduced_robot.model
        if q_reference.shape != (14,) or not np.isfinite(q_reference).all() or np.any(q_reference < model.lowerPositionLimit) or np.any(q_reference > model.upperPositionLimit):
            raise ValueError('recorded positions outside model limits')
        targets = arms.forward(ik, q_reference)
        side = 0 if args.side == 'left' else 1
        targets[side], mapping = single_target(targets[side], p0, p1, args.position_scale, args.max_translation_m)
        q, iterations = q_reference.copy(), []
        fixed = slice(7, 14) if side == 0 else slice(0, 7)
        bound = args.max_joint_excursion_rad
        if not math.isfinite(bound) or not 0 < bound <= .03:
            raise ValueError('commissioning joint excursion must be (0,0.03] rad')
        import casadi
        # Keep the official model and error terms, anchor posture to the saved
        # reference instead of q=0, and constrain this small commissioning trial.
        ik.opti.minimize(50 * ik.translational_cost + ik.rotation_cost
                        + .02 * casadi.sumsqr(ik.var_q - q_reference) + .1 * ik.smooth_cost)
        ik.opti.subject_to(ik.opti.bounded(q_reference-bound, ik.var_q, q_reference+bound))
        ik.opti.subject_to(ik.var_q[fixed] == q_reference[fixed])
        for index in range(8):
            q, _unused_torque = ik.solve_ik(*targets, q.copy(), np.zeros(14))
            q[fixed] = q_reference[fixed]  # regularization must not move the unused arm
            stats = ik.opti.stats()
            iterations.append({'iteration': index+1, 'solver_success': bool(stats.get('success')),
                               'return_status': stats.get('return_status')})
            if not stats.get('success'):
                break
        check = arms.evaluate(ik, q, targets, .002, .03)
        excursion_ok = bool(np.max(np.abs(q-q_reference)) <= bound+1e-6)
        ok = all(i['solver_success'] for i in iterations) and check['joint_limits_ok'] and check['residuals_ok'] and excursion_ok
        result = {'type': 'recorded_single_arm_position_preview', 'offline_ok': ok,
                  'motion_ready': False, 'commands_published': 0, 'robot_accessed': False,
                  'side': args.side, 'tracker_sn': args.tracker_sn,
                  'accepted_recorded_packets': len(packets), 'robot_recording': state_metadata,
                  'q_reference_rad': q_reference.tolist(), 'joint_delta_rad': (q-q_reference).tolist(),
                  'max_joint_excursion_rad': bound, 'joint_excursion_ok': excursion_ok,
                  'ik_adaptation': 'official model/error terms; posture anchored to saved reference; trial joint window; other arm fixed',
                  'mapping': mapping, 'dependencies': versions, 'iterations': iterations, **check,
                  'limitations': ['Axis directions and metre scale are unverified hypotheses.',
                                 'Only translation follows; tracker mount/orientation uncalibrated.',
                                 'Headset client does not report tracking confidence.',
                                 'Saved robot state is not current feedback or Regular-mode evidence.',
                                 'IK success is not collision or output-gate validation.']}
        if args.svg:
            arms.write_svg(args.svg, ik, q, targets)
    except (ValueError, KeyError, TypeError, OSError, ImportError, RuntimeError) as exc:
        result = {'type': 'recorded_single_arm_position_preview', 'offline_ok': False,
                  'motion_ready': False, 'commands_published': 0, 'error': str(exc)}
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['offline_ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
