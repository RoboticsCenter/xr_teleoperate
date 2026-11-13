import builtins
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import live_pico_arm_ik as live


class Clock:
    now = 10_000_000_000

    def __call__(self):
        return self.now


def reference(clock, ident='r1', seq=1):
    return {'type': 'reference', 'reference_id': ident, 'state_sequence': seq,
            'received_monotonic_ns': clock.now, 'reference_kind': 'live',
            'clock_domain': 'local_monotonic', 'state_valid': True,
            'q_rad': list(live.arms.REFERENCE_Q), 'right_sn': 'RIGHT',
            'axis_config': {'id': 'UNVERIFIED_TEST_AXES', 'verified': False,
                            'pose_unit': 'm', 'T_robot_axes_xr_axes': [list(r) for r in live.HYPOTHESIS_AXES]}}


def sample(clock, seq=1, x=0.):
    return {'type': 'sample', 'source': 'native_json_callback', 'snapshot_atomic': True,
            'packet_sequence': seq, 'local_monotonic_ns': clock.now,
            'app_timestamp_ns': 1_700_000_000_000_000_000+seq*10_000_000,
            'device_id': 'HEADSET', 'issues': [], 'motion_declared_count': 1,
            'motion_array_length': 2, 'tracking_valid': None,
            'trackers': [{'serial_number': 'RIGHT', 'representation_ok': True,
                          'xyzw_pose': [x, 0., 0., 0., 0., 0., 1.],
                          'tracking_valid': None, 'confidence': None, 'sensor_timestamp_ns': None}]}


class Backend:
    calls = 0
    fail = False
    q_offset = 0.

    def set_reference(self, q):
        self.q = list(q)

    def solve(self, delta):
        self.calls += 1
        self.delta = delta
        if self.fail:
            raise ValueError('ik_solver_failed')
        q = list(self.q)
        q[7] += self.q_offset
        return {'q_rad': q, 'bounded_delta_m': delta, 'translation_clipped': False,
                'solver_iterations': [{'success': True}],
                'residuals': {s: {'position_m': 0., 'rotation_rad': 0.} for s in live.arms.SIDES}}


class StreamGateTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.backend = Clock(), Backend()
        self.gate = live.LivePicoArmIK(self.backend, clock_ns=self.clock)

    def start(self):
        self.assertEqual(self.gate.process(reference(self.clock))['status'], 'reference_accepted')
        self.clock.now += 10_000_000
        first = self.gate.process(sample(self.clock))
        self.assertTrue(first['accepted'])
        return first

    def next(self, seq=2, x=.01):
        self.clock.now += 10_000_000
        return self.gate.process(sample(self.clock, seq, x))

    def test_first_target_exact_reference_and_missing_quality_never_true(self):
        out = self.start()
        self.assertEqual(out['q_candidate_rad'], live.arms.REFERENCE_Q)
        self.assertEqual(self.backend.calls, 0)
        self.assertIsNone(out['tracking_valid'])
        out = self.next()
        self.assertTrue(out['accepted'])
        self.assertFalse(out['motion_ready'])
        self.assertEqual(out['commands_published'], 0)
        self.assertEqual(out['q_candidate_rad'][:7], live.arms.REFERENCE_Q[:7])
        self.assertAlmostEqual(self.backend.delta[1], -.0002)

    def test_original_sequence_and_receive_time_survive_processing(self):
        self.start()
        self.clock.now += 10_000_000
        packet = sample(self.clock, 2)
        self.clock.now += 30_000_000
        out = self.gate.process(packet)
        self.assertEqual(out['sequence'], 2)
        self.assertEqual(out['received_t'], packet['local_monotonic_ns']/1e9)
        self.assertEqual(out['source_received_monotonic_ns'], packet['local_monotonic_ns'])
        self.assertEqual(out['source_app_timestamp_ns'], packet['app_timestamp_ns'])
        self.assertEqual(out['source_age_ms'], 30.)

    def test_stale_output_after_slow_solve_has_no_candidate(self):
        self.start()
        solve = self.backend.solve
        def slow(delta):
            result = solve(delta)
            self.clock.now += 110_000_000
            return result
        self.backend.solve = slow
        out = self.next()
        self.assertIn('pico_after_solve', out['reason'])
        self.assertNotIn('q_candidate_rad', out)

    def test_invalid_reference_cannot_be_relabelled_fresh(self):
        cases = [lambda r: r.update(received_monotonic_ns=self.clock.now-200_000_000),
                 lambda r: r.update(reference_kind='recorded'),
                 lambda r: r.update(clock_domain='pc2_monotonic'),
                 lambda r: r.update(state_valid=False),
                 lambda r: r.update(q_rad=[0.]*13),
                 lambda r: r['axis_config'].update(id=''),
                 lambda r: r['axis_config'].update(pose_unit='cm'),
                 lambda r: r['axis_config'].update(T_robot_axes_xr_axes=[[1,0,0],[0,1,0],[0,0,-1]])]
        for alter in cases:
            with self.subTest(alter=alter):
                gate = live.LivePicoArmIK(Backend(), clock_ns=self.clock)
                record = reference(self.clock)
                alter(record)
                self.assertEqual(gate.process(record)['status'], 'rejected')

    def test_bad_tracking_and_motion_length_fail_closed(self):
        for field, val in [('tracking_valid', False), ('confidence', 2), ('confidence', True), ('xyzw_pose', [0]*7)]:
            with self.subTest(field=field):
                gate = live.LivePicoArmIK(Backend(), clock_ns=self.clock)
                gate.process(reference(self.clock))
                packet = sample(self.clock)
                packet['trackers'][0][field] = val
                self.assertEqual(gate.process(packet)['status'], 'rejected')
        gate = live.LivePicoArmIK(Backend(), clock_ns=self.clock)
        gate.process(reference(self.clock))
        packet = sample(self.clock)
        packet['motion_declared_count'] = 2
        self.assertIn('motion_count', gate.process(packet)['reason'])

    def test_claimed_valid_without_confidence_stays_unknown(self):
        self.gate.process(reference(self.clock))
        packet = sample(self.clock)
        packet['trackers'][0]['tracking_valid'] = True
        out = self.gate.process(packet)
        self.assertTrue(out['accepted'])
        self.assertIsNone(out['tracking_valid'])

    def test_known_valid_still_preview_only(self):
        self.gate.process(reference(self.clock))
        packet = sample(self.clock)
        packet['trackers'][0].update(tracking_valid=True, confidence=1)
        out = self.gate.process(packet)
        self.assertIs(out['tracking_valid'], True)
        self.assertIs(out['motion_ready'], False)

    def test_duplicates_and_app_timestamp_regression_reject(self):
        self.start()
        packet = sample(self.clock)
        self.assertIn('replayed', self.gate.process(packet)['reason'])
        gate = live.LivePicoArmIK(Backend(), clock_ns=self.clock)
        gate.process(reference(self.clock))
        gate.process(sample(self.clock))
        self.clock.now += 10_000_000
        packet = sample(self.clock, 2)
        packet['app_timestamp_ns'] -= 100_000_000
        self.assertIn('regressed', gate.process(packet)['reason'])

    def test_jump_latches_until_distinct_fresh_reference(self):
        self.start()
        self.assertIn('pose_jump', self.next(x=.2)['reason'])
        self.assertEqual(self.next(3)['state'], 'LATCHED')
        self.clock.now += 10_000_000
        bad = reference(self.clock, 'r2', 1)
        self.assertIn('reference_state', self.gate.process(bad)['reason'])
        good = reference(self.clock, 'r2', 2)
        self.assertEqual(self.gate.process(good)['status'], 'reference_accepted')
        self.assertEqual(self.next(4)['status'], 'origin_captured')

    def test_orientation_jump_rejected_even_when_target_orientation_fixed(self):
        self.start()
        self.clock.now += 10_000_000
        packet = sample(self.clock, 2)
        packet['trackers'][0]['xyzw_pose'][3:] = [0., 0., 1., 0.]
        self.assertIn('pose_jump', self.gate.process(packet)['reason'])

    def test_solver_failure_and_excursion_never_emit_fallback(self):
        for attr, val in [('fail', True), ('q_offset', .031)]:
            self.setUp()
            self.start()
            setattr(self.backend, attr, val)
            out = self.next()
            self.assertEqual(out['status'], 'rejected')
            self.assertNotIn('q_candidate_rad', out)

    def test_disconnect_requires_new_reference_and_stream_gap_latches(self):
        self.start()
        out = self.gate.process({'type': 'event', 'event': 'device_missing'})
        self.assertEqual(out['state'], 'LATCHED')
        self.assertFalse(self.next()['accepted'])
        self.setUp()
        self.start()
        self.clock.now += 300_000_000
        self.assertIn('stream_gap', self.next()['reason'])

    def test_replay_preserves_old_clock_and_is_never_live(self):
        self.gate = live.LivePicoArmIK(self.backend, clock_ns=self.clock, offline_replay=True)
        ref = reference(self.clock)
        ref['reference_kind'] = 'recorded'
        self.gate.process(ref)
        packet = sample(self.clock)
        self.clock.now += 100_000_000_000
        out = self.gate.process(packet)
        self.assertTrue(out['accepted'])
        self.assertEqual(out['source_received_monotonic_ns'], packet['local_monotonic_ns'])
        self.assertEqual(out['mode'], 'offline_recorded_replay')
        self.assertIsNone(out['source_age_ms'])
        self.assertFalse(out['motion_ready'])
        live_gate = live.LivePicoArmIK(Backend(), clock_ns=self.clock)
        self.assertEqual(live_gate.process(ref)['status'], 'rejected')


