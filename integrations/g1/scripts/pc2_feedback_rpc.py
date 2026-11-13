#!/usr/bin/env python3
"""Finite SSH stdin/stdout state reader; snapshot requests only, no commands."""
import argparse
import json
import math
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time

TOPICS = ('rt/lowstate', 'rt/brainco/left/state', 'rt/brainco/right/state')


def values(motors, name):
    result = [float(getattr(m, name)) for m in motors]
    if not all(math.isfinite(v) for v in result):
        raise ValueError('nonfinite ' + name)
    return result


def decode_body(message, crc=None):
    if len(message.motor_state) != 35:
        raise ValueError('expected 35 HG slots')
    if crc is not None and crc.Crc(message) != message.crc:
        raise ValueError('invalid HG lowstate CRC')
    motors = message.motor_state[:29]
    return {'q': values(motors, 'q'), 'dq': values(motors, 'dq'),
            'tau_est': values(motors, 'tau_est'), 'tick': int(message.tick),
            'mode_pr': int(message.mode_pr), 'mode_machine': int(message.mode_machine),
            'motorstate': [int(m.motorstate) for m in motors],
            'motor_mode': [int(m.mode) for m in motors], 'crc_valid': crc is not None}


def decode_hand(message):
    if len(message.states) != 6:
        raise ValueError('expected six hand states')
    q = values(message.states, 'q')
    if any(not 0 <= v <= 1 for v in q):
        raise ValueError('hand q outside normalized range')
    return {'q': q, 'dq': values(message.states, 'dq')}


class Latest:
    def __init__(self, decode, clock=time.monotonic_ns):
        self.decode, self.clock = decode, clock
        self.lock = threading.Lock()
        self.sequence = 0
        self.record = None

    def receive(self, message):
        received_ns = self.clock()
        try:
            payload, error = self.decode(message), None
        except Exception as exc:
            payload, error = None, type(exc).__name__ + ': ' + str(exc)
        with self.lock:
            self.sequence += 1
            self.record = {'sequence': self.sequence, 'received_monotonic_ns': received_ns,
                           'payload': payload, 'error': error}

    def snapshot(self):
        with self.lock:
            return self.record


def request_id(request):
    if (not isinstance(request, dict) or set(request) != {'request_id'}
            or type(request['request_id']) is not int or request['request_id'] < 0):
        raise ValueError('only {request_id: nonnegative integer} snapshot requests accepted')
    return request['request_id']


def serve(args):
    output = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    def emit(value):
        print(json.dumps(value, allow_nan=False, separators=(',', ':')), file=output, flush=True)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    watchdog = threading.Timer(args.seconds + 10, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    subscribers = []
    code = 0
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
        from unitree_sdk2py.utils.crc import CRC
        ChannelFactoryInitialize(0, args.interface)
        crc = CRC()
        streams = {name: Latest((lambda message: decode_body(message, crc))
                               if name == TOPICS[0] else decode_hand) for name in TOPICS}
        for name in TOPICS:
            subscriber = ChannelSubscriber(name, LowState_ if name == TOPICS[0] else MotorStates_)
            subscribers.append(subscriber)
            subscriber.Init(streams[name].receive, 0)
        clock_id = 'linux-monotonic:' + Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        emit({'type': 'feedback_ready', 'clock_id': clock_id, 'interface': args.interface,
              'commands_published': 0, 'maximum_runtime_s': args.seconds})
        started, buffer, previous_id = time.monotonic(), b'', -1
        while not stopped.is_set() and time.monotonic() - started < args.seconds:
            if not select.select([sys.stdin.fileno()], [], [], .1)[0]:
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 8192:
                raise ValueError('snapshot request buffer exceeded')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                rid = request_id(json.loads(line))
                if rid <= previous_id:
                    raise ValueError('snapshot request IDs must increase')
                previous_id = rid
                snapshots = {name: stream.snapshot() for name, stream in streams.items()}
                emit({'type': 'feedback_snapshot', 'request_id': rid, 'clock_id': clock_id,
                      'streams': snapshots, 'server_send_monotonic_ns': time.monotonic_ns(),
                      'commands_published': 0})
    except (BrokenPipeError, KeyboardInterrupt):
        code = 130
    except Exception as exc:
        emit({'type': 'feedback_error', 'error': type(exc).__name__ + ': ' + str(exc),
              'commands_published': 0})
        code = 1
    finally:
        for subscriber in reversed(subscribers):
            try:
                subscriber.Close()
            except Exception as exc:
                print('subscriber close: ' + str(exc), file=sys.stderr)
                code = 1
    return code


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface', default='eth0')
    p.add_argument('--seconds', type=float, default=30)
    args = p.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 120:
        p.error('seconds must be finite and in (0,120]')
    return serve(args)


if __name__ == '__main__':
    raise SystemExit(main())
