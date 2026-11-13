import importlib.util
import json
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts/pico_raw_probe.py'
SPEC = importlib.util.spec_from_file_location('pico_raw_probe', PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def packet(count=2, stamp=100, joints=None):
    if joints is None:
        joints = [{'sn': 'LEFT', 'p': '0,0,0,0,0,0,1'},
                  {'sn': 'RIGHT', 'p': '1,2,3,0,0,0,1'}]
    return json.dumps({'value': json.dumps({'timeStampNs': stamp,
                       'Motion': {'len': count, 'joints': joints}})})


def with_controller(*, trigger=.8, grip=0., focus=True, input_mode=1, stamp=100):
    data = json.loads(json.loads(packet(count=1, stamp=stamp))['value'])
    data.update(Controller={'right': {'trigger': trigger, 'grip': grip}},
                appState={'focus': focus}, Input=input_mode)
    return {'value': data}


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.decoder = module.PacketDecoder()

    def test_two_received_poses_are_atomic_but_not_motion_ready(self):
        r = self.decoder.decode(packet(), 'HEADSET', 1000)
        self.assertTrue(r['two_tracker_packet'])
        self.assertTrue(r['snapshot_atomic'])
        self.assertFalse(r['motion_ready'])
        self.assertFalse(r['confidence_available'])
        self.assertIsNone(r['trackers'][0]['tracking_valid'])

    def test_valid_length_drops_old_cached_tail(self):
        self.assertEqual(len(self.decoder.decode(packet(count=1), 'H', 1)['trackers']), 1)
        self.assertEqual(self.decoder.decode(packet(count=0, stamp=101), 'H', 2)['trackers'], [])

    def test_bad_count_is_never_clamped_into_a_valid_pair(self):
        for count in (-1, 4, True, None, '2'):
            r = self.decoder.decode(packet(count=count), 'H', 1)
            self.assertIn('invalid_motion_count', r['issues'])
            self.assertEqual(r['trackers'], [])

    def test_duplicate_serials_and_nonfinite_pose_refuse_pair(self):
        for joints in ([{'sn': 'A', 'p': '0,0,0,0,0,0,1'}] * 2,
                       [{'sn': 'A', 'p': 'NaN,0,0,0,0,0,1'}, {'sn': 'B', 'p': '0,0,0,0,0,0,1'}]):
            r = module.PacketDecoder().decode(packet(joints=joints), 'H', 1)
            self.assertFalse(r['two_tracker_packet'])

    def test_replay_and_clock_regression_do_not_reset_watermark(self):
        self.decoder.decode(packet(stamp=100), 'H', 1)
        for stamp in (100, 99, 100):
            self.assertIn('repeated_or_regressed_app_timestamp', self.decoder.decode(packet(stamp=stamp), 'H', 2)['issues'])
        self.assertTrue(self.decoder.decode(packet(stamp=101), 'H', 3)['two_tracker_packet'])

    def test_disconnect_reset_and_device_clocks_are_separate(self):
        self.decoder.decode(packet(), 'A', 1)
        self.assertTrue(self.decoder.decode(packet(), 'B', 2)['two_tracker_packet'])
        self.decoder.reset('A')
        self.assertTrue(self.decoder.decode(packet(), 'A', 3)['two_tracker_packet'])
        self.assertFalse(self.decoder.decode(packet(), 'B', 4)['two_tracker_packet'])

    def test_body_stream_cannot_become_wrist_trackers(self):
        r = self.decoder.decode(json.dumps({'value': {'timeStampNs': 100, 'Body': {}}}), 'H', 1)
        self.assertEqual(r['trackers'], [])
        self.assertIn('no_independent_motion_stream', r['issues'])

    def test_missing_timestamp_never_counts_as_live_pair(self):
        for stamp in (0, None, True, '100'):
            r = module.PacketDecoder().decode(packet(stamp=stamp), 'H', 1)
            self.assertFalse(r['two_tracker_packet'])

    def test_confidence_is_only_reported_when_transmitted(self):
        joints = [{'sn': 'A', 'p': '0,0,0,0,0,0,1', 'confidence': 1},
                  {'sn': 'B', 'p': '0,0,0,0,0,0,1', 'confidence': 2}]
        r = self.decoder.decode(packet(joints=joints), 'H', 1)
        self.assertTrue(r['confidence_available'])
        self.assertTrue(r['trackers'][0]['tracking_valid'])
        self.assertFalse(r['trackers'][1]['tracking_valid'])
        self.assertFalse(r['motion_ready'])

    def test_right_trigger_uses_explicit_analog_threshold(self):
        for amount, held in ((0., False), (.799, False), (.8, True), (1., True)):
            r = module.PacketDecoder().decode(json.dumps(with_controller(trigger=amount)), 'H', 123)
            enable = r['controller_enable']
            self.assertIs(enable['held'], held)
            self.assertEqual(enable['threshold'], .8)
            self.assertEqual(enable['control'], 'trigger')
            self.assertIsNone(enable['vendor_controller_valid'])
            self.assertIsNone(r['trackers'][0]['tracking_valid'])

    def test_truthy_strings_bools_nonfinite_and_out_of_range_never_enable(self):
        for amount in (True, False, '1', 'true', None, -1., 1.01, float('nan'), float('inf')):
            with self.subTest(amount=amount):
                r = module.PacketDecoder().decode(json.dumps(with_controller(trigger=amount)), 'H', 1)
                self.assertIsNone(r['controller_enable']['held'])
                self.assertIsNone(r['controller_enable']['trigger'])
                json.dumps(r, allow_nan=False)

    def test_missing_focus_mode_or_selected_control_stays_unknown(self):
        for section, field in (('appState', 'focus'), ('Controller', 'right'), (None, 'Input')):
            raw = with_controller()
            if section:
                del raw['value'][section][field]
            else:
                del raw['value'][field]
            r = self.decoder.decode(json.dumps(raw), 'H'+field, 1)
            self.assertIsNone(r['controller_enable']['held'])
        for focus, mode in (('true', 1), (1, 1), (True, True), (True, '1')):
            r = module.PacketDecoder().decode(json.dumps(with_controller(focus=focus,input_mode=mode)), 'H', 1)
            self.assertIsNone(r['controller_enable']['held'])

    def test_focus_loss_and_inactive_controller_mode_inhibit(self):
        for focus, mode in ((False,1), (True,0), (True,2)):
            r = module.PacketDecoder().decode(json.dumps(with_controller(focus=focus,input_mode=mode)), 'H', 1)
            self.assertIs(r['controller_enable']['held'], False)
            self.assertEqual(r['controller_enable']['status'], 'inhibited')

    def test_no_cached_button_survives_next_packet(self):
        first = self.decoder.decode(json.dumps(with_controller(trigger=1.)), 'H', 1)
        self.assertIs(first['controller_enable']['held'], True)
        raw = with_controller(stamp=101)
        del raw['value']['Controller']
        second = self.decoder.decode(json.dumps(raw), 'H', 2)
        self.assertIsNone(second['controller_enable']['held'])

    def test_replayed_packet_inhibits_button_despite_numeric_press(self):
        raw = json.dumps(with_controller())
        self.decoder.decode(raw, 'H', 1)
        repeated = self.decoder.decode(raw, 'H', 2)
        self.assertIs(repeated['controller_enable']['held'], False)
        self.assertFalse(repeated['controller_enable']['packet_structure_ok'])

    def test_button_and_tracker_share_exact_packet_identity(self):
        r = self.decoder.decode(json.dumps(with_controller(stamp=999)), 'H', 555_000_111)
        e = r['controller_enable']
        self.assertEqual(e['sequence'], r['packet_sequence'])
        self.assertEqual(e['local_monotonic_ns'], r['local_monotonic_ns'])
        self.assertEqual(e['app_timestamp_ns'], r['app_timestamp_ns'])
        self.assertEqual(e['received_t'], 555_000_111/1e9)
        self.assertFalse(e['receiver_age_checked'])

    def test_explicit_grip_selection_never_ors_with_trigger(self):
        decoder = module.PacketDecoder(enable_control='grip')
        raw = json.dumps(with_controller(trigger=1.,grip=0.))
        self.assertFalse(decoder.decode(raw, 'H', 1)['controller_enable']['held'])
        raw = json.dumps(with_controller(trigger=0.,grip=1.,stamp=101))
        self.assertTrue(decoder.decode(raw, 'H', 2)['controller_enable']['held'])

    def test_missing_motion_and_bad_count_cannot_leave_held_true(self):
        for mode in ('missing', 'bad_count'):
            raw = with_controller()
            if mode == 'missing':
                del raw['value']['Motion']
            else:
                raw['value']['Motion']['len'] = True
            result = module.PacketDecoder().decode(json.dumps(raw), 'H', 1)
            self.assertFalse(result['controller_enable']['held'])

    def test_saved_raw_capture_button_counts_and_unknown_tracker_quality(self):
        path = PATH.parents[1]/'diagnostics/teleop-pico-current-raw-20261111-1810.jsonl'
        if not path.exists():
            self.skipTest('session capture unavailable')
        counts = {'held':0, 'released':0}
        for line in path.read_text().splitlines():
            row = json.loads(line)
            result = self.decoder.decode(row['raw'], row['device_id'], row['local_monotonic_ns'])
            self.assertIsNone(result['trackers'][0]['tracking_valid'])
            self.assertEqual(result['motion_declared_count'], 1)
            self.assertEqual(len(result['trackers']), 1)
            self.assertTrue(result['app_focused'])
            self.assertEqual(result['controller_enable']['grip'], 0.)
            counts[result['controller_enable']['status']] += 1
        self.assertEqual(counts, {'held':39, 'released':815})


if __name__ == '__main__':
    unittest.main()
