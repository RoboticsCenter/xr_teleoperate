"""Check input validity claims and receiver cleanup without hardware/SDK."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pico_probe.py"
spec = importlib.util.spec_from_file_location("pico_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class FakeSDK:
    def __init__(self):
        self.stamp = 10
        self.count = 2
        self.poses = [[0, 0, 0, 0, 0, 0, 1], [1, 0, 0, 0, 0, 0, 1]]
        self.sns = ["RIGHT", "LEFT"]
        self.closed = 0
        self.initialized = 0

    def init(self): self.initialized += 1
    def close(self): self.closed += 1
    def get_time_stamp_ns(self):
        self.stamp += 1
        return self.stamp
    def get_motion_timestamp_ns(self): return 0
    def num_motion_data_available(self): return self.count
    def get_motion_tracker_pose(self): return self.poses
    def get_motion_tracker_serial_numbers(self): return self.sns
    def get_left_controller_pose(self): return [0] * 7
    def get_right_controller_pose(self): return [0, 0, 0, 0, 0, 0, 1]
    def get_headset_pose(self): return [0, 0, 0, 0, 0, 0, 1]


class PicoProbeTests(unittest.TestCase):
    def snapshot(self, sdk):
        return probe.read_snapshot(sdk, probe.TimestampObserver(), probe.TimestampObserver(), "LEFT", "RIGHT")

    def test_help_does_not_import_sdk(self):
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--left-sn", result.stdout)

    def test_missing_sdk_is_machine_readable(self):
        import json
        result = subprocess.run([sys.executable, "-S", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "sdk_unavailable")

    def test_nonfinite_cli_rejected(self):
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--seconds", "nan"], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_side_mapping_uses_serial_not_index(self):
        frame = self.snapshot(FakeSDK())
        self.assertEqual(frame["side_bindings"]["left"]["cache_index"], 1)
        self.assertEqual(frame["side_bindings"]["right"]["cache_index"], 0)

    def test_progressing_top_does_not_validate_tracker(self):
        frame = self.snapshot(FakeSDK())
        self.assertTrue(frame["timestamp_changed_during_read"])
        self.assertEqual(frame["motion_timestamp"]["change"], "unavailable")
        self.assertFalse(frame["motion_ready"])
        self.assertIsNone(frame["trackers"][0]["tracking_valid"])
        self.assertEqual(frame["trackers"][0]["source_freshness"], "unknown")

    def test_duplicate_serial_and_mismatched_arrays_reported(self):
        sdk = FakeSDK()
        sdk.sns = ["LEFT", "LEFT"]
        sdk.count = 3
        frame = self.snapshot(sdk)
        self.assertEqual(frame["side_bindings"]["left"]["status"], "ambiguous")
        self.assertIn("getter_array_lengths_disagree_with_count", frame["issues"])
        self.assertIn("duplicate_tracker_serial_numbers", frame["issues"])

    def test_zero_count_does_not_expose_array_tail(self):
        sdk = FakeSDK()
        sdk.count = 0
        self.assertEqual(self.snapshot(sdk)["trackers"], [])

    def test_pose_checks_do_not_repair_invalid_quaternions(self):
        for pose in ([0] * 7, [float("nan")] * 7, [1, 2, 3]):
            self.assertFalse(probe.pose_report(pose)["representation_ok"])
        self.assertTrue(probe.pose_report([0, 0, 0, 0, 0, 0, 1])["representation_ok"])

    def test_clock_regression_is_distinct_from_advance(self):
        obs = probe.TimestampObserver()
        obs.observe(100, 1_000_000)
        self.assertEqual(obs.observe(90, 2_000_000)["change"], "regressed")
        self.assertEqual(obs.observe(90, 3_000_000)["ms_since_observed_value_change"], 1)
        self.assertEqual(obs.advances, 0)

    def test_close_after_getter_failure(self):
        sdk = FakeSDK()
        with patch.object(sdk, "get_motion_tracker_pose", side_effect=RuntimeError("getter failed")):
            with self.assertRaisesRegex(RuntimeError, "getter failed"):
                probe.observe(sdk, 0.01, 10, lambda _: None)
        self.assertEqual(sdk.closed, 1)

    def test_missing_api_does_not_initialize(self):
        sdk = FakeSDK()
        sdk.get_motion_timestamp_ns = None
        with self.assertRaisesRegex(RuntimeError, "SDK API missing"):
            probe.observe(sdk, 0.01, 10, lambda _: None)
        self.assertEqual(sdk.initialized, 0)

    def test_no_data_exit_and_cleanup(self):
        sdk = FakeSDK()
        sdk.count = 0
        sdk.sns = []
        sdk.poses = []
        records = []
        self.assertEqual(probe.observe(sdk, 0.01, 10, records.append), 1)
        self.assertEqual(sdk.closed, 1)
        self.assertEqual(records[-1]["status"], "input_not_observed")

    def test_valid_looking_cache_is_never_motion_ready(self):
        records = []
        sdk = FakeSDK()
        self.assertEqual(probe.observe(sdk, 0.025, 100, records.append), 0)
        self.assertFalse(records[-1]["motion_ready"])
        self.assertEqual(records[-1]["motion_timestamp_advances"], 0)


if __name__ == "__main__":
    unittest.main()
