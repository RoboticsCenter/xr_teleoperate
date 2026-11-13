"""Owned LAUNCH/START/STOP worker lifecycle; no SDK imports or session time limit."""
from collections import deque
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

CONTROL_SCRIPTS = {'dashboard_worker.py', 'start_right_arm.py', 'run_right_arm_teleop.py',
                   'run_live_arm_preview.py', 'pico_raw_probe.py', 'g1_right_arm_runtime.py',
                   'arm_teleop.py', 'teleop_hand_and_arm.py', 'pico_probe.py',
                   'preview_pico_single_arm.py', 'run_wuji_hand_session.py',
                   'wuji_probe.py', 'wuji_quality_probe.py'}


def _identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _conflicts():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = [x.decode(errors='replace') for x in (entry / 'cmdline').read_bytes().split(b'\0') if x]
            if argv and 'python' in Path(argv[0]).name:
                names = {Path(arg).name for arg in argv[1:]} & CONTROL_SCRIPTS
                if names:
                    found.append(f'{sorted(names)[0]} (pid {entry.name})')
        except OSError:
            pass
    return found


def _clean(value, password):
    if isinstance(value, str):
        if password:
            for representation in (password, json.dumps(password)[1:-1], repr(password)[1:-1]):
                value = value.replace(representation, '[redacted]')
        return value
    if isinstance(value, list):
        return [_clean(x, password) for x in value]
    if isinstance(value, dict):
        return {k: _clean(v, password) for k, v in value.items()
                if k.lower() not in ('ssh_password', 'password', 'credential', 'credentials')}
    return value


