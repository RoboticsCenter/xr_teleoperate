import copy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pc2_feedback_rpc import Latest, decode_body, decode_hand, request_id
from wifi_feedback_time import FeedbackTimeBounds


class FeedbackTests(unittest.TestCase):
    def reply(self):
        return {'type': 'feedback_snapshot', 'request_id': 1, 'clock_id': 'linux-monotonic:remote',
                'server_send_monotonic_ns': 9_000_000_000,
                'streams': {'rt/lowstate': {'sequence': 2, 'received_monotonic_ns': 8_995_000_000,
                                           'error': None, 'payload': {'q': [0]*29}}}}

    def test_cross_host_age_uses_roundtrip_not_raw_clock_subtraction(self):
        result = FeedbackTimeBounds().convert(self.reply(), 1, 1_000_000_000, 1_020_000_000)
        entry = result['streams']['rt/lowstate']
        self.assertEqual(entry['status'], 'fresh_bound')
        self.assertAlmostEqual(entry['age_upper_bound_s'], .025)
        self.assertEqual(entry['conservative_received_monotonic_ns'], 995_000_000)

    def test_delay_and_missing_state_do_not_look_fresh(self):
        reply = self.reply()
        reply['streams']['hand'] = None
        result = FeedbackTimeBounds().convert(reply, 1, 1_000_000_000, 1_200_000_000)
        self.assertEqual(result['streams']['rt/lowstate']['status'], 'stale_bound')
        self.assertEqual(result['streams']['hand']['status'], 'missing_or_invalid')

    def test_repeated_cached_state_keeps_first_receipt_estimate(self):
        bounds, reply = FeedbackTimeBounds(), self.reply()
        first = bounds.convert(reply, 1, 1_000_000_000, 1_020_000_000)['streams']['rt/lowstate']
        reply['request_id'] = 2
        reply['server_send_monotonic_ns'] += 10_000_000
        second = bounds.convert(reply, 2, 1_030_000_000, 1_040_000_000)['streams']['rt/lowstate']
        self.assertEqual(first['conservative_received_monotonic_ns'], second['conservative_received_monotonic_ns'])
        self.assertAlmostEqual(second['age_upper_bound_s'], .045)

    def test_invalid_request_clock_replay_and_changed_cached_payload_rejected(self):
        for mutation in ('id', 'clock', 'time', 'payload'):
            with self.subTest(mutation=mutation):
                bounds, reply = FeedbackTimeBounds(), self.reply()
                bounds.convert(reply, 1, 1_000_000_000, 1_020_000_000)
                reply = copy.deepcopy(reply)
                reply['request_id'] = 2
                if mutation == 'id': reply['request_id'] = 1
                if mutation == 'clock': reply['clock_id'] += 'changed'
                if mutation == 'time': reply['server_send_monotonic_ns'] = 1
                if mutation == 'payload': reply['streams']['rt/lowstate']['payload']['q'][0] = 1
                with self.assertRaises(ValueError):
                    bounds.convert(reply, 2, 1_030_000_000, 1_040_000_000)

    def test_invalid_latest_frame_does_not_preserve_old_good_state(self):
        latest = Latest(decode_hand, clock=lambda: 10)
        latest.receive(NS(states=[NS(q=.5,dq=0)]*6))
        self.assertIsNone(latest.snapshot()['error'])
        latest.receive(NS(states=[NS(q=float('nan'),dq=0)]*6))
        self.assertIsNone(latest.snapshot()['payload'])
        self.assertIsNotNone(latest.snapshot()['error'])
        self.assertEqual(latest.snapshot()['sequence'], 2)

    def test_body_all_physical_joints_and_faults_are_retained(self):
        motors = [NS(q=i/100,dq=0,tau_est=0,motorstate=i,mode=1) for i in range(35)]
        decoded = decode_body(NS(motor_state=motors,tick=123,mode_pr=0,mode_machine=5))
        self.assertEqual(len(decoded['q']), 29)
        self.assertEqual(decoded['motorstate'][-1], 28)

    def test_requests_have_no_control_operation(self):
        self.assertEqual(request_id({'request_id': 1}), 1)
        for value in ({'request_id': True}, {'request_id': 1, 'command': []}, {'enable': True}):
            with self.assertRaises(ValueError): request_id(value)

    def test_body_wire_crc_is_required_for_a_live_reference(self):
        motors = [NS(q=0,dq=0,tau_est=0,motorstate=0,mode=1) for _ in range(35)]
        message = NS(motor_state=motors,tick=123,mode_pr=0,mode_machine=5,crc=42)
        self.assertFalse(decode_body(message)['crc_valid'])
        self.assertTrue(decode_body(message,NS(Crc=lambda msg:42))['crc_valid'])
        with self.assertRaises(ValueError):
            decode_body(message,NS(Crc=lambda msg:43))


if __name__ == '__main__': unittest.main()
