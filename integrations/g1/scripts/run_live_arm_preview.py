#!/usr/bin/env python3
"""Run PICO -> bounded IK using fresh G1 state over Wi-Fi. Preview only."""
import argparse
import getpass
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from wifi_feedback_client import WifiFeedbackClient

ROOT = Path(__file__).resolve().parents[1]
IK_PYTHON = Path.home()/'.cache/unitree-g1-arms/env/bin/python'


def lines(process, destination):
    try:
        for line in process.stdout:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            destination.put(item)
    finally:
        destination.put(None)


def stop(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass


def reference_from_feedback(feedback, sn):
    body = feedback['streams']['rt/lowstate']
    if body['status'] != 'fresh_bound':
        raise ValueError('robot state age bound is not fresh')
    state = body['source']['payload']
    if state.get('crc_valid') is not True or state['mode_pr'] != 0 or any(state['motorstate']):
        raise ValueError('robot state mode/fault is not suitable for reference')
    return {'type': 'reference', 'reference_id': str(uuid.uuid4()),
            'reference_kind': 'live', 'state_valid': True, 'clock_domain': 'local_monotonic',
            'received_monotonic_ns': body['conservative_received_monotonic_ns'],
            'state_sequence': body['sequence'], 'q_rad': state['q'][15:29], 'right_sn': sn,
            'time_provenance': 'conservative_rtt_bound',
            'source_clock_id': feedback['source_clock_id'], 'age_upper_bound_s': body['age_upper_bound_s'],
            'axis_config': {'id': 'preview-xr-right-up-back-v1', 'pose_unit': 'm', 'verified': False,
                            'T_robot_axes_xr_axes': [[0,0,-1],[-1,0,0],[0,1,0]]}}


def run(seconds, output_path, password, sn='PC2310MLL9241957G'):
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise ValueError('seconds must be in (0,60]')
    pico = ik = None
    feedback = WifiFeedbackClient(seconds=seconds+20)
    qik, qpico = queue.Queue(), queue.Queue()
    summary = {'type': 'session_summary', 'commands_published': 0, 'motion_ready': False,
               'pico_packets_forwarded': 0, 'ik_candidates': 0, 'discarded_queued_pico_records': 0,
               'max_right_joint_delta_rad': 0.0, 'max_left_joint_delta_rad': 0.0}
    with output_path.open('w') as output, output_path.with_suffix('.log').open('w') as log:
        def emit(record):
            print(json.dumps(record, allow_nan=False), file=output, flush=True)
        try:
            env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
            ik = subprocess.Popen([str(IK_PYTHON), str(ROOT/'scripts/live_pico_arm_ik.py'),
                                   '--seconds', str(seconds+15)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=log, text=True, bufsize=1, env=env, start_new_session=True)
            threading.Thread(target=lines,args=(ik,qik),daemon=True).start()
            ready = qik.get(timeout=15)
            if not ready or ready.get('type') != 'ready':
                raise RuntimeError('IK worker did not become ready: '+str(ready))
            emit(ready)
            emit(feedback.open(password))
            reference = None
            pico = subprocess.Popen([sys.executable,str(ROOT/'scripts/pico_raw_probe.py'),
                                     '--seconds',str(seconds),'--expected-trackers','1'],
                                    stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1,
                                    start_new_session=True)
            threading.Thread(target=lines,args=(pico,qpico),daemon=True).start()
            deadline = time.monotonic()+seconds+3
            while time.monotonic() < deadline:
                packet = qpico.get(timeout=max(.001,deadline-time.monotonic()))
                if packet is None:
                    break
                # Use the newest packet available; preserved source time still bounds freshness.
                while not qpico.empty():
                    newer = qpico.get_nowait()
                    if newer is None:
                        break
                    packet = newer
                    summary['discarded_queued_pico_records'] += 1
                if packet.get('type') != 'sample':
                    emit({'type':'pico_metadata','record':packet})
                    if packet.get('event') in ('server_disconnected','device_missing'):
                        raise RuntimeError('PICO '+packet['event'])
                    continue
                snapshot = feedback.snapshot()
                emit(snapshot)
                if snapshot['streams']['rt/lowstate']['status'] != 'fresh_bound':
                    raise RuntimeError('Wi-Fi state became stale')
                if reference is None:
                    # Wait for the stream before capturing the robot reference.
                    # A subsequent packet becomes the origin; never restamp it.
                    reference = reference_from_feedback(snapshot,sn)
                    emit(reference)
                    ik.stdin.write(json.dumps(reference)+'\n'); ik.stdin.flush()
                    result = qik.get(timeout=2)
                    emit(result)
                    if not result or result.get('status') != 'reference_accepted':
                        raise RuntimeError('IK reference rejected: '+str(result))
                    continue
                if packet['local_monotonic_ns'] < reference['received_monotonic_ns']:
                    summary['discarded_queued_pico_records'] += 1
                    continue
                emit({'type':'pico_input','record':packet})
                ik.stdin.write(json.dumps(packet)+'\n'); ik.stdin.flush()
                result = qik.get(timeout=2)
                if result is None:
                    raise RuntimeError('IK worker ended unexpectedly')
                emit(result)
                summary['pico_packets_forwarded'] += 1
                if result.get('accepted'):
                    summary['ik_candidates'] += 1
                    delta = [abs(a-b) for a,b in zip(result['q_candidate_rad'],reference['q_rad'])]
                    summary['max_right_joint_delta_rad'] = max(summary['max_right_joint_delta_rad'],max(delta[7:]))
                    summary['max_left_joint_delta_rad'] = max(summary['max_left_joint_delta_rad'],max(delta[:7]))
                if result.get('state') == 'LATCHED':
                    summary['stop_reason'] = result.get('reason')
                    break
            summary.setdefault('stop_reason','input_window_ended')
        except (Exception, KeyboardInterrupt) as exc:
            summary['stop_reason'] = type(exc).__name__+': '+str(exc)
        finally:
            stop(pico)
            if ik and ik.stdin:
                try: ik.stdin.close()
                except BrokenPipeError: pass
            stop(ik)
            feedback.close()
            emit(summary)
    return summary


def main():
    def interrupted(signum, _frame):
        raise KeyboardInterrupt('signal '+str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds',type=float,default=10)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    result=run(args.seconds,args.output,getpass.getpass('PC2 SSH password: '))
    print(json.dumps(result,indent=2))
    return 0 if result['ik_candidates'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
