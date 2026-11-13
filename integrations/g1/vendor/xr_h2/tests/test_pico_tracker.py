"""Tracker coordinates, freshness, and hybrid ownership without robot hardware."""
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from pico_tracker.geometry import (IncrementalWrist, heading_basis, PICO_TO_ROBOT,
                                   pose_valid, matmul, transpose)
from pico_tracker.input import TrackerInput, read_frame
from quest_h2_teleop import command
from test_hybrid_hands import controller

IDENTITY = [[1., 0., 0., .3], [0., 1., 0., .2], [0., 0., 1., .1], [0., 0., 0., 1.]]
POSE = [0., 1., 0., 0., 0., 0., 1.]


class Coordinates(unittest.TestCase):
    def assertMatrix(self, actual, expected):
        for a, b in zip(actual, expected):
            for x, y in zip(a, b): self.assertAlmostEqual(x, y)

    def test_zero_increment_keeps_measured_robot_pose_and_mounting_rotation(self):
        start = [1., 2., 3., .5, .5, .5, .5]
        mapper = IncrementalWrist(start, IDENTITY, PICO_TO_ROBOT)
        self.assertMatrix(mapper.target(start), IDENTITY)
        self.assertMatrix(mapper.target(start[:3]+[-x for x in start[3:]]), IDENTITY)

    def test_forward_left_and_up_use_correct_robot_axes(self):
        mapper = IncrementalWrist(POSE, IDENTITY, PICO_TO_ROBOT)
        for translation, axis in (([0, 0, -.1], 0), ([-.1, 0, 0], 1), ([0, .1, 0], 2)):
            target = mapper.target([POSE[i]+translation[i] for i in range(3)]+POSE[3:])
            for i in range(3): self.assertAlmostEqual(target[i][3]-IDENTITY[i][3], .1 if i == axis else 0.)

    def test_rotation_maps_openxr_yaw_into_robot_z_rotation(self):
        mapper = IncrementalWrist(POSE, IDENTITY, PICO_TO_ROBOT)
        s = math.sqrt(.5)
        target = mapper.target(POSE[:3]+[0., s, 0., s])
        self.assertMatrix([row[:3] for row in target[:3]], [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])

    def test_heading_basis_adapts_to_headset_yaw_without_mirroring(self):
        self.assertMatrix(heading_basis(POSE), PICO_TO_ROBOT)
        s = math.sqrt(.5)
        basis = heading_basis(POSE[:3]+[0., s, 0., s])
        self.assertMatrix(basis, [[-1., 0., 0.], [0., 0., 1.], [0., 1., 0.]])
        self.assertMatrix(matmul(basis, transpose(basis)), [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])
        with self.assertRaises(ValueError): heading_basis([0.]*7)

    def test_measured_tracker_offset_removes_position_orbit_at_stationary_wrist(self):
        start = [.1, 0., 0., 0., 0., 0., 1.]
        mapper = IncrementalWrist(start, IDENTITY, PICO_TO_ROBOT, [-.1, 0., 0.])
        s = math.sqrt(.5)
        rotated = [0., .1, 0., 0., 0., s, s]
        target = mapper.target(rotated)
        for i in range(3): self.assertAlmostEqual(target[i][3], IDENTITY[i][3])

    def test_nan_zero_and_nonunit_quaternion_cannot_be_targets(self):
        for bad in ([0.]*7, [float('nan')]+POSE[1:], POSE[:-1]+[5.], []):
            self.assertFalse(pose_valid(bad))


class InputLifecycle(unittest.TestCase):
    def test_input_selection_requires_live_serial_and_head_and_rejects_stale_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root/'snapshot.json'
            frame = {'received_mono': time.monotonic(), 'trackers': {'left': POSE},
                     'head_pose': POSE, 'pose_age_ms': {'left': 10}, 'sample_hz': 90}
            path.write_text(json.dumps(frame))
            inputs = TrackerInput(root, path)
            self.assertFalse(inputs.status()['ready'])
            self.assertFalse(inputs.select('unknown')[0])
            self.assertTrue(inputs.select('left')[0])
            self.assertTrue(inputs.status()['ready'])
            frame['pose_age_ms']['left'] = 2000
            path.write_text(json.dumps(frame))
            self.assertFalse(inputs.status()['ready'])
            frame['received_mono'] -= 2
            path.write_text(json.dumps(frame))
            self.assertIsNone(read_frame(path))
            self.assertFalse(inputs.status()['connected'])

    def test_ssh_backlog_cannot_make_an_old_pose_fresh(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'snapshot.json'
            path.write_text(json.dumps({'received_mono': time.monotonic(),
                                        'sample_mono_on_robot': time.monotonic()-2}))
            self.assertIsNone(read_frame(path))

    def test_native_launch_owns_arms_only_and_has_no_browser_or_finger_controller(self):
        argv = command('pico_wuji')
        self.assertTrue(argv[1].endswith('/pico_tracker/arm_teleop.py'))
        self.assertNotIn('--ee', argv)
        self.assertNotIn('teleop_hand_and_arm.py', ' '.join(argv))

    def test_native_start_requires_tracker_before_activating_fingers(self):
        up, events = controller('pico_wuji')
        up.tracker = Mock()
        up.tracker.status.return_value = {'ready': False, 'message': 'Enable Head'}
        self.assertFalse(up.start()[0])
        self.assertEqual(events, [])
        up.tracker.status.return_value = {'ready': True}
        self.assertTrue(up.start()[0])
        self.assertEqual(events, ['glove_start', 'CMD_START'])

    def test_failed_native_arm_start_rolls_back_finger_publisher(self):
        up, events = controller('pico_wuji', ipc_ok=False)
        up.tracker = Mock()
        up.tracker.status.return_value = {'ready': True}
        self.assertFalse(up.start()[0])
        self.assertEqual(events, ['glove_start', 'CMD_START', 'glove_stop'])
