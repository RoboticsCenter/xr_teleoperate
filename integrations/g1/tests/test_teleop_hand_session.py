"""Only fake publishers; no SDK/network/device access in these session checks."""
import copy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session = load('hand_session_test_module', 'teleop_hand_session.py')
mapping = load('hand_session_test_mapping', 'preview_hand_dds.py')


class FakePublisher:
    def __init__(self):
        self.messages, self.closed, self.result = [], False, True

    def Write(self, message):
        self.messages.append(message)
        return self.result

    def Close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original, cls.settings = mapping.load_original()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binding = mapping.synthetic_fixture(self.directory.name)
        self.now = 100.
        self.publisher, self.factory_calls = FakePublisher(), []

    def mapper(self, simulated_real=False):
        binding = copy.deepcopy(self.binding)
        if simulated_real:
            # Fixture metadata tests the real-mode gate; these files are never
            # saved as actual operator evidence and only fake transport is used.
            binding['kind'] = binding['sdk_model']['kind'] = 'operator_calibrated'
        return mapping.HandDDSPreview(binding, self.directory.name, self.original,
                                      self.settings, allow_synthetic=not simulated_real)

    def factory(self, interface, domain, side):
        self.factory_calls.append((interface, domain, side))
        return (self.publisher, lambda: SimpleNamespace(cmds=[]),
                lambda: SimpleNamespace(mode=0, q=0., dq=0., tau=0., kp=0., kd=0., reserve=[0, 0, 0]))

    def make(self, enabled=False, **kwargs):
        mapper = self.mapper(simulated_real=enabled)
        return session.BrainCoHandSession(
            mapper, enable_motion=enabled, interface='test0',
            exclusive_control_confirmed=True, feedback_identity_confirmed=True,
            command_service_confirmed=True, transport_factory=kwargs.pop('transport_factory', self.factory),
            clock=lambda: self.now, **kwargs)

    def record(self, active, seq=1, q=None):
        return mapping.synthetic_record(active.mapper, seq=seq, now=self.now, q=q)

    def test_default_never_creates_publisher_and_reuses_measured_step_limit(self):
        active = self.make()
        result = active.step(self.record(active), original_filter_passed=True)
        self.assertEqual(result['step_limited_1000'], [580] * 6)
        self.assertFalse(result['published'])
        self.assertTrue(result['input_quality_passed'])
        self.assertFalse(result['allow_low_quality'])
        self.assertEqual(self.factory_calls, [])
        self.assertFalse(active.close()['service_timeout_policy_required'])

    def test_enable_requires_explicit_real_binding_and_all_confirmations(self):
        for options in ({}, {'enable_motion': 1},
                        {'enable_motion': True, 'interface': 'test0'},
                        {'enable_motion': True, 'interface': 'test0',
                         'exclusive_control_confirmed': True, 'feedback_identity_confirmed': True,
                         'command_service_confirmed': True}):
            if not options:
                continue
            with self.assertRaises(ValueError):
                session.BrainCoHandSession(self.mapper(), **options)
        self.assertEqual(self.factory_calls, [])

    def test_quality_is_required_and_rejection_latches_before_transport(self):
        active = self.make(True)
        result = active.step(self.record(active))
        self.assertEqual(result['reason'], 'original_input_quality_not_passed')
        self.assertFalse(result['input_quality_passed'])
        self.assertNotIn('command_preview', result)
        self.assertEqual(active.step(self.record(active), original_filter_passed=True)['status'], 'latched')
        self.assertEqual(self.factory_calls, [])

    def test_enabled_first_and_next_commands_bound_to_current_measured_position(self):
        active = self.make(True)
        for seq, q, expected in ((1, [.5] * 6, .58), (2, [.5] * 6, .58), (3, [.6] * 6, .68)):
            self.now = 100 + seq * .02
            result = active.step(self.record(active, seq, q), original_filter_passed=True)
            self.assertTrue(result['published'])
            self.assertFalse(result['acknowledged'])
            self.assertEqual([x.q for x in self.publisher.messages[-1].cmds], [expected] * 6)
        self.assertEqual(len(self.factory_calls), 1)
        self.assertEqual(active.write_count, 3)
        close = active.close()
        self.assertTrue(self.publisher.closed)
        self.assertFalse(close['physical_stop_confirmed'])
        self.assertEqual(len(self.publisher.messages), 3)  # close sends no hold

    def test_stale_nonfinite_and_replayed_inputs_latch_without_republishing(self):
        for field in ('stale_glove', 'stale_state', 'nonfinite'):
            active = self.make(True)
            record = self.record(active)
            if field == 'stale_glove':
                record['glove']['source_t'] -= 1
            elif field == 'stale_state':
                record['measured']['received_t'] -= 1
            else:
                record['measured']['q_normalized'][0] = float('nan')
            self.assertEqual(active.step(record, original_filter_passed=True)['status'], 'latched')
        self.assertEqual(self.factory_calls, [])
        active = self.make(True)
        record = self.record(active)
        active.step(record, original_filter_passed=True)
        self.now += .01
        self.assertEqual(active.step(record, original_filter_passed=True)['status'], 'latched')
        self.assertEqual(len(self.publisher.messages), 1)

    def test_input_gap_is_detected_on_idle_and_cannot_be_hidden_by_new_frame(self):
        for idle_check in (False, True):
            active = self.make(True)
            active.step(self.record(active), original_filter_passed=True)
            self.now += .21
            if idle_check:
                self.assertEqual(active.check_timeout()['reason'], 'input_timeout')
            result = active.step(self.record(active, seq=2), original_filter_passed=True)
            self.assertEqual(result['reason'], 'input_timeout')

    def test_transport_initialization_delay_rechecks_freshness_before_first_write(self):
        def slow(*args):
            self.now += .25
            return self.factory(*args)
        active = self.make(True, transport_factory=slow)
        result = active.step(self.record(active), original_filter_passed=True)
        self.assertEqual(result['reason'], 'inputs_expired_before_write')
        self.assertEqual(self.publisher.messages, [])
        active.close()
        self.assertTrue(self.publisher.closed)

    def test_write_failure_latches_and_preserves_unknown_delivery_on_close(self):
        self.publisher.result = False
        active = self.make(True)
        result = active.step(self.record(active), original_filter_passed=True)
        self.assertTrue(result['write_delivery_unknown'])
        self.assertNotIn('command_preview', result)
        self.now += .01
        self.assertTrue(active.step(self.record(active, seq=2), original_filter_passed=True)['write_delivery_unknown'])
        self.assertEqual(len(self.publisher.messages), 1)
        close = active.close()
        self.assertTrue(close['write_delivery_unknown'])
        self.assertTrue(close['service_timeout_policy_required'])

    def test_encoder_rejects_invalid_vectors_and_uses_official_field_defaults(self):
        for value in ({'cmds': []}, {'cmds': [{'q': .5, 'dq': 0}] * 6},
                      {'cmds': [{'q': float('nan'), 'dq': .2}] * 6},
                      {'cmds': [{'q': True, 'dq': .2}] * 6}):
            with self.assertRaises(ValueError):
                session.encode_command(value, lambda: SimpleNamespace(), lambda: SimpleNamespace())


if __name__ == '__main__':
    unittest.main()
