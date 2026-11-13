"""Protocol rejection and PTY transport tests; never access physical serial ports."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from types import SimpleNamespace
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'brainco_serial_probe.py'
SPEC = importlib.util.spec_from_file_location('brainco_serial_probe', SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def response(slave, function, values):
    data = struct.pack('>' + 'H' * len(values), *values)
    body = bytes([slave, function, len(data)]) + data
    return body + probe.crc16(body).to_bytes(2, 'little')


def text_registers(value):
    return list(struct.unpack('>10H', value.encode('ascii').ljust(20, b'\0')))


VALUES = {
    3000: text_registers('V1.2.3'),
    3010: text_registers('BC-TEST-LEFT'),
    901: [2],
    937: [1],
    2000: [10, 20, 30, 40, 50, 60] + [65535, 0, 1, 2, 3, 4] + [65534] * 6 + [0, 1, 2, 3, 0, 0],
}


class ProbeTests(unittest.TestCase):
    def test_official_status_request_bytes(self):
        self.assertEqual(probe.read_request(126, 4, 2000, 24).hex(), '7e0407d00018fb42')
        self.assertEqual(probe.read_request(127, 4, 2000, 24).hex(), '7f0407d00018fa93')

    def test_write_functions_unlisted_reads_and_broadcast_are_rejected(self):
        for args in [(126, 6, 937, 1), (126, 16, 1010, 12), (126, 3, 900, 1),
                     (126, 4, 2000, 23), (0, 4, 2000, 24), (248, 4, 2000, 24)]:
            with self.assertRaises(ValueError):
                probe.read_request(*args)

    def test_crc_address_function_count_exception_and_trailing_data(self):
        valid = response(126, 3, [1])
        exception = bytes([126, 0x83, 2])
        exception += probe.crc16(exception).to_bytes(2, 'little')
        bad = [valid[:-1] + bytes([valid[-1] ^ 1]), response(127, 3, [1]),
               response(126, 4, [1]), response(126, 3, [1, 2]), exception, valid + b'\0']
        for frame in bad:
            with self.subTest(frame=frame.hex()), self.assertRaises(ValueError):
                probe.decode_response(frame, 126, 3, 1)

    def test_units_are_reported_without_reconfiguring_or_assumed_scaling(self):
        decoded = probe.decode_results({name: VALUES[address] for name, _, address, _ in probe.READS})
        self.assertEqual(decoded['hand_side'], 'left')
        self.assertEqual(decoded['unit_mode'], 'physical')
        self.assertEqual(decoded['positions_raw_u16'], [10, 20, 30, 40, 50, 60])
        self.assertEqual(decoded['speeds_raw_i16'][0], -1)
        self.assertEqual(decoded['currents_raw_i16'], [-2] * 6)
        self.assertTrue(decoded['raw_only'])

    def test_plan_needs_no_packages_or_existing_device(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--port', '/dev/does-not-exist',
                                 '--slave', '126', '--plan'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['port_opened'])

    def test_finite_bounds_and_exact_port_are_required(self):
        for args in [('--timeout', 'nan'), ('--timeout', '100'), ('--slave', '0'),
                     ('--port', '/tmp/regular-file')]:
            result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--port', '/dev/missing',
                                     '--slave', '126', *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def run_pty(self, mode):
        master, slave_fd = pty.openpty()
        port_path = os.ttyname(slave_fd)
        before = termios.tcgetattr(slave_fd)
        requests, errors = [], []
        stopped = threading.Event()

        def emulator():
            try:
                pending = bytearray()
                while not stopped.is_set():
                    if not select.select([master], [], [], 0.01)[0]:
                        continue
                    pending.extend(os.read(master, 64))
                    while len(pending) >= 8:
                        request = bytes(pending[:8])
                        del pending[:8]
                        requests.append(request)
                        sid, fc, address, count = struct.unpack('>BBHH', request[:6])
                        if probe.crc16(request) or (fc, address, count) not in probe.ALLOWED_READS:
                            raise AssertionError('unapproved serial request ' + request.hex())
                        if mode == 'timeout':
                            continue
                        frame = response(sid, fc, VALUES[address])
                        if mode == 'bad_crc':
                            frame = frame[:-1] + bytes([frame[-1] ^ 1])
                        # Deliberately fragment both header and body.
                        os.write(master, frame[:1])
                        time.sleep(0.001)
                        os.write(master, frame[1:4])
                        time.sleep(0.001)
                        os.write(master, frame[4:])
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=emulator, daemon=True)
        thread.start()
        try:
            args = SimpleNamespace(port=Path(port_path), baud=460800, slave=126, timeout=0.1)
            report = probe.probe(args)
            self.assertEqual(termios.tcgetattr(slave_fd), before)
            # The exclusive flag must have been released, even on failure.
            reopened = os.open(port_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            os.close(reopened)
        finally:
            stopped.set()
            thread.join(timeout=1)
            os.close(slave_fd)
            os.close(master)
        self.assertFalse(errors, errors)
        return report, requests

    def test_complete_fragmented_pty_read_and_settings_restoration(self):
        report, requests = self.run_pty('success')
        self.assertTrue(report['ok'], report)
        self.assertEqual(len(requests), 5)
        self.assertEqual(report['device']['serial_number'], 'BC-TEST-LEFT')
        self.assertTrue(all(frame[1] in (3, 4) for frame in requests))

    def test_timeout_stops_endpoint_before_same_shape_next_request(self):
        report, requests = self.run_pty('timeout')
        self.assertFalse(report['ok'])
        self.assertEqual(len(requests), 1)
        self.assertIn('TimeoutError', report['error'])

    def test_corrupt_crc_stops_endpoint_and_restores_settings(self):
        report, requests = self.run_pty('bad_crc')
        self.assertFalse(report['ok'])
        self.assertEqual(len(requests), 1)
        self.assertIn('CRC', report['error'])


if __name__ == '__main__':
    unittest.main()
