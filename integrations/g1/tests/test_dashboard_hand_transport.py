import json
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_hand_transport as transport


class FakeSSH:
    def __init__(self, executable, args, **kwargs):
        self.executable, self.args, self.kwargs = executable, args, kwargs
        self.mode = '--enable-commands' in args[-1]
        self.alive = True
        self.before = ''
        self.records, self.sent = queue.Queue(), []
        self.writes = dict(left=0, right=0)

    def expect(self, pattern, timeout):
        if pattern == '[Pp]assword:':
            return 0
        self.before = json.dumps(self.records.get(timeout=timeout))
        return 0

    def sendline(self, line):
        self.sent.append(line)
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        if 'sudo_password' in record:
            self.records.put(dict(type='ready', command_mode=self.mode))
        elif record['command'] == 'snapshot':
            self.records.put(dict(type='snapshot', request_id=record['request_id'], hands={
                'right': dict(q=[.5]*6, dq=[0.]*6, age_s=.01, received=12345,
                              sequence=1, fresh=True, error=None)}))
        elif record['command'] == 'target':
            side = record['side']
            self.writes[side] += 1
            self.records.put(dict(type='target_result', request_id=record['request_id'], side=side,
                published=True, dds_write_count=self.writes[side], acknowledged=False))
        elif record['command'] == 'stop':
            self.records.put(dict(type='final', completed=True, actual_write_count=None,
                                  physical_stop_verified=False))

    def isalive(self):
        return self.alive

    def close(self, force):
        self.alive = False


