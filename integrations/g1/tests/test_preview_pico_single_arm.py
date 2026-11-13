import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import preview_pico_single_arm as preview
try:
    import numpy as np
    import pinocchio.casadi
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'requires the isolated arm environment')
class SingleArmTests(unittest.TestCase):
    def test_translation_clamp_preserves_reference_rotation(self):
        reference = np.eye(4)
        target, metadata = preview.single_target(reference, [0, 0, 0], [0, 0, -1], 1., .01)
        self.assertTrue(metadata['was_clipped'])
        np.testing.assert_allclose(target[:3, 3], [.01, 0, 0])
        np.testing.assert_array_equal(target[:3, :3], reference[:3, :3])
        np.testing.assert_array_equal(reference, np.eye(4))

    def test_invalid_scale_or_bound_is_not_silently_clamped(self):
        for scale, bound in [(float('nan'), .01), (-1, .01), (1, .03), (0, .01)]:
            with self.assertRaises(ValueError):
                preview.single_target(np.eye(4), [0]*3, [0]*3, scale, bound)

    def test_cached_getters_and_bad_len_cannot_supply_recorded_input(self):
        sample = {'type': 'sample', 'source': 'cached_getters', 'issues': [],
                  'motion_declared_count': 1, 'trackers': [{'serial_number': 'R'}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.jsonl'
            path.write_text('\n'.join([json.dumps(sample)]*2))
            with self.assertRaises(ValueError):
                preview.selected_packets(path, 'R')
            sample['source'] = 'native_json_callback'
            sample['motion_declared_count'] = 0
            path.write_text('\n'.join([json.dumps(sample)]*2))
            with self.assertRaises(ValueError):
                preview.selected_packets(path, 'R')

    def test_stale_robot_recording_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps({'streams': {'rt/lowstate': {'status': 'stale', 'invalid_count': 0}}}))
            with self.assertRaises(ValueError):
                preview.recorded_arm_state(path)

    def test_real_recording_small_trial_is_bounded_and_left_arm_fixed(self):
        recording = ROOT / 'diagnostics/pico-right-object-2026-11-11.jsonl'
        state = ROOT / 'diagnostics/pc2-feedback-service-verified-2026-11-11.json'
        if not recording.exists() or not state.exists():
            self.skipTest('this session recordings unavailable')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'preview.json'
            with contextlib.redirect_stdout(io.StringIO()):
                rc = preview.main(['--pico-jsonl', str(recording), '--robot-state', str(state),
                                   '--side', 'right', '--tracker-sn', 'PC2310MLL9241957G',
                                   '--position-scale', '.02', '--output', str(output)])
            self.assertEqual(rc, 0)
            report = json.loads(output.read_text())
            self.assertEqual(report['joint_delta_rad'][:7], [0.]*7)
            self.assertLessEqual(max(map(abs, report['joint_delta_rad'])), .030001)
            self.assertFalse(report['motion_ready'])
            self.assertEqual(report['commands_published'], 0)


if __name__ == '__main__':
    unittest.main()
