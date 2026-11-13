#!/usr/bin/env python3
"""Read Revo2 identity/unit mode/state; Linux Python 3.8+, standard library only.

Only five allowlisted Modbus FC03/04 requests; no SDK, server, target writes,
retries or low-latency setting changes. --plan never opens the serial port.
Exit 0: valid complete report; 1: probe/cleanup failure; 2: CLI error.
"""
import argparse
import copy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import sys
import termios
import time

SOURCE = 'https://www.brainco-hz.com/docs/revolimb-hand/en/revo2/modbus_pro.html'
READS = (
    ('firmware', 4, 3000, 10),
    ('serial_number', 4, 3010, 10),
    ('hand_side', 3, 901, 1),
    ('unit_mode', 3, 937, 1),
    ('motor_status', 4, 2000, 24),
)
ALLOWED_READS = frozenset((fc, address, count) for _, fc, address, count in READS)
CHANNELS = ['thumb_flex', 'thumb_aux', 'index', 'middle', 'ring', 'pinky']
OPEN_SETTLE_S = 0.150  # Official SDK c/demo/debug_detect.cpp FTDI warm-up.


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
    return crc


def read_request(slave, function, address, count):
    if not 1 <= slave <= 247:
        raise ValueError('slave must be 1..247; broadcast is forbidden')
    if (function, address, count) not in ALLOWED_READS:
        raise ValueError('request is outside the fixed read-only allowlist')
    body = struct.pack('>BBHH', slave, function, address, count)
    return body + struct.pack('<H', crc16(body))


def decode_response(frame, slave, function, count):
    if len(frame) < 5 or crc16(frame) != 0:
        raise ValueError('short response or invalid Modbus CRC')
    if frame[0] != slave:
        raise ValueError('response slave does not match request')
    if frame[1] == function | 0x80:
        if len(frame) != 5:
            raise ValueError('malformed Modbus exception response')
        raise ValueError('Modbus exception code %d' % frame[2])
    if frame[1] != function or frame[2] != count * 2 or len(frame) != count * 2 + 5:
        raise ValueError('response function, byte count or length mismatch')
    return list(struct.unpack('>' + 'H' * count, frame[3:-2]))


def signed16(value):
    return value - 65536 if value & 0x8000 else value


def identity_string(registers):
    raw = struct.pack('>' + 'H' * len(registers), *registers)
    value = raw.split(b'\0', 1)[0].decode('ascii')
    if not value or not all(32 <= ord(char) <= 126 for char in value):
        raise ValueError('empty or non-printable identity string')
    return value


def decode_results(results):
    side, unit = results['hand_side'][0], results['unit_mode'][0]
    state = results['motor_status']
    if len(state) != 24:
        raise ValueError('expected 24 motor-status registers')
    return {
        'firmware': identity_string(results['firmware']),
        'serial_number': identity_string(results['serial_number']),
        'hand_side_raw': side,
        'hand_side': {1: 'right', 2: 'left'}.get(side, 'unknown'),
        'unit_mode_raw': unit,
        'unit_mode': {0: 'normalized', 1: 'physical'}.get(unit, 'unknown'),
        'channels': CHANNELS,
        'positions_raw_u16': state[:6],
        'speeds_raw_i16': [signed16(v) for v in state[6:12]],
        'currents_raw_i16': [signed16(v) for v in state[12:18]],
        'motor_states_raw_u16': state[18:24],
        'raw_only': True,
        'note': 'Raw registers; no assumed unit mode or physical-unit scaling.',
        'identity_valid': side in (1, 2) and unit in (0, 1),
    }


