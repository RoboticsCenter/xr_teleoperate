#!/usr/bin/env python3
"""Read PC2 state over authenticated SSH with conservative age bounds; no output."""
import argparse
import base64
import getpass
import json
import math
from pathlib import Path
from network_config import robot_host
import shlex
import sys
import time

from wifi_feedback_time import FeedbackTimeBounds

ROOT = Path(__file__).resolve().parents[1]


class WifiFeedbackClient:
    def __init__(self, host=None, seconds=30, max_age_s=.1):
        host = robot_host() if host is None else host
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            raise ValueError('seconds must be in (0,120]')
        if host != robot_host():
            raise ValueError('This endpoint is bound to the verified PC2 Wi-Fi address')
        self.host, self.seconds = host, seconds
        self.bounds, self.process, self.sequence = FeedbackTimeBounds(max_age_s), None, 0
        self.ready = None

    def read_record(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.process.expect(r'\r?\n', timeout=max(.001, deadline-time.monotonic()))
            line = self.process.before.strip()
            try:
                value = json.loads(line)
            except ValueError:
                continue  # native diagnostics share SSH stderr/PTY, never counted as data
            if isinstance(value, dict) and str(value.get('type', '')).startswith('feedback_'):
                return value
        raise TimeoutError('No feedback JSON response')

    def open(self, password):
        import pexpect
        if self.process is not None:
            raise RuntimeError('feedback session already opened')
        source = (ROOT / 'scripts/pc2_feedback_rpc.py').read_bytes()
        encoded = base64.b64encode(source).decode()
        bootstrap = "import base64;exec(compile(base64.b64decode(" + repr(encoded) + "),'<pc2_feedback_rpc>','exec'))"
        remote = '/home/unitree/miniconda3/envs/tv/bin/python -u -B -c ' + shlex.quote(bootstrap)
        remote += ' --interface eth0 --seconds ' + shlex.quote(str(self.seconds))
        args = ['-T', '-o', 'ConnectTimeout=5', '-o', 'NumberOfPasswordPrompts=1',
                '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=192.168.123.164',
                '-o', 'UserKnownHostsFile=' + str(ROOT / 'diagnostics/g1-known_hosts'),
                'unitree@' + self.host, remote]
        self.process = pexpect.spawn('ssh', args, encoding='utf-8', echo=False, timeout=12, maxread=65536)
        self.process.delaybeforesend = 0
        try:
            self.process.expect('[Pp]assword:', timeout=8)
            self.process.sendline(password)
            self.ready = self.read_record(12)
            if self.ready.get('type') != 'feedback_ready':
                raise RuntimeError('Remote feedback reader did not become ready: ' + str(self.ready))
            return self.ready
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        if self.process is None or self.ready is None:
            raise RuntimeError('feedback session is not open')
        self.sequence += 1
        sent = time.monotonic_ns()
        self.process.sendline(json.dumps({'request_id': self.sequence}))
        reply = self.read_record(2)
        received = time.monotonic_ns()
        if reply.get('type') == 'feedback_error':
            raise RuntimeError(reply.get('error'))
        result = self.bounds.convert(reply, self.sequence, sent, received)
        result.update(type='wifi_feedback', local_request_sent_ns=sent,
                      local_reply_received_ns=received, motion_ready=False)
        return result

    def close(self):
        if self.process is None:
            return
        import pexpect
        try:
            if self.process.isalive():
                self.process.sendeof()
                self.process.expect(pexpect.EOF, timeout=4)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
        finally:
            self.process.close(force=True)
            self.process = self.ready = None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds', type=float, default=10)
    p.add_argument('--rate-hz', type=float, default=10)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 100:
        p.error('seconds must be in (0,100]')
    if not math.isfinite(args.rate_hz) or not 0 < args.rate_hz <= 20:
        p.error('rate must be in (0,20]')
    client = WifiFeedbackClient(seconds=args.seconds+10)
    try:
        client.open(getpass.getpass('PC2 SSH password: '))
        start = time.monotonic()
        with args.output.open('w') as output:
            while time.monotonic()-start < args.seconds:
                value = client.snapshot()
                line = json.dumps(value, allow_nan=False)
                print(line, file=output, flush=True)
                print(line, flush=True)
                time.sleep(1/args.rate_hz)
    finally:
        client.close()


if __name__ == '__main__':
    main()
