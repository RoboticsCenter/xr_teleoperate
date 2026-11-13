"""Offline contract checks; original hand retargeting is the only mapping engine."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'preview_hand_dds.py'
SPEC = importlib.util.spec_from_file_location('preview_hand_dds', SCRIPT)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original, cls.settings = preview.load_original()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.binding = preview.synthetic_fixture(self.base)
        self.adapter = self.make()

    def make(self, binding=None, allow=True):
        return preview.HandDDSPreview(binding or self.binding, self.base, self.original,
                                      self.settings, allow_synthetic=allow)

    def record(self, seq=1, now=100, amount=100, q=None):
        return preview.synthetic_record(self.adapter, seq, now, amount, q)

    def assert_refused(self, record, code, now=None):
        result = self.adapter.preview(record, record['now_monotonic_s'] if now is None else now)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['reason'], code, result)
        self.assertNotIn('command_preview', result)
        self.assertFalse(result['published'])
        self.assertFalse(result['hardware_output'])
        return result

    def test_closed_pose_keeps_original_order_and_uses_measured_step_limit(self):
        result = self.adapter.preview(self.record(), 100)
        self.assertEqual(result['mapped_goal_1000'], [900, 950, 950, 950, 950, 950])
        self.assertEqual(result['step_limited_1000'], [580] * 6)
        self.assertEqual(result['command_preview']['cmds'], [{'q': .58, 'dq': .2}] * 6)
        self.assertEqual(result['dds_channels'], ['Thumb', 'Thumb_aux', 'Index', 'Middle', 'Ring', 'Pinky'])
        self.assertEqual(result['legacy_segment_ms_not_translated'], 200)
        self.assertFalse(result['acknowledged'])

    def test_step_limit_uses_actual_feedback_not_last_preview(self):
        self.adapter.preview(self.record(), 100)
        second = self.adapter.preview(self.record(2, 100.02), 100.02)
        self.assertEqual(second['step_limited_1000'], [580] * 6)
        third = self.adapter.preview(self.record(3, 100.04, q=[.6] * 6), 100.04)
        self.assertEqual(third['step_limited_1000'], [680] * 6)

    def test_original_lowpass_uses_elapsed_time_and_original_functions(self):
        calls = []
        class OriginalProxy:
            def __getattr__(_, name):
                value = getattr(self.original, name)
                if name not in ('readings', 'closure', 'LowPass', 'targets', 'step_limited'):
                    return value
                def tracked(*args, **kwargs):
                    calls.append(name)
                    return value(*args, **kwargs)
                return tracked
        adapter = preview.HandDDSPreview(self.binding, self.base, OriginalProxy(), self.settings,
                                         allow_synthetic=True)
        first = preview.synthetic_record(adapter, amount=0)
        adapter.preview(first, 100)
        second = preview.synthetic_record(adapter, 2, 100.02, amount=100)
        result = adapter.preview(second, 100.02)
        self.assertEqual(calls, ['LowPass', 'readings', 'closure', 'targets', 'step_limited',
                                'readings', 'closure', 'targets', 'step_limited'])
        # A fresh 20 ms update must be smoothed, rather than jumping directly to 900/950.
        self.assertGreater(result['mapped_goal_1000'][0], 100)
        self.assertLess(result['mapped_goal_1000'][0], 900)

    def test_stale_and_future_input_produce_no_candidate(self):
        for section, field, delta, code in (
            ('glove', 'source_t', -1, 'stale_or_future_glove'),
            ('glove', 'source_t', 1, 'stale_or_future_glove'),
            ('measured', 'received_t', -1, 'stale_or_future_feedback'),
            ('measured', 'received_t', 1, 'stale_or_future_feedback'),
        ):
            with self.subTest(section=section, delta=delta):
                self.adapter = self.make()
                record = self.record()
                record[section][field] += delta
                self.assert_refused(record, code)

    def test_sn_side_user_clock_topic_and_model_binding_are_not_optional(self):
        cases = [
            (('glove', 'sn'), 'another_glove', 'glove_identity_mismatch'),
            (('glove', 'sdk_user'), 'another_user', 'glove_identity_mismatch'),
            (('glove', 'hand_side'), 'right', 'side_mismatch'),
            (('measured', 'hand_side'), 'right', 'side_mismatch'),
            (('measured', 'source_id'), 'another_hand', 'feedback_identity_mismatch'),
            (('measured', 'topic'), 'rt/brainco/right/state', 'feedback_identity_mismatch'),
            (('clock_id',), 'another_clock', 'clock_mismatch'),
            (('sdk_model_sha256',), 'wrong', 'model_identity_mismatch'),
            (('binding_id',), 'wrong', 'binding_mismatch'),
        ]
        for path, value, code in cases:
            with self.subTest(path=path):
                self.adapter = self.make()
                record = self.record()
                target = record if len(path) == 1 else record[path[0]]
                target[path[-1]] = value
                self.assert_refused(record, code)
        self.adapter = self.make()
        record = self.record()
        del record['glove']['hand_side']
        self.assert_refused(record, 'side_mismatch')

    def test_bad_feedback_or_glove_numbers_fail_closed(self):
        for value in (float('nan'), float('inf'), -float('inf'), True, '0.5', 10 ** 1000):
            with self.subTest(value=value):
                self.adapter = self.make()
                record = self.record()
                record['measured']['q_normalized'][2] = value
                self.assert_refused(record, 'nonfinite_or_invalid_number')
                self.adapter = self.make()
                record = self.record()
                record['glove']['joint_angles']['index'] = value
                self.assert_refused(record, 'nonfinite_or_invalid_number')
        self.adapter = self.make()
        record = self.record(q=[1.1] * 6)
        self.assert_refused(record, 'out_of_range')

    def test_wrong_vector_length_or_channel_order(self):
        record = self.record(q=[.5] * 5)
        self.assert_refused(record, 'invalid_six_channel_vector')
        self.adapter = self.make()
        record = self.record()
        record['measured']['channels'] = list(reversed(preview.DDS_CHANNELS))
        self.assert_refused(record, 'channel_order_mismatch')

    def test_repeated_glove_frame_and_rewritten_feedback_are_rejected(self):
        first = self.record()
        self.adapter.preview(first, 100)
        self.assert_refused(first, 'replayed_or_regressed_glove', now=100.01)
        self.adapter = self.make()
        self.adapter.preview(self.record(), 100)
        second = self.record(2, 100.02)
        second['measured']['sample_id'] = 1
        self.assert_refused(second, 'rewritten_feedback')

    def test_same_feedback_may_be_reused_only_while_unchanged_and_fresh(self):
        first = self.record()
        self.adapter.preview(first, 100)
        second = self.record(2, 100.02)
        second['measured'] = copy.deepcopy(first['measured'])
        result = self.adapter.preview(second, 100.02)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['step_limited_1000'], [580] * 6)

    def test_rejection_resets_smoothing_but_preserves_clock_and_replay_guards(self):
        self.adapter.preview(self.record(amount=100), 100)
        stale = self.record(2, 100.02, amount=0)
        stale['measured']['received_t'] -= 1
        self.assert_refused(stale, 'stale_or_future_feedback')
        backwards = self.record(3, 100.01, amount=0)
        self.assert_refused(backwards, 'regressed_clock')
        recovered = self.adapter.preview(self.record(4, 100.04, amount=0), 100.04)
        self.assertEqual(recovered['mapped_goal_1000'], [100, 0, 0, 0, 0, 0])
        self.assertEqual(recovered['step_limited_1000'], [420] * 6)

    def test_impossible_envelope_and_step_intersection_is_refused(self):
        self.assert_refused(self.record(q=[0] * 6), 'step_envelope_conflict')

    def test_uncalibrated_or_wrong_side_artifacts_do_not_load(self):
        for mutation in (
            {'calibration_confirmed': False}, {'sdk_user': 'Default'},
            {'side': 'right'}, {'calibrated_at': ''},
        ):
            with self.subTest(mutation=mutation):
                binding = copy.deepcopy(self.binding)
                binding.update(mutation)
                with self.assertRaises(preview.Refused):
                    self.make(binding)
        binding = copy.deepcopy(self.binding)
        binding['sdk_model']['side'] = 'right'
        with self.assertRaises(preview.Refused):
            self.make(binding)

    def test_missing_or_changed_model_and_calibration_refused(self):
        for filename in ('left_hand.urdf', 'finger-map.json'):
            with self.subTest(filename=filename):
                path = self.base / filename
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                with self.assertRaisesRegex(preview.Refused, 'SHA-256'):
                    self.make()
                path.write_bytes(original)
                path.unlink()
                with self.assertRaises(preview.Refused):
                    self.make()
                path.write_bytes(original)

    def test_synthetic_fixture_cannot_be_loaded_in_regular_mode(self):
        with self.assertRaises(preview.Refused) as ctx:
            self.make(allow=False)
        self.assertEqual(ctx.exception.code, 'synthetic_requires_demo')

    def test_speed_matches_service_gate_and_never_uses_zero_as_hold(self):
        for speed in (0, .0009, 1.001):
            binding = copy.deepcopy(self.binding)
            binding['speed_normalized'][0] = speed
            with self.assertRaises(preview.Refused) as ctx:
                self.make(binding)
            self.assertEqual(ctx.exception.code, 'out_of_range')
        binding = copy.deepcopy(self.binding)
        binding['speed_normalized'] = [.001, 1, .2, .3, .4, .5]
        adapter = self.make(binding)
        result = adapter.preview(preview.synthetic_record(adapter), 100)
        self.assertEqual([c['dq'] for c in result['command_preview']['cmds']], binding['speed_normalized'])

    def test_original_file_hash_is_enforced_before_loading(self):
        source = self.base / 'source'
        (source / 'teleop').mkdir(parents=True)
        for relative in ('teleop/retarget.py', 'settings.example.json'):
            shutil.copyfile(preview.DEFAULT_SOURCE / relative, source / relative)
        with (source / 'teleop/retarget.py').open('a') as f:
            f.write('\nraise AssertionError("modified source must never execute")\n')
        with self.assertRaises(preview.Refused) as ctx:
            preview.load_original(source)
        self.assertEqual(ctx.exception.code, 'upstream_hash_mismatch')


class CommandTests(unittest.TestCase):
    def test_help_does_not_need_numpy_or_any_sdk(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replay_requires_explicit_recording_and_binding(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--binding', 'unused', '--replay-clock'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('explicit recording', result.stderr)

    def test_demo_is_reproducible_and_never_imports_hardware_or_opens_network(self):
        code = '''
import runpy,sys
def guard(event,args):
    if event in ('socket.__new__', 'subprocess.Popen'):
        raise AssertionError('Unexpected network/process operation: '+event)
    if event == 'import' and args[0].split('.')[0] in ('unitree_sdk2py','cyclonedds','wuji_sdk','serial','bc_stark_sdk'):
        raise AssertionError('Unexpected hardware dependency: '+args[0])
    if event == 'open' and isinstance(args[0],str) and (args[0].startswith('/dev/tty') or args[0].startswith('/dev/serial')):
        raise AssertionError('Unexpected hardware device open')
sys.addaudithook(guard)
sys.argv=[sys.argv[1],'--demo']
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        outputs = []
        for _ in range(2):
            result = subprocess.run([sys.executable, '-c', code, str(SCRIPT)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])
        records = [json.loads(line) for line in outputs[0].splitlines()]
        self.assertEqual([r['status'] for r in records], ['preview'] * 3 + ['refused', 'preview'])
        self.assertTrue(all(r['synthetic'] and not r['hardware_output'] for r in records))


if __name__ == '__main__':
    unittest.main()
