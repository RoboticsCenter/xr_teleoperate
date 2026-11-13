#!/usr/bin/env python3
"""Read official XRoboToolkit Motion Tracker poses; emit input JSONL only.

Some released clients omit Motion.timeStampNs. In that case the SDK's packet
timestamp is used, and per-tracker pose-change age remains visible separately.
No invented timestamp is presented as a tracker hardware timestamp.
"""
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pico_tracker.geometry import pose_valid


def main():
    # Native SDK logs use C stdout. Keep them out of the JSONL input channel.
    json_fd = os.dup(1)
    os.dup2(2, 1)
    os.set_blocking(json_fd, False)
    import xrobotoolkit_sdk as xrt
    xrt.init()
    session = uuid.uuid4().hex
    previous, changed = {}, {}
    sequence, last_stamp, last_emit = 0, None, time.monotonic()
    try:
        while True:
            tick = time.monotonic()
            packet_stamp = xrt.get_time_stamp_ns()
            motion_stamp = xrt.get_motion_timestamp_ns()
            stamp = motion_stamp or packet_stamp
            if stamp and stamp != last_stamp:
                poses = xrt.get_motion_tracker_pose()
                serials = xrt.get_motion_tracker_serial_numbers()
                if stamp == (xrt.get_motion_timestamp_ns() or xrt.get_time_stamp_ns()):
                    trackers = {sn: list(pose) for sn, pose in zip(serials, poses)
                                if sn and pose_valid(pose)}
                    for sn, pose in trackers.items():
                        if pose != previous.get(sn):
                            changed[sn] = tick
                        previous[sn] = pose
                    frame = {'version': 1, 'source': 'xrobotoolkit_sdk',
                             'session': session, 'seq': sequence, 'trackers': trackers,
                             'head_pose': list(xrt.get_headset_pose()),
                             'motion_timestamp_ns': motion_stamp, 'packet_timestamp_ns': packet_stamp,
                             'timestamp_source': 'motion' if motion_stamp else 'packet',
                             'sample_at_unix_ns': time.time_ns(),
                             'sample_mono': tick,
                             'sample_hz': round(1/max(tick-last_emit, 1e-6), 1),
                             'pose_age_ms': {sn: round((tick-changed[sn])*1000, 1) for sn in trackers}}
                    data = (json.dumps(frame, separators=(',', ':'), allow_nan=False)+'\n').encode()
                    if len(data) <= 4096:
                        try:
                            os.write(json_fd, data)
                        except BlockingIOError:
                            pass  # latest samples win; never queue old poses behind SSH
                    sequence += 1
                    last_stamp, last_emit = stamp, tick
            time.sleep(max(0., 1/120-(time.monotonic()-tick)))
    finally:
        xrt.close()
        os.close(json_fd)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
