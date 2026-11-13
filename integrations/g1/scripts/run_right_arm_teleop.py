#!/usr/bin/env python3
"""PICO right-wrist commissioning session over Wi-Fi; defaults to no output."""
import argparse
import getpass
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from pc2_arm_client import PC2ArmClient
from run_live_arm_preview import ROOT, IK_PYTHON, lines, reference_from_feedback, stop
from wifi_feedback_client import WifiFeedbackClient

SN = 'PC2310MLL9241957G'


def commissioning_config():
    # Fixed official example gains, bounded by LowCmdSession. These have not
    # been validated for the installed hand load; this is a supervised test.
    return {'right_kp':[40.]*7,'right_kd':[1.]*7,'nonright_kd':[8.]*22,
            'calibration_id':'preview-xr-right-up-back-v1',
            'enable_source':'authorized_ten_second_window',
            'gain_review_id':'unitree-814556d-example40-1-rlgym276801e-damp8',
            'input_policy_id':'supervised_tracker_window_v1',
            'max_session_s':10.,'max_command_hz':200.,'gain_ramp_s':2.,
            'max_excursion_rad':.03,'max_speed_rad_s':.05}


def response_for_challenge(challenge, packet, candidate, decision, reference_id):
    """Keep unknown vendor validity unknown; attest only the named local policy."""
    if packet['local_monotonic_ns'] < challenge['_local_received_monotonic_ns']:
        raise ValueError('packet predates received PC2 challenge')
    if candidate.get('source_packet_sequence') != packet['packet_sequence']:
        raise ValueError('IK and source packet differ')
    if candidate.get('reference_id') != reference_id or challenge.get('reference_id') != reference_id:
        raise ValueError('reference changed')
    q=candidate['q_candidate_rad']
    if len(q)!=14 or candidate.get('accepted') is not True:
        raise ValueError('no accepted IK target')
    if decision['input_policy_id']=='supervised_tracker_window_v1':
        return {'type':'tracker_target','session_id':challenge['session_id'],'nonce':challenge['nonce'],
                'sequence':challenge['sequence'],'input_sequence':packet['packet_sequence'],
                'generated_after_challenge':True,'q':q[7:],
                'tracking_valid':candidate.get('tracking_valid'),
                'calibration_id':candidate['calibration_id'],'reference_id':reference_id,
                'input_policy_id':decision['input_policy_id'],
                'supervised_input_accepted':decision['supervised_input_accepted']}
    enable=packet.get('controller_enable',{})
    if enable.get('sequence') != packet['packet_sequence']:
        raise ValueError('enable and source packet differ')
    if enable.get('local_monotonic_ns') != packet['local_monotonic_ns']:
        raise ValueError('enable time differs from source packet')
    return {'type':'target','session_id':challenge['session_id'],'nonce':challenge['nonce'],
            'sequence':challenge['sequence'],'input_sequence':packet['packet_sequence'],
            'enable_sequence':enable['sequence'],'generated_after_challenge':True,
            'q':q[7:],'tracking_valid':candidate.get('tracking_valid'),
            'held':enable.get('held') is True,'calibration_id':candidate['calibration_id'],
            'reference_id':reference_id,'enable_source':enable['source_id'],
            'input_policy_id':decision['input_policy_id'],
            'supervised_input_accepted':decision['supervised_input_accepted']}


def newest_after(incoming, stamp, deadline):
    while time.monotonic()<deadline:
        value=incoming.get(timeout=max(.001,deadline-time.monotonic()))
        if value is None:
            raise RuntimeError('PICO input ended')
        if value.get('event') in ('server_disconnected','device_missing'):
            raise RuntimeError('PICO '+value['event'])
        if value.get('type')!='sample':
            continue
        if value['local_monotonic_ns']>=stamp:
            return value
    raise TimeoutError('no PICO packet after challenge')


