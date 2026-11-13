"""Process lifecycle checks using inert local workers; no SDK or network access."""
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dashboard_session as session


FAKE_WORKER = '''
import json, signal, sys, time
mode = MODE
credential = json.loads(sys.stdin.readline())
def emit(**record):
    print(json.dumps(record), flush=True)
print('diagnostic ' + credential['ssh_password'], flush=True)
emit(type='diagnostic', ssh_password=credential['ssh_password'], nested={'password': credential['ssh_password']})
if mode == 'exit':
    sys.exit(0)
if mode == 'crash_after_count':
    emit(type='state', stage='following', actual_write_count=8)
    sys.exit(1)
if mode == 'hang':
    while True:
        time.sleep(.1)
emit(type='state', stage='preparing' if mode == 'preparing' else 'ready',
     simulation=False, running=False, output_dir='/invalid', accepted_targets=0)
for line in sys.stdin:
    command = json.loads(line)['command']
    if command == 'start':
        emit(type='state', stage='following', accepted_targets=1)
        if mode == 'recover':
            emit(type='state', stage='reconnecting', message='source input reconnecting')
    if command == 'stop':
        if mode == 'preparing':
            emit(type='state', stage='ready')
        emit(type='final', completed=True, last_action_unknown=False)
        break
'''


class SessionTests(unittest.TestCase):
    def test_worker_mode_and_release_state_reach_status(self):
        manager = self.manager()
        manager._accept(dict(type='state',stage='ready',control_mode='normal_walking',
            control_topic='rt/arm_sdk',control_verified=True))
        self.assertEqual(manager.status()['control_topic'],'rt/arm_sdk')
        self.assertEqual(manager.status()['control_mode'],'normal_walking')
        self.assertTrue(manager.status()['control_verified'])
        manager._accept(dict(type='final',stage='stopped',completed=True,
            stop_behavior='release_arm_weight',arm_weight_released=True))
        self.assertTrue(manager.status()['arm_weight_released'])
        self.assertEqual(manager.status()['stop_behavior'],'release_arm_weight')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        self.write_worker()

    def write_worker(self, mode='normal'):
        (self.root / 'scripts/dashboard_worker.py').write_text(
            FAKE_WORKER.replace('mode = MODE', 'mode = ' + repr(mode)))

    def manager(self, **kwargs):
        manager = session.SessionManager(self.root, simulation=kwargs.pop('simulation', True), **kwargs)
        self.addCleanup(manager.close)
        return manager

    def wait(self, manager, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = manager.status()
            if predicate(state):
                return state
            time.sleep(.01)
        self.fail('session state did not settle: ' + repr(manager.status()))

    def test_constructor_is_inert(self):
        with mock.patch.object(session.subprocess, 'Popen') as popen:
            manager = self.manager()
            self.assertTrue(manager.status()['can_launch'])
            self.assertFalse(manager.status()['can_start'])
            self.assertIsNone(manager.status()['actual_write_count'])
            with self.assertRaises(RuntimeError):
                manager.start()
            popen.assert_not_called()
        self.assertFalse((self.root / 'diagnostics').exists())

    def test_launch_start_stop_restart(self):
        manager = self.manager()
        launched = manager.launch()
        self.assertTrue(launched['running'])
        ready = self.wait(manager, lambda s: s['can_start'])
        self.assertTrue(ready['simulation'])
        self.assertNotEqual(ready['output_dir'], '/invalid')
        self.assertEqual(ready['accepted_targets'], 0)
        manager.start()
        self.wait(manager, lambda s: s['stage'] == 'following')
        with self.assertRaises(RuntimeError):
            manager.start()
        with self.assertRaises(RuntimeError):
            manager.launch()
        manager.stop()
        ended = self.wait(manager, lambda s: not s['running'])
        self.assertEqual(ended['stage'], 'stopped')
        self.assertTrue(ended['completed'])
        self.assertIsNone(ended['actual_write_count'])
        self.assertEqual(ended['accepted_targets'], 1)
        self.assertNotEqual(manager.launch()['session_id'], launched['session_id'])

    def test_launch_uses_exclusive_process_lock(self):
        first, second = self.manager(), self.manager()
        first.launch()
        self.wait(first, lambda s: s['can_start'])
        with self.assertRaisesRegex(RuntimeError, 'owns the robot input'):
            second.launch()
        first.stop()
        self.wait(first, lambda s: not s['running'])
        second.launch()
        self.wait(second, lambda s: s['can_start'])

    def test_stop_during_preparation_rejects_start_and_late_ready(self):
        self.write_worker('preparing')
        manager = self.manager()
        manager.launch()
        self.wait(manager, lambda s: bool(s['log_tail']))
        stopped = manager.stop()
        self.assertFalse(stopped['can_start'])
        with self.assertRaises(RuntimeError):
            manager.start()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertEqual(state['accepted_targets'], 0)
        self.assertEqual(state['stage'], 'stopped')

    def test_preparation_deadline_and_no_fixed_session_deadline(self):
        self.write_worker('preparing')
        manager = self.manager(launch_timeout_s=.1)
        manager.launch()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertIn('startup deadline', state['error'])
        self.write_worker('recover')
        manager.launch()
        self.wait(manager, lambda s: s['can_start'])
        manager.start()
        self.wait(manager, lambda s: s['stage'] == 'reconnecting')
        with manager._lock:
            manager._born -= 3600
        time.sleep(.2)
        self.assertTrue(manager.status()['running'])
        self.assertIsNone(manager.status()['error'])

    def test_missing_final_keeps_motion_result_unknown(self):
        self.write_worker('exit')
        manager = self.manager()
        manager.launch()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertIsNone(state['actual_write_count'])
        self.assertTrue(state['last_action_unknown'])
        self.assertFalse(state['completed'])
        self.assertEqual(state['stage'], 'error')

    def test_unresponsive_worker_gets_bounded_owned_stop(self):
        self.write_worker('hang')
        manager = self.manager(stop_timeout_s=.05)
        manager.launch()
        self.wait(manager, lambda s: bool(s['log_tail']))
        pid = manager._process.pid
        manager.stop()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertEqual(manager._process.pid, pid)
        self.assertEqual(state['exit_code'], -signal.SIGTERM)
        self.assertTrue(state['last_action_unknown'])
        self.assertIsNone(state['actual_write_count'])

    def test_interim_count_is_unknown_after_abnormal_exit(self):
        self.write_worker('crash_after_count')
        manager = self.manager()
        manager.launch()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertIsNone(state['actual_write_count'])
        self.assertTrue(state['last_action_unknown'])

    def test_password_stays_in_pipe_and_is_redacted_from_logs(self):
        secret = 'sensitive-PW-$-12345'
        manager = self.manager(simulation=False, password=secret)
        original = session.subprocess.Popen
        with mock.patch.object(session, '_conflicts', return_value=[]), \
                mock.patch.object(session.subprocess, 'Popen', wraps=original) as popen:
            manager.launch()
            self.wait(manager, lambda s: s['can_start'])
            self.assertNotIn(secret, repr(popen.call_args))
            self.assertNotIn('env', popen.call_args.kwargs)
        manager.stop()
        state = self.wait(manager, lambda s: not s['running'])
        self.assertNotIn(secret, json.dumps(state))
        logs = list((self.root / 'diagnostics/dashboard-runs').glob('*.worker.log'))
        self.assertEqual(len(logs), 1)
        self.assertNotIn(secret, logs[0].read_text())
        self.assertIn('[redacted]', logs[0].read_text())
        self.assertNotIn('ssh_password', logs[0].read_text())

    def test_existing_control_process_rejected_without_spawning(self):
        manager = self.manager(simulation=False, password='test')
        with mock.patch.object(session, '_conflicts', return_value=['arm_teleop.py (pid 345)']), \
                mock.patch.object(session.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'arm_teleop.py'):
                manager.launch()
            popen.assert_not_called()

    def test_reused_pid_is_never_signalled(self):
        manager = self.manager()
        manager._owned = {12345: 'old-start'}
        with mock.patch.object(manager, '_track_children'), \
                mock.patch.object(session, '_identity', return_value=('new-start', 'S')), \
                mock.patch.object(session.os, 'kill') as kill:
            manager._signal_owned(signal.SIGKILL)
            kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
