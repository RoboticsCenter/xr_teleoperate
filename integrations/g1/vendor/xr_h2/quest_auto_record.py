"""Automatic wrist-camera/joint recording for Quest sessions; no robot commands."""
import json
import os
from pathlib import Path
import tempfile
import threading


class AutoRecording:
    def __init__(self, recorder, state_reader, settings_path, start_worker=True):
        self.recorder, self.state_reader = recorder, state_reader
        self.settings_path = Path(settings_path)
        self.lock = threading.RLock()
        self.wake, self.done = threading.Event(), threading.Event()
        self.enabled, self.error = True, ''
        self.session = self.latched = None
        self.owned = self.paused = self.failed = False
        try:
            settings = json.loads(self.settings_path.read_text())
            if not isinstance(settings, dict) or set(settings) != {'enabled'} or type(settings['enabled']) is not bool:
                raise ValueError('Expected {"enabled": true or false}')
            self.enabled = settings['enabled']
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            self.enabled, self.error = False, f'Auto recording settings: {exc}'
        self._publish()
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

    def _publish(self):
        if self.error:
            message = self.error
        elif not self.enabled:
            message = 'Auto REC is off. Manual REC is available.'
        elif self.paused:
            message = 'Auto REC paused for this session; resumes at the next Quest START.'
        elif self.owned:
            message = 'Auto REC: wrist cameras + joints; saves when teleop exits.'
        elif self.session:
            message = 'Auto REC: using the existing manual recording; manual STOP REC ends it.'
        else:
            message = 'Auto REC enabled: starts with Quest START, saves when teleop exits.'
        self._status = {'enabled': self.enabled, 'session_active': self.session is not None,
                        'owned': self.owned, 'paused': self.paused, 'error': self.error,
                        'message': message}

    def status(self):
        return dict(self._status)

    def notify(self):
        self.wake.set()

    def _stop_owned(self):
        if self.owned and self.recorder.running():
            self.recorder.stop()
        self.owned = False

    def update(self, state):
        """Latch START until process exit; browser/heartbeat loss never ends a take."""
        with self.lock:
            identity = state.get('session') if state.get('running') else None
            if identity is None:
                self.latched = None
            elif state.get('started'):
                self.latched = identity
            elif self.latched != identity:
                self.latched = None
            active = self.latched
            if active != self.session:
                self._stop_owned()
                self.session = active
                self.paused = self.failed = False
                self.error = ''
            if not self.enabled or active is None or self.paused or self.failed:
                self._publish()
                return
            if self.recorder.running():
                self._publish()
                return
            if self.owned:
                process = self.recorder.process
                self.owned = False
                if process is None or process.poll() != 0:
                    self.failed = True
                    self.error = 'Automatic recorder exited unexpectedly. Check recorder.log or use manual REC.'
                    self._publish()
                    return
                # Normal 20-minute completion: continue in a new segment.
            try:
                ok, message = self.recorder.start()
            except Exception as exc:
                ok, message = False, str(exc)
            self.owned = ok
            if not ok:
                self.failed = True
                self.error = f'Auto REC could not start: {message}'
            self._publish()

    def _run(self):
        while not self.done.is_set():
            try:
                state = self.state_reader()  # Never hold our lock while reading the teleop lock.
                self.update(state)
            except Exception as exc:
                with self.lock:
                    self.error = f'Auto REC status error: {exc}'
                    self._publish()
            self.wake.wait(.25)
            self.wake.clear()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be true or false')
        with self.lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile('w', dir=self.settings_path.parent, delete=False) as target:
                    temporary = target.name
                    json.dump({'enabled': enabled}, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.settings_path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            self.enabled, self.error = enabled, ''
            self.paused = self.failed = False
            if not enabled:
                self._stop_owned()
            self._publish()
        self.notify()
        return True, 'Auto REC enabled.' if enabled else 'Auto REC disabled; manual REC is available.'

    def manual_start(self):
        with self.lock:
            ok, message = self.recorder.start()
            if ok:
                self.owned = False
                self.paused = self.session is not None
                self.error = ''
            self._publish()
            return ok, message

    def manual_stop(self):
        with self.lock:
            self.owned = False
            self.paused = self.session is not None
            result = self.recorder.stop()
            self._publish()
            return result

    def close(self):
        self.done.set()
        self.notify()
        if self.worker:
            self.worker.join(timeout=12)
        with self.lock:
            self._stop_owned()
            self._publish()
