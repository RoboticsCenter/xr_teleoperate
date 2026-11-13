"""Real original mapping/filter with synthetic inputs and owned fake readers."""
import io
import json
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import dashboard_gloves as glove


def until(predicate, timeout=2):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('condition did not become true')


def frame(side, seq=1, values=None, now=None):
    return dict(hand_side=side, sn=glove.SERIALS[side], sdk_user=glove.SDK_USER,
                source_t=time.monotonic() if now is None else now, seq=seq, timestamp_us=seq*1000,
                joint_angles=values or dict(thumb=0., index=0., middle=0., ring=0., little=0.))


class GloveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copyfile(ROOT/'sources.lock.json', self.root/'sources.lock.json')
        self.sdk = self.root/'sdk'
        (self.sdk/'users/person/models').mkdir(parents=True)
        (self.sdk/'users/registry.toml').write_text('current_user_id = "person"\n[[users]]\nuser_id = "person"\ndisplay_name = "brainco-teleop"\nis_default = false\n')
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="synthetic-test"/>')
        self.manager = glove.DashboardGloves(self.root, sdk_root=self.sdk)
        self.addCleanup(self.manager.close)

    def enable_fake(self, side):
        hand = self.manager.hands[side]
        hand.running = True
        self.assertTrue(hand.accept(frame(side)))
        return hand

    def left_artifacts(self):
        (self.sdk/'users/person/models/left_hand.urdf').write_text('<robot name="synthetic-left-test"/>')
        (self.root/'config').mkdir(exist_ok=True)
        calibration = dict(channels=list(self.manager.retarget.CHANNELS), side='left',
                           open=[0., None, 0., 0., 0., 0.], closed=[100., None, 100., 100., 100., 100.])
        (self.root/'config/hand-calibration-left.json').write_text(json.dumps(calibration))
        self.manager.refresh()

    def capture_pose(self, hand, pose, value, seq=2):
        self.manager.capture(hand.side, pose, .03)
        self.assertTrue(hand.accept(frame(hand.side, seq,
            {key: value for key in ('thumb', 'index', 'middle', 'ring', 'little')})))
        until(lambda: hand.capture_state is None)

    def test_construction_only_reads_metadata_no_reader_or_sdk(self):
        with patch.object(glove.subprocess, 'Popen') as popen:
            manager = glove.DashboardGloves(self.root, sdk_root=self.sdk)
            manager.close()
        popen.assert_not_called()
        self.assertFalse(self.manager.status()['right']['running'])
        self.assertTrue(self.manager.status()['right']['mapping_ready'])

    def test_missing_left_does_not_copy_right_model_or_calibration(self):
        self.enable_fake('left')
        self.assertFalse(self.manager.status()['left']['mapping_ready'])
        self.assertIsNone(self.manager.target('left', [500.]*6))
        self.assertFalse((self.sdk/'users/person/models/left_hand.urdf').exists())
        self.assertFalse((self.root/'config/hand-calibration-left.json').exists())

    def test_feedback_required_step_bound_uses_measured_not_previous_command(self):
        hand = self.enable_fake('right')
        raw = dict(zip(('thumb', 'index', 'middle', 'ring', 'little'),
                       [hand.calibration['open'][i] for i in (0,2,3,4,5)]))
        hand.accept(frame('right', seq=2, values=raw))
        self.assertIsNone(self.manager.target('right', None))
        for _ in range(2):
            result = self.manager.target('right', [500.]*6)
            self.assertEqual(result['targets'], [420]*6)
            self.assertEqual(result['q_normalized'], [.42]*6)
            self.assertFalse(result['published'])
        self.assertIsNone(self.manager.target('right', [float('nan')]*6))
        self.assertIsNone(self.manager.target('right', [1001.]*6))

    def test_stale_side_holds_and_recovers_without_latching_other_side(self):
        self.left_artifacts()
        left, right = self.enable_fake('left'), self.enable_fake('right')
        left.frame['source_t'] -= .6
        self.assertIsNone(self.manager.target('left', [500.]*6))
        self.assertIsNotNone(self.manager.target('right', [500.]*6))
        self.assertTrue(left.accept(frame('left', seq=2)))
        self.assertIsNotNone(self.manager.target('left', [500.]*6))

    def test_dds_float32_feedback_restores_integer_register_before_original_limiter(self):
        import struct
        hand = self.enable_fake('right')
        raw = dict(zip(('thumb', 'index', 'middle', 'ring', 'little'),
                       [hand.calibration['open'][i] for i in (0, 2, 3, 4, 5)]))
        hand.accept(frame('right', seq=2, values=raw))
        normalized = struct.unpack('f', struct.pack('f', .617))[0]
        self.assertLess(normalized * 1000, 617)
        result = self.manager.target('right', [normalized * 1000] * 6)
        self.assertEqual(result['measured'], [617] * 6)
        self.assertEqual(result['targets'], [537] * 6)
        self.assertTrue(all(abs(q - normalized) <= .08 + 1e-7 for q in result['q_normalized']))

    def test_invalid_identity_and_duplicates_do_not_refresh_age(self):
        hand = self.enable_fake('right')
        original = hand.frame['source_t']
        self.assertFalse(hand.accept(frame('left', seq=2)))
        self.assertFalse(hand.accept(frame('right')))
        self.assertEqual(hand.frame['source_t'], original)
        self.assertFalse(hand.accept(frame('right', seq=2, now=time.monotonic()-1.)))

    def test_original_confidence_filter_is_unchanged(self):
        module = types.ModuleType('original_bridge_test')
        exec(compile((glove.SOURCE/'wuji_bridge.py').read_bytes(), '<original>', 'exec'), module.__dict__)
        source = types.SimpleNamespace(fingers=[types.SimpleNamespace(angles=[.1]*5, confidence=.5) for _ in range(5)])
        self.assertIsNotNone(module.joint_readings(source))
        source.fingers[2].confidence = .49
        self.assertIsNone(module.joint_readings(source))

    def test_wrong_side_or_zero_span_calibration_rejected(self):
        value = dict(self.manager.hands['right'].calibration)
        with self.assertRaises(ValueError): glove.valid_calibration(value, 'left', self.manager.retarget)
        value['closed'] = list(value['open'])
        with self.assertRaises(ValueError): glove.valid_calibration(value, 'right', self.manager.retarget)

    def test_open_fist_capture_uses_original_medians_and_project_file(self):
        (self.sdk/'users/person/models/left_hand.urdf').write_text('<robot name="test"/>')
        self.manager.refresh()
        hand = self.enable_fake('left')
        self.manager.capture('left', 'open', .03)
        self.assertIsNone(self.manager.target('left', [500.]*6))
        for seq, amount in [(2, 0.), (3, 1.), (4, 2.)]:
            hand.accept(frame('left', seq, {key: amount for key in ('thumb','index','middle','ring','little')}))
        until(lambda: hand.capture_state is None)
        self.manager.capture('left', 'fist', .03)
        for seq, amount in [(5, 99.), (6, 100.), (7, 101.)]:
            hand.accept(frame('left', seq, {key: amount for key in ('thumb','index','middle','ring','little')}))
        until(lambda: hand.capture_state is None)
        saved = json.loads((self.root/'config/hand-calibration-left.json').read_text())
        self.assertEqual(saved['side'], 'left')
        self.assertEqual(saved['open'], [1.,None,1.,1.,1.,1.])
        self.assertEqual(saved['closed'], [100.,None,100.,100.,100.,100.])
        self.assertEqual(saved['sdk_model_sha256'], glove.digest(self.sdk/'users/person/models/left_hand.urdf'))
        self.assertEqual(saved['sdk_user_id'], 'person')
        self.assertEqual(saved['sn'], glove.SERIALS['left'])
        self.assertEqual(saved['capture_session'], hand.input_session)
        self.assertEqual(saved['sample_counts'], {'open': 3, 'fist': 3})
        self.assertEqual(hand.status()['capture_result']['state'], 'saved')
        self.assertTrue(hand.status()['calibration_binding_verified'])
        self.assertTrue(hand.status()['mapping_ready'])

    def test_capture_never_invents_missing_personal_model(self):
        self.enable_fake('left')
        with self.assertRaises(ValueError): self.manager.capture('left', 'open', .03)

    def test_capture_no_samples_reports_error_preserves_existing_calibration(self):
        hand = self.enable_fake('right')
        self.manager.capture('right', 'open', .02)
        until(lambda: hand.capture_state is None)
        self.assertIn('No valid', hand.capture_error)
        self.assertFalse((self.root/'config/hand-calibration-right.json').exists())
        self.assertEqual(hand.calibration_path, str(glove.SOURCE/'calibration.json'))

    def test_cancel_capture_preserves_existing_file_and_other_side(self):
        self.left_artifacts()
        hand = self.enable_fake('left')
        right = self.enable_fake('right')
        path = self.root/'config/hand-calibration-left.json'
        before = path.read_bytes()
        self.manager.capture('left', 'open', .1)
        hand.accept(frame('left', 2))
        state = self.manager.cancel_capture('left')
        self.assertIsNone(state['capture'])
        self.assertFalse(state['capture_cancel_ready'])
        self.assertEqual(state['capture_result']['state'], 'cancelled')
        hand.capture_thread.join(timeout=1)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(right.status()['input_live'])
        self.assertEqual(hand.samples, {})

    def test_refresh_detects_new_model_but_old_reader_cannot_supply_it(self):
        hand = self.enable_fake('left')
        hand.reader_binding = hand._binding()
        (self.sdk/'users/person/models/left_hand.urdf').write_text('<robot name="new-model"/>')
        state = self.manager.refresh('left')
        self.assertTrue(state['model_ready'])
        self.assertFalse(state['reader_model_current'])
        self.assertFalse(state['input_live'])
        self.assertFalse(hand.accept(frame('left', 2)))
        with self.assertRaisesRegex(ValueError, 'restart'):
            self.manager.start_input('left')

    def test_model_change_during_capture_discards_samples_without_saving(self):
        hand = self.enable_fake('right')
        self.manager.capture('right', 'open', .04)
        hand.accept(frame('right', 2))
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="changed"/>')
        until(lambda: hand.capture_state is None)
        self.assertEqual(hand.status()['capture_result']['state'], 'error')
        self.assertIn('model changed', hand.capture_error)
        self.assertEqual(hand.samples, {})
        self.assertFalse((self.root/'config/hand-calibration-right.json').exists())
        self.assertFalse(hand.status()['calibration_ready'])

    def test_changed_model_between_poses_requires_new_open_capture(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.assertIn('open', hand.samples)
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="changed-between"/>')
        self.manager.refresh('right')
        hand.accept(frame('right', 3))
        with self.assertRaisesRegex(ValueError, 'open hand first'):
            self.manager.capture('right', 'fist', .03)
        self.assertFalse((self.root/'config/hand-calibration-right.json').exists())

    def test_different_input_session_cannot_mix_open_and_fist(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        hand.input_session = 'a-new-reader-session'
        with self.assertRaisesRegex(ValueError, 'session changed'):
            self.manager.capture('right', 'fist', .03)
        self.assertEqual(hand.samples, {})

    def test_stop_clears_pending_pair_and_joins_capture(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.manager.capture('right', 'fist', .2)
        self.manager.stop_input('right')
        self.assertFalse(hand.capture_thread.is_alive())
        self.assertEqual(hand.samples, {})
        self.assertIsNone(hand.capture_state)
        self.assertEqual(hand.capture_result['state'], 'cancelled')

    def test_calibration_identity_checks_sn_user_and_model(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', 100., 3)
        path = self.root/'config/hand-calibration-right.json'
        original = json.loads(path.read_text())
        for key, value in [('sn', glove.SERIALS['left']), ('sdk_user', 'another'),
                           ('sdk_user_id', 'different-user'), ('sdk_model_sha256', 'new-model')]:
            with self.subTest(key=key):
                path.write_text(json.dumps({**original, key: value}))
                result = self.manager.refresh('right')
                self.assertFalse(result['mapping_ready'])
                self.assertIn(key, result['calibration_error'])

    def test_original_nonempty_rule_and_minimum_span_remain(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', self.manager.retarget.MIN_SPAN/2, 3)
        self.assertEqual(hand.capture_result['state'], 'error')
        self.assertIn('reads the same', hand.capture_error)
        self.assertFalse((self.root/'config/hand-calibration-right.json').exists())
        self.capture_pose(hand, 'fist', self.manager.retarget.MIN_SPAN*2, 4)
        self.assertEqual(hand.capture_result['state'], 'saved')
        self.assertEqual(hand.calibration['sample_counts'], {'open': 1, 'fist': 1})

    def test_existing_project_calibration_is_backed_up_atomically(self):
        hand = self.enable_fake('right')
        path = self.root/'config/hand-calibration-right.json'
        path.parent.mkdir(exist_ok=True)
        old = json.dumps(hand.calibration).encode()
        path.write_bytes(old)
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', 100., 3)
        backup = Path(hand.capture_result['backup_path'])
        self.assertEqual(backup.read_bytes(), old)
        self.assertNotEqual(path.read_bytes(), old)
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_failed_atomic_replace_keeps_previous_calibration_and_backup(self):
        hand = self.enable_fake('right')
        path = self.root/'config/hand-calibration-right.json'
        path.parent.mkdir(exist_ok=True)
        old = json.dumps(hand.calibration).encode()
        path.write_bytes(old)
        self.capture_pose(hand, 'open', 0.)
        with patch.object(Path, 'replace', side_effect=OSError('synthetic replace failure')):
            self.capture_pose(hand, 'fist', 100., 3)
        self.assertEqual(hand.capture_result['state'], 'error')
        self.assertEqual(path.read_bytes(), old)
        backups = list(path.parent.glob('hand-calibration-right-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old)
        self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_preview_is_original_static_mapping_and_does_not_advance_filter(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', 100., 3)
        hand.accept(frame('right', 4, {key: 50. for key in ('thumb','index','middle','ring','little')}))
        preview = hand.status()['preview']
        self.assertTrue(preview['ready'])
        self.assertEqual(preview['closure'], [.5]*6)
        self.assertEqual(preview['goals'], self.manager.retarget.targets([.5]*6,
            self.manager.settings['target_min'], self.manager.settings['target_max']))
        self.assertIsNone(hand.smooth.value)
        self.assertFalse(preview['published'])
        self.assertFalse(preview['filtered'])
        hand.frame['source_t'] -= .6
        self.assertFalse(hand.status()['preview']['ready'])
        self.assertIsNone(hand.status()['preview']['goals'])

    def test_right_model_invalidation_survives_restart_and_blocks_original_fallback(self):
        hand = self.manager.hands['right']
        original_hash = glove.digest(glove.SOURCE/'calibration.json')
        self.assertTrue(hand.status()['mapping_ready'])
        self.assertFalse(hand.status()['calibration_binding_verified'])
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="recalibrated"/>')
        state = self.manager.invalidate_calibration('right')
        self.assertFalse(state['calibration_ready'])
        self.assertTrue(state['calibration_invalidated'])
        marker = json.loads(hand.invalidation_path.read_text())
        self.assertEqual(marker['sdk_model_sha256'], glove.digest(self.sdk/'users/person/models/right_hand.urdf'))
        self.assertEqual(marker['previous_calibration_sha256'], original_hash)
        restarted = glove.DashboardGloves(self.root, sdk_root=self.sdk)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.status()['right']['mapping_ready'])
        self.assertTrue(restarted.status()['right']['calibration_invalidated'])
        self.assertIn('capture open and fist', restarted.status()['right']['calibration_error'])
        self.assertEqual(glove.digest(glove.SOURCE/'calibration.json'), original_hash)

    def test_new_pose_pair_acknowledges_invalidation_in_one_atomic_save(self):
        self.manager.invalidate_calibration('right')
        hand = self.enable_fake('right')
        marker_id = hand.invalidation['id']
        self.capture_pose(hand, 'open', 0.)
        self.assertFalse(hand.status()['mapping_ready'])
        self.capture_pose(hand, 'fist', 100., 3)
        self.assertEqual(hand.capture_result['state'], 'saved')
        self.assertEqual(hand.calibration['supersedes_invalidation'], marker_id)
        self.assertTrue(hand.invalidation_path.exists())
        self.assertFalse(hand.status()['calibration_invalidated'])
        restarted = glove.DashboardGloves(self.root, sdk_root=self.sdk)
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.status()['right']['mapping_ready'])
        self.assertFalse(restarted.status()['right']['calibration_invalidated'])

    def test_repeated_invalidation_backs_up_project_and_revokes_old_acknowledgement(self):
        self.manager.invalidate_calibration('right')
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', 100., 3)
        path = self.root/'config/hand-calibration-right.json'
        before = path.read_bytes()
        old_marker = hand.invalidation_path.read_bytes()
        state = self.manager.invalidate_calibration('right')
        self.assertFalse(state['mapping_ready'])
        marker = hand.invalidation
        self.assertNotEqual(marker['id'], json.loads(old_marker)['id'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(Path(marker['backup_path']).read_bytes(), before)
        self.assertEqual(Path(marker['previous_marker_backup_path']).read_bytes(), old_marker)
        restarted = glove.DashboardGloves(self.root, sdk_root=self.sdk)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.status()['right']['mapping_ready'])

    def test_failed_invalidation_does_not_leave_mapping_ready_or_refresh_it_back(self):
        hand = self.enable_fake('right')
        self.assertTrue(hand.status()['mapping_ready'])
        with patch.object(Path, 'replace', side_effect=OSError('marker cannot be saved')):
            with self.assertRaisesRegex(RuntimeError, 'Could not persist'):
                self.manager.invalidate_calibration('right')
        self.assertFalse(hand.status()['mapping_ready'])
        self.assertFalse(hand.refresh()['mapping_ready'])
        self.assertIsNone(self.manager.target('right', [500]*6))
        with self.assertRaisesRegex(ValueError, 'Could not persist'):
            self.manager.capture('right', 'open', .03)
        # Explicit retry can persist the barrier; it does not revive old mapping.
        self.assertTrue(self.manager.invalidate_calibration('right')['calibration_invalidated'])
        self.assertFalse(hand.status()['mapping_ready'])
        self.assertEqual(hand.invalidation_fault, '')

    def test_invalidation_cancels_pending_pair_and_malformed_marker_fails_closed(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.manager.capture('right', 'fist', .2)
        self.manager.invalidate_calibration('right')
        self.assertIsNone(hand.capture_state)
        self.assertEqual(hand.samples, {})
        self.assertFalse(hand.status()['mapping_ready'])
        hand.invalidation_path.write_text('{broken json')
        restarted = glove.DashboardGloves(self.root, sdk_root=self.sdk)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.status()['right']['mapping_ready'])
        self.assertTrue(restarted.status()['right']['calibration_invalidated'])
        self.assertIn('invalidation marker', restarted.status()['right']['calibration_error'])

    def test_changed_right_model_invalidated_fallback_open_refresh_fist(self):
        """Model B + actual unbound source fallback + matching reader binding."""
        hand = self.manager.hands['right']
        self.assertEqual(hand.status()['calibration_origin'], 'original_repository')
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="right-model-B"/>')
        self.manager.refresh()
        self.manager.invalidate_calibration('right')
        hand = self.enable_fake('right')
        hand.reader_binding = hand._binding()
        self.assertFalse(hand.status()['calibration_ready'])
        self.assertTrue(hand.status()['calibration_pending'])
        self.capture_pose(hand, 'open', 0.)
        context = hand.capture_context
        for _ in range(4):
            state = self.manager.refresh('right')
            self.manager.start_input('right')  # Already-running reader; no process.
            self.assertEqual(state['captured_poses'], {'open': 1})
            self.assertEqual(state['capture_result']['state'], 'open_captured')
            self.assertFalse(state['mapping_ready'])
            self.assertTrue(state['calibration_pending'])
            self.assertEqual(hand.capture_context, context)
        self.capture_pose(hand, 'fist', 100., 3)
        self.assertTrue(hand.status()['mapping_ready'])
        self.assertFalse(hand.status()['calibration_pending'])
        self.assertEqual(hand.capture_result['state'], 'saved')
        self.assertEqual(hand.calibration['sdk_model_sha256'], glove.digest(self.sdk/'users/person/models/right_hand.urdf'))
        self.assertEqual(hand.calibration['supersedes_invalidation'], hand.invalidation['id'])

    def test_completed_open_result_changes_when_input_stops(self):
        self.manager.invalidate_calibration('right')
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.manager.stop_input('right')
        state = hand.status()
        self.assertEqual(state['captured_poses'], {})
        self.assertEqual(state['capture_result']['state'], 'cancelled')
        self.assertTrue(state['capture_result']['discarded'])
        self.assertIn('input stopped', state['capture_result']['message'])
        self.assertFalse(state['calibration_ready'])

    def test_completed_open_result_changes_when_original_reader_exits(self):
        class Ended:
            def __init__(self, *args, **kwargs):
                self.stdout, self.stderr = io.StringIO(''), io.StringIO('No fresh valid glove data\n')
            def poll(self): return 1
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        with patch.object(self.manager, 'popen', side_effect=Ended) as popen:
            hand._run(sys.executable)
        self.assertEqual(popen.call_count, 1)
        state = hand.status()
        self.assertEqual(state['captured_poses'], {})
        self.assertEqual(state['capture_result']['state'], 'error')
        self.assertIn('input ended', state['capture_error'])
        self.assertIn('capture open again', state['capture_result']['message'])

    def test_completed_open_result_changes_when_reader_session_restarts(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        hand.running = False
        self.manager.python = sys.executable
        with patch.object(hand, '_run', return_value=None):
            self.manager.start_input('right')
            hand.thread.join(timeout=1)
        state = hand.status()
        self.assertEqual(state['captured_poses'], {})
        self.assertEqual(state['capture_result']['state'], 'error')
        self.assertIn('input restarted', state['capture_error'])

    def test_completed_open_result_changes_when_model_changes(self):
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        (self.sdk/'users/person/models/right_hand.urdf').write_text('<robot name="changed-after-open"/>')
        state = self.manager.refresh('right')
        self.assertEqual(state['captured_poses'], {})
        self.assertEqual(state['capture_result']['state'], 'error')
        self.assertIn('model changed', state['capture_error'])

    def test_saved_calibration_result_survives_stop_but_not_model_invalidation(self):
        self.manager.invalidate_calibration('right')
        hand = self.enable_fake('right')
        self.capture_pose(hand, 'open', 0.)
        self.capture_pose(hand, 'fist', 100., 3)
        saved = dict(hand.capture_result)
        self.manager.stop_input('right')
        state = hand.refresh()
        self.assertEqual(state['captured_poses'], {})
        self.assertTrue(state['calibration_ready'])
        self.assertEqual(state['capture_result'], saved)
        state = self.manager.invalidate_calibration('right')
        self.assertFalse(state['calibration_ready'])
        self.assertEqual(state['capture_result']['state'], 'error')
        self.assertIn('recalibrated', state['capture_result']['message'])

    def test_calibration_pending_is_only_missing_or_expected_invalidated_mapping(self):
        self.assertFalse(self.manager.status()['left']['calibration_pending'])  # model absent
        (self.sdk/'users/person/models/left_hand.urdf').write_text('<robot name="left"/>')
        self.assertTrue(self.manager.refresh('left')['calibration_pending'])
        self.left_artifacts()
        hand = self.manager.hands['left']
        path = self.root/'config/hand-calibration-left.json'
        good = json.loads(path.read_text())
        self.assertFalse(hand.status()['calibration_pending'])
        for bad in ('{broken', json.dumps({**good, 'closed': good['open']}),
                    json.dumps({**good, 'sn': 'wrong-glove'})):
            path.write_text(bad)
            state = self.manager.refresh('left')
            self.assertFalse(state['calibration_pending'])
            self.assertFalse(state['calibration_ready'])
        path.write_text(json.dumps(good))
        self.assertTrue(self.manager.invalidate_calibration('left')['calibration_pending'])
        hand.invalidation_path.write_text('{broken marker')
        state = self.manager.refresh('left')
        self.assertFalse(state['calibration_pending'])
        self.assertFalse(state['calibration_ready'])

    def test_reader_lifecycle_is_independent_and_arguments_preserved(self):
        processes = []
        class Lines:
            def __init__(self, first):
                self.queue = queue.Queue(); self.queue.put(first)
            def __iter__(self):
                while True:
                    value = self.queue.get()
                    if value is None: return
                    yield value
            def close(self): pass
        class Process:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                side = argv[argv.index('--side')+1]
                self.stdout = Lines(json.dumps(frame(side))+'\n')
                self.stderr = io.StringIO('')
                self.code = None
                processes.append(self)
            def poll(self): return self.code
            def send_signal(self, sig):
                self.code = 0; self.stdout.queue.put(None)
            def wait(self, timeout=None):
                if self.code is None: raise subprocess.TimeoutExpired('fake', timeout)
                return self.code
            def kill(self): self.send_signal(signal.SIGKILL)
        self.manager.python, self.manager.popen = sys.executable, Process
        self.manager.start_input('left'); self.manager.start_input('right')
        until(lambda: all(v['connected'] for v in self.manager.status().values()))
        self.manager.stop_input('left')
        self.assertFalse(self.manager.status()['left']['running'])
        self.assertTrue(self.manager.status()['right']['running'])
        self.manager.stop_input('right')
        for process in processes:
            self.assertEqual(process.poll(), 0)
            self.assertIn(str(glove.SOURCE/'wuji_bridge.py'), process.argv)
            self.assertNotIn('--min-confidence', process.argv)
            self.assertNotIn('--count', process.argv)
            self.assertNotIn('--timeout-s', process.argv)

    def test_reader_exit_never_automatically_restarts(self):
        class Ended:
            def __init__(self, *args, **kwargs):
                self.stdout, self.stderr = io.StringIO(''), io.StringIO('No fresh valid glove data\n')
            def poll(self): return 1
        with patch.object(self.manager, 'popen', side_effect=Ended) as popen:
            self.manager.python = sys.executable
            self.manager.start_input('right')
            until(lambda: not self.manager.status()['right']['running'])
            self.assertEqual(popen.call_count, 1)
            self.assertIn('Check inputs', self.manager.status()['right']['error'])
            self.manager.start_input('right')
            until(lambda: not self.manager.status()['right']['running'])
            self.assertEqual(popen.call_count, 2)


if __name__ == '__main__': unittest.main()