class TransportTests(unittest.TestCase):
    def backend(self, **kwargs):
        self.children = []
        def spawn(*args, **opts):
            child = FakeSSH(*args, **opts)
            self.children.append(child)
            return child
        backend = transport.HandTransport(password='private-credential-XYZ', spawn=spawn, **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_constructor_and_snapshot_are_inert(self):
        backend = self.backend()
        self.assertEqual(backend.snapshot(), {})
        self.assertEqual(self.children, [])

    def test_feedback_open_does_not_enable_commands(self):
        backend = self.backend()
        self.assertFalse(backend.open()['command_mode'])
        self.assertFalse(backend.send('right', [.5]*6, [.2]*6)['published'])
        self.assertEqual(self.children[0].writes['right'], 0)
        sample = backend.snapshot()['right']
        self.assertTrue(sample['fresh'])
        self.assertGreater(sample['age_s'], .01)
        self.assertNotIn('received', sample)

    def test_configured_robot_address_retains_pinned_host_identity(self):
        backend=self.backend()
        with mock.patch.object(transport,'robot_host',return_value='10.1.10.73') as configured:
            backend.open(False)
        configured.assert_called_once_with(backend.root)
        self.assertIn('unitree@10.1.10.73',self.children[0].args)
        self.assertIn('StrictHostKeyChecking=yes',self.children[0].args)
        self.assertIn('HostKeyAlias=192.168.123.164',self.children[0].args)
        self.assertEqual(self.children[0].writes,{'left':0,'right':0})

    def test_command_upgrade_stops_only_owned_feedback_instance(self):
        backend = self.backend(max_step=.08)
        backend.open(False)
        first = self.children[0]
        backend.open(True)
        self.assertFalse(first.isalive())
        self.assertIn(json.dumps({'command': 'stop'}), first.sent)
        self.assertEqual(len(self.children), 2)
        self.assertTrue(backend.send('right', [.5]*6, .2)['published'])
        self.assertEqual(json.loads(self.children[1].sent[-1])['speed'], [.2]*6)

    def test_pause_one_side_and_resume_does_not_affect_other(self):
        backend = self.backend()
        backend.open(True)
        backend.hold('right')
        self.assertFalse(backend.send('right', [.5]*6, [.2]*6)['published'])
        self.assertTrue(backend.send('left', [.5]*6, [.2]*6)['published'])
        backend.enable('right', True)
        self.assertTrue(backend.send('right', [.5]*6, [.2]*6)['published'])
        targets = [json.loads(x) for x in self.children[0].sent if x.startswith('{')]
        self.assertEqual([x['side'] for x in targets if x.get('command') == 'target'], ['left', 'right'])

    def test_secret_is_only_sent_in_authenticated_input(self):
        backend = self.backend()
        ready = backend.open(True)
        child = self.children[0]
        secret = 'private-credential-XYZ'
        self.assertNotIn(secret, repr((child.args, child.kwargs, ready)))
        self.assertFalse(child.kwargs['echo'])
        self.assertEqual(child.sent[:2], [secret, json.dumps({'sudo_password': secret})])
        self.assertIn('StrictHostKeyChecking=yes', child.args)
        self.assertIn('ServerAliveInterval=2', child.args)

    def test_full_round_trip_time_counts_toward_feedback_age(self):
        backend = self.backend()
        backend.open(False)
        def response(command):
            time.sleep(.015)
            return {'hands': {'right': dict(q=[.5]*6, age_s=.09, fresh=True)}}
        with mock.patch.object(backend, '_request', side_effect=response):
            sample = backend.snapshot()['right']
        self.assertFalse(sample['fresh'])
        self.assertGreater(sample['age_s'], .1)

    def test_close_reports_unknown_physical_result(self):
        backend = self.backend()
        backend.open(True)
        final = backend.close()
        self.assertIsNone(final['actual_write_count'])
        self.assertFalse(final['physical_stop_verified'])
        self.assertIsNone(backend.process)

    def test_cancelled_target_never_enters_ssh_pipe(self):
        backend = self.backend()
        backend.open(True)
        sent = len(self.children[0].sent)
        result = backend.send('right', [.5]*6, .2, should_send=lambda: False)
        self.assertFalse(result['published'])
        self.assertEqual(len(self.children[0].sent), sent)

    def test_cancelled_input_lifecycle_never_opens_transport(self):
        backend = self.backend()
        stopped = threading.Event()
        stopped.set()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            backend.open(True, cancel_event=stopped)
        self.assertEqual(self.children, [])

    def test_external_stop_cancels_subsequent_target(self):
        backend = self.backend()
        stopped = threading.Event()
        backend.open(True, cancel_event=stopped)
        stopped.set()
        self.assertFalse(backend.send('right', [.5]*6, .2)['published'])
        self.assertEqual(self.children[0].writes['right'], 0)

    def test_authentication_can_be_cancelled_without_sending_credentials(self):
        import pexpect
        stopped = threading.Event()
        child = FakeSSH('ssh', ['fake'])
        def expect(pattern, timeout):
            stopped.set()
            raise pexpect.TIMEOUT('a transcript must not escape')
        child.expect = expect
        backend = transport.HandTransport(password='private-credential-XYZ', spawn=lambda *a, **k: child)
        with self.assertRaisesRegex(RuntimeError, 'initialization failed') as caught:
            backend.open(True, cancel_event=stopped)
        self.assertNotIn('transcript', str(caught.exception))
        self.assertEqual(child.sent, [])
        self.assertFalse(child.isalive())

    def test_known_remote_initialization_failure_is_actionable_and_retryable(self):
        backend = self.backend()
        original_spawn = backend._spawn
        def fail_first(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            if len(self.children) == 1:
                original_send = child.sendline
                def sendline(line):
                    original_send(line)
                    if line.startswith('{') and 'sudo_password' in json.loads(line):
                        child.records.get_nowait()
                        child.records.put(dict(type='final', completed=False,
                            error='RuntimeError: neither verified hand port is present'))
                child.sendline = sendline
            return child
        backend._spawn = fail_first
        with self.assertRaisesRegex(RuntimeError, 'check hand power and USB'):
            backend.open(True)
        self.assertIsNone(backend.process)
        self.assertIsNone(backend.ready)
        self.assertFalse(backend.command_mode)
        self.assertFalse(any(backend.enabled.values()))
        self.assertFalse(self.children[0].isalive())
        self.assertTrue(backend.open(True)['command_mode'])
        self.assertEqual(len(self.children), 2)
        self.assertTrue(backend.send('right', [.5]*6, .2)['published'])

    def test_unknown_remote_error_and_cleanup_text_are_not_exposed(self):
        backend = self.backend()
        secret = 'private-credential-XYZ'
        child = FakeSSH('ssh', ['fake'])
        original_send = child.sendline
        def sendline(line):
            original_send(line)
            if line.startswith('{') and 'sudo_password' in json.loads(line):
                child.records.get_nowait()
                child.records.put(dict(type='final', error='RuntimeError: '+secret,
                    cleanup_errors=['unexpected transcript: '+secret], actual_write_count=None))
        child.sendline = sendline
        backend._spawn = lambda *args, **kwargs: child
        with self.assertRaisesRegex(RuntimeError, 'check the hand service startup log') as caught:
            backend.open()
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(backend.final))
        self.assertIsNone(backend.final['actual_write_count'])

    def test_ssh_failures_have_fixed_reasons_without_transcripts(self):
        import pexpect
        cases = (
            ('Permission denied (publickey,password)', 'SSH authentication failed'),
            ('Host key verification failed', 'SSH identity could not be verified'),
            ('REMOTE HOST IDENTIFICATION HAS CHANGED', 'verify the robot host key'),
            ('connect to host 192.168.31.65 port 22: Connection refused', 'wait for the robot to boot'),
            ('No route to host', 'unreachable over Wi-Fi'),
            ('Network is unreachable', 'unreachable over Wi-Fi'),
            ('Connection timed out', 'Connecting to G1 timed out'),
            ('Connection reset by peer', 'SSH connection was interrupted'),
            ('unrecognized startup failure', 'SSH closed before authentication'),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                backend = self.backend()
                child = FakeSSH('ssh', ['fake'])
                def expect(pattern, timeout):
                    child.before = raw+'\nprivate-credential-XYZ\nPRIVATE TRANSCRIPT'
                    child.alive = False
                    raise pexpect.EOF('private-credential-XYZ PRIVATE EXCEPTION')
                child.expect = expect
                backend._spawn = lambda *args, **kwargs: child
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    backend.open(True)
                self.assertNotIn('private-credential-XYZ', str(caught.exception))
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(child.sent, [])
                self.assertIsNone(backend.process)

    def test_authentication_eof_allows_next_open_to_reconnect(self):
        import pexpect
        backend = self.backend()
        original_spawn = backend._spawn
        def fail_first(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            if len(self.children) == 1:
                def expect(pattern, timeout):
                    child.before = 'Connection refused'
                    child.alive = False
                    raise pexpect.EOF('PRIVATE TRANSCRIPT')
                child.expect = expect
            return child
        backend._spawn = fail_first
        with self.assertRaisesRegex(RuntimeError, 'wait for the robot to boot'):
            backend.open(True)
        self.assertTrue(backend.open(True)['command_mode'])
        self.assertEqual(len(self.children), 2)

    def test_laptop_spawn_failure_does_not_expose_exception_and_can_retry(self):
        backend = self.backend()
        original_spawn = backend._spawn
        backend._spawn = mock.Mock(side_effect=OSError('PRIVATE TRANSCRIPT private-credential-XYZ'))
        with self.assertRaisesRegex(RuntimeError, 'could not start SSH') as caught:
            backend.open(True)
        self.assertNotIn('private-credential-XYZ', str(caught.exception))
        self.assertIsNone(backend.process)
        backend._spawn = original_spawn
        self.assertTrue(backend.open(True)['command_mode'])

    def test_bridge_disconnect_and_timeout_after_authentication_are_actionable(self):
        import pexpect
        for failure, expected in ((pexpect.EOF('PRIVATE TRANSCRIPT'), 'closed before readiness'),
                                  (TimeoutError('PRIVATE TRANSCRIPT'), 'did not become ready in time')):
            with self.subTest(failure=type(failure).__name__):
                backend = self.backend()
                original_spawn = backend._spawn
                def spawn(*args, **kwargs):
                    child = original_spawn(*args, **kwargs)
                    original_expect = child.expect
                    def expect(pattern, timeout):
                        if pattern == '[Pp]assword:':
                            return original_expect(pattern, timeout)
                        child.alive = False
                        raise failure
                    child.expect = expect
                    return child
                backend._spawn = spawn
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    backend.open(True)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertIsNone(backend.process)
                self.assertIsNone(backend.ready)

    def test_upgrade_close_exception_does_not_leak_transcript(self):
        backend = self.backend()
        backend.open(False)
        child = self.children[0]
        def failing_close(force):
            child.alive = False
            raise OSError('PRIVATE TRANSCRIPT private-credential-XYZ')
        child.close = failing_close
        with self.assertRaisesRegex(RuntimeError, 'could not be closed cleanly') as caught:
            backend.open(True)
        self.assertNotIn('private-credential-XYZ', str(caught.exception))
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertIsNone(backend.process)
        self.assertTrue(backend.open(True)['command_mode'])

    def test_close_failure_still_clears_local_state_for_explicit_retry(self):
        backend = self.backend()
        backend.open(True)
        first = self.children[0]
        def failing_close(force):
            first.alive = False
            raise OSError('fake close failure')
        first.close = failing_close
        with self.assertRaisesRegex(OSError, 'fake close failure'):
            backend.close()
        self.assertIsNone(backend.process)
        self.assertIsNone(backend.ready)
        self.assertFalse(backend.command_mode)
        self.assertFalse(any(backend.enabled.values()))
        self.assertFalse(backend._protocol_sent)
        self.assertTrue(backend.open(True)['command_mode'])
        self.assertEqual(len(self.children), 2)

    def test_remote_reason_allowlist_never_echoes_suffixes(self):
        for raw, expected in (
            ('RuntimeError: an existing BrainCo service already owns the serial devices', 'Another BrainCo service'),
            ('RuntimeError: sudo authentication failed', 'sudo authentication failed'),
            ('RuntimeError: serial devices are occupied or occupancy check failed', 'hand ports are occupied'),
            ('RuntimeError: owned BrainCo service exited', 'service exited'),
            ('RuntimeError: audited service dependency differs: PRIVATE', 'differ from the audited version'),
            ('ModuleNotFoundError: PRIVATE', 'Python dependency is missing'),
            ('CalledProcessError: PRIVATE', 'service setup failed'),
            ('BlockingIOError: PRIVATE', 'holds the service lock'),
        ):
            with self.subTest(raw=raw):
                result = transport._remote_failure(raw)
                self.assertIn(expected, result)
                self.assertNotIn('PRIVATE', result)

    def test_transport_limits_cannot_expand_original_envelope(self):
        for settings in ({'max_step': .2}, {'feedback_max_age_s': 2.}, {'max_step': float('nan')}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                transport.HandTransport(**settings)


if __name__ == '__main__':
    unittest.main()
