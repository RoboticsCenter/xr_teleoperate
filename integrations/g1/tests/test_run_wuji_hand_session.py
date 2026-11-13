"""Integrated collector tests: original mapper, fake input/state, no hardware."""
import importlib.util
import copy
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('run_wuji_hand_session_test', ROOT / 'scripts/run_wuji_hand_session.py')
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)
time_bounds = run.load('_hand_test_wifi_time', 'wifi_feedback_time.py')


class Clock:
    now = 100.
    def __call__(self):
        return self.now


class Lines:
    def __init__(self, clock, events):
        self.clock, self.events = clock, list(events)
    def get(self, timeout):
        if not self.events:
            self.clock.now += timeout
            raise queue.Empty
        self.clock.now += .01
        return self.events.pop(0)(self.clock.now)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.binding = run.live.mapping.synthetic_fixture(self.base)
        original, settings = run.live.mapping.load_original()
        self.mapper = run.live.mapping.HandDDSPreview(self.binding, self.base, original, settings,
                                                    allow_synthetic=True)
        self.clock = Clock()
        self.receiver = run.live.FeedbackReceiver(self.mapper, self.clock)
        self.receiver.receive(NS(states=[NS(q=.5)] * 6))
        self.args = run.parser().parse_args([])
        self.args.seconds, self.args.idle_timeout = .03, .1
        self.args.interface, self.args.side = 'test0', 'left'

    def event(self, seq=1, **changes):
        def output(now):
            frame = run.live.mapping.synthetic_record(self.mapper, seq=seq, now=now)['glove']
            frame.update(changes)
            return 'line', json.dumps(frame), now
        return output

    def collect(self, events, **kwargs):
        emitted = []
        result = run.collect(self.args, self.mapper, self.receiver,
                             Lines(self.clock, events), emitted.append, clock=self.clock, **kwargs)
        return result, emitted

    def test_default_collector_reaches_original_mapper_and_session_without_transport(self):
        with patch.object(run.output, 'official_transport', side_effect=AssertionError('No hardware')):
            report, events = self.collect([self.event()])
        self.assertTrue(report['ok'])
        self.assertEqual(report['preview_count'], 1)
        self.assertEqual(report['published_count'], 0)
        output = events[0]
        self.assertEqual(output['result']['step_limited_1000'], [580] * 6)
        self.assertTrue(output['result']['input_quality_passed'])
        self.assertFalse(output['result']['allow_low_quality'])
        self.assertLess(output['input']['glove']['source_t'], self.clock.now)
        self.assertEqual(events[-1]['type'], 'hand_output_closed')

    def test_no_accepted_original_frames_never_constructs_session(self):
        self.args.seconds = 1
        factory = Mock(side_effect=AssertionError('No accepted frame'))
        report, events = self.collect([], session_factory=factory)
        factory.assert_not_called()
        self.assertEqual(report['reason'], 'no_accepted_original_reader_frames')
        self.assertFalse(report['input_quality_passed'])
        self.assertFalse(events)

    def test_explicit_output_path_reports_fake_writes_without_mislabeling_preview(self):
        self.binding['kind'] = self.binding['sdk_model']['kind'] = 'operator_calibrated'
        original, settings = run.live.mapping.load_original()
        self.mapper = run.live.mapping.HandDDSPreview(self.binding, self.base, original, settings)
        self.args.enable_motion = True
        publisher = Mock()
        publisher.Write.return_value = True
        def factory(args, mapper, clock):
            return run.output.BrainCoHandSession(mapper, enable_motion=True, interface='test0',
                exclusive_control_confirmed=True, feedback_identity_confirmed=True,
                command_service_confirmed=True, clock=clock,
                transport_factory=lambda *_: (publisher, lambda: NS(cmds=[]), lambda: NS()))
        report, events = self.collect([self.event()], session_factory=factory)
        self.assertTrue(report['ok'])
        self.assertEqual(report['preview_count'], 0)
        self.assertEqual(report['published_count'], 1)
        self.assertTrue(events[0]['published'])
        self.assertEqual(events[0]['result']['status'], 'published')
        publisher.Write.assert_called_once()
        publisher.Close.assert_called_once()

    def test_input_gap_checks_session_while_idle_and_closes_output(self):
        self.args.seconds, self.args.idle_timeout = 1, 2
        report, events = self.collect([self.event()])
        self.assertEqual(report['reason'], 'input_timeout')
        self.assertLess(self.clock.now - 100, .3)
        self.assertEqual(events[-1]['type'], 'hand_output_closed')
        self.assertNotIn('command_preview', events[-2]['result'])

    def test_stale_feedback_latches_before_producing_any_target(self):
        self.clock.now += .3
        self.args.idle_timeout = 1
        report, events = self.collect([self.event()])
        self.assertEqual(report['reason'], 'mapping:stale_or_future_feedback')
        self.assertEqual(events[0]['input']['measured']['received_t'], 100.)
        self.assertNotIn('command_preview', events[0]['result'])

    def test_wrong_identity_or_stale_glove_never_asserts_quality_passed(self):
        for changes in ({'sn': 'neighbor'}, {'source_t': 50.}):
            with self.subTest(changes=changes):
                report, events = self.collect([self.event(**changes)])
                self.assertTrue(report['reason'].startswith('invalid_original_frame:'))
                self.assertEqual(report['accepted_original_frames'], 0)
                self.assertFalse(events)

    def test_missing_feedback_does_not_create_session_or_fake_measured_values(self):
        self.receiver.latest = None
        self.args.seconds = .2
        factory = Mock(side_effect=AssertionError('No measured state'))
        report, events = self.collect([self.event(i + 1) for i in range(20)], session_factory=factory)
        self.assertEqual(report['reason'], 'feedback_timeout')
        factory.assert_not_called()
        self.assertTrue(all(e['type'] == 'waiting_for_feedback' for e in events))

    def test_invalid_motion_request_and_missing_binding_refuse_before_supervisor(self):
        with patch.object(run, 'supervise') as supervise, patch('sys.stdout', io.StringIO()):
            self.assertEqual(run.main(['--enable-motion']), 2)
            self.assertEqual(run.main(['--side', 'right', '--sn', 'WG1KA06260821549',
                                       '--user', 'brainco-teleop', '--interface', 'lo']), 2)
            self.assertEqual(run.main(['--demo', '--enable-motion']), 2)
        supervise.assert_not_called()

    def test_demo_uses_original_filter_and_never_initializes_transport(self):
        events = []
        with patch.object(run.output, 'official_transport', side_effect=AssertionError('No hardware')):
            self.assertEqual(run.demo(run.live.mapping.DEFAULT_SOURCE, events.append), 0)
        self.assertEqual(len([e for e in events if e['type'] == 'hand_result']), 2)
        rejected = [e for e in events if e['type'] == 'synthetic_filter_rejection']
        self.assertEqual(rejected[0]['confidence'], .49)
        self.assertFalse(rejected[0]['input_quality_passed'])
        self.assertTrue(all(e['synthetic'] and not e['published'] for e in events))

    def test_worker_uses_original_reader_no_threshold_override_and_always_cleans_up(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        manager, subscriber = Mock(), Mock()
        channel = NS(ChannelFactoryInitialize=Mock(), ChannelSubscriber=Mock(return_value=subscriber))
        modules = {'wuji_sdk': NS(), 'unitree_sdk2py.core.channel': channel,
                   'unitree_sdk2py.idl.unitree_go.msg.dds_': NS(MotorStates_=object)}
        self.args.sn, self.args.user = self.binding['glove_sn'], self.binding['sdk_user']
        with patch.dict('sys.modules', modules), \
                patch.object(run.live, 'verify_sdk_context', return_value=manager), \
                patch.object(run.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(run.live, 'BridgeLines'), patch.object(run.signal, 'signal'), \
                patch.object(run, 'collect', side_effect=ValueError('test collection failure')):
            with self.assertRaisesRegex(ValueError, 'test collection failure'):
                run.worker(self.args, self.mapper, run.live.mapping.DEFAULT_SOURCE, lambda _: None)
        command = popen.call_args.args[0]
        self.assertIn(str(run.live.mapping.DEFAULT_SOURCE / 'wuji_bridge.py'), command)
        self.assertNotIn('--min-confidence', command)
        self.assertNotIn('--enable-commands', command)
        subscriber.Close.assert_called_once()
        manager.disconnect_all.assert_called_once()
        process.send_signal.assert_called()

    def test_supervisor_deadline_only_stops_own_group(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired('worker', 15)
        with patch.object(run.live, 'stop_owned') as stop:
            self.assertEqual(run.supervise([], 3, lambda _: None, popen=Mock(return_value=process)), 3)
        stop.assert_called_once_with(process, group=True)

    def test_help_needs_no_site_packages_and_unknown_quality_override_is_rejected(self):
        path = str(ROOT / 'scripts/run_wuji_hand_session.py')
        result = subprocess.run([sys.executable, '-S', path, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, '-S', path, '--min-confidence', '.1'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def wifi_report(self, side='left', seq=7, sent_ns=100_000_000_000,
                    received_ns=100_020_000_000, remote_now=9_000_000_000,
                    remote_received=8_995_000_000, request_id=1, bounds=None):
        bounds = bounds or time_bounds.FeedbackTimeBounds(self.mapper.feedback_age)
        raw = {'type': 'feedback_snapshot', 'request_id': request_id,
               'clock_id': 'linux-monotonic:PC2', 'server_send_monotonic_ns': remote_now,
               'streams': {'rt/brainco/{}/state'.format(side): {
                   'sequence': seq, 'received_monotonic_ns': remote_received,
                   'payload': {'q': [.5] * 6, 'dq': [0.] * 6}, 'error': None}}}
        report = bounds.convert(raw, request_id, sent_ns, received_ns)
        report.update(local_request_sent_ns=sent_ns, local_reply_received_ns=received_ns)
        return report

    def test_wifi_both_sides_preserve_remote_sequence_and_cached_conservative_time(self):
        for side in ('left', 'right'):
            mapper = NS(side=side, feedback_age=.2, binding={'feedback_source_id': run.wifi_feedback_source_id(side)})
            receiver = run.WifiHandFeedbackReceiver(mapper, Mock(), self.clock)
            bounds = time_bounds.FeedbackTimeBounds(.2)
            receiver.accept(self.wifi_report(side=side, bounds=bounds))
            first = copy.deepcopy(receiver.latest)
            receiver.accept(self.wifi_report(side=side, bounds=bounds, request_id=2,
                sent_ns=100_050_000_000, received_ns=100_060_000_000, remote_now=9_040_000_000))
            self.assertEqual(receiver.latest, first)
            self.assertEqual(first['sample_id'], 7)  # remote sample, not RPC request_id
            self.assertEqual(first['received_t'], 99.995)
            self.assertEqual(first['source_received_monotonic_ns'], 8_995_000_000)
            self.assertEqual(first['source_id'], run.wifi_feedback_source_id(side))

    def test_wifi_stronger_roundtrip_bound_expires_cache_without_restamping(self):
        receiver = run.WifiHandFeedbackReceiver(self.mapper, Mock(), self.clock)
        bounds = time_bounds.FeedbackTimeBounds(.2)
        receiver.accept(self.wifi_report(bounds=bounds, received_ns=100_010_000_000))
        receiver.accept(self.wifi_report(bounds=bounds, request_id=2, sent_ns=100_050_000_000,
            received_ns=100_090_000_000, remote_now=9_080_000_000))
        self.assertEqual(receiver.latest['received_t'], 99.995)
        self.assertAlmostEqual(receiver.bound_age, .125)
        self.clock.now, receiver.last_poll = 100.17, 100.15
        value, error = receiver.snapshot()
        self.assertIsNone(value)
        self.assertIn('conservative age bound', error)
        receiver.client.snapshot.assert_not_called()

    def test_wifi_rejects_stale_invalid_cache_mutation_clock_change_and_replay(self):
        for change in ('stale', 'nan', 'cache_time', 'cache_q', 'clock', 'sequence'):
            receiver = run.WifiHandFeedbackReceiver(self.mapper, Mock(), self.clock)
            report = self.wifi_report()
            receiver.accept(report)
            changed = copy.deepcopy(report)
            entry = changed['streams']['rt/brainco/left/state']
            if change == 'stale': entry['status'] = 'stale_bound'
            elif change == 'nan': entry['source']['payload']['q'][0] = float('nan')
            elif change == 'cache_time': entry['conservative_received_monotonic_ns'] += 1_000_000
            elif change == 'cache_q': entry['source']['payload']['q'][0] = .6
            elif change == 'clock': changed['source_clock_id'] = 'linux-monotonic:OTHER'
            else: entry['sequence'] = entry['source']['sequence'] = 6
            with self.subTest(change=change), self.assertRaises(ValueError):
                receiver.accept(changed)

    def test_wifi_prepare_requires_specific_source_binding_but_no_local_dds_interface(self):
        self.binding['kind'] = self.binding['sdk_model']['kind'] = 'operator_calibrated'
        self.binding['clock_id'] = run.live.clock_id()
        self.binding['feedback_source_id'] = run.wifi_feedback_source_id('left')
        binding_path = self.base / 'binding.json'
        binding_path.write_text(json.dumps(self.binding))
        args = run.parser().parse_args(['--feedback-transport', 'wifi-ssh', '--side', 'left',
             '--sn', self.binding['glove_sn'], '--user', self.binding['sdk_user'], '--binding', str(binding_path)])
        with patch.object(run.live, 'prepare', side_effect=AssertionError('No local DDS setup')):
            mapper, _ = run.prepare(args)
        self.assertEqual(mapper.binding['feedback_source_id'], self.binding['feedback_source_id'])
        self.binding['feedback_source_id'] = 'dds:wrong-old-source'
        binding_path.write_text(json.dumps(self.binding))
        with self.assertRaisesRegex(ValueError, 'feedback_source_id'):
            run.prepare(args)

    def test_wifi_motion_is_rejected_before_password_sdk_or_supervisor(self):
        with patch.object(run, 'prepare') as prepare, patch.object(run, 'supervise') as supervise, \
                patch.object(run.getpass, 'getpass') as password, patch('sys.stdout', io.StringIO()) as out:
            code = run.main(['--feedback-transport', 'wifi-ssh', '--enable-motion',
                             '--exclusive-control-confirmed', '--feedback-identity-confirmed',
                             '--command-service-confirmed'])
        self.assertEqual(code, 2)
        self.assertIn('no hand command transport', out.getvalue())
        prepare.assert_not_called()
        supervise.assert_not_called()
        password.assert_not_called()

    def test_wifi_worker_does_not_import_local_unitree_and_closes_ssh_on_error(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        manager, wifi = Mock(), Mock()
        wifi.open.return_value = {'type': 'feedback_ready'}
        self.args.feedback_transport, self.args.interface = 'wifi-ssh', None
        self.args.sn, self.args.user = self.binding['glove_sn'], self.binding['sdk_user']
        with patch.dict('sys.modules', {'wuji_sdk': NS(), 'unitree_sdk2py': None}), \
                patch.object(run.live, 'verify_sdk_context', return_value=manager), \
                patch.object(run, 'create_wifi_client', return_value=wifi), \
                patch.object(run.subprocess, 'Popen', return_value=process), \
                patch.object(run.live, 'BridgeLines'), patch.object(run.signal, 'signal'), \
                patch.object(run, 'collect', side_effect=ValueError('test failed')):
            with self.assertRaisesRegex(ValueError, 'test failed'):
                run.worker(self.args, self.mapper, run.live.mapping.DEFAULT_SOURCE, lambda _: None,
                           password='FAKE_TEST_PASSWORD')
        wifi.open.assert_called_once_with('FAKE_TEST_PASSWORD')
        wifi.close.assert_called_once()
        manager.disconnect_all.assert_called_once()
        process.send_signal.assert_called()

    def test_wifi_password_uses_pipe_not_argv_environment_or_records(self):
        process = Mock()
        process.wait.return_value = 0
        popen = Mock(return_value=process)
        with patch.object(run.live, 'stop_owned'):
            run.supervise(['--feedback-transport', 'wifi-ssh'], 3, lambda _: None,
                          popen=popen, password='FAKE_SECRET')
        self.assertNotIn('FAKE_SECRET', repr(popen.call_args))
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        envelope = process.stdin.write.call_args.args[0]
        self.assertEqual(run.read_worker_password(io.StringIO(envelope)), 'FAKE_SECRET')
        process.stdin.close.assert_called_once()

    def test_worker_rejects_malformed_password_envelope_without_echoing_secret(self):
        for payload in ('{"ssh_password":"SECRET\\nSECOND"}\n', '{"ssh_password":null}\n',
                        '{"ssh_password":"SECRET","extra":true}\n', 'not-json-SECRET\n'):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, '^Invalid worker credential envelope$'):
                run.read_worker_password(io.StringIO(payload))

    def test_wifi_feedback_reaches_existing_mapper_with_rtt_bound_and_no_dds(self):
        report = self.wifi_report()
        client = Mock()
        def snapshot():
            self.clock.now = 100.02
            return report
        client.snapshot.side_effect = snapshot
        self.receiver = run.WifiHandFeedbackReceiver(self.mapper, client, self.clock)
        self.args.seconds = .04
        summary, events = self.collect([self.event()])
        self.assertTrue(summary['ok'])
        measured = events[0]['input']['measured']
        self.assertEqual(measured['received_t'], 99.995)
        self.assertEqual(measured['sample_id'], 7)
        self.assertEqual(events[0]['result']['step_limited_1000'], [580] * 6)
        self.assertFalse(events[0]['published'])


if __name__ == '__main__':
    unittest.main()