class ReadOnlyPort:
    """Every write is an allowlisted read request. Existing owners must be checked.

    flock is advisory; TIOCEXCL does not evict existing or privileged owners.
    Original termios settings are restored before closing, including on error.
    """
    def __init__(self, path, baud, timeout):
        self.path, self.baud, self.timeout = path, baud, timeout
        self.fd, self.original, self.exclusive = None, None, False

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.exclusive = True
            self.original = termios.tcgetattr(self.fd)
            current = copy.deepcopy(self.original)
            current[0] = current[1] = current[3] = 0
            current[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            current[4] = current[5] = getattr(termios, 'B%d' % self.baud)
            current[6][termios.VMIN] = current[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, current)
            time.sleep(OPEN_SETTLE_S)
            self._require_idle()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is None:
            return
        error = None
        try:
            if self.original is not None:
                try:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
                except OSError as exc:
                    error = exc
            if self.exclusive:
                try:
                    fcntl.ioctl(self.fd, termios.TIOCNXCL)
                except OSError as exc:
                    error = error or exc
        finally:
            os.close(self.fd)
            self.fd = None
        if error is not None:
            raise error

    def __exit__(self, *_exc):
        self.close()

    def _require_idle(self):
        # No flush: unsolicited traffic could mean an active competing client.
        if select.select([self.fd], [], [], 0)[0]:
            raise RuntimeError('unsolicited serial data or closed port; refusing query')

    def _read_exact(self, count, deadline):
        chunks = bytearray()
        while len(chunks) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise TimeoutError('serial response timeout: %d/%d bytes' % (len(chunks), count))
            chunk = os.read(self.fd, count - len(chunks))
            if not chunk:
                raise EOFError('serial port closed while reading')
            chunks.extend(chunk)
        return bytes(chunks)

    def query(self, slave, function, address, count):
        frame = read_request(slave, function, address, count)
        self._require_idle()
        deadline = time.monotonic() + self.timeout
        sent = 0
        while sent < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                raise TimeoutError('serial request write timeout')
            size = os.write(self.fd, frame[sent:])
            if size <= 0:
                raise OSError('serial write made no progress')
            sent += size
        header = self._read_exact(3, deadline)
        if header[0] != slave or header[1] not in (function, function | 0x80):
            raise ValueError('unexpected response address/function')
        if header[1] == function | 0x80:
            response = header + self._read_exact(2, deadline)
        else:
            if header[2] != count * 2:
                raise ValueError('unexpected response byte count')
            response = header + self._read_exact(count * 2 + 2, deadline)
        return decode_response(response, slave, function, count), response


def probe(args, port_factory=ReadOnlyPort):
    report = {
        'schema': 'brainco-serial-probe-v1',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'hostname': socket.gethostname(),
        'port': str(args.port), 'resolved_port': str(Path(args.port).resolve()),
        'baud': args.baud, 'slave_id': args.slave,
        'open_settle_s': OPEN_SETTLE_S,
        'read_only_register_operations': True, 'sdk_used': False,
        'protocol_source': SOURCE, 'transactions': [], 'ok': False,
    }
    results = {}
    try:
        with port_factory(str(args.port), args.baud, args.timeout) as port:
            for name, function, address, count in READS:
                start = time.monotonic()
                entry = {'name': name, 'function': function, 'address': address,
                         'count': count, 'request_hex': read_request(args.slave, function, address, count).hex()}
                report['transactions'].append(entry)
                try:
                    registers, raw = port.query(args.slave, function, address, count)
                except Exception as exc:
                    entry['error'] = '%s: %s' % (type(exc).__name__, exc)
                    raise
                entry.update(response_hex=raw.hex(), registers=registers,
                             elapsed_ms=(time.monotonic() - start) * 1000)
                results[name] = registers
                time.sleep(0.005)
            report['device'] = decode_results(results)
            report['ok'] = report['device']['identity_valid']
            if not report['ok']:
                report['error'] = 'Unrecognized hand-side or unit-mode register value'
    except (Exception, KeyboardInterrupt) as exc:
        report['ok'] = False
        report['error'] = '%s: %s' % (type(exc).__name__, exc)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, type=Path, help='Exact /dev/... port; no scan')
    parser.add_argument('--slave', required=True, type=int, help='Exact ID; defaults 126/127 do not prove side')
    parser.add_argument('--baud', type=int, default=460800, choices=(19200, 57600, 115200, 460800, 1000000))
    parser.add_argument('--timeout', type=float, default=1.0, help='Per transaction, 0.05..5 seconds')
    parser.add_argument('--plan', action='store_true', help='Print frames without opening port')
    args = parser.parse_args(argv)
    if not 1 <= args.slave <= 247:
        parser.error('--slave must be 1..247; broadcast is forbidden')
    if not math.isfinite(args.timeout) or not 0.05 <= args.timeout <= 5:
        parser.error('--timeout must be finite and within 0.05..5 seconds')
    if not args.port.is_absolute() or not str(args.port).startswith('/dev/'):
        parser.error('--port must be an absolute /dev/... path')
    if not hasattr(termios, 'B%d' % args.baud):
        parser.error('this platform does not support selected baud rate')
    if args.plan:
        print(json.dumps({'port_opened': False, 'port': str(args.port), 'slave_id': args.slave,
                          'requests': [{'name': name, 'function': fc, 'address': address,
                                        'count': count, 'hex': read_request(args.slave, fc, address, count).hex()}
                                       for name, fc, address, count in READS]}, indent=2))
        return 0
    report = probe(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
