"""Laptop SSH transport for the managed per-hand BrainCo service."""
import base64
import io
import json
import math
from pathlib import Path
import shlex
import threading
import time
import zipfile
from network_config import robot_host

ROOT = Path(__file__).resolve().parents[1]


def _remote_failure(error):
    """Return only owned messages, never a remote exception or SSH transcript."""
    reasons = {
        'an existing BrainCo service already owns the serial devices':
            'Another BrainCo service owns the hand ports; stop that service before retrying.',
        'sudo authentication failed':
            'G1 sudo authentication failed; check the robot credential before retrying.',
        'neither verified hand port is present':
            'Neither hand serial port is present on G1; check hand power and USB connections.',
        'serial devices are occupied or occupancy check failed':
            'G1 hand ports are occupied or could not be checked; check the existing hand service.',
        'owned BrainCo service exited':
            'The BrainCo service exited; check its startup log on G1.',
    }
    if isinstance(error, str):
        reason = error.removeprefix('RuntimeError: ')
        if reason in reasons:
            return reasons[reason]
        if reason.startswith('audited service dependency differs: '):
            return 'G1 hand service files differ from the audited version; check the deployed service before retrying.'
        for prefix, message in (
            ('BlockingIOError:', 'Another dashboard hand bridge holds the service lock; stop that bridge before retrying.'),
            ('ModuleNotFoundError:', 'A G1 hand bridge Python dependency is missing; check the robot runtime.'),
            ('ImportError:', 'A G1 hand bridge Python dependency could not load; check the robot runtime.'),
            ('FileNotFoundError:', 'A required G1 hand service file or program is missing; check the robot deployment.'),
            ('PermissionError:', 'G1 denied access to a hand service resource; check its permissions.'),
            ('CalledProcessError:', 'G1 hand service setup failed; check its build and startup logs.'),
            ('TimeoutExpired:', 'G1 hand service setup timed out; check its build and startup logs.'),
        ):
            if error.startswith(prefix):
                return message
    return 'The remote hand bridge failed; check the hand service startup log on G1.'


class _OpenError(RuntimeError):
    """An initialization reason composed entirely from local fixed text."""


def remote_command(root, enable_commands, max_step, feedback_max_age_s):
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(root/'scripts/pc2_hand_bridge.py', 'pc2_hand_bridge.py')
        archive.write(root/'patches/brainco-dashboard-hold-resume.patch', 'service.patch')
    encoded = base64.b64encode(archive_bytes.getvalue()).decode()
    arguments = ['--start-service', '--interface', 'eth0', '--max-step', str(max_step),
                 '--feedback-max-age-s', str(feedback_max_age_s)]
    if enable_commands:
        arguments.append('--enable-commands')
    bootstrap = ('import base64,io,zipfile,tempfile,sys,runpy; '
        "d=tempfile.TemporaryDirectory(prefix='g1-hand-'); "
        f'zipfile.ZipFile(io.BytesIO(base64.b64decode({encoded!r}))).extractall(d.name); '
        f"sys.argv=['pc2_hand_bridge.py','--patch',d.name+'/service.patch']+{arguments!r}; "
        "runpy.run_path(d.name+'/pc2_hand_bridge.py',run_name='__main__')")
    return ' '.join(shlex.quote(x) for x in
                    ['/home/unitree/miniconda3/envs/tv/bin/python', '-u', '-B', '-c', bootstrap])


