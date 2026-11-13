#!/usr/bin/env python3
"""Discover Wuji gloves without connecting; optionally preview the original reader.

Default mode only scans and reads local SDK user/model metadata. A finite live
preview requires an exact SN, side, named SDK user and a WujiGlove discovery type.
The source reader and its mapping remain in the user-selected upstream project.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


DEFAULT_SOURCE = Path('/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop')


def bounded_int(low, high):
    def parse(value):
        value = int(value)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f'must be between {low} and {high}')
        return value
    return parse


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    p.add_argument('--output', type=Path, help='Write the structured JSON report here')
    p.add_argument('--sn', help='Exact discovered WujiGlove SN for a live preview')
    p.add_argument('--side', choices=('left', 'right'))
    p.add_argument('--user', help='Required active SDK user; never switches users')
    p.add_argument('--frames', type=bounded_int(0, 120), default=0,
                   help='0 = discovery only; 1–120 = original reader input preview')
    p.add_argument('--scan-timeout', type=bounded_int(2, 60), default=15)
    p.add_argument('--preview-timeout', type=bounded_int(2, 30), default=10)
    p.add_argument('--_scan-worker', action='store_true', help=argparse.SUPPRESS)
    return p


def user_summary(user):
    # Do not export descriptions, external IDs, credentials or registry contents.
    return {key: user.get(key) for key in ('user_id', 'display_name', 'is_default')}


def scan_sdk(sdk):
    """Only manager lifecycle/discovery/user reads: no device connection."""
    manager = sdk.SdkManager.instance()
    try:
        current_user = user_summary(manager.current_user())
        users = [user_summary(user) for user in manager.list_users()]
        devices = []
        for device in manager.scan():
            dtype = device.device_type
            devices.append({
                'sn': device.sn,
                'address': device.address,
                'transport': str(device.transport_type),
                'device_type': str(dtype),
                'verified_glove': dtype == sdk.DeviceType.WujiGlove,
            })
        return {'devices': devices, 'current_user': current_user, 'users': users}
    finally:
        # This worker owns no connections; other processes are not disconnected.
        manager.disconnect_all()


def scan_worker():
    # Native SDK logging can use fd 1. Keep the parent/worker JSON channel clean.
    saved_stdout = os.dup(1)
    try:
        sys.stdout.flush()
        os.dup2(2, 1)
        try:
            import wuji_sdk
            report = scan_sdk(wuji_sdk)
            report['sdk_version'] = importlib.metadata.version('wuji-sdk')
        except Exception as exc:
            report = {'error': {'type': type(exc).__name__, 'message': str(exc)}}
        sys.stdout.flush()
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 1 if 'error' in report else 0


def run_scan(timeout, runner=subprocess.run):
    try:
        result = runner([sys.executable, str(Path(__file__).resolve()), '--_scan-worker'],
                        capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'error': {'type': 'ScanTimeout', 'message': f'SDK scan exceeded {timeout}s'}}
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {'error': {'type': 'InvalidWorkerResult',
                         'message': 'SDK worker did not return structured JSON'},
                'worker_exit_code': result.returncode,
                'sdk_log': result.stderr[-4000:]}
    report['worker_exit_code'] = result.returncode
    report['sdk_log'] = result.stderr[-4000:]
    return report


def model_summary(path):
    result = {'path': str(path), 'exists': path.is_file(), 'readable_xml': False}
    if result['exists']:
        try:
            data = path.read_bytes()
            root = ET.fromstring(data)
            result.update({
                'readable_xml': root.tag == 'robot',
                'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'modified_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                'joint_count': len(root.findall('joint')),
                'movable_joint_count': sum(j.get('type') != 'fixed' for j in root.findall('joint')),
            })
        except (OSError, ET.ParseError) as exc:
            result['error'] = type(exc).__name__
    return result


def calibration_summary(current_user, source, home=None):
    home = Path.home() if home is None else Path(home)
    result = {'sdk_models': {}, 'model_presence_does_not_verify_wearer_or_accuracy': True}
    uid = current_user.get('user_id') if current_user else None
    if uid and isinstance(uid, str) and Path(uid).name == uid and uid not in ('.', '..'):
        base = home / '.wuji' / 'sdk' / 'users' / uid / 'models'
        result['sdk_models'] = {side: model_summary(base / f'{side}_hand.urdf')
                                for side in ('left', 'right')}
    else:
        result['sdk_model_note'] = 'No named current SDK user; no user hand model verified'
    path = source / 'calibration.json'
    mapping = {'path': str(path), 'exists': path.is_file()}
    if mapping['exists']:
        try:
            data = json.loads(path.read_text())
            mapping.update({key: data.get(key) for key in ('side', 'time', 'channels')})
            mapping['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            opened, closed = data.get('open', []), data.get('closed', [])
            mapping['thumb_opposition_follows_flexion'] = (
                len(opened) == 6 and len(closed) == 6 and
                opened[1] is None and closed[1] is None)
        except (OSError, ValueError, TypeError) as exc:
            mapping['error'] = type(exc).__name__
    result['upstream_finger_mapping'] = mapping
    return result


def network_summary(runner=subprocess.run):
    result = {'default_glove_ips': {'left': '192.168.1.100', 'right': '192.168.1.101'}}
    try:
        data = json.loads(runner(['ip', '-j', '-4', 'address', 'show'], capture_output=True,
                                 text=True, check=True, timeout=3).stdout)
        result['interfaces'] = [
            {'name': item['ifname'], 'state': item.get('operstate'),
             'ipv4': [f"{a['local']}/{a['prefixlen']}" for a in item.get('addr_info', [])
                      if a.get('family') == 'inet']}
            for item in data if item['ifname'] != 'lo']
        result['has_default_glove_subnet'] = any(
            ipaddress.ip_address('192.168.1.100') in ipaddress.ip_interface(address).network
            for interface in result['interfaces'] for address in interface['ipv4'])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['error'] = type(exc).__name__
    return result


def select_glove(devices, sn):
    matches = [d for d in devices if d['sn'] == sn]
    if not matches:
        raise ValueError('Requested SN is absent from this scan; no connection attempted')
    # Unknown Zenoh discovery is not upgraded to a glove based on its name/SN.
    if not all(d.get('verified_glove') is True for d in matches):
        raise ValueError('Requested SN is not a verified DeviceType.WujiGlove; no connection attempted')
    return matches[0]


def preview_original(args, scan, calibration, runner=subprocess.run):
    select_glove(scan.get('devices', []), args.sn)
    current = scan.get('current_user', {})
    if current.get('is_default') is not False or current.get('display_name') != args.user:
        raise ValueError('Active named SDK user differs from --user; no user switch or connection attempted')
    if not calibration.get('sdk_models', {}).get(args.side, {}).get('readable_xml'):
        raise ValueError('Requested side has no readable named-user hand model; no connection attempted')
    bridge = args.source / 'wuji_bridge.py'
    if not bridge.is_file():
        raise ValueError('Original wuji_bridge.py is missing')
    command = [sys.executable, str(bridge), '--sn', args.sn, '--side', args.side,
               '--user', args.user, '--count', str(args.frames), '--timeout-s', '5']
    try:
        result = runner(command, cwd=args.source, capture_output=True, text=True,
                        timeout=args.preview_timeout)
    except subprocess.TimeoutExpired:
        return {'status': 'preview_timeout', 'frames': [], 'timeout_s': args.preview_timeout}
    frames = []
    for line in result.stdout.splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(frame, dict) and frame.get('sn') == args.sn
                and frame.get('hand_side') == args.side and frame.get('sdk_user') == args.user
                and isinstance(frame.get('joint_angles'), dict)):
            frames.append(frame)
    success = result.returncode == 0 and len(frames) == args.frames
    return {'status': 'preview_complete' if success else 'preview_incomplete',
            'reader': str(bridge), 'exit_code': result.returncode,
            'requested_frames': args.frames, 'received_frames': len(frames),
            'frames': frames, 'reader_log': result.stderr[-4000:],
            'hardware_output': False}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args._scan_worker:
        return scan_worker()
    if args.frames and not all((args.sn, args.side, args.user)):
        p.error('--frames requires --sn, --side and --user')
    args.source = args.source.expanduser().resolve()
    scan = run_scan(args.scan_timeout)
    calibration = calibration_summary(scan.get('current_user'), args.source)
    report = {'timestamp_utc': datetime.now(timezone.utc).isoformat(),
              'mode': 'input_preview' if args.frames else 'discovery_only',
              'hardware_output': False, 'network': network_summary(),
              'discovery': scan, 'calibration': calibration}
    gloves = [d for d in scan.get('devices', []) if d.get('verified_glove')]
    if 'error' in scan:
        report['status'] = 'scan_failed'
    elif gloves:
        report['status'] = 'gloves_discovered'
    elif scan.get('devices'):
        report['status'] = 'no_verified_glove'
    else:
        report['status'] = 'no_devices'
    if args.frames and 'error' not in scan:
        try:
            report['preview'] = preview_original(args, scan, calibration)
            report['status'] = report['preview']['status']
        except ValueError as exc:
            report['status'] = 'preview_refused'
            report['reason'] = str(exc)
    if not gloves:
        report['next_steps'] = [
            'Confirm the glove Ethernet adapter reaches this host or an identified SDK bridge host.',
            'For a direct glove link, verify its actual subnet (factory defaults: 192.168.1.100/.101).',
            'Unknown/Zenoh entries need type verification; they are never auto-connected by this probe.',
        ]
    encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['status'] in ('gloves_discovered', 'preview_complete') else 1


if __name__ == '__main__':
    raise SystemExit(main())