try:
    import pinocchio.casadi
    HAVE_IK = True
except ImportError:
    HAVE_IK = False


@unittest.skipUnless(HAVE_IK, 'requires isolated Pinocchio/CasADi environment')
class RealSolverTests(unittest.TestCase):
    def test_recorded_frames_load_model_once_and_keep_limits(self):
        import preview_pico_single_arm as recorded
        path = ROOT/'diagnostics/teleop-pico-current-20261111-1810.jsonl'
        if not path.exists():
            self.skipTest('session recording absent')
        packets = [json.loads(x) for x in path.read_text().splitlines() if json.loads(x).get('type') == 'sample'][:40]
        q, _ = recorded.recorded_arm_state(ROOT/'diagnostics/pc2-feedback-service-verified-2026-11-11.json')
        clock = Clock()
        clock.now = packets[0]['local_monotonic_ns']
        ref = reference(clock)
        ref.update(q_rad=q, right_sn='PC2310MLL9241957G', reference_kind='recorded')
        original_import = builtins.__import__
        def no_controls(name, *args, **kwargs):
            if name.startswith(('unitree_sdk', 'xrobotoolkit')):
                raise AssertionError('control import forbidden')
            return original_import(name, *args, **kwargs)
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
             patch.object(builtins, '__import__', side_effect=no_controls), \
             patch.object(live.arms, 'load_ik', wraps=live.arms.load_ik) as loader:
            backend = live.OfficialRightArmIK()
            gate = live.LivePicoArmIK(backend, clock_ns=clock, offline_replay=True)
            self.assertEqual(gate.process(ref)['status'], 'reference_accepted')
            for packet in packets:
                out = gate.process(packet)
                self.assertTrue(out['accepted'], out)
                self.assertEqual(out['q_candidate_rad'][:7], q[:7])
                self.assertLessEqual(max(abs(a-b) for a,b in zip(out['q_candidate_rad'], q)), .030001)
                self.assertLessEqual(out['residuals']['right']['position_m'], .002)
                self.assertFalse(out['motion_ready'])
            self.assertEqual(loader.call_count, 1)

    def test_real_optimizer_failure_is_not_a_successful_fallback(self):
        backend = live.OfficialRightArmIK()
        backend.ik.opti.solver('ipopt', {'ipopt.max_iter': 0, 'ipopt.print_level': 0, 'print_time': False})
        with patch('logging.Logger.error'), self.assertRaisesRegex(ValueError, 'ik_solver_failed'):
            backend.solve([.001, 0., 0.])

    def test_large_displacement_is_clipped_before_residual_rejection(self):
        backend = live.OfficialRightArmIK()
        try:
            result = backend.solve([.1, 0., 0.])
        except live.IKRejected as exc:
            result = exc.details
        self.assertTrue(result['translation_clipped'])
        self.assertAlmostEqual(sum(x*x for x in result['bounded_delta_m'])**.5, .01)

    def test_cli_times_out_even_if_stdin_never_closes(self):
        p = subprocess.Popen([sys.executable, str(ROOT/'scripts/live_pico_arm_ik.py'), '--seconds', '.05'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(json.loads(p.stdout.readline())['type'], 'ready')
            p.wait(timeout=5)
            rows = [json.loads(line) for line in p.stdout]
            self.assertEqual(rows[-1]['reason'], 'time_limit')
            self.assertEqual(rows[-1]['candidates'], 0)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()


if __name__ == '__main__':
    unittest.main()