class HandTransport:
    def __init__(self, root=ROOT, password='', *, max_step=.1, feedback_max_age_s=.1, spawn=None):
        if not isinstance(password, str) or any(c in password for c in ('\n', '\r', '\0')):
            raise ValueError('invalid SSH credential')
        if not math.isfinite(max_step) or not 0 < max_step <= .1 or not math.isfinite(feedback_max_age_s) or not 0 < feedback_max_age_s <= .1:
            raise ValueError('hand limits cannot exceed the original maximums')
        self.root, self._password = Path(root), password
        self.max_step, self.max_age = max_step, feedback_max_age_s
        self._spawn = spawn
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._external_stop = None
        self._protocol_sent = False
        self.process = self._pexpect = None
        self.command_mode = False
        self._request_id = 0
        self.enabled = dict(left=False, right=False)
        self.final = self.ready = None

    def _cancelled(self):
        return self._stop.is_set() or (self._external_stop is not None and self._external_stop.is_set())

    def _read(self, timeout=5., *, closing=False):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self._cancelled() and not closing:
                raise RuntimeError('hand transport opening/request cancelled')
            try:
                self.process.expect(r'\r?\n', timeout=min(.25, max(.001, deadline-time.monotonic())))
            except self._pexpect.TIMEOUT:
                continue
            except self._pexpect.EOF:
                raise RuntimeError('hand bridge connection closed') from None
            line = self.process.before.strip()
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get('type') == 'final':
                if record.get('error'):
                    record['error'] = _remote_failure(record['error'])
                if record.get('cleanup_errors'):
                    record['cleanup_errors'] = [_remote_failure(item) for item in record['cleanup_errors']]
                self.final = record
            return record
        raise TimeoutError('hand bridge response timed out')

    def _open_failure(self, exc, phase):
        if self._cancelled():
            return 'Opening the hand connection was cancelled.'
        if isinstance(exc, _OpenError):
            return str(exc)
        # Only recognize fixed SSH diagnostics. Never interpolate this buffer or
        # str(exc): pexpect exceptions can embed the command and credentials.
        transcript = getattr(self.process, 'before', '')
        if isinstance(transcript, str):
            transcript = transcript.lower()
            for needles, message in (
                (('permission denied',), 'G1 SSH authentication failed; check the robot credential.'),
                (('host key verification failed', 'remote host identification has changed'),
                 'G1 SSH identity could not be verified; verify the robot host key before retrying.'),
                (('connection refused',), 'G1 SSH is not accepting connections yet; wait for the robot to boot, then retry.'),
                (('no route to host', 'network is unreachable'),
                 'G1 is unreachable over Wi-Fi; check robot power and its network connection.'),
                (('connection timed out', 'operation timed out'),
                 'Connecting to G1 timed out; check robot power and its Wi-Fi connection.'),
                (('connection reset', 'broken pipe', 'connection closed by'),
                 'The G1 SSH connection was interrupted; check robot power and Wi-Fi, then retry.'),
            ):
                if any(needle in transcript for needle in needles):
                    return message
        if phase == 'prepare':
            return 'The laptop could not prepare the hand bridge; check its runtime and bridge files.'
        if phase == 'spawn':
            return 'The laptop could not start SSH; check that the SSH client is installed and available.'
        if isinstance(exc, TimeoutError):
            if phase == 'authenticate':
                return 'G1 SSH authentication timed out; check robot power and its Wi-Fi connection.'
            return 'The G1 hand bridge did not become ready in time; check hand power, USB and the service startup log.'
        if phase == 'authenticate':
            return 'G1 SSH closed before authentication; check robot power, Wi-Fi and SSH availability.'
        return 'The G1 hand bridge connection closed before readiness; check robot power, Wi-Fi and its service startup log.'

    def open(self, enable_commands=False, *, cancel_event=None):
        if type(enable_commands) is not bool:
            raise ValueError('enable_commands must be an explicit bool')
        with self._lock:
            self._external_stop = cancel_event
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('hand transport opening cancelled')
            if self.process is not None:
                if self.command_mode == enable_commands and self.process.isalive():
                    return self.ready
                # Upgrade replaces only the feedback service owned by this object.
                try:
                    self.close()
                except Exception:
                    raise RuntimeError('hand bridge initialization failed: The previous hand connection '
                        'could not be closed cleanly; check the hand service on G1 before retrying.') from None
            self._stop.clear()
            if self._cancelled():
                raise RuntimeError('hand transport opening cancelled')
            self._protocol_sent = False
            self.final = self.ready = None
            phase = 'prepare'
            try:
                import pexpect
                self._pexpect = pexpect
                command = remote_command(self.root, enable_commands, self.max_step, self.max_age)
                args = ['-T', '-o', 'ConnectTimeout=5', '-o', 'NumberOfPasswordPrompts=1',
                    '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=3',
                    '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=192.168.123.164',
                    '-o', 'UserKnownHostsFile='+str(self.root/'diagnostics/g1-known_hosts'),
                    'unitree@'+robot_host(self.root), command]
                phase = 'spawn'
                self.process = (self._spawn or pexpect.spawn)('ssh', args, encoding='utf-8', echo=False,
                    timeout=10, maxread=65536)
                self.process.delaybeforesend = 0
                phase = 'authenticate'
                authentication_deadline = time.monotonic()+8
                while True:
                    if self._cancelled():
                        raise RuntimeError('hand authentication cancelled')
                    try:
                        self.process.expect('[Pp]assword:', timeout=.25)
                        break
                    except pexpect.TIMEOUT:
                        if time.monotonic() >= authentication_deadline:
                            raise TimeoutError('hand authentication timed out') from None
                self.process.sendline(self._password)
                self.process.sendline(json.dumps({'sudo_password': self._password}))
                self._protocol_sent = True
                phase = 'initialize'
                deadline = time.monotonic()+130
                while time.monotonic() < deadline:
                    record = self._read(max(.001, deadline-time.monotonic()))
                    if record.get('type') == 'ready':
                        self.command_mode, self.ready = enable_commands, record
                        self.enabled = dict(left=enable_commands, right=enable_commands)
                        return record
                    if record.get('type') == 'final':
                        raise _OpenError(record.get('error') or
                            'The G1 hand bridge exited before readiness; check its startup log.')
                raise TimeoutError('hand bridge initialization timed out')
            except BaseException as exc:
                reason = self._open_failure(exc, phase)
                try:
                    self.close()
                except Exception:
                    pass
                raise RuntimeError('hand bridge initialization failed: '+reason) from None

    def _request(self, command):
        with self._lock:
            if self.process is None or not self.process.isalive():
                raise RuntimeError('hand transport is not open')
            self._request_id += 1
            rid = self._request_id
            self.process.sendline(json.dumps(dict(command, request_id=rid), allow_nan=False))
            deadline = time.monotonic()+5
            while True:
                record = self._read(max(.001, deadline-time.monotonic()))
                if record.get('type') == 'final':
                    raise RuntimeError('hand bridge ended: '+str(record.get('error')))
                if record.get('request_id') == rid:
                    return record
                if time.monotonic() >= deadline:
                    raise TimeoutError('hand bridge response mismatch')

    def snapshot(self):
        if self.process is None:
            return {}
        before = time.monotonic()
        result = self._request({'command': 'snapshot'})
        elapsed = time.monotonic()-before
        hands = {}
        for side, sample in result.get('hands', {}).items():
            age = sample.get('age_s')
            if side not in self.enabled or type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                continue
            # Clocks differ; full RTT is a conservative extra age, not subtraction
            # of laptop and robot monotonic timestamps.
            hands[side] = dict(sample, age_s=age+elapsed,
                fresh=sample.get('fresh') is True and age+elapsed <= self.max_age)
            hands[side].pop('received', None)
        return hands

    def enable(self, side, enabled=True):
        if side not in self.enabled or type(enabled) is not bool:
            raise ValueError('expected a hand side and bool')
        with self._lock:
            self.enabled[side] = enabled

    def hold(self, side):
        """Pause new targets for this side; retain the service's last target."""
        self.enable(side, False)

    def send(self, side, q, speed, *, should_send=None):
        with self._lock:
            if side not in self.enabled:
                raise ValueError('unknown hand side')
            if not self.command_mode or not self.enabled[side]:
                return dict(published=False, side=side, error='hand side is paused or command mode is disabled')
            if self._cancelled() or (should_send is not None and not should_send()):
                return dict(published=False, side=side, error='hand target cancelled')
            if type(speed) in (int, float):
                speed = [speed]*6
            return self._request(dict(command='target', side=side, q=q, speed=speed))

    def close(self):
        self._stop.set()
        with self._lock:
            process = self.process
            if process is None:
                return self.final
            try:
                if process.isalive() and self._protocol_sent:
                    process.sendline(json.dumps({'command': 'stop'}))
                    deadline = time.monotonic()+9
                    while self.final is None and process.isalive() and time.monotonic() < deadline:
                        try:
                            self._read(max(.001, deadline-time.monotonic()), closing=True)
                        except (RuntimeError, TimeoutError):
                            break
            finally:
                try:
                    process.close(force=True)
                finally:
                    self.process = self.ready = None
                    self._protocol_sent = False
                    self.command_mode = False
                    self.enabled = dict(left=False, right=False)
            return self.final
