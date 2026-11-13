#!/usr/bin/env python3
"""Observe XRoboToolkit cached PICO inputs; never send device/robot commands.

Run separately from teleoperation: PCService v1.0.0 replaces an earlier SDK
feedback subscriber when a new one connects. Exit 0 means input observations,
not verified tracking or permission to move a robot. See docs/pico-bringup.md.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time


REQUIRED_API = (
    "init", "close", "get_time_stamp_ns", "get_motion_timestamp_ns",
    "get_left_controller_pose", "get_right_controller_pose", "get_headset_pose",
    "num_motion_data_available", "get_motion_tracker_pose",
    "get_motion_tracker_serial_numbers",
)
LIMITATIONS = [
    "SDK getters expose cached data; disconnect does not clear these caches.",
    "No per-tracker/controller tracking-valid or confidence getter is exposed.",
    "Top-level timestamp progress does not prove tracker updates or validity.",
    "Motion timestamp may stay zero; Unity Motion.len may be ignored upstream.",
    "Separate getter calls are not an atomic frame, including SN/pose pairing.",
    "PCService v1.0.0 replaces its previous SDK feedback subscriber on connection.",
]


def pose_report(raw):
    """Validate representation only: a unit quaternion is not tracking evidence."""
    try:
        values = [float(x) for x in raw]
    except (TypeError, ValueError, OverflowError):
        return {"xyzw_pose": None, "representation_ok": False, "issue": "not_numeric"}
    if len(values) != 7:
        return {"xyzw_pose": None, "representation_ok": False, "issue": "expected_7_values"}
    if not all(math.isfinite(x) for x in values):
        return {"xyzw_pose": None, "representation_ok": False, "issue": "nonfinite"}
    norm = math.hypot(*values[3:])
    ok = abs(norm - 1.0) <= 0.05
    return {"xyzw_pose": values, "quaternion_norm": norm,
            "representation_ok": ok, "issue": None if ok else "nonunit_quaternion"}


class TimestampObserver:
    """Report getter value changes, never infer sensor freshness from them."""

    def __init__(self):
        self.previous = None
        self.last_change_ns = None
        self.advances = 0
        self.regressions = 0

    def observe(self, value, local_ns):
        value = int(value)
        previous = self.previous
        if value <= 0:
            state = "unavailable"
        elif previous is None or previous <= 0:
            state = "first_positive"
        elif value > previous:
            state = "advanced"
            self.advances += 1
        elif value < previous:
            state = "regressed"
            self.regressions += 1
        else:
            state = "unchanged"
        if previous is not None and value != previous:
            self.last_change_ns = local_ns
        self.previous = value
        return {"value_ns": value, "change": state,
                "ms_since_observed_value_change": None if self.last_change_ns is None
                else round((local_ns - self.last_change_ns) / 1e6, 3),
                "source_age_ms": None}


def bind_sides(trackers, left_sn=None, right_sn=None):
    result = {}
    for side, requested in (("left", left_sn), ("right", right_sn)):
        matches = [t["cache_index"] for t in trackers if requested and t["serial_number"] == requested]
        status = "not_configured" if not requested else (
            "matched_cached_sn" if len(matches) == 1 else "missing" if not matches else "ambiguous")
        result[side] = {"requested_sn": requested, "status": status,
                        "cache_index": matches[0] if len(matches) == 1 else None}
    return result


def read_snapshot(sdk, top_observer, motion_observer, left_sn=None, right_sn=None):
    top_before = int(sdk.get_time_stamp_ns())
    motion_before = int(sdk.get_motion_timestamp_ns())
    count = int(sdk.num_motion_data_available())
    poses = list(sdk.get_motion_tracker_pose())
    serials = list(sdk.get_motion_tracker_serial_numbers())
    controllers = {side: {**pose_report(getattr(sdk, f"get_{side}_controller_pose")()),
                          "serial_number": None, "serial_number_status": "not_exposed_by_sdk",
                          "tracking_valid": None} for side in ("left", "right")}
    head = pose_report(sdk.get_headset_pose())
    motion_after = int(sdk.get_motion_timestamp_ns())
    top_after = int(sdk.get_time_stamp_ns())
    local_ns = time.monotonic_ns()
    issues = []
    if not 0 <= count <= 3:
        issues.append("tracker_count_outside_sdk_range")
    if len(poses) != count or len(serials) != count:
        issues.append("getter_array_lengths_disagree_with_count")
    trackers = []
    for index in range(max(0, min(count, len(poses), len(serials), 3))):
        sn = serials[index] if isinstance(serials[index], str) else None
        trackers.append({"cache_index": index, "serial_number": sn,
                         **pose_report(poses[index]), "tracking_valid": None,
                         "source_freshness": "unknown"})
    nonempty_sns = [t["serial_number"] for t in trackers if t["serial_number"]]
    if len(set(nonempty_sns)) != len(nonempty_sns):
        issues.append("duplicate_tracker_serial_numbers")
    if len(nonempty_sns) != len(trackers):
        issues.append("missing_tracker_serial_number")
    return {"type": "sample", "local_monotonic_ns": local_ns,
            "top_level_timestamp": top_observer.observe(top_after, local_ns),
            "motion_timestamp": motion_observer.observe(motion_after, local_ns),
            "timestamp_changed_during_read": top_before != top_after or motion_before != motion_after,
            "snapshot_atomic": False, "sdk_reported_tracker_count": count,
            "pose_array_length": len(poses), "sn_array_length": len(serials),
            "trackers": trackers, "side_bindings": bind_sides(trackers, left_sn, right_sn),
            "controllers": controllers, "headset": {**head, "tracking_valid": None},
            "issues": issues, "motion_ready": False}


def observe(sdk, seconds, hz, emit, left_sn=None, right_sn=None):
    """Initialize only the PICO receiver and close it even after a getter error."""
    missing = [name for name in REQUIRED_API if not callable(getattr(sdk, name, None))]
    if missing:
        raise RuntimeError("SDK API missing: " + ", ".join(missing))
    top, motion = TimestampObserver(), TimestampObserver()
    samples = 0
    peak_trackers = 0
    sn_seen = set()
    pair_observed = False
    sdk.init()
    try:
        start = time.monotonic()
        deadline = start + seconds
        next_sample = start
        while time.monotonic() < deadline:
            frame = read_snapshot(sdk, top, motion, left_sn, right_sn)
            samples += 1
            peak_trackers = max(peak_trackers, frame["sdk_reported_tracker_count"])
            usable = {t["serial_number"] for t in frame["trackers"]
                      if t["serial_number"] and t["representation_ok"]}
            sn_seen.update(t["serial_number"] for t in frame["trackers"] if t["serial_number"])
            if len(usable) >= 2 and not frame["issues"]:
                requested = {s for s in (left_sn, right_sn) if s}
                pair_observed |= requested <= usable
            emit(frame)
            next_sample += 1 / hz
            time.sleep(max(0.0, min(next_sample, deadline) - time.monotonic()))
    finally:
        sdk.close()
    observed = pair_observed and top.advances > 0
    summary = {"type": "summary", "samples": samples, "peak_cached_tracker_count": peak_trackers,
               "tracker_sns_seen": sorted(sn_seen), "top_timestamp_advances": top.advances,
               "motion_timestamp_advances": motion.advances,
               "top_timestamp_regressions": top.regressions, "motion_timestamp_regressions": motion.regressions,
               "two_tracker_pose_caches_observed": pair_observed,
               "status": "input_cache_observed" if observed else "input_not_observed",
               "tracking_valid": None, "motion_ready": False,
               "limitations": LIMITATIONS}
    emit(summary)
    return 0 if observed else 1


@contextmanager
def sdk_stdout_to_stderr():
    """Native SDK writes to fd 1; keep diagnostic JSON separate from native logs."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        # SDK's std::cout can leave buffered bytes even after close().
        import ctypes
        ctypes.CDLL(None).fflush(None)
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def positive_finite(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=positive_finite, default=3.0)
    parser.add_argument("--hz", type=positive_finite, default=10.0, help="getter polling rate; not sensor frame rate")
    parser.add_argument("--left-sn", help="bind left wrist by exact tracker serial number")
    parser.add_argument("--right-sn", help="bind right wrist by exact tracker serial number")
    parser.add_argument("--output", type=Path, help="write JSONL to file (default: stdout); native SDK logs go to stderr")
    args = parser.parse_args(argv)
    if args.hz > 200:
        parser.error("--hz must be <= 200")
    if args.left_sn and args.left_sn == args.right_sn:
        parser.error("left/right tracker serial numbers must differ")
    # A duplicate fd is necessary because native output redirection changes fd 1.
    with (args.output.open("w", encoding="utf-8") if args.output else
          os.fdopen(os.dup(1), "w", encoding="utf-8")) as output:
        def emit(record):
            output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            output.flush()
        with sdk_stdout_to_stderr():
            try:
                sdk = importlib.import_module("xrobotoolkit_sdk")
            except (ImportError, OSError) as exc:
                emit({"type": "error", "status": "sdk_unavailable", "detail": str(exc),
                      "python": sys.executable, "motion_ready": False,
                      "next_step": "Use the isolated Python environment in docs/pico-bringup.md."})
                return 2
            try:
                try:
                    version = importlib.metadata.version("xrobotoolkit-sdk")
                except importlib.metadata.PackageNotFoundError:
                    version = "unknown"
                emit({"type": "metadata", "sdk_version": version, "sdk_path": getattr(sdk, "__file__", None),
                      "python": sys.executable, "readonly_getters": True,
                      "pose_order": ["x", "y", "z", "qx", "qy", "qz", "qw"],
                      "limitations": LIMITATIONS})
                return observe(sdk, args.seconds, args.hz, emit, args.left_sn, args.right_sn)
            except KeyboardInterrupt:
                emit({"type": "error", "status": "interrupted", "motion_ready": False})
                return 130
            except Exception as exc:
                emit({"type": "error", "status": "sdk_error", "detail": str(exc), "motion_ready": False})
                return 2


if __name__ == "__main__":
    raise SystemExit(main())
