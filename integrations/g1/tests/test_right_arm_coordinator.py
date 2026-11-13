"""Coordinator integration contracts; fake SSH, workers, PICO, and robot state."""
import ast
import copy
import importlib
import io
import json
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
coordinator = importlib.import_module('run_right_arm_teleop')
client = importlib.import_module('pc2_arm_client')


def packet(seq=2, receipt=1_000_000_010, held=True):
    return {'type': 'sample', 'packet_sequence': seq, 'local_monotonic_ns': receipt,
            'app_timestamp_ns': 9_000_000_000 + seq,
            'controller_enable': {'sequence': seq, 'local_monotonic_ns': receipt,
                'source_id': 'pico_right_trigger', 'held': held}}


def candidate(source, reference='REF'):
    return {'accepted': True, 'source_packet_sequence': source['packet_sequence'],
            'source_received_monotonic_ns': source['local_monotonic_ns'],
            'source_app_timestamp_ns': source['app_timestamp_ns'],
            'reference_id': reference, 'calibration_id': 'preview-xr-right-up-back-v1',
            'q_candidate_rad': [i / 100 for i in range(14)], 'tracking_valid': None}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.challenge = {'type': 'challenge', 'session_id': 'SESSION', 'nonce': 'NONCE',
                          'sequence': 1, 'reference_id': 'REF',
                          '_local_received_monotonic_ns': 1_000_000_000}
        self.decision = {'input_policy_id': 'supervised_position_only_v1',
                         'supervised_input_accepted': True}

    def test_reply_binds_challenge_to_same_source_packet_without_restamping(self):
        source = packet()
        before = copy.deepcopy(source)
        reply = coordinator.response_for_challenge(self.challenge, source, candidate(source),
                                                   self.decision, 'REF')
        self.assertEqual(source, before)
        self.assertEqual(reply['nonce'], 'NONCE')
        self.assertEqual(reply['sequence'], 1)
        self.assertEqual(reply['input_sequence'], reply['enable_sequence'])
        self.assertEqual(reply['q'], [i / 100 for i in range(7, 14)])
        self.assertIsNone(reply['tracking_valid'])
        self.assertNotIn('received_t', reply)  # PC2 uses its nonce issue-time bound
        self.assertNotIn('local_monotonic_ns', reply)

    def test_prechallenge_mismatched_input_enable_or_reference_is_rejected(self):
        for change in ('old_packet', 'ik_sequence', 'enable_sequence', 'enable_time', 'reference'):
            source = packet()
            result = candidate(source)
            if change == 'old_packet':
                source['local_monotonic_ns'] = 999_999_999
            elif change == 'ik_sequence':
                result['source_packet_sequence'] += 1
            elif change == 'enable_sequence':
                source['controller_enable']['sequence'] += 1
            elif change == 'enable_time':
                source['controller_enable']['local_monotonic_ns'] += 1
            else:
                result['reference_id'] = 'OTHER'
            with self.subTest(change=change), self.assertRaises(ValueError):
                coordinator.response_for_challenge(self.challenge, source, result, self.decision, 'REF')

    def test_unknown_validity_and_policy_rejection_are_never_upgraded(self):
        source = packet(held=False)
        decision = dict(self.decision, supervised_input_accepted=False)
        reply = coordinator.response_for_challenge(self.challenge, source, candidate(source), decision, 'REF')
        self.assertFalse(reply['held'])
        self.assertFalse(reply['supervised_input_accepted'])
        self.assertIsNone(reply['tracking_valid'])

    def test_tracker_window_has_no_synthetic_controller_enable(self):
        source=packet()
        source.pop('controller_enable')
        decision={'input_policy_id':'supervised_tracker_window_v1','supervised_input_accepted':True}
        reply=coordinator.response_for_challenge(self.challenge,source,candidate(source),decision,'REF')
        self.assertEqual(reply['type'],'tracker_target')
        self.assertIsNone(reply['tracking_valid'])
        self.assertTrue(reply['supervised_input_accepted'])
        for key in ('held','enable_sequence','enable_source','received_t'):
            self.assertNotIn(key,reply)
        self.assertEqual(coordinator.commissioning_config()['enable_source'],'authorized_ten_second_window')

    def test_new_packet_selection_preserves_source_time_and_stops_on_eof(self):
        incoming = queue.Queue()
        incoming.put({'type': 'metadata'})
        incoming.put(packet(receipt=1))
        fresh = packet(receipt=100)
        incoming.put(fresh)
        self.assertIs(coordinator.newest_after(incoming, 50, time.monotonic() + 1), fresh)
        incoming.put(None)
        with self.assertRaisesRegex(RuntimeError, 'ended'):
            coordinator.newest_after(incoming, 50, time.monotonic() + 1)

    def test_real_memory_bootstrap_runs_plan_without_sdk_or_shell_interpolation(self):
        reference = "REF ' ; $(this_must_never_execute) `or_this`"
        config = coordinator.commissioning_config()
        command = client.remote_command(reference, config, 30)
        argv = shlex.split(command)
        self.assertEqual(argv[argv.index('--reference-id') + 1], reference)
        self.assertEqual(json.loads(argv[argv.index('--config-json') + 1]), config)
        self.assertNotIn('--enable-motion', argv)
        # --plan returns before SDK initialization; only local Python is launched.
        python_index = argv.index('/home/unitree/miniconda3/envs/tv/bin/python')
        result = subprocess.run([sys.executable] + argv[python_index + 1:] + ['--plan'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['default'], 'preview')

    def test_motion_requires_explicit_current_release_and_bootstrap_flags(self):
        with self.assertRaises(ValueError):
            client.PC2ArmClient('REF', {}, enable_motion=True)
        for seconds in (0, 61, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                client.remote_command('REF', {}, seconds)
        argv = shlex.split(client.remote_command('REF', coordinator.commissioning_config(), 10, True))
        for option in ('--enable-motion', '--suspended-confirmed', '--operator-ready',
                       '--exclusive-control-confirmed'):
            self.assertIn(option, argv)

    def test_client_json_receipt_keeps_remote_stamp_separate(self):
        process = Mock()
        records = ['native log', json.dumps({'type': 'challenge', 'issued_monotonic_ns': 10})]
        def expect(*_args, **_kwargs):
            process.before = records.pop(0)
        process.expect.side_effect = expect
        active = client.PC2ArmClient('REF', {})
        active.process = process
        with patch.object(client.time, 'monotonic_ns', return_value=123):
            report = active.read_record(1)
        self.assertEqual(report['issued_monotonic_ns'], 10)
        self.assertEqual(report['_local_received_monotonic_ns'], 123)

    def test_client_open_treats_runtime_final_as_terminal_before_ready(self):
        process = Mock()
        fake_pexpect = NS(spawn=Mock(return_value=process))
        active = client.PC2ArmClient('REF', {})
        with patch.dict('sys.modules', {'pexpect': fake_pexpect}), \
                patch.object(client, 'remote_command', return_value='NO NETWORK'), \
                patch.object(active, 'read_record', side_effect=[{'type': 'runtime_final', 'reason': 'init_failed'},
                                                                 RuntimeError('read past terminal')]) as read, \
                patch.object(active, 'close') as close:
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                active.open('TEST_PASSWORD_ONLY')
        self.assertEqual(read.call_count, 1)
        close.assert_called_once()

    def test_sources_compile_without_importing_device_sdks(self):
        for name in ('run_right_arm_teleop.py', 'pc2_arm_client.py'):
            source = ROOT / 'scripts' / name
            compile(source.read_bytes(), str(source), 'exec')

    def test_cli_does_not_report_success_after_remote_rejection(self):
        failed = {'targets_sent': 1, 'commands_published': 0, 'stop_reason': 'remote_ended',
                  'remote_result': {'type': 'runtime_final',
                                    'reason': 'gate:invalid_tracking_or_calibration',
                                    'last_action_unknown': False, 'actual_write_count': 0}}
        with patch.object(coordinator, 'run', return_value=failed), \
                patch.object(coordinator.getpass, 'getpass', return_value='TEST_ONLY'), \
                patch.object(coordinator.signal, 'signal'), \
                patch.object(sys, 'argv', ['run_right_arm_teleop.py', '--output', '/unused/fake.jsonl']), \
                patch('sys.stdout', io.StringIO()):
            self.assertNotEqual(coordinator.main(), 0)

    def test_coordinator_registers_policy_reference_before_evaluate(self):
        tree = ast.parse((ROOT / 'scripts/run_right_arm_teleop.py').read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'policy']
        references = [node.lineno for node in calls if node.func.attr == 'set_reference']
        evaluations = [node.lineno for node in calls if node.func.attr == 'evaluate']
        self.assertTrue(references)
        self.assertLess(min(references), min(evaluations))

    def test_fake_coordinator_captures_terminal_write_result_and_cleans_workers(self):
        resources = {}
        class Input:
            def __init__(self, process):
                self.process, self.closed = process, False
            def write(self, line):
                value = json.loads(line)
                if value['type'] == 'reference':
                    resources['reference'] = value
                    self.process.destination.put({'status': 'reference_accepted'})
                else:
                    result = candidate(value, resources['reference']['reference_id'])
                    self.process.destination.put(result)
            def flush(self):
                pass
            def close(self):
                self.closed = True
        def popen(command, **kwargs):
            name = 'ik' if any('live_pico_arm_ik.py' in str(x) for x in command) else 'pico'
            process = NS(name=name, stdin=None, stdout=None)
            process.stdin = Input(process)
            resources[name] = process
            return process
        def lines(process, destination):
            process.destination = destination
            if process.name == 'ik':
                destination.put({'type': 'ready'})
        class Feedback:
            def __init__(self, **kwargs):
                resources['feedback'] = self
                self.closed = False
            def open(self, password):
                return {'type': 'feedback_ready'}
            def snapshot(self):
                return {'type': 'wifi_feedback', 'source_clock_id': 'REMOTE_CLOCK', 'streams': {
                    'rt/lowstate': {'status': 'fresh_bound', 'source': {'payload': {
                        'crc_valid': True, 'mode_pr': 0, 'motorstate': [0] * 35, 'q': [0.] * 35}},
                        'conservative_received_monotonic_ns': time.monotonic_ns(), 'sequence': 1,
                        'age_upper_bound_s': .01}}}
            def close(self):
                self.closed = True
        class Remote:
            def __init__(self, reference, config, **kwargs):
                resources['remote'] = self
                self.reference, self.sent = reference, []
                self.process = NS(sendeof=lambda: self.sent_event.set())
                self.sent_event, self.closed = threading.Event(), False
                self.calls, self.emit = 0, kwargs['emit']
            def open(self, password):
                return {'type': 'runtime_ready'}
            def read_record(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    time.sleep(.22)
                    return dict(type='challenge', session_id='SESSION', nonce='NONCE', sequence=1,
                                reference_id=self.reference, _local_received_monotonic_ns=time.monotonic_ns())
                if self.calls == 2:
                    self.sent_event.wait(timeout)
                    return {'type': 'runtime_final', 'actual_write_count': 0,
                            'damping': None, 'last_action_unknown': False}
                raise RuntimeError('EOF after final')
            def send(self, value):
                self.sent.append(value)
                self.sent_event.set()
            def close(self, *, drain=True):
                self.closed = True
                self.process = None
                self.close_drained = drain
        class Policy:
            def __init__(self, sn):
                self.reference = None
            def set_reference(self, reference):
                self.reference = reference
            def evaluate(self, source, target, **kwargs):
                if self.reference is None:
                    raise AssertionError('policy reference not set')
                return {'input_policy_id': 'supervised_tracker_window_v1', 'supervised_input_accepted': True}
        sequence = [0]
        def fresh(_incoming, stamp, _deadline, **kwargs):
            time.sleep(.001)
            sequence[0] += 1
            return packet(sequence[0], max(time.monotonic_ns(), stamp + 1))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('sys.modules', {'pico_supervised_input': NS(TrackerWindowPolicy=Policy)}), \
                patch.object(coordinator.subprocess, 'Popen', side_effect=popen), \
                patch.object(coordinator, 'lines', side_effect=lines), \
                patch.object(coordinator, 'WifiFeedbackClient', Feedback), \
                patch.object(coordinator, 'PC2ArmClient', Remote), \
                patch.object(coordinator, 'newest_after', side_effect=fresh), \
                patch.object(coordinator, 'stop') as stop:
            report = coordinator.run(Path(tmp) / 'session.jsonl', 'TEST_PASSWORD_ONLY', seconds=5)
            self.assertEqual(report.get('remote_result', {}).get('type'), 'runtime_final', report)
            self.assertEqual(report['commands_published'], 0)
            self.assertGreater(report['ik_candidates'],5)
            self.assertTrue(resources['feedback'].closed and resources['remote'].closed)
            self.assertFalse(resources['remote'].close_drained)
            self.assertTrue(resources['ik'].stdin.closed)
            self.assertEqual(stop.call_count, 2)


if __name__ == '__main__':
    unittest.main()
