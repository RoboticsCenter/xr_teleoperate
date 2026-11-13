"""Packet freshness and SDK callback tests; no SDK library or hardware is opened."""
import ctypes
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pico_arm_stream as stream

POSE = [.2, 1., -.4, 0., 0., 0., 1.]
HEAD = [0., 1.5, 0., 0., 0., 0., 1.]


def pose_text(pose=POSE):
    return ','.join(str(item) for item in pose)


def packet(stamp=100, *, motion=True, controllers=True, head=True):
    result = {'timeStampNs': stamp}
    if motion:
        result['Motion'] = {'len': 1, 'joints': [{'sn': 'TRACKER-1', 'p': pose_text()}]}
    if controllers:
        result['Controller'] = {side: {'pose': pose_text()} for side in ('left', 'right')}
    if head:
        result['Head'] = {'pose': pose_text(HEAD)}
    return result


class PacketFrames(unittest.TestCase):
    def setUp(self):
        self.builder = stream.FrameBuilder(session='reader-session')

    def consume(self, data, tick=10.):
        return self.builder.consume(data, sample_mono=tick, sample_at_unix_ns=123456)

    def test_complete_sdk_envelope_preserves_source_contract(self):
        result = self.consume(json.dumps({'value': json.dumps(packet())}))
        self.assertEqual(result['session'], 'reader-session')
        self.assertEqual(result['seq'], 0)
        self.assertEqual(result['trackers'], {'TRACKER-1': POSE})
        self.assertEqual(result['controllers'], {'left': POSE, 'right': POSE})
        self.assertEqual(result['head_pose'], HEAD)
        self.assertEqual(result['timestamp_source'], 'packet')
        self.assertEqual(result['tracker_sample_mono'], 10.)
        self.assertEqual(result['controller_sample_mono_by_side'], {'left': 10., 'right': 10.})
        self.assertEqual(result['controller_validity_source'], 'packet_pose')
        self.assertEqual(result['tracker_head_poses'], {'TRACKER-1': HEAD})
        self.assertEqual(result['controller_head_poses'], {'left': HEAD, 'right': HEAD})

    def test_cached_source_poses_keep_their_original_packet_head(self):
        data = packet()
        data['Motion']['timeStampNs'] = 50
        data['Controller']['left']['timeStampNs'] = 60
        self.consume(data)
        newer_head = [.6, 1.6, .2, 0., 0., 0., 1.]
        data['Head']['pose'] = pose_text(newer_head)
        data['timeStampNs'] = 101
        result = self.consume(data, 10.2)
        self.assertEqual(result['head_pose'], newer_head)
        self.assertEqual(result['tracker_head_poses'], {'TRACKER-1': HEAD})
        self.assertEqual(result['controller_head_poses']['left'], HEAD)
        self.assertEqual(result['controller_head_poses']['right'], newer_head)
        self.assertEqual(result['tracker_sample_mono'], 10.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 10.)
        data['Motion']['timeStampNs'] = 51
        data['Controller']['left']['timeStampNs'] = 61
        data['timeStampNs'] = 102
        result = self.consume(data, 10.3)
        self.assertEqual(result['tracker_head_poses'], {'TRACKER-1': newer_head})
        self.assertEqual(result['controller_head_poses'], {'left': newer_head, 'right': newer_head})

    def test_new_pose_without_valid_packet_head_removes_old_pair(self):
        for bad_head in (None, {'pose': '0,0,0,0,0,0,0'},
                         {'pose': pose_text(HEAD), 'trackingValid': False}):
            with self.subTest(head=bad_head):
                self.builder.reset()
                self.consume(packet())
                data = packet(101)
                data['Head'] = bad_head
                result = self.consume(data, 10.1)
                self.assertIsNone(result['head_pose'])
                self.assertEqual(result['tracker_head_poses'], {})
                self.assertEqual(result['controller_head_poses'], {})
                self.assertTrue(result['trackers'])
                self.assertTrue(result['controllers'])

    def test_missing_current_head_keeps_cached_pair_but_not_global_head_validity(self):
        data = packet()
        data['Motion']['timeStampNs'] = 50
        for side in ('left', 'right'):
            data['Controller'][side]['timeStampNs'] = 60
        self.consume(data)
        data.pop('Head')
        data['timeStampNs'] = 101
        result = self.consume(data, 10.1)
        self.assertIsNone(result['head_pose'])
        self.assertEqual(result['tracker_head_poses'], {'TRACKER-1': HEAD})
        self.assertEqual(result['controller_head_poses'], {'left': HEAD, 'right': HEAD})

    def test_source_removal_and_invalidity_remove_matching_head_pairs(self):
        self.consume(packet())
        data = packet(101)
        data['Motion']['len'] = 0
        data['Controller']['left']['isTracked'] = False
        result = self.consume(data, 10.1)
        self.assertEqual(result['tracker_head_poses'], {})
        self.assertEqual(result['controller_head_poses'], {'right': HEAD})
        result = self.consume(packet(102, controllers=False, motion=False), 10.2)
        self.assertEqual(result['controller_head_poses'], {})
        self.assertEqual(result['tracker_head_poses'], {})

    def test_paired_head_snapshot_does_not_alias_global_or_internal_cache(self):
        data = packet()
        data['Controller']['left']['timeStampNs'] = 60
        result = self.consume(data)
        result['head_pose'][0] = 8.
        self.assertEqual(result['controller_head_poses']['left'], HEAD)
        result['controller_head_poses']['left'][0] = 9.
        data['timeStampNs'] = 101
        result = self.consume(data, 10.1)
        self.assertEqual(result['controller_head_poses']['left'], HEAD)

    def test_stationary_fresh_poses_remain_valid_without_movement_gate(self):
        self.consume(packet())
        result = self.consume(packet(101), 4000.)
        self.assertEqual(result['trackers']['TRACKER-1'], POSE)
        self.assertEqual(result['controllers']['left'], POSE)
        self.assertEqual(result['tracker_sample_mono'], 4000.)
        self.assertEqual(result['controller_sample_mono'], 4000.)
        self.assertEqual(result['pose_age_ms']['TRACKER-1'], 3990000.)

    def test_head_object_packets_cannot_refresh_disabled_controller_cache(self):
        self.consume(packet())
        result = self.consume(packet(101, controllers=False), 11.)
        self.assertEqual(result['controllers'], {})
        self.assertEqual(result['controller_sample_mono'], 10.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 10.)
        self.assertEqual(result['tracker_sample_mono'], 11.)
        result = self.consume(packet(102, controllers=False), 12.)
        self.assertEqual(result['controllers'], {})
        self.assertEqual(result['controller_sample_mono'], 10.)

    def test_controller_packets_cannot_refresh_disabled_head_or_tracker_cache(self):
        self.consume(packet())
        result = self.consume(packet(101, motion=False, head=False), 11.)
        self.assertEqual(result['trackers'], {})
        self.assertIsNone(result['head_pose'])
        self.assertEqual(result['tracker_sample_mono'], 10.)
        self.assertEqual(result['head_sample_mono'], 10.)
        self.assertEqual(result['controller_sample_mono'], 11.)

    def test_per_side_absence_and_explicit_invalid_tracking_omit_pose(self):
        self.consume(packet())
        for flag in ('trackingValid', 'isTracked', 'isConnected'):
            data = packet(101)
            data['Controller'] = {'left': {'pose': pose_text(), flag: False}}
            self.builder.reset()
            result = self.consume(data)
            self.assertEqual(result['controllers'], {})
            self.assertEqual(result['controller_sample_mono_by_side'], {})

    def test_source_timestamps_age_independently_of_other_packets(self):
        data = packet()
        data['Motion']['timeStampNs'] = 50
        data['Controller']['left']['timeStampNs'] = 60
        self.consume(data)
        data['timeStampNs'] = 101
        result = self.consume(data, 11.)
        self.assertEqual(result['tracker_sample_mono'], 10.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 10.)
        self.assertEqual(result['controller_sample_mono_by_side']['right'], 11.)
        self.assertEqual(result['controller_sample_mono'], 11.)
        data['timeStampNs'] = 102
        data['Motion']['timeStampNs'] = 51
        data['Controller']['left']['timeStampNs'] = 61
        result = self.consume(data, 12.)
        self.assertEqual(result['tracker_sample_mono'], 12.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 12.)

    def test_duplicate_or_old_packets_do_not_emit_new_samples(self):
        self.consume(packet())
        self.assertIsNone(self.consume(packet(), 11.))
        self.assertIsNone(self.consume(packet(99), 12.))
        self.assertIsNone(self.consume(packet(0), 13.))
        result = self.consume(packet(101), 14.)
        self.assertEqual(result['seq'], 1)

    def test_source_timestamp_domains_are_not_compared_to_each_other(self):
        self.consume(packet(10000))
        data = packet(10001)
        data['Motion']['timeStampNs'] = 50
        data['Controller']['left']['timeStampNs'] = 60
        result = self.consume(data, 11.)
        self.assertEqual(result['tracker_sample_mono'], 11.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 11.)
        result = self.consume(packet(10002), 12.)
        self.assertEqual(result['tracker_sample_mono'], 12.)
        self.assertEqual(result['controller_sample_mono_by_side']['left'], 12.)

    def test_motion_invalidity_clears_pose_even_if_source_timestamp_is_stalled(self):
        data = packet()
        data['Motion']['timeStampNs'] = 50
        self.consume(data)
        data['timeStampNs'] = 101
        data['Motion']['countValid'] = False
        result = self.consume(data, 11.)
        self.assertEqual(result['trackers'], {})
        self.assertEqual(result['tracker_sample_mono'], 10.)

    def test_motion_len_excludes_released_client_stale_array_tail(self):
        data = packet()
        data['Motion']['joints'].append({'sn': 'DISCONNECTED', 'p': pose_text()})
        result = self.consume(data)
        self.assertEqual(list(result['trackers']), ['TRACKER-1'])
        data['timeStampNs'] = 101
        data['Motion']['len'] = 0
        self.assertEqual(self.consume(data)['trackers'], {})

    def test_invalid_geometry_is_omitted_without_changing_other_source(self):
        for invalid in ('0,0,0,0,0,0,0', 'nan,0,0,0,0,0,1', 'bad', [1, 2],
                        [True, 1., 0., 0., 0., 0., 1.]):
            self.builder.reset()
            data = packet()
            data['Controller']['left']['pose'] = invalid
            data['Motion']['joints'][0]['p'] = invalid
            result = self.consume(data)
            self.assertEqual(result['controllers'], {'right': POSE})
            self.assertEqual(result['trackers'], {})

    def test_reset_accepts_restarted_source_clock_and_clears_stale_sources(self):
        self.consume(packet())
        self.builder.reset()
        result = self.consume(packet(1, controllers=False, motion=False), 12.)
        self.assertEqual(result['controllers'], {})
        self.assertEqual(result['trackers'], {})
        self.assertIsNone(result['controller_sample_mono'])
        self.assertIsNone(result['tracker_sample_mono'])
        self.assertEqual(result['session'], 'reader-session')

    def test_invalid_envelope_does_not_advance_timestamp(self):
        for invalid in ('not-json', '[]', {'value': '[]'}):
            with self.assertRaises(ValueError):
                self.consume(invalid)
        self.assertEqual(self.consume(packet())['seq'], 0)