def run(output_path,password,*,enable_motion=False,control_release_verified=False,seconds=30):
    from pico_supervised_input import TrackerWindowPolicy
    if not 5<=seconds<=45:
        raise ValueError('runtime window must be 5..45 seconds')
    ik=pico=remote=reader_thread=None
    feedback=WifiFeedbackClient(seconds=seconds+20)
    qi,qp,qc=queue.Queue(),queue.Queue(),queue.Queue(maxsize=1)
    remote_ended=threading.Event()
    identifier=str(uuid.uuid4())
    summary={'type':'session_summary','motion_requested':enable_motion,
             'targets_sent':0,'ik_candidates':0,'commands_published':None,
             'physical_motion_verified':False,'reference_id':identifier}
    with output_path.open('w') as output,output_path.with_suffix('.log').open('w') as log:
        output_lock=threading.Lock()
        def emit(record):
            with output_lock:
                print(json.dumps(record,allow_nan=False),file=output,flush=True)
        def runtime_reader():
            try:
                while True:
                    item=remote.read_record(2)
                    if item['type']=='challenge':
                        if qc.full():
                            try: qc.get_nowait()
                            except queue.Empty: pass
                        qc.put_nowait(item)
                    elif item['type'] in ('runtime_final','runtime_summary','summary','runtime_error','error'):
                        summary['remote_result']=item
                        if type(item.get('actual_write_count')) is int:
                            summary['commands_published']=item['actual_write_count']
                        remote_ended.set()
                        return
            except Exception as exc:
                summary['remote_reader_end']=type(exc).__name__+': '+str(exc)
                remote_ended.set()
        try:
            config=commissioning_config()
            emit({'type':'configuration','config':config,'mode':'motion' if enable_motion else 'preview',
                  'axes_physically_verified':False,'vendor_tracking_quality_available':False})
            env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1')
            ik=subprocess.Popen([str(IK_PYTHON),str(ROOT/'scripts/live_pico_arm_ik.py'),
                                 '--seconds',str(seconds+20)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                stderr=log,text=True,bufsize=1,start_new_session=True,env=env)
            threading.Thread(target=lines,args=(ik,qi),daemon=True).start()
            ready=qi.get(timeout=15)
            if not ready or ready.get('type')!='ready': raise RuntimeError('IK initialization failed')
            emit(ready)
            emit(feedback.open(password))
            pico=subprocess.Popen([sys.executable,str(ROOT/'scripts/pico_raw_probe.py'),
                                   '--seconds',str(seconds+10),'--expected-trackers','1'],
                                  stdout=subprocess.PIPE,stderr=log,text=True,bufsize=1,start_new_session=True)
            threading.Thread(target=lines,args=(pico,qp),daemon=True).start()
            remote=PC2ArmClient(identifier,config,seconds=seconds,enable_motion=enable_motion,
                                control_release_verified=control_release_verified,emit=emit)
            emit(remote.open(password))
            reader_thread=threading.Thread(target=runtime_reader,daemon=True)
            reader_thread.start()
            deadline=time.monotonic()+seconds
            # Confirm a stream exists before capturing a matching fresh G1 pose.
            newest_after(qp,time.monotonic_ns(),min(deadline,time.monotonic()+8))
            state=feedback.snapshot(); emit(state)
            reference=reference_from_feedback(state,SN); reference['reference_id']=identifier
            emit(reference)
            ik.stdin.write(json.dumps(reference)+'\n'); ik.stdin.flush()
            accepted=qi.get(timeout=2); emit(accepted)
            if not accepted or accepted.get('status')!='reference_accepted':
                raise RuntimeError('IK reference not accepted')
            policy=TrackerWindowPolicy(sn=SN)
            policy.set_reference(reference)
            challenge=None
            while time.monotonic()<deadline and not remote_ended.is_set():
                try:
                    challenge=qc.get_nowait()
                except queue.Empty:
                    pass
                # Continue validating live input during PC2 publisher setup.
                packet=newest_after(qp,max(challenge['_local_received_monotonic_ns'] if challenge else 0,
                                           reference['received_monotonic_ns']),
                                    min(deadline,time.monotonic()+.1))
                ik.stdin.write(json.dumps(packet)+'\n'); ik.stdin.flush()
                result=qi.get(timeout=.3)
                emit({'type':'pico_input','record':packet}); emit(result)
                if not result or result.get('accepted') is not True:
                    raise RuntimeError('IK target rejected: '+str(result and result.get('reason')))
                summary['ik_candidates']+=1
                decision=policy.evaluate(packet,result,now_ns=time.monotonic_ns())
                emit({'type':'supervised_input_policy','record':decision})
                if (decision.get('supervised_input_accepted') is not True
                        and decision.get('reason') not in ('waiting_for_trigger','waiting_for_controller')):
                    if decision.get('reason')=='supervised_ten_second_window_expired':
                        summary['stop_reason']='supervised_window_completed'
                        break
                    raise RuntimeError('PICO supervised input rejected: '+str(decision.get('reason')))
                if challenge is not None:
                    reply=response_for_challenge(challenge,packet,result,decision,identifier)
                    remote.send(reply)
                    summary['targets_sent']+=1
                    challenge=None
            summary.setdefault('stop_reason','remote_ended' if remote_ended.is_set() else 'runtime_window_ended')
        except (Exception,KeyboardInterrupt) as exc:
            summary['stop_reason']=type(exc).__name__+': '+str(exc)
        finally:
            # EOF asks PC2 to perform its explicit active-session damping path.
            # Avoid two readers racing on the same pexpect stream during cleanup.
            if remote and remote.process:
                try: remote.process.sendeof()
                except Exception: pass
                if reader_thread:
                    reader_thread.join(timeout=3)
                remote.close(drain=reader_thread is None)
                if reader_thread and reader_thread.is_alive():
                    reader_thread.join(timeout=1)
            stop(pico)
            if ik and ik.stdin:
                try: ik.stdin.close()
                except (BrokenPipeError,OSError): pass
            stop(ik)
            feedback.close()
            if not enable_motion: summary['commands_published']=0
            emit(summary)
    return summary


def main():
    def interrupted(signum,_frame): raise KeyboardInterrupt('signal '+str(signum))
    signal.signal(signal.SIGTERM,interrupted)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=float,default=30)
    p.add_argument('--enable-motion',action='store_true')
    p.add_argument('--control-release-verified',action='store_true',
                   help='requires current successful empty CheckMode and writer/ownership review')
    args=p.parse_args()
    result=run(args.output,getpass.getpass('PC2 SSH password: '),enable_motion=args.enable_motion,
               control_release_verified=args.control_release_verified,seconds=args.seconds)
    print(json.dumps(result,indent=2))
    remote=result.get('remote_result',{})
    completed=(result.get('stop_reason') in ('remote_ended','runtime_window_ended','supervised_window_completed')
               and remote.get('type')=='runtime_final'
               and remote.get('reason') in ('stdin_eof','client_stop','runtime_deadline','trigger_released',
                                           'authorized_window_deadline')
               and remote.get('last_action_unknown') is False
               and remote.get('accepted_targets',0)>0)
    if args.enable_motion:
        completed=completed and remote.get('actual_write_count',0)>0
    return 0 if completed else 1


if __name__=='__main__':
    raise SystemExit(main())