class SessionManager:
    def __init__(self, root, python=sys.executable, password='', simulation=False,
                 *, launch_timeout_s=60., stop_timeout_s=10.):
        if not isinstance(password, str) or type(simulation) is not bool or not 0 < launch_timeout_s <= 180 or not 0 < stop_timeout_s <= 30:
            raise ValueError('invalid session configuration')
        self.root, self.python = Path(root).resolve(), str(python)
        self._password, self.simulation = password, simulation
        self.launch_timeout_s, self.stop_timeout_s = launch_timeout_s, stop_timeout_s
        self._lock, self._stdin_lock = threading.RLock(), threading.Lock()
        self._process = self._lock_file = self._reader = self._monitor = self._log = None
        self._born = self._ended = self._stop_at = self._term_at = None
        self._owned, self._tail = {}, deque(maxlen=24)
        self._closed = self._final_seen = self._prepared = False
        self._state = self._initial()

    def _initial(self):
        return dict(running=False, stage='idle', elapsed_s=0., session_id=None,
                    output_dir=None, started_at=None, error=None, actual_write_count=None,
                    accepted_targets=0, completed=False, stop_requested=False,
                    start_requested=False, simulation=self.simulation,
                    last_action_unknown=None, exit_code=None)

    def _write(self, record):
        with self._stdin_lock:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError('worker is not running')
            self._process.stdin.write(json.dumps(record, allow_nan=False) + '\n')
            self._process.stdin.flush()

    def launch(self, options=None):
        with self._lock:
            if self._closed or self._state['running']:
                raise RuntimeError('manager is closed or a session is already running')
            if not self.simulation and (not isinstance(self._password, str) or not self._password
                    or any(c in self._password for c in ('\n', '\r', '\0'))):
                raise ValueError('PC2 credentials are not configured')
            runs = self.root / 'diagnostics' / 'dashboard-runs'
            runs.mkdir(parents=True, exist_ok=True)
            handle = (runs.parent / 'dashboard-session.lock').open('a+')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise RuntimeError('another dashboard session owns the robot input') from None
            try:
                conflicts = [] if self.simulation else _conflicts()
                if conflicts:
                    raise RuntimeError('another control/input process is active: ' + ', '.join(conflicts))
                sid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
                output = runs / sid
                argv = [self.python, '-u', '-B', str(self.root / 'scripts/dashboard_worker.py'),
                        '--output-dir', str(output)] + (['--simulation'] if self.simulation else [])
                self._log = (runs / (sid + '.worker.log')).open('w', encoding='utf-8')
                self._process = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                    errors='replace', bufsize=1,
                    start_new_session=True, close_fds=True)
                self._lock_file = handle
                self._state = self._initial()
                self._state.update(running=True, stage='preparing', session_id=sid,
                    output_dir=str(output), started_at=datetime.now(timezone.utc).isoformat())
                self._born, self._ended = time.monotonic(), None
                self._stop_at = self._term_at = None
                self._owned, self._final_seen, self._prepared = {}, False, False
                self._tail.clear()
                self._track_children()
                self._write({'ssh_password': self._password if not self.simulation else '', 'session_options': options or {}})
                self._reader = threading.Thread(target=self._read_output, args=(self._process,), daemon=True)
                self._monitor = threading.Thread(target=self._watch, daemon=True)
                self._reader.start()
                self._monitor.start()
                return self.status()
            except BaseException:
                if self._process is not None and self._process.poll() is None:
                    self._signal_owned(signal.SIGKILL)
                    self._process.wait(timeout=2)
                if self._process is not None:
                    for stream in (self._process.stdin, self._process.stdout):
                        if stream:
                            try:
                                stream.close()
                            except OSError:
                                pass
                handle.close()
                self._lock_file = None
                if self._log:
                    self._log.close()
                    self._log = None
                self._state.update(running=False, stage='error')
                raise

    def start(self):
        with self._lock:
            if not self.status()['can_start']:
                raise RuntimeError('LAUNCH must reach READY before START')
            self._write({'command': 'start'})
            self._state['start_requested'] = True
            return self.status()

    def stop(self):
        with self._lock:
            if not self._state['running'] or self._state['stop_requested']:
                return self.status()
            self._state.update(stop_requested=True, stage='stopping')
            self._stop_at = time.monotonic()
            try:
                self._write({'command': 'stop'})
            except (OSError, RuntimeError):
                self._signal_owned(signal.SIGTERM, worker_only=True)
                self._term_at = time.monotonic()
            return self.status()

    def status(self):
        with self._lock:
            value = dict(self._state)
            value.update(can_launch=not value['running'] and not self._closed,
                can_stop=value['running'] and not value['stop_requested'],
                can_start=value['running'] and value['stage'] == 'ready'
                    and not value['stop_requested'] and not value['start_requested'],
                elapsed_s=round(max(0., (self._ended or time.monotonic()) - self._born), 2) if self._born else 0.,
                log_tail=list(self._tail))
            return _clean(value, self._password)

    def _read_output(self, process):
        written = 0
        for line in process.stdout:
            line = line.rstrip('\r\n')
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                record = None
            safe = _clean(record if isinstance(record, dict) else line, self._password)
            display = json.dumps(safe, ensure_ascii=False) if isinstance(safe, dict) else safe
            with self._lock:
                self._tail.append(display[:2000])
                if self._log and written < 2_000_000:
                    self._log.write(display[:10000] + '\n')
                    self._log.flush()
                    written += len(display[:10000]) + 1
                if isinstance(safe, dict):
                    self._accept(safe)
        process.stdout.close()

    def _accept(self, record):
        kind = record.get('type')
        if kind not in ('state', 'final') or self._final_seen:
            return
        for name in ('actual_write_count', 'accepted_targets'):
            count = record.get(name)
            if type(count) is int and count >= 0:
                self._state[name] = count
        for name in ('error', 'message'):
            if name in record and (record[name] is None or isinstance(record[name], str)):
                self._state[name] = record[name]
        if type(record.get('start_requested')) is bool and not self._state['stop_requested']:
            self._state['start_requested'] = record['start_requested']
        if type(record.get('physical_stop_verified')) is bool:
            self._state['physical_stop_verified'] = record['physical_stop_verified']
        if type(record.get('last_action_unknown')) is bool:
            self._state['last_action_unknown'] = record['last_action_unknown']
        for name in ('control_mode', 'control_topic', 'stop_behavior'):
            if isinstance(record.get(name), str):
                self._state[name] = record[name][:80]
        for name in ('control_verified', 'arm_weight_released'):
            if type(record.get(name)) is bool:
                self._state[name] = record[name]
        stage = record.get('stage')
        if isinstance(stage, str) and stage:
            self._prepared |= stage in ('ready', 'following')
            if not self._state['stop_requested']:
                self._state['stage'] = stage[:80]
        if kind == 'final':
            self._final_seen = True
            self._final_at = time.monotonic()
            self._state['completed'] = record.get('completed') is True

    def _track_children(self):
        """Remember descendants and PID start times, including children of threads."""
        if self._process is None:
            return
        pending, visited = [self._process.pid, *self._owned], set()
        while pending:
            pid = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            identity = _identity(pid)
            if not identity or (pid in self._owned and self._owned[pid] != identity[0]):
                continue
            self._owned[pid] = identity[0]
            try:
                for task in Path(f'/proc/{pid}/task').iterdir():
                    try:
                        pending.extend(int(p) for p in (task / 'children').read_text().split())
                    except (OSError, ValueError):
                        pass
            except OSError:
                pass

    def _signal_owned(self, signum, worker_only=False):
        self._track_children()
        for pid, started in list(self._owned.items()):
            if worker_only and pid != self._process.pid:
                continue
            identity = _identity(pid)
            if identity and identity[0] == started and identity[1] != 'Z':
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass

    def _watch(self):
        process = self._process
        while process.poll() is None:
            with self._lock:
                self._track_children()
                now = time.monotonic()
                if not self._prepared and not self._state['stop_requested'] and now - self._born > self.launch_timeout_s:
                    self._state['error'] = 'worker did not become ready before the startup deadline'
                    self.stop()
                if self._final_seen and not self._state['stop_requested'] and now - self._final_at > 2:
                    self.stop()
                if self._stop_at and self._term_at is None and now - self._stop_at > self.stop_timeout_s:
                    self._signal_owned(signal.SIGTERM, worker_only=True)
                    self._term_at = now
                if self._term_at and now - self._term_at > 2:
                    self._signal_owned(signal.SIGKILL)
            time.sleep(.05)
        self._reader.join(timeout=1)
        # A worker crash must not leave its observed children running. PID identity
        # is checked for each signal; no process-name-based killing is permitted.
        with self._lock:
            self._signal_owned(signal.SIGTERM)
        cleanup_deadline = time.monotonic() + 1
        while time.monotonic() < cleanup_deadline:
            if not any((ident := _identity(pid)) and ident[0] == born and ident[1] != 'Z'
                       for pid, born in self._owned.items()):
                break
            time.sleep(.05)
        with self._lock:
            self._signal_owned(signal.SIGKILL)
        self._reader.join(timeout=1)
        with self._lock:
            code = process.returncode
            if not self._final_seen or code != 0:
                self._state.update(completed=False, last_action_unknown=True, actual_write_count=None)
                self._state['error'] = self._state['error'] or (
                    f'worker exited ({code}) without a confirmed final result')
            self._state.update(running=False, exit_code=code)
            self._state['stage'] = ('error' if self._state['error'] else
                'stopped' if self._state['stop_requested'] else 'finished')
            self._ended = time.monotonic()
            try:
                output = Path(self._state['output_dir'])
                output.mkdir(parents=True, exist_ok=True)
                summary = self.status()
                summary.pop('log_tail', None)
                (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
            except OSError as exc:
                self._tail.append('Could not save session summary: ' + str(exc))
            if self._log:
                self._log.close()
                self._log = None
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None

    def close(self):
        with self._lock:
            self._closed = True
            self.stop()
            monitor = self._monitor
        if monitor and monitor is not threading.current_thread():
            monitor.join(timeout=self.stop_timeout_s + 5)
            if monitor.is_alive():
                with self._lock:
                    self._signal_owned(signal.SIGKILL)
                monitor.join(timeout=3)