class NativeCallback(unittest.TestCase):
    def test_constructor_is_inert_and_start_close_use_exactly_one_client(self):
        library = Mock()
        library.PXREAInit.return_value = 0
        loader = Mock(return_value=library)
        client = stream.RawSdkClient(library_path='/fake/sdk.so', loader=loader)
        loader.assert_not_called()
        client.start()
        client.start()
        self.assertEqual(library.PXREAInit.call_count, 1)
        self.assertEqual(ctypes.sizeof(stream.DeviceStateJson), 16384)
        self.assertEqual(library.PXREAInit.argtypes,
                         [ctypes.c_void_p, stream.Callback, ctypes.c_uint])
        client.close()
        client.close()
        self.assertEqual(library.PXREADeinit.call_count, 1)

    def test_callback_copies_data_and_keeps_only_latest_complete_packet(self):
        client = stream.RawSdkClient()
        message = stream.DeviceStateJson()
        message.stateJson = json.dumps({'value': json.dumps(packet())}).encode()
        client._receive(None, stream.DEVICE_STATE_JSON, 0, ctypes.addressof(message))
        message.stateJson = b'changed after callback'
        _, latest = client.take_latest()
        self.assertEqual(stream.decode_packet(latest[0])['timeStampNs'], 100)
        self.assertEqual(client.take_latest(), (0, None))
        for stamp in (101, 102, 103):
            message.stateJson = json.dumps(packet(stamp)).encode()
            client._receive(None, stream.DEVICE_STATE_JSON, 0, ctypes.addressof(message))
        _, latest = client.take_latest()
        self.assertEqual(stream.decode_packet(latest[0])['timeStampNs'], 103)
        client._receive(None, stream.SERVER_DISCONNECT, 0, None)
        self.assertEqual(client.take_latest(), (1, None))

    def test_nonblocking_pipe_drops_full_buffer_instead_of_queueing(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        os.set_blocking(writer, False)
        frame = stream.FrameBuilder().consume(packet(), sample_mono=10., sample_at_unix_ns=1)
        self.assertTrue(stream.write_frame(writer, frame))
        result = json.loads(os.read(reader, 4096))
        self.assertEqual(result['controllers']['left'], POSE)
        while True:
            try:
                os.write(writer, b'x' * 4096)
            except BlockingIOError:
                break
        self.assertFalse(stream.write_frame(writer, frame))


if __name__ == '__main__':
    unittest.main()
