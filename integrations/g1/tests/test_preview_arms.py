"""Run with the documented isolated Pinocchio/CasADi environment."""

import builtins
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
    import pinocchio
    from pinocchio import casadi
except ImportError as exc:
    raise unittest.SkipTest("双臂测试需要 docs/arm-preview.md 的隔离 Pinocchio/CasADi 环境") from exc


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/preview_arms.py"
spec = importlib.util.spec_from_file_location("preview_arms", SCRIPT)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


def sample(left, right, reversed_order=False):
    result = {"type": "sample", "motion_ready": False, "trackers": [
        {"serial_number": "test-left", "xyzw_pose": left},
        {"serial_number": "test-right", "xyzw_pose": right}]}
    if reversed_order:
        result["trackers"].reverse()
    return result


def calibration():
    identity = np.eye(4).tolist()
    offset = np.eye(4)
    offset[0, 3] = .05
    return {"schema": "g1-arms-offline-calibration-v1", "pose_unit": "m",
            "left_sn": "test-left", "right_sn": "test-right",
            "reference_sample_index": 0, "position_scale": 1.,
            "T_robot_axes_xr_axes": identity,
            "T_tracker_wrist": {"left": identity, "right": identity},
            "T_robot_wrist_ik": {"left": offset.tolist(), "right": offset.tolist()},
            "q_reference_rad": preview.REFERENCE_Q}


class Coordinates(unittest.TestCase):
    def setUp(self):
        self.reference = [np.eye(4), np.eye(4)]
        self.reference[0][:3, 3] = [.2, .3, .5]
        self.reference[1][:3, 3] = [.2, -.3, .5]
        self.origin = sample([1, 2, 3, 0, 0, 0, 1], [4, 5, 6, 0, 0, 0, 1])

    def test_reference_has_no_coordinate_jump(self):
        targets = preview.replay_targets([self.origin], calibration(), self.reference, 0)
        np.testing.assert_allclose(targets, self.reference, atol=1e-12)

    def test_serial_binding_survives_array_reordering(self):
        moved = sample([1.01, 2, 3, 0, 0, 0, 1], [4, 5.02, 6, 0, 0, 0, 1], True)
        targets = preview.replay_targets([self.origin, moved], calibration(), self.reference, 1)
        np.testing.assert_allclose(targets[0][:3, 3], [.21, .3, .5])
        np.testing.assert_allclose(targets[1][:3, 3], [.2, -.28, .5])

    def test_tracker_mount_and_robot_offset_each_rotate(self):
        cal = calibration()
        mount = np.eye(4)
        mount[0, 3] = .1
        cal["T_tracker_wrist"]["left"] = mount.tolist()
        quarter_turn = np.sqrt(.5)
        moved = sample([1, 2, 3, 0, 0, quarter_turn, quarter_turn], [4, 5, 6, 0, 0, 0, 1])
        targets = preview.replay_targets([self.origin, moved], cal, self.reference, 1)
        # Mount rotates (-.1,+.1); the separate 5cm robot IK offset rotates (-.05,+.05).
        np.testing.assert_allclose(targets[0][:3, 3], [.05, .45, .5], atol=1e-12)

    def test_unbound_or_duplicate_tracker_is_rejected(self):
        cal = calibration()
        cal["right_sn"] = cal["left_sn"]
        with self.assertRaises(ValueError):
            preview.replay_targets([self.origin], cal, self.reference, 0)
        with self.assertRaises(ValueError):
            preview.tracker_pose(self.origin, "not-present")
        self.origin["trackers"].append(self.origin["trackers"][0])
        with self.assertRaises(ValueError):
            preview.tracker_pose(self.origin, "test-left")

    def test_invalid_transform_or_quaternion_is_rejected(self):
        reflection = np.eye(4)
        reflection[0, 0] = -1
        for bad in (reflection, np.zeros((4, 4)), np.full((4, 4), np.nan)):
            with self.assertRaises(ValueError):
                preview.transform(bad, "test")
        with self.assertRaises(ValueError):
            preview.pose_transform([0] * 7)


@unittest.skipUnless(all((preview.DEFAULT_SOURCE / name).is_file() for name in preview.FILES),
                     "需要下载固定版官方 IK/滤波器/URDF，见 docs/arm-preview.md")
class OfficialSolver(unittest.TestCase):
    def test_real_offline_solve_without_network_or_control_imports(self):
        original_import = builtins.__import__

        def forbid_control(name, *args, **kwargs):
            if name.startswith(("unitree", "xrobotoolkit")) or "robot_arm" in name:
                raise AssertionError(f"control/SDK import forbidden: {name}")
            return original_import(name, *args, **kwargs)

        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                patch.object(builtins, "__import__", side_effect=forbid_control):
            result = preview.run(preview.parser().parse_args([]))
        self.assertTrue(result["offline_ok"])
        self.assertFalse(result["motion_ready"])
        self.assertEqual(result["commands_published"], 0)
        self.assertEqual(len(result["q_rad"]), 14)
        self.assertTrue(result["joint_limits_ok"])
        self.assertLess(result["residuals"]["left"]["position_m"], .003)

    def test_unreachable_target_is_not_accepted_by_optimizer_success(self):
        result = preview.run(preview.parser().parse_args(["--translation-m", "2"]))
        self.assertFalse(result["offline_ok"])
        self.assertFalse(result["residuals_ok"])
        self.assertGreater(result["residuals"]["left"]["position_m"], 1.)

    def test_official_exception_fallback_is_reported_as_failure(self):
        ik, versions = preview.load_ik(preview.DEFAULT_SOURCE)
        ik.opti.solver("ipopt", {"ipopt.max_iter": 0, "ipopt.print_level": 0, "print_time": False})
        with patch.object(preview, "load_ik", return_value=(ik, versions)), \
                patch.object(ik, "Visualization", False), \
                patch("logging.Logger.error"):
            result = preview.run(preview.parser().parse_args([]))
        self.assertFalse(result["offline_ok"])
        self.assertFalse(result["iterations"][0]["solver_success"])
        self.assertEqual(len(result["iterations"]), 1)
        np.testing.assert_allclose(result["q_rad"], preview.REFERENCE_Q)

    def test_upstream_modification_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in preview.FILES:
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((preview.DEFAULT_SOURCE / name).read_bytes())
            path = root / next(iter(preview.FILES))
            path.write_text(path.read_text() + "\n# unexpected change\n")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                preview.load_ik(root)

    def test_saved_raw_pico_without_calibration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "calibration"):
            preview.run(preview.parser().parse_args(["--pico-jsonl", "unused.jsonl"]))

    def test_saved_pico_file_uses_calibration_and_keeps_offline_label(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frames = [sample([1, 2, 3, 0, 0, 0, 1], [4, 5, 6, 0, 0, 0, 1]),
                      sample([1.01, 2, 3, 0, 0, 0, 1], [4.01, 5, 6, 0, 0, 0, 1], True)]
            (root / "samples.jsonl").write_text('\n'.join(map(json.dumps, frames)))
            (root / "calibration.json").write_text(json.dumps(calibration()))
            result = preview.run(preview.parser().parse_args([
                "--pico-jsonl", str(root / "samples.jsonl"),
                "--calibration", str(root / "calibration.json")]))
        self.assertTrue(result["offline_ok"])
        self.assertIn("unverified_freshness", result["mode"])
        self.assertFalse(result["motion_ready"])


if __name__ == "__main__":
    unittest.main()
