"""Session recording policy with fake processes; no camera or robot connection."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quest_auto_record import AutoRecording


class FakeProcess:
    def __init__(self):
        self.returncode = None
    def poll(self):
        return self.returncode


class FakeRecorder:
    def __init__(self):
        self.process = None
        self.starts = self.stops = 0
        self.error = None
    def running(self):
        return self.process is not None and self.process.poll() is None
    def start(self):
        if self.error:
            raise OSError(self.error)
        if self.running():
            return False, 'Already recording'
        self.starts += 1
        self.process = FakeProcess()
        return True, 'Recording'
    def stop(self):
        if not self.running():
            return False, 'Not recording'
        self.stops += 1
        self.process.returncode = 0
        return True, 'Saved'
    def status(self):
        return {'recording': self.running(), 'takes': self.starts}


IDLE = {'running': False, 'session': None, 'started': False}
READY = {'running': True, 'session': (42,), 'started': False}
FOLLOWING = dict(READY, started=True)


class AutomaticRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rec = FakeRecorder()
        self.path = Path(self.temp.name) / 'settings.json'
        self.auto = AutoRecording(self.rec, lambda: IDLE, self.path, start_worker=False)

    def test_idle_or_launched_ready_does_not_record(self):
        for state in (IDLE, READY, READY):
            self.auto.update(state)
        self.assertEqual(self.rec.starts, 0)
        self.assertTrue(self.auto.status()['enabled'])

    def test_start_records_once_and_exit_saves(self):
        for _ in range(5):
            self.auto.update(FOLLOWING)
        self.assertEqual(self.rec.starts, 1)
        self.assertTrue(self.auto.status()['owned'])
        self.auto.update(IDLE)
        self.assertEqual(self.rec.stops, 1)
        self.assertFalse(self.auto.status()['session_active'])

    def test_lost_heartbeat_or_stop_flag_keeps_recording_until_process_exits(self):
        self.auto.update(FOLLOWING)
        for _ in range(5):
            self.auto.update(READY)
        self.assertTrue(self.rec.running())
        self.assertEqual(self.rec.stops, 0)

    def test_manual_recording_is_not_stopped_at_teleop_exit(self):
        self.auto.manual_start()
        self.auto.update(FOLLOWING)
        self.auto.update(IDLE)
        self.assertTrue(self.rec.running())
        self.assertEqual(self.rec.starts, 1)
        self.assertEqual(self.rec.stops, 0)

    def test_manual_stop_pauses_this_session_but_next_session_records(self):
        self.auto.update(FOLLOWING)
        self.auto.manual_stop()
        for _ in range(3):
            self.auto.update(FOLLOWING)
        self.assertFalse(self.rec.running())
        self.assertTrue(self.auto.status()['paused'])
        self.auto.update(IDLE)
        self.auto.update(dict(FOLLOWING, session=(43,)))
        self.assertEqual(self.rec.starts, 2)
        self.assertTrue(self.rec.running())

    def test_normal_segment_completion_continues_but_crash_does_not_retry(self):
        self.auto.update(FOLLOWING)
        self.rec.process.returncode = 0
        self.auto.update(FOLLOWING)
        self.assertEqual(self.rec.starts, 2)
        self.rec.process.returncode = 1
        for _ in range(3):
            self.auto.update(FOLLOWING)
        self.assertEqual(self.rec.starts, 2)
        self.assertIn('exited unexpectedly', self.auto.status()['error'])
        self.auto.update(IDLE)
        self.auto.update(dict(FOLLOWING, session=(43,)))
        self.assertEqual(self.rec.starts, 3)

    def test_start_error_is_visible_and_does_not_spin(self):
        self.rec.error = 'disk unavailable'
        self.auto.update(FOLLOWING)
        self.assertIn('disk unavailable', self.auto.status()['error'])
        self.rec.error = None
        self.auto.update(FOLLOWING)
        self.assertEqual(self.rec.starts, 0)
        self.assertTrue(self.auto.manual_start()[0])
        self.assertTrue(self.rec.running())

    def test_new_process_between_polls_ends_old_take_and_waits_for_its_start(self):
        self.auto.update(FOLLOWING)
        self.auto.update(dict(READY, session=(43,)))
        self.assertEqual(self.rec.stops, 1)
        self.assertFalse(self.rec.running())
        self.auto.update(dict(FOLLOWING, session=(43,)))
        self.assertEqual(self.rec.starts, 2)

    def test_enabled_setting_persists_and_enable_mid_session_starts_recording(self):
        self.auto.set_enabled(False)
        self.auto.update(FOLLOWING)
        self.assertFalse(self.rec.running())
        restarted = AutoRecording(self.rec, lambda: IDLE, self.path, start_worker=False)
        self.assertFalse(restarted.status()['enabled'])
        self.auto.set_enabled(True)
        self.auto.update(FOLLOWING)
        self.assertTrue(self.rec.running())
        self.auto.set_enabled(False)
        self.assertFalse(self.rec.running())

    def test_disabling_auto_leaves_manual_recording_running(self):
        self.auto.manual_start()
        self.auto.update(FOLLOWING)
        self.auto.set_enabled(False)
        self.assertTrue(self.rec.running())

    def test_invalid_config_or_nonboolean_setting_is_rejected(self):
        self.path.write_text('{bad json')
        obj = AutoRecording(self.rec, lambda: IDLE, self.path, start_worker=False)
        self.assertFalse(obj.status()['enabled'])
        self.assertIn('settings', obj.status()['error'])
        for value in (1, 'true', None, []):
            with self.assertRaises(ValueError):
                self.auto.set_enabled(value)

    def test_supervisor_works_without_browser_requests(self):
        state = dict(IDLE)
        obj = AutoRecording(self.rec, lambda: dict(state), self.path)
        try:
            state.update(FOLLOWING)
            obj.notify()
            end = time.monotonic() + 2
            while not self.rec.running() and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue(self.rec.running())
            state.update(READY)
            time.sleep(.3)
            self.assertTrue(self.rec.running())
            state.update(IDLE)
            obj.notify()
            end = time.monotonic() + 2
            while self.rec.running() and time.monotonic() < end:
                time.sleep(.01)
            self.assertFalse(self.rec.running())
        finally:
            obj.close()


if __name__ == '__main__':
    unittest.main()
