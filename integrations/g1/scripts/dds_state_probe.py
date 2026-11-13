#!/usr/bin/env python3
"""Subscribe to G1/BrainCo state only; no robot command publishers or controllers.

Requires unitree_sdk2_python and its CycloneDDS dependencies. Run in a separate
process, e.g. python dds_state_probe.py --interface eth0 --seconds 5.
Exit 0: all three streams have fresh valid samples; 1: missing/stale/invalid data;
2: initialization or cleanup failure; 3: hard timeout; 130: interrupted.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import json
import math
import os
import signal
import socket
import sys
import threading
import time


TOPICS = ('rt/lowstate', 'rt/brainco/left/state', 'rt/brainco/right/state')
MAX_AGE_S = 0.5


def finite_values(states, field):
    values = [float(getattr(state, field)) for state in states]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('nonfinite ' + field)
    return values


def decode_lowstate(message):
    motors = list(message.motor_state)
    # HG LowState IDL at the audited SDK commit has a fixed array of 35 slots;
    # only indices 0..28 correspond to the G1_29 physical joints.
    if len(motors) != 35:
        raise ValueError('expected 35 HG motor_state slots, received %d' % len(motors))
    q = finite_values(motors, 'q')
    dq = finite_values(motors, 'dq')
    return {
        'mode_machine': int(message.mode_machine),
        'mode_pr': int(message.mode_pr),
        'tick': int(message.tick),
        'motor_slots': len(motors),
        'left_arm_indices': list(range(15, 22)),
        'right_arm_indices': list(range(22, 29)),
        'left_arm_q_rad': q[15:22],
        'right_arm_q_rad': q[22:29],
        'left_arm_dq_rad_s': dq[15:22],
        'right_arm_dq_rad_s': dq[22:29],
    }


def decode_hand(message):
    states = list(message.states)
    if len(states) != 6:
        raise ValueError('expected 6 hand states, received %d' % len(states))
    q = finite_values(states, 'q')
    dq = finite_values(states, 'dq')
    if any(value < 0.0 or value > 1.0 for value in q):
        raise ValueError('hand q outside normalized [0,1] range')
    return {
        'channels': ['Thumb', 'Thumb_aux', 'Index', 'Middle', 'Ring', 'Pinky'],
        'q_normalized': q,
        'dq_normalized': dq,
    }


class Stream:
    def __init__(self, decode):
        self.decode = decode
        self.lock = threading.Lock()
        self.count = 0
        self.valid_count = 0
        self.invalid_count = 0
        self.first_received = None
        self.last_received = None
        self.first_valid = None
        self.last_valid = None
        self.last_error = None
        self.latest = None

    def receive(self, message):
        received = time.monotonic()
        try:
            value = self.decode(message)
            error = None
        except Exception as exc:
            value = None
            error = '%s: %s' % (type(exc).__name__, exc)
        with self.lock:
            self.count += 1
            if self.first_received is None:
                self.first_received = received
            self.last_received = received
            if error is not None:
                self.invalid_count += 1
                self.last_error = error
            else:
                self.valid_count += 1
                if self.first_valid is None:
                    self.first_valid = received
                self.last_valid = received
                self.latest = value

    def snapshot(self, now):
        with self.lock:
            age = None if self.last_valid is None else max(0.0, now - self.last_valid)
            span = None if self.first_valid is None else self.last_valid - self.first_valid
            if self.count == 0:
                status = 'missing'
            elif self.invalid_count:
                status = 'invalid_samples'
            elif age is None or age > MAX_AGE_S:
                status = 'stale'
            else:
                status = 'fresh'
            return {
                'status': status, 'received_count': self.count,
                'valid_count': self.valid_count, 'invalid_count': self.invalid_count,
                'first_receive_monotonic_s': self.first_received,
                'last_receive_monotonic_s': self.last_received,
                'first_valid_monotonic_s': self.first_valid,
                'last_valid_monotonic_s': self.last_valid,
                'last_valid_age_s': age,
                'valid_arrival_hz': ((self.valid_count - 1) / span
                                     if span is not None and span > 0 else None),
                'last_error': self.last_error, 'latest_valid_sample': self.latest,
            }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', required=True, help='NIC on this host, e.g. eth0')
    parser.add_argument('--domain', type=int, default=0)
    parser.add_argument('--seconds', type=float, default=5.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 30:
        parser.error('--seconds must be finite and in (0,30]')
    if not 0 <= args.domain <= 232:
        parser.error('--domain must be in [0,232]')
    try:
        socket.if_nametoindex(args.interface)
    except OSError:
        parser.error('network interface does not exist: ' + args.interface)

    output = sys.stdout
    reported = threading.Event()
    stop = threading.Event()
    started = time.monotonic()
    streams = {name: Stream(decode_lowstate if name == 'rt/lowstate' else decode_hand)
               for name in TOPICS}
    report = {
        'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'interface': args.interface, 'domain': args.domain,
        'requested_observation_s': args.seconds,
        'max_valid_sample_age_s': MAX_AGE_S,
        'command_publishers_created': 0,
        'limitations': [
            'Arrival freshness is measured on this host, not a verified source clock.',
            'DDS state does not authenticate robot/hand identity or certify motion readiness.',
            'No mode changes, hardware configuration writes or service starts are performed.',
            'Hand states have no source timestamp or hardware execution acknowledgement.',
        ],
    }

    def write_report(code, error=None):
        now = time.monotonic()
        report['elapsed_s'] = now - started
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['streams'] = {name: stream.snapshot(now) for name, stream in streams.items()}
        report['all_streams_fresh'] = all(item['status'] == 'fresh'
                                          for item in report['streams'].values())
        report['exit_code'] = code
        if error is not None:
            report['error'] = error
        print(json.dumps(report, indent=2, allow_nan=False), file=output, flush=True)
        reported.set()

    def hard_timeout():
        if not reported.is_set():
            write_report(3, 'hard timeout during SDK setup, observation or reader cleanup')
        else:
            print('DDS probe exceeded shutdown deadline', file=sys.stderr, flush=True)
        # SDK/native shutdown must not keep this diagnostic alive indefinitely.
        os._exit(3)

    watchdog = threading.Timer(args.seconds + 15.0, hard_timeout)
    watchdog.daemon = True
    watchdog.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stop.set())
    subscribers = []
    error = None
    cleanup_errors = []
    # The official SDK prints some diagnostics to stdout; preserve JSON stdout.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_

            ChannelFactoryInitialize(args.domain, args.interface)
            for name in TOPICS:
                subscriber = ChannelSubscriber(name, LowState_ if name == 'rt/lowstate' else MotorStates_)
                # queueLen=0 invokes the short callback directly, without a queue
                # worker whose join could otherwise delay Close().
                subscribers.append(subscriber)
                subscriber.Init(streams[name].receive, 0)
            report['observation_started_monotonic_s'] = time.monotonic()
            stop.wait(args.seconds)
            report['observation_finished_monotonic_s'] = time.monotonic()
        except Exception as exc:
            error = '%s: %s' % (type(exc).__name__, exc)
        finally:
            for subscriber in reversed(subscribers):
                try:
                    subscriber.Close()
                except Exception as exc:
                    cleanup_errors.append('%s: %s' % (type(exc).__name__, exc))
    if cleanup_errors:
        report['cleanup_errors'] = cleanup_errors
    if error is not None or cleanup_errors:
        code = 2
    elif stop.is_set():
        code = 130
    else:
        now = time.monotonic()
        code = 0 if all(stream.snapshot(now)['status'] == 'fresh'
                        for stream in streams.values()) else 1
    write_report(code, error)
    # Leave the daemon watchdog armed through native-object finalization. It
    # does not keep a normally exiting process alive.
    return code


if __name__ == '__main__':
    raise SystemExit(main())
