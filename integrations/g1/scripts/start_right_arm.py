#!/usr/bin/env python3
"""Start the bounded right-arm Tracker test; preview only unless --enable-motion.

The motion path verifies a 5-second preview, performs at most one reviewed
ReleaseMode call, observes mode/writers again, then runs for at most 10 seconds.
No PICO controller is required. This entry does not command the Wuji/BrainCo hands.
"""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import json
import math
from pathlib import Path
from network_config import robot_host
import shlex
import signal
import time
import types

from g1_mode_probe import AUDITED_FILES
from pc2_arm_client import ROOT

REMOTE_DEADLINE_S = 30.
NORMAL_SESSION_ENDS = {'remote_ended','runtime_window_ended','supervised_window_completed'}
NORMAL_RUNTIME_ENDS = {'stdin_eof','client_stop','runtime_deadline','authorized_window_deadline'}


def strict_zero(value):
    return type(value) is int and value == 0


def positive_integer(value):
    return type(value) is int and value > 0


def verify_session(result, *, motion):
    remote = result.get('remote_result',{}) if isinstance(result,dict) else {}
    if (not isinstance(result,dict) or not isinstance(remote,dict)
            or result.get('motion_requested') is not motion
            or result.get('stop_reason') not in NORMAL_SESSION_ENDS
            or remote.get('type') != 'runtime_final'
            or remote.get('reason') not in NORMAL_RUNTIME_ENDS
            or remote.get('preview') is not (not motion)
            or remote.get('last_action_unknown') is not False
            or remote.get('errors') != []
            or not positive_integer(remote.get('accepted_targets'))):
        raise ValueError('session_did_not_complete_with_known_valid_targets')
    actual = remote.get('actual_write_count')
    if motion:
        if not positive_integer(actual) or result.get('commands_published') != actual:
            raise ValueError('motion_session_has_no_confirmed_write_count')
    elif not strict_zero(actual) or not strict_zero(result.get('commands_published')):
        raise ValueError('preview_reported_hardware_writes_or_unknown_count')


def rpc_ids(report):
    calls = report.get('rpc_calls')
    if not isinstance(calls,list):
        raise ValueError('missing_rpc_evidence')
    ids = []
    for call in calls:
        if not isinstance(call,dict) or type(call.get('api_id')) is not int:
            raise ValueError('invalid_rpc_evidence')
        if json.loads(call.get('parameter','null')) != {}:
            raise ValueError('unexpected_rpc_parameters')
        ids.append(call['api_id'])
    return ids


def verify_release(report):
    if (not isinstance(report,dict) or report.get('type') != 'release_result'
            or report.get('error') or report.get('cleanup_error')
            or report.get('sdk_source_sha256') != AUDITED_FILES
            or report.get('service_inactive_verified') is not True
            or not strict_zero(report.get('motor_commands_published'))
            or report.get('mode_restored') is not False):
        raise ValueError('release_not_verified')
    before,after = report.get('before',{}),report.get('after',{})
    if (not isinstance(before,dict) or not isinstance(after,dict)
            or not strict_zero(before.get('code')) or not strict_zero(after.get('code'))
            or not isinstance(before.get('data'),dict) or not isinstance(after.get('data'),dict)
            or after['data'].get('name') != ''):
        raise ValueError('release_before_after_evidence_invalid')
    if report.get('release_sent') is True:
        if (before['data'].get('name') != 'ai' or not strict_zero(report.get('release_code'))
                or rpc_ids(report) != [1001,1003,1001]):
            raise ValueError('release_sequence_or_result_invalid')
    elif report.get('release_sent') is False:
        if before['data'].get('name') != '' or rpc_ids(report) != [1001]:
            raise ValueError('already_inactive_evidence_invalid')
    else:
        raise ValueError('release_attempt_status_missing')


def verify_mode(report):
    if (not isinstance(report,dict) or not strict_zero(report.get('exit_code'))
            or report.get('error') or report.get('cleanup_errors')
            or report.get('sdk_source_sha256') != AUDITED_FILES
            or not strict_zero(report.get('mode_changes_requested'))
            or not strict_zero(report.get('motion_command_publishers_created'))
            or rpc_ids(report) != [1001]):
        raise ValueError('mode_probe_failed')
    mode,state,discovery = (report.get(key) for key in ('check_mode','lowstate','publication_discovery'))
    if (not isinstance(mode,dict) or not strict_zero(mode.get('code'))
            or not isinstance(mode.get('data_raw'),dict) or mode['data_raw'].get('name') != ''):
        raise ValueError('motion_service_is_not_verified_empty')
    if (not isinstance(state,dict) or state.get('fresh') is not True
            or not positive_integer(state.get('valid_count'))
            or type(state.get('age_s')) not in (int,float) or not math.isfinite(state['age_s'])
            or not 0 <= state['age_s'] < .5):
        raise ValueError('mode_probe_feedback_not_fresh')
    if (not isinstance(discovery,dict) or discovery.get('errors') != []
            or discovery.get('lowcmd_writers_observed') != []):
        raise ValueError('lowcmd_writer_present_or_discovery_incomplete')


