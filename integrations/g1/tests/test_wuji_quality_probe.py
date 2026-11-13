"""Input-only contract tests; no real SDK manager or hardware is created."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('wuji_quality_probe', SCRIPTS / 'wuji_quality_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def frame(seq=1, timestamp=100, confidence=.9, field='fingers'):
    return NS(header=NS(seq=seq, timestamp_us=timestamp), **{
        field: [NS(confidence=confidence, angles=[.1] * 5) for _ in range(5)]})


class Subscription:
    def __init__(self, frames=()):
        self.frames, self.closed = list(frames), False

    def recv(self):
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


class Glove:
    def __init__(self, sn='LEFT', side='left', emf_available=True):
        self.serial, self.side, self.emf_available = sn, side, emf_available
        self.ik, self.emf = Subscription([frame()]), Subscription([frame(seq=99, field='poses')])
        self.subscribed = []

    def __getattr__(self, name):
        raise AssertionError('Forbidden glove operation: ' + name)

    def sn(self):
        return NS(get=lambda: self.serial)

    def hand_side(self):
        return NS(get=lambda: self.side)

    def version(self):
        return NS(get=lambda: '1.0.0')

    def hand_joint_angles(self):
        self.subscribed.append('ik')
        return NS(subscribe=lambda: self.ik)

    def emf_poses(self):
        if not self.emf_available:
            raise AttributeError('No emf_poses resource')
        self.subscribed.append('emf')
        return NS(subscribe=lambda: self.emf)


def fake_sdk(glove=None, device_type='glove', address='192.168.1.100:50001', transport='UDP'):
    glove = Glove() if glove is None else glove
    class Manager:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            raise AssertionError('Forbidden manager operation: ' + name)

        def current_user(self):
            return {'user_id': 'u_test', 'display_name': 'operator', 'is_default': False}

        def scan(self):
            self.calls.append('scan')
            return [NS(sn='LEFT', address=address, device_type=device_type, transport_type=transport)]

        def connect(self, **kwargs):
            self.calls.append(('connect', kwargs))
            return glove

        def disconnect_all(self):
            self.calls.append('disconnect_all')
    manager = Manager()
    sdk = NS(SdkManager=NS(instance=lambda: manager), DeviceType=NS(WujiGlove='glove'),
             WujiGlove=Glove, ConnectOptions=lambda **kw: kw)
    return sdk, manager, glove


class ProbeTests(unittest.TestCase):
    def args(self, **kwargs):
        values = dict(sn='LEFT', side='left', user='operator', seconds=.003, source=probe.DEFAULT_SOURCE)
        values.update(kwargs)
        return NS(**values)

    def test_help_and_invalid_arguments_never_import_sdk(self):
        script = str(SCRIPTS / 'wuji_quality_probe.py')
        result = subprocess.run([sys.executable, '-S', script, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for extra in ([], ['--sn', 'LEFT'], ['--side', 'left'],
                      ['--sn', 'LEFT', '--side', 'left', '--seconds', 'nan'],
                      ['--sn', 'LEFT', '--side', 'left', '--seconds', '0'],
                      ['--sn', 'LEFT', '--side', 'left', '--seconds', '30.1'],
                      ['--sn', 'LEFT', '--side', 'left', '--min-confidence', '.1']):
            with self.subTest(extra=extra):
                result = subprocess.run([sys.executable, '-S', script] + extra,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)

    def test_original_filter_keeps_threshold_and_is_called_without_override(self):
        original, metadata = probe.load_original_filter(probe.DEFAULT_SOURCE)
        self.assertEqual(Path(original.__code__.co_filename), probe.DEFAULT_SOURCE / 'wuji_bridge.py')
        self.assertEqual(original.__defaults__, (.5,))
        self.assertEqual(metadata['confidence_threshold'], .5)
        stats, spy = probe.StreamStats('fingers', .5), mock.Mock(wraps=original)
        stats.add(frame(confidence=.4999), 1, spy)
        stats.add(frame(seq=2, timestamp=200, confidence=.5), 2, spy)
        self.assertEqual(stats.accepted, 1)
        self.assertEqual(spy.call_count, 2)
        self.assertTrue(all(len(call.args) == 1 and not call.kwargs for call in spy.call_args_list))

    def test_unreviewed_source_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, sentinel = Path(tmp), Path(tmp) / 'executed'
            (source / 'wuji_bridge.py').write_text('from pathlib import Path\nPath(%r).touch()\n' % str(sentinel))
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                probe.load_original_filter(source)
            self.assertFalse(sentinel.exists())
            self.assertFalse((source / '__pycache__').exists())

    def test_unknown_and_zenoh_rejected_before_connect(self):
        for options in ({'device_type': 'unknown'}, {'transport': 'Zenoh'},
                        {'address': 'zenoh://LEFT'}, {'address': 'localhost:50001'},
                        {'address': '192.168.1.100:0'}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                sdk, manager, _ = fake_sdk(**options)
                report = probe.probe_sdk(sdk, self.args(), home=tmp)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(manager.calls, ['scan', 'disconnect_all'])

    def test_missing_or_ambiguous_exact_serial_rejected(self):
        item = dict(sn='LEFT', verified_glove=True, transport='UDP', address='192.168.1.100:50001')
        for entries, serial in (([item], 'LEF'), ([item, item], 'LEFT'), ([], 'LEFT')):
            with self.assertRaises(ValueError):
                probe.select_direct_glove(entries, serial)

    def test_readback_wrong_identity_never_subscribes(self):
        for glove in (Glove(sn='OTHER'), Glove(side='right')):
            with tempfile.TemporaryDirectory() as tmp:
                sdk, manager, _ = fake_sdk(glove)
                report = probe.probe_sdk(sdk, self.args(), home=tmp)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(glove.subscribed, [])
                self.assertEqual(manager.calls[-1], 'disconnect_all')

    def test_single_direct_connection_missing_model_diagnosis_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk, manager, glove = fake_sdk()
            report = probe.probe_sdk(sdk, self.args(), home=tmp)
            self.assertEqual(report['status'], 'sampled')
            self.assertFalse(report['personal_model_available'])
            self.assertFalse(report['teleoperation_ready'])
            self.assertEqual(manager.calls[0], 'scan')
            self.assertEqual(manager.calls[1], ('connect', {
                'address': '192.168.1.100:50001', 'device_name': 'g1_wuji_quality_probe',
                'options': dict(timeout_ms=1000, retry_count=1, enable_bridge=False,
                                auto_time_sync_interval_ms=None)}))
            self.assertEqual(len(manager.calls), 3)
            self.assertEqual(manager.calls[-1], 'disconnect_all')
            self.assertTrue(glove.ik.closed and glove.emf.closed)
            self.assertEqual(report['sample']['timestamp_matching']['matched_timestamp_count'], 1)

    def test_emf_unavailable_explicit_and_does_not_block_ik_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk, _, glove = fake_sdk(Glove(emf_available=False))
            report = probe.probe_sdk(sdk, self.args(), home=tmp)
            self.assertEqual(report['status'], 'sampled')
            self.assertEqual(report['raw_emf_subscription']['status'], 'unavailable')
            self.assertEqual(report['sample']['timestamp_matching']['status'], 'unavailable_no_exact_matches')
            self.assertTrue(glove.ik.closed)

    def test_timestamp_matching_ignores_seq_rejects_ambiguous_and_nearest(self):
        ik, emf = probe.StreamStats('fingers', .5), probe.StreamStats('poses', .85)
        for seq, timestamp in [(1, 100), (2, 200), (3, 300), (4, 300)]:
            ik.add(frame(seq, timestamp, .4), seq)
        for seq, timestamp in [(99, 100), (2, 201), (3, 300)]:
            emf.add(frame(seq, timestamp, .9, field='poses'), seq)
        result = probe.matched_summary(ik, emf)
        self.assertEqual(result['matched_timestamp_count'], 1)
        self.assertEqual(result['ambiguous_ik_timestamps'], 1)
        self.assertEqual(result['unmatched_ik_timestamps'], 2)
        self.assertEqual(result['ik_confidence_by_finger']['thumb']['below_threshold'], 1)
        self.assertEqual(result['raw_emf_confidence_by_finger']['thumb']['below_threshold'], 0)

    def test_duplicates_nonfinite_and_short_frames_not_accepted(self):
        original, _ = probe.load_original_filter(probe.DEFAULT_SOURCE)
        stats = probe.StreamStats('fingers', .5)
        bad = frame(confidence=float('nan'))
        stats.add(bad, 1, original)
        stats.add(bad, 2, original)
        short = frame(2, 200)
        short.fingers.pop()
        stats.add(short, 3, original)
        result = stats.summary(0, 4)
        self.assertEqual(stats.accepted, 0)
        self.assertEqual(result['unique_frame_keys'], 2)
        self.assertEqual(result['duplicate_frames'], 1)
        self.assertEqual(result['confidence_by_finger']['little']['missing_or_nonfinite'], 2)
        json.dumps(result, allow_nan=False)

    def test_timeout_terminates_then_kills_worker_without_retry(self):
        process = mock.Mock(pid=1234, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('worker', 13),
                                            subprocess.TimeoutExpired('worker', 1), ('', '')]
        with mock.patch.object(probe.os, 'killpg') as kill:
            popen = mock.Mock(return_value=process)
            result = probe.run_bounded(self.args(seconds=3), popen)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(kill.call_args_list, [mock.call(1234, probe.signal.SIGTERM),
                                              mock.call(1234, probe.signal.SIGKILL)])
        self.assertEqual(result['error']['type'], 'WorkerTimeout')
        self.assertFalse(result['teleoperation_ready'])

    def test_stream_failure_still_closes_both_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk, manager, glove = fake_sdk()
            def failed(*args):
                raise RuntimeError('fixture disconnected')
            result = probe.probe_sdk(sdk, self.args(), home=tmp, sampler=failed)
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(glove.ik.closed and glove.emf.closed)
            self.assertEqual(manager.calls[-1], 'disconnect_all')


if __name__ == '__main__':
    unittest.main()
