#!/usr/bin/env python3
"""Receive tracker JSONL via authenticated SSH. No DDS or motor commands."""
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pico_tracker.geometry import pose_valid
from pico_tracker.input import RUNTIME, FRAME_PATH


def main():
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    with (RUNTIME / 'receiver.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        temporary = FRAME_PATH.with_suffix('.tmp')
        last_session, last_sequence = None, -1
        clock_offset = None
        for line in sys.stdin:
            if len(line) > 16000:
                continue
            try:
                frame = json.loads(line)
                if frame['source'] != 'xrobotoolkit_sdk' or frame['version'] != 1:
                    continue
                if not isinstance(frame['trackers'], dict) or len(frame['trackers']) > 3:
                    continue
                if not all(isinstance(sn, str) and pose_valid(p) for sn, p in frame['trackers'].items()):
                    continue
                if frame['session'] == last_session and frame['seq'] <= last_sequence:
                    continue
                if not isinstance(frame.get('sample_mono'), (int, float)) or not math.isfinite(frame['sample_mono']):
                    continue
                if frame['session'] != last_session:
                    clock_offset = None
                last_session, last_sequence = frame['session'], frame['seq']
                received = time.monotonic()
                offset = received-frame['sample_mono']
                clock_offset = offset if clock_offset is None else min(clock_offset, offset)
                frame['received_mono'] = received
                # Estimate added transport delay relative to this session's fastest
                # delivery, without assuming synchronized wall clocks. Old queued
                # frames must not acquire a new sample age just because SSH resumes.
                frame['sample_mono_on_robot'] = frame['sample_mono']+clock_offset
                frame['transport_delay_ms'] = max(0., (offset-clock_offset)*1000)
                data = json.dumps(frame, allow_nan=False)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as output:
                    output.write(data)
                temporary.replace(FRAME_PATH)
            except (ValueError, KeyError, TypeError):
                continue


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
