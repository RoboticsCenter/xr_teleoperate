"""Isolation checks for discovery and delegation to the original input reader."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'wuji_probe.py'
SPEC = importlib.util.spec_from_file_location('wuji_probe', SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class OnlyReadManager:
    """Any SDK connection, publishing or mutation is an immediate test failure."""
    def __init__(self, devices):
        self.devices = devices
        self.calls = []

    def __getattr__(self, name):
        raise AssertionError(f'Unapproved SDK operation: {name}')

    def current_user(self):
        self.calls.append('current_user')
        return {'user_id': 'u_test', 'display_name': 'operator', 'is_default': False,
                'external_id': 'must-not-export', 'description': 'must-not-export'}

    def list_users(self):
        self.calls.append('list_users')
        return []

    def scan(self):
        self.calls.append('scan')
        return self.devices

    def disconnect_all(self):
        self.calls.append('disconnect_all')


class ProbeTests(unittest.TestCase):
    def test_discovery_never_connects_or_writes_and_filters_exact_type(self):
        glove_type, hand_type, unknown_type = object(), object(), object()
        devices = [SimpleNamespace(sn=sn, address='zenoh://' + sn,
                                   transport_type='Zenoh', device_type=dtype)
                   for sn, dtype in [('glove', glove_type), ('robot', hand_type),
                                     ('WG1K_unknown', unknown_type)]]
        manager = OnlyReadManager(devices)
        sdk = SimpleNamespace(SdkManager=SimpleNamespace(instance=lambda: manager),
                              DeviceType=SimpleNamespace(WujiGlove=glove_type))
        report = probe.scan_sdk(sdk)
        self.assertEqual([d['verified_glove'] for d in report['devices']], [True, False, False])
        self.assertEqual(manager.calls, ['current_user', 'list_users', 'scan', 'disconnect_all'])
        self.assertNotIn('must-not-export', json.dumps(report))

    def test_unknown_robot_or_conflicting_duplicate_cannot_be_selected(self):
        for devices in ([{'sn': 'X', 'verified_glove': False}],
                        [{'sn': 'X', 'verified_glove': True},
                         {'sn': 'X', 'verified_glove': False}]):
            with self.assertRaises(ValueError):
                probe.select_glove(devices, 'X')
        with self.assertRaises(ValueError):
            probe.select_glove([{'sn': 'other', 'verified_glove': True}], 'X')

    def test_exact_serial_required(self):
        chosen = {'sn': 'WG1K_exact', 'verified_glove': True}
        self.assertIs(probe.select_glove([chosen], 'WG1K_exact'), chosen)
        with self.assertRaises(ValueError):
            probe.select_glove([chosen], 'WG1K')

    def test_help_without_site_packages_or_sdk(self):
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--frames', result.stdout)

    def test_finite_frames_and_explicit_identity_required(self):
        for extra in (['--frames', '1'], ['--frames', '-1'], ['--frames', '121']):
            result = subprocess.run([sys.executable, '-S', str(SCRIPT), *extra],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_scan_timeout_is_structured(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        self.assertEqual(probe.run_scan(2, timeout)['error']['type'], 'ScanTimeout')

    def test_preview_refuses_missing_model_and_wrong_user_before_runner(self):
        args = SimpleNamespace(sn='X', side='left', user='operator')
        scan = {'devices': [{'sn': 'X', 'verified_glove': True}],
                'current_user': {'is_default': False, 'display_name': 'other'}}
        def forbidden(*args, **kwargs):
            self.fail('Must refuse before starting any subprocess')
        with self.assertRaises(ValueError):
            probe.preview_original(args, scan, {}, forbidden)
        scan['current_user']['display_name'] = 'operator'
        with self.assertRaises(ValueError):
            probe.preview_original(args, scan, {'sdk_models': {}}, forbidden)

    def test_valid_preview_delegates_only_to_original_reader_with_finite_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / 'wuji_bridge.py').write_text('# fixture, never executed')
            args = SimpleNamespace(source=source, sn='X', side='right', user='operator',
                                   frames=1, preview_timeout=4)
            scan = {'devices': [{'sn': 'X', 'verified_glove': True}],
                    'current_user': {'is_default': False, 'display_name': 'operator'}}
            cal = {'sdk_models': {'right': {'readable_xml': True}}}
            def runner(command, **kwargs):
                self.assertEqual(command[1], str(source / 'wuji_bridge.py'))
                self.assertEqual(command[2:], ['--sn', 'X', '--side', 'right',
                                               '--user', 'operator', '--count', '1',
                                               '--timeout-s', '5'])
                self.assertEqual(kwargs['timeout'], 4)
                frame = {'sn': 'X', 'hand_side': 'right', 'sdk_user': 'operator',
                         'joint_angles': {'thumb': 20}}
                return SimpleNamespace(stdout=json.dumps(frame), stderr='', returncode=0)
            self.assertEqual(probe.preview_original(args, scan, cal, runner)['status'],
                             'preview_complete')

    def test_model_summary_is_local_read_only_and_side_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / '.wuji' / 'sdk' / 'users' / 'u_test' / 'models'
            models.mkdir(parents=True)
            path = models / 'right_hand.urdf'
            path.write_text('<robot><joint type="fixed"/><joint type="revolute"/></robot>')
            before = path.read_bytes()
            result = probe.calibration_summary({'user_id': 'u_test'}, root, home=root)
            self.assertFalse(result['sdk_models']['left']['exists'])
            self.assertEqual(result['sdk_models']['right']['movable_joint_count'], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(models.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
