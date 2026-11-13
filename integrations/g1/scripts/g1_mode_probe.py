#!/usr/bin/env python3
"""Read-only G1 queries and writer observation; never changes robot mode.

Uses SDK2 Python commit 814556d15970dd2ecf1c9984e845ca02ab07e206.
No rt/arm_sdk or rt/lowcmd publisher is created. Default sends CheckMode/API1001
once; --fsm-only sends GetFsmId/API7001 once; --writer-only sends no RPC.
Both permitted requests use {}. --plan imports no DDS or robot SDK.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

AUDITED_FILES = {
    'comm/motion_switcher/motion_switcher_client.py': '133d78f6619f6e32d971b40020868618f10db0fd191731085a4b08615448b195',
    'comm/motion_switcher/motion_switcher_api.py': '933210a39b46f9303de1991b5647b5780845886d94d491604de0444caaa9c60c',
    'rpc/client.py': '20840192dee42525b64ecc3a3e8d5ec5d1d7e122b7bfc4236c482cb2b0f6e186',
    'rpc/client_base.py': '2a815a5c798be95df1c8a4af525873c2ee47d5e0bbfbcc8a4ccf2d991a341321',
    'rpc/client_stub.py': '37c860bb4783c2f13b24bc369ebf99b2131a21dfb49f0f7a8b74e79960d879b0',
    'core/channel.py': '8a58eea2bc6bb8792e5b5fa76949407bd8c2ef8af07172d1a3a465bcc544e247',
}
ARM_NAMES = [side + '_' + joint for side in ('left', 'right') for joint in
             ('shoulder_pitch', 'shoulder_roll', 'shoulder_yaw', 'elbow',
              'wrist_roll', 'wrist_pitch', 'wrist_yaw')]
AUDITED_LOCO_FILES = {
    'g1/loco/g1_loco_client.py': '5fbc07b30994676cd2f7994a2389b24e9b51e2c13dfde7e565bfd77898300ce2',
    'g1/loco/g1_loco_api.py': '0127e2b266ced4bce67fc264c1858e53ee9e0d9301ac06f3d58af4903e50c908',
}


def finite(values):
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError('non-finite state')
    return result


def decode_state(message):
    motors = list(message.motor_state)
    if len(motors) != 35:
        raise ValueError('expected 35 HG motor slots')
    q = finite(motor.q for motor in motors[:29])
    dq = finite(motor.dq for motor in motors[:29])
    tau = finite(motor.tau_est for motor in motors[:29])
    imu = message.imu_state
    return {
        'mode_machine_raw': int(message.mode_machine), 'mode_pr_raw': int(message.mode_pr),
        'tick': int(message.tick), 'version': list(message.version),
        'body_indices': list(range(15)), 'body_q_rad': q[:15], 'body_dq_rad_s': dq[:15],
        'arms': [dict(index=i + 15, name=name, q_rad=q[i + 15], dq_rad_s=dq[i + 15],
                      tau_est_nm=tau[i + 15], mode_raw=int(motors[i + 15].mode),
                      motorstate_raw=int(motors[i + 15].motorstate))
                 for i, name in enumerate(ARM_NAMES)],
        'imu': {'quaternion_raw': finite(imu.quaternion), 'rpy_rad': finite(imu.rpy),
                'gyroscope_raw': finite(imu.gyroscope), 'accelerometer_raw': finite(imu.accelerometer),
                'temperature_raw': int(imu.temperature)},
    }


def process_inventory():
    matches, inaccessible = [], 0
    needles = ('teleop', 'arm_control', 'arm_sdk', 'sport', 'motion_switcher', 'brainco', '/unitree/module/')
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            args = (path / 'cmdline').read_bytes().split(b'\0')
            args = [arg.decode(errors='replace') for arg in args if arg]
            if not args:
                continue
            scripts = [arg for arg in args[1:] if arg.endswith(('.py', '.sh')) and len(arg) < 300]
            inspected = [args[0]] + scripts
            if any(needle in value.lower() for value in inspected for needle in needles):
                matches.append({'pid': int(path.name), 'uid': path.stat().st_uid,
                                'executable': args[0], 'scripts': scripts})
        except (OSError, ValueError):
            inaccessible += 1
    result = {'matching_processes': matches, 'unreadable_process_entries': inaccessible,
              'scope': 'PC2 process snapshot only; cannot exclude other hosts or concealed publishers'}
    try:
        units = subprocess.run(['systemctl', 'list-units', '--type=service', '--all',
                                '--no-pager', '--no-legend'], capture_output=True, text=True, timeout=3)
        result['related_service_lines'] = [line for line in units.stdout.splitlines()
                                           if any(word in line.lower() for word in needles + ('unitree',))]
        result['systemctl_exit_code'] = units.returncode
        result['systemctl_stderr'] = units.stderr
    except Exception as exc:
        result['systemctl_error'] = str(exc)
    return result


def query_fsm_only(args):
    """Exactly one read-only sport API7001; no CheckMode or 710x calls."""
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'rpc_calls': [],
              'mode_changes_requested': 0, 'motion_command_publishers_created': 0}
    output = sys.stdout
    finished = threading.Event()
    def timeout():
        if not finished.is_set():
            print(json.dumps({'error': 'FSM query hard timeout', 'rpc_calls': report['rpc_calls']}), file=output, flush=True)
        os._exit(3)
    watchdog = threading.Timer(12, timeout)
    watchdog.daemon = True
    watchdog.start()
    client = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import unitree_sdk2py.g1.loco.g1_loco_client as loco_module
            from unitree_sdk2py.g1.loco.g1_loco_api import ROBOT_API_ID_LOCO_GET_FSM_ID
            sdk_root = Path(loco_module.__file__).resolve().parents[2]
            expected = dict(AUDITED_FILES, **AUDITED_LOCO_FILES)
            actual = {name: hashlib.sha256((sdk_root / name).read_bytes()).hexdigest() for name in expected}
            report['sdk_source_sha256'] = actual
            report['loco_source_text'] = {name: (sdk_root / name).read_text() for name in AUDITED_LOCO_FILES}
            if actual != expected or ROBOT_API_ID_LOCO_GET_FSM_ID != 7001:
                raise RuntimeError('Loco/RPC implementation differs from audited source; no RPC sent')
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            ChannelFactoryInitialize(0, args.interface)
            class QueryOnlyLoco(loco_module.LocoClient):
                def _Call(self, api_id, parameter):
                    if api_id != 7001 or json.loads(parameter) != {} or report['rpc_calls']:
                        raise RuntimeError('Only one GetFsmId query is permitted')
                    report['rpc_calls'].append({'service': 'sport', 'api_id': api_id, 'parameter': parameter})
                    result = super()._Call(api_id, parameter)
                    report['response_raw'] = {'code': result[0], 'data': result[1]}
                    return result
            client = QueryOnlyLoco()
            client.SetTimeout(args.rpc_timeout)
            client.Init()
            code, fsm_id = client.GetFsmId()
            report['get_fsm_id'] = {'code': code, 'fsm_id_raw': fsm_id}
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            if client is not None:
                try:
                    stub = getattr(client, '_ClientBase__stub')
                    getattr(stub, '_ClientStub__recvChannel').CloseReader()
                    getattr(stub, '_ClientStub__sendChannel').CloseWriter()
                except Exception as exc: report['cleanup_error'] = str(exc)
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['exit_code'] = 0 if not report.get('error') and not report.get('cleanup_error') and report.get('get_fsm_id', {}).get('code') == 0 else 1
        print(json.dumps(report, indent=2), file=output, flush=True)
        finished.set()
    return report['exit_code']


def inspect_arm_writer(args):
    """DDS subscriptions only; no RPC client is constructed on this path."""
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'arm writer discovery and command/state subscriptions only',
              'rpc_calls': [], 'motion_command_publishers_created': 0,
              'observation_s': args.seconds, 'interface': args.interface}
    output = sys.stdout
    printed = threading.Event()
    stop = threading.Event()
    def deadline():
        if not printed.is_set():
            print(json.dumps({'error': 'writer observation hard timeout', 'rpc_calls': []}), file=output, flush=True)
        os._exit(3)
    watchdog = threading.Timer(args.seconds + 15, deadline)
    watchdog.daemon = True
    watchdog.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    count, invalid, changed = 0, 0, 0
    first = last = None
    previous = latest = measured = None
    weights, publication_handles = [], set()
    writers, participants, errors = {}, {}, []
    command_reader = state_reader = publication_reader = participant_reader = None
    try:
        socket.if_nametoindex(args.interface)
        with contextlib.redirect_stdout(sys.stderr):
            import unitree_sdk2py.comm.motion_switcher.motion_switcher_client as motion_module
            sdk_root = Path(motion_module.__file__).resolve().parents[2]
            actual = {name: hashlib.sha256((sdk_root / name).read_bytes()).hexdigest() for name in AUDITED_FILES}
            if actual != AUDITED_FILES:
                raise RuntimeError('SDK source differs from audited version')
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelFactory
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
            from cyclonedds.topic import Topic
            from cyclonedds.sub import DataReader
            from cyclonedds.qos import Qos, Policy
            from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication, BuiltinTopicDcpsParticipant
            ChannelFactoryInitialize(0, args.interface)
            participant = getattr(ChannelFactory, '_ChannelFactory__participant')
            qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(1024))
            command_topic = Topic(participant, 'rt/arm_sdk', LowCmd_)
            state_topic = Topic(participant, 'rt/lowstate', LowState_)
            command_reader = DataReader(participant, command_topic, qos=qos)
            state_reader = DataReader(participant, state_topic, qos=qos)
            publication_reader = BuiltinDataReader(participant, BuiltinTopicDcpsPublication)
            participant_reader = BuiltinDataReader(participant, BuiltinTopicDcpsParticipant)
            end = time.monotonic() + args.seconds
            while not stop.is_set() and time.monotonic() < end:
                for endpoint in publication_reader.take(100):
                    if getattr(endpoint, 'topic_name', None) == 'rt/arm_sdk':
                        writers[str(endpoint.key)] = {
                            'writer_guid': str(endpoint.key), 'participant_guid': str(endpoint.participant_key),
                            'topic_name': endpoint.topic_name, 'type_name': endpoint.type_name,
                            'qos_repr': repr(endpoint.qos)[:4096],
                            'instance_state_raw': str(getattr(getattr(endpoint, 'sample_info', None), 'instance_state', None)),
                        }
                for endpoint in participant_reader.take(100):
                    if hasattr(endpoint, 'key'):
                        participants[str(endpoint.key)] = {'participant_guid': str(endpoint.key),
                                                          'qos_repr': repr(endpoint.qos)[:4096]}
                for sample in command_reader.take(100):
                    info = getattr(sample, 'sample_info', None)
                    if not getattr(info, 'valid_data', True):
                        continue
                    try:
                        motors = list(sample.motor_cmd)
                        if len(motors) != 35: raise ValueError('expected 35 LowCmd motor slots')
                        row = {'mode_pr_raw': int(sample.mode_pr), 'mode_machine_raw': int(sample.mode_machine),
                               'arm_weight_motor29_q_raw': finite([motors[29].q])[0],
                               'arms': [dict(index=i + 15, name=name,
                                             mode_raw=int(motors[i + 15].mode),
                                             q_rad=finite([motors[i + 15].q])[0],
                                             dq_rad_s=finite([motors[i + 15].dq])[0],
                                             tau_raw=finite([motors[i + 15].tau])[0],
                                             kp_raw=finite([motors[i + 15].kp])[0],
                                             kd_raw=finite([motors[i + 15].kd])[0])
                                        for i, name in enumerate(ARM_NAMES)]}
                        signature = json.dumps(row, sort_keys=True)
                        if previous is not None and previous != signature: changed += 1
                        previous, latest = signature, row
                        now = time.monotonic()
                        first = now if first is None else first
                        last = now
                        count += 1
                        weights.append(row['arm_weight_motor29_q_raw'])
                        if info is not None:
                            publication_handles.add(int(info.publication_handle))
                    except Exception as exc:
                        invalid += 1
                        errors.append(str(exc))
                for sample in state_reader.take(100):
                    if getattr(getattr(sample, 'sample_info', None), 'valid_data', True):
                        try: measured = decode_state(sample)
                        except Exception as exc: errors.append('state: ' + str(exc))
                stop.wait(.01)
            report['matched_publication_api_available'] = hasattr(command_reader, 'get_matched_publications')
            report['matched_publications'] = [] if report['matched_publication_api_available'] else None
            if hasattr(command_reader, 'get_matched_publications'):
                for handle in command_reader.get_matched_publications():
                    entry = {'publication_handle': int(handle), 'observed_in_command_samples': int(handle) in publication_handles}
                    try:
                        endpoint = command_reader.get_matched_publication_data(handle)
                        if endpoint is not None:
                            entry.update(writer_guid=str(endpoint.key), participant_guid=str(endpoint.participant_key),
                                         topic_name=endpoint.topic_name, type_name=endpoint.type_name,
                                         qos_repr=repr(endpoint.qos)[:4096])
                    except Exception as exc: entry['lookup_error'] = str(exc)
                    report['matched_publications'].append(entry)
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        command_reader = state_reader = publication_reader = participant_reader = None
        report['command_stream'] = {
            'valid_samples': count, 'invalid_samples': invalid, 'changed_samples': changed,
            'first_receive_monotonic_s': first, 'last_receive_monotonic_s': last,
            'arrival_hz': (count - 1) / (last - first) if count > 1 and last > first else None,
            'last_sample_age_s': None if last is None else time.monotonic() - last,
            'weight_min': min(weights) if weights else None, 'weight_max': max(weights) if weights else None,
            'latest': latest, 'sample_publication_handles': sorted(publication_handles),
        }
        report['arm_sdk_writers_observed'] = list(writers.values())
        report['associated_participants'] = [participants[w['participant_guid']] for w in writers.values()
                                              if w['participant_guid'] in participants]
        report['latest_measured_lowstate'] = measured
        report['errors'] = errors[:20]
        report['limits'] = ['No samples does not prove that no controller exists or that ownership is available',
                            'Advertising a writer does not by itself prove active commands',
                            'Targets, weights and motor modes are observations, not permission to act',
                            'Command and measured state snapshots are asynchronous']
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['exit_code'] = 1 if report.get('error') else 0
        print(json.dumps(report, indent=2, allow_nan=False), file=output, flush=True)
        printed.set()
    return report['exit_code']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='eth0')
    parser.add_argument('--seconds', type=float, default=4.0)
    parser.add_argument('--rpc-timeout', type=float, default=2.0)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--writer-only', action='store_true', help='only read arm commands/discovery/state; send no RPC')
    parser.add_argument('--fsm-only', action='store_true', help='send one audited GetFsmId API7001 query only')
    args = parser.parse_args(argv)
    if args.writer_only and args.fsm_only:
        parser.error('--writer-only and --fsm-only are mutually exclusive')
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 10:
        parser.error('--seconds must be finite and in (0,10]')
    if not math.isfinite(args.rpc_timeout) or not 0 < args.rpc_timeout <= 3:
        parser.error('--rpc-timeout must be finite and in (0,3]')
    if args.plan:
        print(json.dumps({'rpc': None if args.writer_only else {'service': 'sport' if args.fsm_only else 'motion_switcher', 'api_id': 7001 if args.fsm_only else 1001, 'parameter': {}, 'maximum_calls': 1},
                          'reads': [] if args.fsm_only else (['rt/arm_sdk', 'rt/lowstate', 'DCPSPublication', 'DCPSParticipant'] if args.writer_only else ['rt/lowstate', 'DCPSPublication', '/proc', 'systemctl list-units']),
                          'motion_command_publishers': 0, 'mode_changes': 0}, indent=2))
        return 0
    if args.writer_only:
        return inspect_arm_writer(args)
    if args.fsm_only:
        return query_fsm_only(args)
    socket.if_nametoindex(args.interface)
    started = time.monotonic()
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'interface': args.interface,
              'motion_command_publishers_created': 0, 'rpc_calls': [], 'mode_changes_requested': 0,
              'requested_observation_s': args.seconds, 'rpc_timeout_s': args.rpc_timeout,
              'limits': ['mode_machine is raw and is not interpreted as Regular mode',
                         'No observed rt/arm_sdk writer is not a guarantee of exclusive control',
                         'LowState/IMU cannot establish ground contact or whether the robot is suspended',
                         'Process inventory covers PC2 only; CheckMode is a single point-in-time response']}
    output = sys.stdout
    lock = threading.Lock()
    stop = threading.Event()
    sent_report = threading.Event()
    state = {'valid_count': 0, 'invalid_count': 0, 'last_valid_monotonic_s': None, 'latest': None}
    def receive(message):
        now = time.monotonic()
        try:
            sample = decode_state(message)
            error = None
        except Exception as exc:
            sample, error = None, str(exc)
        with lock:
            if error:
                state['invalid_count'] += 1
                state['last_error'] = error
            else:
                state['valid_count'] += 1
                state['last_valid_monotonic_s'] = now
                state['latest'] = sample
    def hard_timeout():
        if not sent_report.is_set():
            print(json.dumps({'error': 'hard process deadline', 'rpc_calls': report['rpc_calls']}), file=output, flush=True)
        os._exit(3)
    watchdog = threading.Timer(args.seconds + 15, hard_timeout)
    watchdog.daemon = True
    watchdog.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    low = client = builtin = None
    observed, discovery_errors = {}, []
    try:
        report['process_inventory'] = process_inventory()
        with contextlib.redirect_stdout(sys.stderr):
            import unitree_sdk2py.comm.motion_switcher.motion_switcher_client as motion_module
            from unitree_sdk2py.comm.motion_switcher.motion_switcher_api import MOTION_SWITCHER_API_ID_CHECK_MODE
            sdk_root = Path(motion_module.__file__).resolve().parents[2]
            actual = {name: hashlib.sha256((sdk_root / name).read_bytes()).hexdigest() for name in AUDITED_FILES}
            report['sdk_source_sha256'] = actual
            if actual != AUDITED_FILES or MOTION_SWITCHER_API_ID_CHECK_MODE != 1001:
                raise RuntimeError('SDK implementation differs from audited source; no RPC sent')
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelFactory, ChannelSubscriber
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
            ChannelFactoryInitialize(0, args.interface)
            low = ChannelSubscriber('rt/lowstate', LowState_)
            low.Init(receive, 0)
            try:
                from cyclonedds.builtin import BuiltinDataReader, BuiltinTopicDcpsPublication
                participant = getattr(ChannelFactory, '_ChannelFactory__participant')
                builtin = BuiltinDataReader(participant, BuiltinTopicDcpsPublication)
            except Exception as exc:
                discovery_errors.append(str(exc))

            class QueryOnlyClient(motion_module.MotionSwitcherClient):
                def _Call(self, api_id, parameter):
                    if api_id != 1001 or json.loads(parameter) != {} or report['rpc_calls']:
                        raise RuntimeError('Only one CheckMode query is permitted')
                    report['rpc_calls'].append({'api_id': api_id, 'parameter': parameter})
                    return super()._Call(api_id, parameter)

            client = QueryOnlyClient()
            client.SetTimeout(args.rpc_timeout)
            client.Init()  # local API registration only; lease disabled by constructor
            query_started = time.monotonic()
            code, data = client.CheckMode()
            report['check_mode'] = {'code': code, 'data_raw': data, 'elapsed_s': time.monotonic() - query_started}
            deadline = time.monotonic() + args.seconds
            while not stop.is_set() and time.monotonic() < deadline:
                if builtin is not None:
                    try:
                        for endpoint in builtin.take(100):
                            if not hasattr(endpoint, 'topic_name'):
                                continue
                            observed[str(endpoint.key)] = {
                                'writer_guid': str(endpoint.key), 'participant_guid': str(endpoint.participant_key),
                                'topic_name': endpoint.topic_name, 'type_name': endpoint.type_name,
                                'instance_state_raw': str(getattr(getattr(endpoint, 'sample_info', None), 'instance_state', None)),
                            }
                    except Exception as exc:
                        discovery_errors.append(str(exc)); builtin = None
                stop.wait(.05)
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            if client is not None:
                try:
                    stub = getattr(client, '_ClientBase__stub')
                    getattr(stub, '_ClientStub__recvChannel').CloseReader()
                    getattr(stub, '_ClientStub__sendChannel').CloseWriter()
                except Exception as exc:
                    report.setdefault('cleanup_errors', []).append(str(exc))
            if low is not None:
                try: low.Close()
                except Exception as exc: report.setdefault('cleanup_errors', []).append(str(exc))
            builtin = None
        with lock:
            report['lowstate'] = dict(state)
        last = report['lowstate']['last_valid_monotonic_s']
        report['lowstate']['age_s'] = None if last is None else time.monotonic() - last
        report['lowstate']['fresh'] = last is not None and report['lowstate']['age_s'] < .5
        report['publication_discovery'] = {
            'total_unique_endpoints_observed': len(observed),
            'arm_sdk_writers_observed': [x for x in observed.values() if x['topic_name'] == 'rt/arm_sdk'],
            'lowcmd_writers_observed': [x for x in observed.values() if x['topic_name'] == 'rt/lowcmd'],
            'topic_names_observed': sorted(set(x['topic_name'] for x in observed.values())),
            'errors': discovery_errors,
            'absence_does_not_prove_no_writer': True,
        }
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['elapsed_s'] = time.monotonic() - started
        report['exit_code'] = 0 if not report.get('error') and not report.get('cleanup_errors') and report.get('check_mode', {}).get('code') == 0 and report['lowstate']['fresh'] else 1
        print(json.dumps(report, indent=2, allow_nan=False), file=output, flush=True)
        sent_report.set()
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
