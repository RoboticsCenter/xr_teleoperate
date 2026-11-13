import copy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import branch_arm_input as arms
from pico_tracker.input import read_frame


IDENTITY = [[1., 0., 0., 0.], [0., 1., 0., 0.],
            [0., 0., 1., 0.], [0., 0., 0., 1.]]
RZ90 = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]
ROTATION_IDENTITY = [row[:3] for row in IDENTITY[:3]]


class ArmInputTests(unittest.TestCase):
    def setUp(self):
        self.frame = {'session': 'pico-session-1', 'seq': 1,
            'head_pose': [0., 1.6, 0., 0., 0., 0., 1.],
            'trackers': {'LEFT': [-.2, 1., -.3, 0., 0., 0., 1.],
                         'RIGHT': [.2, 1., -.3, 0., 0., 0., 1.]},
            'pose_age_ms': {'LEFT': 10., 'RIGHT': 10.}}
        self.config = {'left_sn': 'LEFT', 'right_sn': 'RIGHT',
                       'offset_tracker_m': [.02, 0., 0.],
                       'right_offset_tracker_m': [0., .03, 0.]}
        self.q = [i / 100 for i in range(14)]
        self.poses = {'left': copy.deepcopy(IDENTITY), 'right': copy.deepcopy(IDENTITY)}
        self.poses['left'][1][3] = .2
        self.poses['right'][1][3] = -.2

    def capture(self, sides):
        return arms.capture(self.frame, self.q, self.poses, self.config, sides)

    def calibration(self, side, rotation=RZ90, scale=1.):
        saved = self.config.setdefault('arm_calibration', {'schema': arms.CALIBRATION_SCHEMA, 'sides': {}})
        saved['sides'][side] = {'serial': self.config[side+'_sn'],
            'rotation_from_heading': copy.deepcopy(rotation), 'translation_scale': scale}
        return saved['sides'][side]

    def test_left_only_uses_left_serial_and_fixes_right_joint_values(self):
        del self.frame['trackers']['RIGHT']
        captured = self.capture('left')
        self.assertEqual(captured.serials, {'left': 'LEFT'})
        self.assertIsNone(captured.fixed_left)
        self.assertEqual(captured.fixed_right, self.q[7:])
        result = captured.targets(self.frame, self.poses)
        self.assertEqual(result['right'], self.poses['right'])
        self.assertEqual(result['fixed_right'], self.q[7:])
        self.assertNotIn('fixed_left', result)

    def test_right_only_does_not_require_left_tracker(self):
        del self.frame['trackers']['LEFT']
        captured = self.capture('right')
        result = captured.targets(self.frame, self.poses)
        self.assertEqual(result['fixed_left'], self.q[:7])
        self.assertNotIn('fixed_right', result)

    def test_both_arms_have_independent_source_mappers_not_mirrored_targets(self):
        captured = self.capture('both')
        self.frame['trackers']['LEFT'][2] -= .04
        self.frame['trackers']['RIGHT'][0] += .07
        result = captured.targets(self.frame, self.poses)
        basis = arms.heading_basis(self.frame['head_pose'])
        for side, key in [('left', 'offset_tracker_m'), ('right', 'right_offset_tracker_m')]:
            expected = arms.IncrementalWrist(captured.initial_trackers[side],
                self.poses[side], basis, self.config[key]).target(self.frame['trackers'][self.config[side + '_sn']])
            self.assertEqual(result[side], expected)
        self.assertAlmostEqual(result['left'][0][3], .04)
        self.assertAlmostEqual(result['right'][0][3], 0.)
        self.assertAlmostEqual(result['right'][1][3], -.27)
        self.assertNotIn('fixed_left', result)
        self.assertNotIn('fixed_right', result)

    def test_stationary_both_wrists_capture_and_follow_without_pose_age_limit(self):
        self.frame['pose_age_ms'] = {'LEFT': 3_600_000., 'RIGHT': 3_600_000.}
        captured = self.capture('both')
        self.assertTrue(arms.live(self.frame, captured.serials, captured.session))
        targets = captured.targets(self.frame, self.poses)
        self.frame['seq'] += 1
        del self.frame['pose_age_ms']
        self.assertTrue(arms.live(self.frame, captured.serials, captured.session))
        self.assertEqual(captured.targets(self.frame, self.poses), targets)

    def test_same_input_session_is_still_required(self):
        captured = self.capture('both')
        self.frame['session'] = 'different-session'
        self.assertFalse(arms.live(self.frame, captured.serials, captured.session))
        with self.assertRaises(ValueError): captured.targets(self.frame, self.poses)

    def test_stationary_packets_still_expire_after_half_second(self):
        self.frame.update(sample_mono_on_robot=100., received_mono=100.)
        self.frame['pose_age_ms'] = {'LEFT': 3_600_000., 'RIGHT': 3_600_000.}
        serials = {'left': 'LEFT', 'right': 'RIGHT'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'latest.json'
            path.write_text(json.dumps(self.frame))
            with patch('pico_tracker.input.time.monotonic', return_value=100.5):
                self.assertTrue(arms.live(read_frame(path), serials))
            with patch('pico_tracker.input.time.monotonic', return_value=100.500001):
                self.assertFalse(arms.live(read_frame(path), serials))

    def test_selected_invalid_pose_blocks_targets(self):
        captured = self.capture('both')
        self.frame['trackers']['LEFT'][6] = 0.
        self.assertFalse(arms.live(self.frame, captured.serials, captured.session))
        with self.assertRaises(ValueError): captured.targets(self.frame, self.poses)

    def test_distinct_serial_required_for_both_arms(self):
        self.config['left_sn'] = self.config['right_sn']
        with self.assertRaisesRegex(ValueError, 'different tracker serial'):
            self.capture('both')

    def test_unassigned_left_tracker_is_not_filled_from_right(self):
        self.config['left_sn'] = ''
        with self.assertRaisesRegex(ValueError, 'every selected arm'):
            self.capture('left')

    def test_heading_basis_keeps_upstream_head_requirement(self):
        self.frame['head_pose'] = None
        with self.assertRaisesRegex(ValueError, 'Enable Head'):
            self.capture('left')

    def test_neutral_metadata_is_a_snapshot(self):
        captured = self.capture('left')
        saved = captured.metadata()
        self.q[7] = 999.
        self.frame['trackers']['LEFT'][0] = 999.
        self.poses['left'][0][3] = 999.
        self.assertEqual(captured.metadata(), saved)
        saved['fixed_right'][0] = 999.
        self.assertNotEqual(captured.metadata(), saved)

    def test_calibration_corrects_each_of_three_translation_axes(self):
        self.calibration('left')
        captured = self.capture('left')
        for axis, expected in ((0, [.1, 0., 0.]), (1, [0., 0., .1]), (2, [0., -.1, 0.])):
            with self.subTest(axis=axis):
                moved = copy.deepcopy(self.frame)
                moved['trackers']['LEFT'][axis] += .1
                result = captured.targets(moved, self.poses)['left']
                for i in range(3):
                    self.assertAlmostEqual(result[i][3] - self.poses['left'][i][3], expected[i])

    def test_calibrated_sides_have_independent_gains_about_robot_origin(self):
        self.poses['left'][0][3] = .4
        self.calibration('left', RZ90, 2.)
        self.calibration('right', ROTATION_IDENTITY, .5)
        captured = self.capture('both')
        self.frame['trackers']['LEFT'][0] += .06
        self.frame['trackers']['RIGHT'][0] += .08
        targets = captured.targets(self.frame, self.poses)
        self.assertAlmostEqual(targets['left'][0][3], .52)
        self.assertAlmostEqual(targets['left'][1][3], .2)
        self.assertAlmostEqual(targets['right'][0][3], 0.)
        self.assertAlmostEqual(targets['right'][1][3], -.24)

    def test_calibrated_neutral_preserves_both_robot_poses_and_rotations(self):
        self.calibration('left', RZ90, 3.)
        self.calibration('right', [[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]], .25)
        self.poses['left'][0][3] = .4
        for i in range(3):
            self.poses['right'][i][:3] = RZ90[i][:]
        self.assertEqual(self.capture('both').targets(self.frame, self.poses), self.poses)

    def test_calibrated_orientation_uses_relative_rotation_and_not_translation_gain(self):
        self.calibration('left', RZ90, 4.)
        for i in range(3):
            self.poses['left'][i][:3] = RZ90[i][:]
        captured = self.capture('left')
        self.frame['trackers']['LEFT'][3:] = [math.sqrt(.5), 0., 0., math.sqrt(.5)]
        result = captured.targets(self.frame, self.poses)['left']
        # Corrected PICO +X maps to robot +X. Relative Rx(90) pre-multiplies Rz(90).
        expected = [[0., -1., 0.], [0., 0., -1.], [1., 0., 0.]]
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(result[i][j], expected[i][j])

    def test_correction_is_composed_after_current_start_heading(self):
        self.calibration('left', RZ90)
        self.frame['head_pose'][3:] = [0., math.sqrt(.5), 0., math.sqrt(.5)]
        captured = self.capture('left')
        self.frame['trackers']['LEFT'][0] += .1
        result = captured.targets(self.frame, self.poses)['left']
        self.assertAlmostEqual(result[0][3], 0.)
        self.assertAlmostEqual(result[1][3], .1)
        self.assertAlmostEqual(result[2][3], 0.)

    def test_missing_side_calibration_keeps_exact_source_mapping(self):
        self.calibration('left', RZ90, 2.)
        captured = self.capture('both')
        self.frame['trackers']['RIGHT'][3:] = [0., 0., math.sqrt(.5), math.sqrt(.5)]
        self.frame['trackers']['RIGHT'][0] += .037
        expected = arms.IncrementalWrist(captured.initial_trackers['right'], self.poses['right'],
            captured.basis, self.config['right_offset_tracker_m']).target(self.frame['trackers']['RIGHT'])
        self.assertEqual(captured.targets(self.frame, self.poses)['right'], expected)
        self.assertFalse(captured.metadata()['arm_calibration']['sides']['right']['applied'])

    def test_uncalibrated_target_stays_exactly_equal_to_original_with_rotation_and_offset(self):
        captured = self.capture('both')
        self.frame['trackers']['LEFT'][3:] = [0., 0., math.sqrt(.5), math.sqrt(.5)]
        self.frame['trackers']['RIGHT'][3:] = [math.sqrt(.5), 0., 0., math.sqrt(.5)]
        self.frame['trackers']['LEFT'][2] -= .073
        self.frame['trackers']['RIGHT'][1] += .021
        result = captured.targets(self.frame, self.poses)
        for side, key in [('left', 'offset_tracker_m'), ('right', 'right_offset_tracker_m')]:
            expected = arms.IncrementalWrist(captured.initial_trackers[side], self.poses[side],
                captured.basis, self.config[key]).target(self.frame['trackers'][side.upper()])
            self.assertEqual(result[side], expected)

    def test_calibration_and_metadata_are_deep_snapshots(self):
        saved = self.calibration('left', RZ90, 2.)
        captured = self.capture('left')
        metadata = captured.metadata()
        self.assertTrue(metadata['arm_calibration']['sides']['left']['applied'])
        self.assertEqual(metadata['arm_calibration']['sides']['left']['rotation_from_heading'], RZ90)
        self.assertEqual(metadata['arm_calibration']['sides']['left']['basis'],
            [[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]])
        saved['rotation_from_heading'][0][0] = 999.
        saved['translation_scale'] = 999.
        self.config['offset_tracker_m'][0] = 999.
        self.assertEqual(captured.metadata(), metadata)
        self.frame['trackers']['LEFT'][0] += .1
        self.assertAlmostEqual(captured.targets(self.frame, self.poses)['left'][0][3], .2)
        metadata['arm_calibration']['sides']['left']['basis'][0][0] = 999.
        metadata['arm_calibration']['sides']['left']['rotation_from_heading'][0][0] = 999.
        self.assertNotEqual(captured.metadata(), metadata)

    def test_selected_stale_serial_or_missing_fields_rejected(self):
        record = self.calibration('left')
        for field in ('serial', 'rotation_from_heading', 'translation_scale'):
            with self.subTest(field=field):
                modified = dict(record)
                del modified[field]
                self.config['arm_calibration']['sides']['left'] = modified
                with self.assertRaises(ValueError): self.capture('left')
        record['serial'] = 'OTHER'
        self.config['arm_calibration']['sides']['left'] = record
        with self.assertRaisesRegex(ValueError, 'serial does not match'):
            self.capture('left')

    def test_unselected_stale_calibration_does_not_block_valid_selected_side(self):
        self.calibration('left')['serial'] = 'OLD-LEFT'
        self.calibration('right', ROTATION_IDENTITY, 1.)
        self.capture('right')
        with self.assertRaises(ValueError): self.capture('both')

    def test_malformed_calibration_container_rejected_instead_of_falling_back(self):
        for invalid in (None, [], {}, {'schema': 'wrong', 'sides': {}},
                        {'schema': arms.CALIBRATION_SCHEMA, 'sides': []}):
            with self.subTest(invalid=invalid):
                self.config['arm_calibration'] = invalid
                with self.assertRaisesRegex(ValueError, 'sides object'):
                    self.capture('left')
        self.config['arm_calibration'] = {'schema': arms.CALIBRATION_SCHEMA, 'sides': {'left': None}}
        with self.assertRaisesRegex(ValueError, 'side object'):
            self.capture('left')

    def test_invalid_rotation_rejects_reflection_shear_nonfinite_and_bool(self):
        for rotation in ([], [[1., 0.], [0., 1.]],
                [[-1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
                [[1., .01, 0.], [0., 1., 0.], [0., 0., 1.]],
                [[1., 0., 0.], [0., 1., 0.], [0., 0., float('nan')]],
                [[1., 0., 0.], [0., 1., 0.], [0., 0., float('inf')]],
                [[True, 0., 0.], [0., 1., 0.], [0., 0., 1.]]):
            with self.subTest(rotation=rotation):
                self.calibration('left', rotation)
                with self.assertRaises(ValueError): self.capture('left')

    def test_gain_bounds_are_shared_finite_and_inclusive(self):
        for gain in (0., -1., .099, 10.001, float('inf'), float('nan'), True, '1'):
            with self.subTest(gain=gain):
                self.calibration('left', scale=gain)
                with self.assertRaisesRegex(ValueError, 'scale must be between'):
                    self.capture('left')
        for gain in (arms.MIN_TRANSLATION_SCALE, arms.MAX_TRANSLATION_SCALE):
            self.calibration('left', scale=gain)
            self.assertEqual(self.capture('left').calibration['sides']['left']['translation_scale'], gain)

    def test_shared_validator_returns_only_independent_validated_fields(self):
        record = self.calibration('left')
        record['unrelated'] = 'ignored metadata'
        validated = arms.validate_side_calibration(record, 'LEFT')
        self.assertEqual(set(validated), {'serial', 'rotation_from_heading', 'translation_scale'})
        record['rotation_from_heading'][0][0] = 999.
        self.assertEqual(validated['rotation_from_heading'], RZ90)

    def test_translation_matrix_takes_precedence_without_changing_orientation(self):
        import numpy as np
        record=self.calibration('left', RZ90, 7.)
        correction=np.array([[-2.,.4,0.],[0.,3.,.5],[0.,0.,.25]])
        record['translation_correction']=correction.tolist()
        captured=self.capture('left')
        frame=copy.deepcopy(self.frame)
        frame['trackers']['LEFT'][:3]=[-.1,1.07,-.34]
        frame['trackers']['LEFT'][3:]=[math.sqrt(.5),0.,0.,math.sqrt(.5)]
        raw=captured.mappers['left'].target(frame['trackers']['LEFT'])
        result=captured.targets(frame,self.poses)['left']
        origin=np.asarray(self.poses['left'])[:3,3]
        np.testing.assert_allclose(np.asarray(result)[:3,3],origin+correction@(np.asarray(raw)[:3,3]-origin))
        np.testing.assert_allclose(np.asarray(result)[:3,:3],np.asarray(raw)[:3,:3])
        self.assertAlmostEqual(np.linalg.det(np.asarray(result)[:3,:3]),1.)
        record['translation_correction'][0][0]=999.
        self.assertEqual(captured.calibration['sides']['left']['translation_correction'][0][0],-2.)

    def test_translation_matrix_rejects_only_invalid_or_numerically_singular_data(self):
        for correction in (None,[],[[1.,0.],[0.,1.]],
                [[1.,0.,0.],[0.,1.,0.],[0.,0.,0.]],
                [[1.,0.,0.],[0.,1.,0.],[0.,0.,float('nan')]],
                [[True,0.,0.],[0.,1.,0.],[0.,0.,1.]]):
            with self.subTest(correction=correction):
                self.calibration('left')['translation_correction']=correction
                with self.assertRaises(ValueError):self.capture('left')
        for correction in ([[1e8,2e7,0.],[0.,-3e8,0.],[0.,0.,1e8]],
                           [[.0001,0.,0.],[0.,.0002,0.],[0.,0.,.0003]]):
            self.calibration('left')['translation_correction']=correction
            self.assertEqual(self.capture('left').calibration['sides']['left']['translation_correction'],correction)

    def test_operator_matrix_survives_save_reload_and_corrects_runtime_three_axes(self):
        import numpy as np
        from dashboard_arm_calibration import ArmCalibration, AXES
        matrices={'left':np.array([[.01,.02,.03],[0.,.1,.01],[0.,0.,-.4]]),
                  'right':np.array([[1e-8,2e-9,0.],[0.,1e-8,3e-9],[0.,0.,1e-8]])}
        frames={}
        with tempfile.TemporaryDirectory() as directory:
            manager=ArmCalibration(directory)
            manager.capture('origin',self.frame,self.config,'both')
            for axis,stage in enumerate(AXES):
                moved=copy.deepcopy(self.frame)
                for side in ('left','right'):
                    moved['trackers'][side.upper()][:3]=(np.asarray(self.frame['trackers'][side.upper()][:3])+
                                                         matrices[side][:,axis]).tolist()
                frames[stage]=moved
                manager.capture(stage,moved,self.config,'both',distance_cm=20.)
            manager.save(self.config,'both')
            self.config['arm_calibration']=ArmCalibration(directory).saved
            captured=self.capture('both')
            for axis,stage in enumerate(AXES):
                result=captured.targets(frames[stage],self.poses)
                for side in ('left','right'):
                    with self.subTest(stage=stage,side=side):
                        np.testing.assert_allclose(np.asarray(result[side])[:3,3]-np.asarray(self.poses[side])[:3,3],
                                                   np.eye(3)[:,axis]*.2,atol=1e-8)
                        np.testing.assert_allclose(np.asarray(result[side])[:3,:3],np.eye(3),atol=1e-12)

    def test_capture_binding_offset_and_serial_are_checked_and_copied(self):
        record = self.calibration('left')
        record['capture_binding'] = {'serial': 'LEFT', 'offset_tracker_m': [.02, 0., 0.],
            'session': 'calibration-input-session', 'origin_captured_at': 'example'}
        captured = self.capture('left')
        self.assertEqual(captured.metadata()['arm_calibration']['sides']['left']['capture_binding'],
                         record['capture_binding'])
        record['capture_binding']['offset_tracker_m'][0] = .03
        self.assertEqual(captured.metadata()['arm_calibration']['sides']['left']['capture_binding']['offset_tracker_m'],
                         [.02, 0., 0.])
        with self.assertRaisesRegex(ValueError, 'offset changed'): self.capture('left')
        for invalid in (None, [], {'serial': 'DIFFERENT', 'offset_tracker_m': [.02, 0., 0.]},
                        {'serial': 'LEFT', 'offset_tracker_m': ['.02', 0., 0.]},
                        {'serial': 'LEFT', 'offset_tracker_m': [float('nan'), 0., 0.]}):
            with self.subTest(binding=invalid):
                record['capture_binding'] = invalid
                with self.assertRaises(ValueError): self.capture('left')

    def test_simulation_calibration_is_not_used_for_robot_target_capture(self):
        self.calibration('left')
        self.config['arm_calibration']['simulation'] = True
        with self.assertRaisesRegex(ValueError, 'Simulated arm calibration'):
            self.capture('left')

    def test_guided_fit_consumed_by_capture_recovers_twenty_cm_xyz_for_each_arm(self):
        import numpy as np
        from dashboard_arm_calibration import ArmCalibration
        from pico_tracker.geometry import rotation

        def quaternion(axis, degrees):
            half = math.radians(degrees)/2
            values = [0., 0., 0., math.cos(half)]
            values[axis] = math.sin(half)
            return values

        def rotation_matrix(axis, degrees):
            return np.asarray(rotation([0., 0., 0.] + quaternion(axis, degrees)))

        self.frame['head_pose'][3:] = quaternion(1, 31.)
        self.frame['trackers']['LEFT'][3:] = quaternion(0, -24.)
        self.frame['trackers']['RIGHT'][3:] = quaternion(2, 19.)
        origin = copy.deepcopy(self.frame)
        transforms = {'left': rotation_matrix(2, 37.) @ rotation_matrix(0, -18.),
                      'right': rotation_matrix(1, -29.) @ rotation_matrix(2, 63.)}
        gains = {'left': 1.7, 'right': .65}
        offsets = {'left': self.config['offset_tracker_m'],
                   'right': self.config['right_offset_tracker_m']}
        initial_points = {side: np.asarray(origin['trackers'][side.upper()][:3]) +
            np.asarray(rotation(origin['trackers'][side.upper()])) @ np.asarray(offsets[side])
            for side in ('left', 'right')}
        movements = {}
        with tempfile.TemporaryDirectory() as directory:
            calibration = ArmCalibration(directory)
            calibration.capture('origin', origin, self.config, 'both')
            for axis, stage in enumerate(('forward', 'left', 'up')):
                moved = copy.deepcopy(origin)
                moved['seq'] += axis+1
                expected_delta = np.eye(3)[axis] * .2
                for side in ('left', 'right'):
                    pose = moved['trackers'][side.upper()]
                    pose[3:] = quaternion(axis, 11.+axis*17.)
                    point = initial_points[side] + transforms[side].T @ expected_delta / gains[side]
                    pose[:3] = (point - np.asarray(rotation(pose)) @ np.asarray(offsets[side])).tolist()
                movements[stage] = moved
                calibration.capture(stage, moved, self.config, 'both', distance_cm=20)
            state = calibration.status(self.config, 'both')
            self.assertTrue(state['can_save'])
            self.assertFalse((Path(directory)/'config/arm-calibration.json').exists())
            calibration.save(self.config, 'both')
            candidate = ArmCalibration(directory).saved
            self.assertEqual(candidate, json.loads((Path(directory)/'config/arm-calibration.json').read_text()))
            config = copy.deepcopy(self.config)
            config['arm_calibration'] = candidate
            captured = arms.capture(origin, self.q, self.poses, config, 'both')
            for side in ('left', 'right'):
                self.assertAlmostEqual(candidate['sides'][side]['translation_scale'], gains[side])
                np.testing.assert_allclose(captured.metadata()['arm_calibration']['sides'][side]['basis'],
                                           transforms[side], atol=1e-12)
            for axis, stage in enumerate(('forward', 'left', 'up')):
                result = captured.targets(movements[stage], self.poses)
                for side in ('left', 'right'):
                    with self.subTest(stage=stage, side=side):
                        delta = np.asarray(result[side])[:3,3] - np.asarray(self.poses[side])[:3,3]
                        np.testing.assert_allclose(delta, np.eye(3)[axis]*.2, atol=1e-12)
                        # Orientation is the same physical relative rotation in
                        # the fitted frame; the independently fitted gain is absent.
                        current_r = np.asarray(rotation(movements[stage]['trackers'][side.upper()]))
                        initial_r = np.asarray(rotation(origin['trackers'][side.upper()]))
                        expected_r = transforms[side] @ current_r @ initial_r.T @ transforms[side].T
                        np.testing.assert_allclose(np.asarray(result[side])[:3,:3], expected_r, atol=1e-12)

            # A PICO world yaw/translation reset before a new START rotates Head
            # and all tracker poses together. The persisted heading-relative
            # correction must still give the same robot targets in the new session.
            world_quaternion = quaternion(1, -53.)
            world_rotation = rotation_matrix(1, -53.)
            def world_frame(frame):
                value = copy.deepcopy(frame)
                value['session'] = 'new-input-session-after-world-reset'
                for pose in [value['head_pose'], *value['trackers'].values()]:
                    pose[:3] = (world_rotation @ np.asarray(pose[:3]) + np.array([1.3, .4, -.7])).tolist()
                    a, b = np.asarray(world_quaternion), np.asarray(pose[3:])
                    pose[3:] = [*(a[3]*b[:3] + b[3]*a[:3] + np.cross(a[:3], b[:3])),
                                a[3]*b[3] - a[:3]@b[:3]]
                return value
            reset_capture = arms.capture(world_frame(origin), self.q, self.poses, config, 'both')
            for stage in ('forward', 'left', 'up'):
                before = captured.targets(movements[stage], self.poses)
                after = reset_capture.targets(world_frame(movements[stage]), self.poses)
                for side in ('left', 'right'):
                    np.testing.assert_allclose(after[side], before[side], atol=1e-12)
            with self.assertRaisesRegex(ValueError, 'input unavailable'):
                captured.targets(world_frame(movements['forward']), self.poses)


class ReferenceBindingTests(unittest.TestCase):
    setUp = ArmInputTests.setUp
    calibration = ArmInputTests.calibration
    capture = ArmInputTests.capture
    def test_legacy_saved_calibration_is_rejected_in_headset_reference(self):
        from arm_reference_frame import reference_frame
        self.calibration('left')
        self.config['arm_reference_frame']='headset'
        mapped=reference_frame(self.frame,'headset')
        with self.assertRaisesRegex(ValueError,'reference frame'):
            arms.capture(mapped,self.q,self.poses,self.config,'left')

    def test_headset_saved_calibration_is_rejected_in_world_reference(self):
        self.calibration('left')
        self.config['arm_calibration']['arm_reference_frame']='headset'
        with self.assertRaisesRegex(ValueError,'reference frame'):
            self.capture('left')

    def test_side_reference_binding_cannot_be_hidden_by_top_level_mode(self):
        from arm_reference_frame import reference_frame, reference_mapping
        record=self.calibration('left')
        self.config['arm_reference_frame']='headset'
        self.config['arm_calibration']['arm_reference_frame']='headset'
        self.config['arm_calibration']['arm_reference_mapping']=reference_mapping('headset')
        record['capture_binding']={'serial':'LEFT','offset_tracker_m':[.02,0.,0.]}
        mapped=reference_frame(self.frame,'headset')
        with self.assertRaisesRegex(ValueError,'capture reference frame'):
            arms.capture(mapped,self.q,self.poses,self.config,'left')
        record['capture_binding']['arm_reference_frame']='headset'
        record['capture_binding']['arm_reference_mapping']=reference_mapping('headset')
        captured=arms.capture(mapped,self.q,self.poses,self.config,'left')
        self.assertEqual(captured.metadata()['arm_reference_frame'],'headset')
        with self.assertRaisesRegex(ValueError,'reference frame changed'):
            captured.targets(self.frame,self.poses)

    def test_headset_capture_requires_explicitly_transformed_input(self):
        self.config['arm_reference_frame']='headset'
        with self.assertRaisesRegex(ValueError,'Input arm reference frame'):
            self.capture('left')

    def test_full_rotation_headset_calibration_is_rejected_without_mutation(self):
        from arm_reference_frame import reference_frame
        self.calibration('left')
        self.config['arm_reference_frame']='headset'
        self.config['arm_calibration']['arm_reference_frame']='headset'
        before=copy.deepcopy(self.config)
        with self.assertRaisesRegex(ValueError,'reference mapping changed'):
            arms.capture(reference_frame(self.frame,'headset'),self.q,self.poses,self.config,'left')
        self.assertEqual(self.config,before)

    def test_old_transformed_headset_pose_cannot_be_reinterpreted_as_heading(self):
        from arm_reference_frame import reference_frame
        self.config['arm_reference_frame']='headset'
        mapped=reference_frame(self.frame,'headset')
        captured=arms.capture(mapped,self.q,self.poses,self.config,'left')
        mapped.pop('arm_reference_mapping')
        with self.assertRaisesRegex(ValueError,'reference mapping changed'):
            arms.capture(mapped,self.q,self.poses,self.config,'left')
        with self.assertRaisesRegex(ValueError,'reference mapping changed'):
            captured.targets(mapped,self.poses)


if __name__ == '__main__':
    unittest.main()