def remote_command(step):
    """Send the reviewed scripts in memory; shell tokens are independently quoted."""
    if step not in ('release','mode_probe'):
        raise ValueError('unknown_remote_step')
    filenames = ['g1_mode_probe'] + (['g1_release_once'] if step=='release' else [])
    sources = {name:base64.b64encode((ROOT/'scripts'/f'{name}.py').read_bytes()).decode()
               for name in filenames}
    # Module registration preserves g1_release_once's import of AUDITED_FILES.
    # Each script retains its own SDK hash check and internal hard deadline.
    bootstrap = '\n'.join([
        'import base64,sys,types',
        'sources='+repr(sources),
        'for name,source in sources.items():',
        '    m=types.ModuleType(name); m.__file__="<"+name+">"; sys.modules[name]=m',
        '    exec(compile(base64.b64decode(source),m.__file__,"exec"),m.__dict__)',
        'sys.argv='+repr(['g1_release_once','--interface','eth0','--release'] if step=='release'
                        else ['g1_mode_probe','--interface','eth0','--seconds','2','--rpc-timeout','2']),
        'raise SystemExit(sys.modules['+repr('g1_release_once' if step=='release' else 'g1_mode_probe')+'].main())',
    ])
    args = ['timeout','--signal=TERM','--kill-after=2s','20s',
            '/home/unitree/miniconda3/envs/tv/bin/python','-u','-B','-c',bootstrap]
    return ' '.join(shlex.quote(arg) for arg in args)


def ssh_args(command):
    # Same fixed identity and host-key checks as PC2ArmClient.open. No alternate
    # host fallback, auto-accept, embedded password, installation or remote file.
    return ['-T','-o','ConnectTimeout=5','-o','NumberOfPasswordPrompts=1',
            '-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
            '-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=192.168.123.164',
            '-o','UserKnownHostsFile='+str(ROOT/'diagnostics/g1-known_hosts'),
            'unitree@'+robot_host(),command]


def extract_report(text, step):
    """Accept exactly one whole report, including the probe's multiline JSON."""
    reports=[]
    decoder=json.JSONDecoder()
    for index,char in enumerate(text):
        if char!='{':
            continue
        try:
            value,_=decoder.raw_decode(text,index)
        except ValueError:
            continue
        if not isinstance(value,dict):
            continue
        if ((step=='release' and value.get('type')=='release_result')
                or (step=='mode_probe' and 'check_mode' in value and 'publication_discovery' in value)):
            reports.append(value)
    if len(reports)!=1:
        raise ValueError('missing_or_ambiguous_remote_report')
    return reports[0]


def remote_once(step,password):
    import pexpect
    command=remote_command(step)
    deadline=time.monotonic()+REMOTE_DEADLINE_S
    process=pexpect.spawn('ssh',ssh_args(command),encoding='utf-8',echo=False,timeout=8,maxread=65536)
    process.delaybeforesend=0
    try:
        process.expect('[Pp]assword:',timeout=min(8,max(.001,deadline-time.monotonic())))
        process.sendline(password)
        process.expect(pexpect.EOF,timeout=max(.001,deadline-time.monotonic()))
        transcript=process.before
        process.close()
        code=process.exitstatus if process.signalstatus is None else -process.signalstatus
        report=extract_report(transcript,step)
        return {'returncode':code,'report':report}
    finally:
        if process.isalive():
            process.close(force=True)


def start(output_dir,password,*,enable_motion=False,seconds=10.,run_fn=None,remote_fn=None):
    if type(enable_motion) is not bool or type(seconds) not in (int,float) or not math.isfinite(seconds) or not 5<=seconds<=10:
        raise ValueError('right-arm window must be 5..10 seconds')
    if run_fn is None:
        from run_right_arm_teleop import run as run_fn
    remote_fn=remote_once if remote_fn is None else remote_fn
    output_dir=Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=False)
    result={'type':'right_arm_start_summary','scope':'right_arm_only','enable_motion':enable_motion,
            'physical_motion_verified':False,'hand_commands_requested':False,
            'release_step_attempted':False,'release_action_unknown':False,
            'mode_restored':False,'automatic_retries':0,'completed':False,'stage':'preview',
            'output_dir':str(output_dir)}
    def save():
        (output_dir/'start-summary.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    try:
        save()
        result['preview']=run_fn(output_dir/'preview.jsonl',password,enable_motion=False,
                                 control_release_verified=False,seconds=5.)
        verify_session(result['preview'],motion=False)
        if not enable_motion:
            result.update(completed=True,stage='preview_completed')
            return result
        result.update(stage='release',release_step_attempted=True,release_action_unknown=True)
        save()
        response=remote_fn('release',password)
        result['release']=response
        save()
        if not isinstance(response,dict) or not strict_zero(response.get('returncode')):
            raise ValueError('release_process_failed_no_retry')
        verify_release(response.get('report'))
        result['release_action_unknown']=False
        result['stage']='mode_probe'
        save()
        response=remote_fn('mode_probe',password)
        result['mode_probe']=response
        save()
        if not isinstance(response,dict) or not strict_zero(response.get('returncode')):
            raise ValueError('mode_probe_process_failed')
        verify_mode(response.get('report'))
        result['stage']='motion'
        save()
        result['motion']=run_fn(output_dir/'motion.jsonl',password,enable_motion=True,
                                control_release_verified=True,seconds=float(seconds))
        verify_session(result['motion'],motion=True)
        result.update(completed=True,stage='motion_session_completed')
    except (Exception,KeyboardInterrupt) as exc:
        # No release retry or SelectMode restoration is attempted on any path.
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        save()
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-motion',action='store_true')
    parser.add_argument('--seconds',type=float,default=10.,help='right-arm session: 5..10 seconds')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 5<=args.seconds<=10:
        parser.error('--seconds must be 5..10')
    if args.output_dir is None:
        args.output_dir=ROOT/'diagnostics'/('start-right-arm-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    signal.signal(signal.SIGTERM,lambda signum,frame: (_ for _ in ()).throw(KeyboardInterrupt('SIGTERM')))
    result=start(args.output_dir,getpass.getpass('PC2 SSH password: '),
                 enable_motion=args.enable_motion,seconds=args.seconds)
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0 if result['completed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
