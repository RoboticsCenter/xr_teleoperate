"""Startup ordering and SSH/report contracts; never connect to hardware."""
import copy
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'scripts') not in sys.path:
    sys.path.insert(0,str(ROOT/'scripts'))
import start_right_arm as starter


def session(motion=False):
    return {'motion_requested':motion,'stop_reason':'remote_ended','commands_published':10 if motion else 0,
            'physical_motion_verified':False,
            'remote_result':{'type':'runtime_final','reason':'runtime_deadline','preview':not motion,
                             'accepted_targets':5,'actual_write_count':10 if motion else 0,
                             'last_action_unknown':False,'errors':[]}}


def calls(ids):
    return [{'api_id':x,'parameter':'{}'} for x in ids]


def release_report(sent=True):
    result={'type':'release_result','sdk_source_sha256':dict(starter.AUDITED_FILES),
            'service_inactive_verified':True,'motor_commands_published':0,'mode_restored':False,
            'before':{'code':0,'data':{'name':'ai' if sent else ''}},
            'after':{'code':0,'data':{'name':''}},'release_sent':sent,
            'rpc_calls':calls([1001,1003,1001] if sent else [1001])}
    if sent: result['release_code']=0
    return result


def mode_report():
    return {'exit_code':0,'sdk_source_sha256':dict(starter.AUDITED_FILES),
            'mode_changes_requested':0,'motion_command_publishers_created':0,'rpc_calls':calls([1001]),
            'check_mode':{'code':0,'data_raw':{'name':''}},
            'lowstate':{'fresh':True,'valid_count':100,'age_s':.01},
            'publication_discovery':{'lowcmd_writers_observed':[],'errors':[]}}


class StartupTests(unittest.TestCase):
    def run_start(self,*,motion=True,preview=None,release=None,mode=None,motion_result=None,seconds=10.):
        events=[]
        def run(path,password,**kwargs):
            self.assertEqual(password,'fake-secret')
            active=kwargs['enable_motion']
            events.append(('motion' if active else 'preview',kwargs))
            return copy.deepcopy((motion_result or session(True)) if active else (preview or session()))
        def remote(step,password):
            self.assertEqual(password,'fake-secret')
            events.append((step,{}))
            report=(release if release is not None else release_report()) if step=='release' else (mode if mode is not None else mode_report())
            if isinstance(report,BaseException): raise report
            return {'returncode':0,'report':copy.deepcopy(report)}
        with tempfile.TemporaryDirectory(prefix='right-arm-start-fake-') as temp:
            path=Path(temp)/'session'
            result=starter.start(path,'fake-secret',enable_motion=motion,seconds=seconds,run_fn=run,remote_fn=remote)
            saved=json.loads((path/'start-summary.json').read_text())
            self.assertEqual(result,saved)
            self.assertNotIn('fake-secret',json.dumps(saved))
        self.assertFalse(result['physical_motion_verified'])
        self.assertFalse(result['hand_commands_requested'])
        self.assertFalse(result['mode_restored'])
        self.assertEqual(result['automatic_retries'],0)
        return result,events

    def test_default_is_only_five_second_preview(self):
        result,events=self.run_start(motion=False)
        self.assertTrue(result['completed'])
        self.assertEqual([x[0] for x in events],['preview'])
        self.assertEqual(events[0][1],dict(enable_motion=False,control_release_verified=False,seconds=5.))
        self.assertFalse(result['release_step_attempted'])

    def test_explicit_motion_has_strict_order_and_verified_flag_only_at_end(self):
        result,events=self.run_start(seconds=7.)
        self.assertTrue(result['completed'])
        self.assertEqual([x[0] for x in events],['preview','release','mode_probe','motion'])
        self.assertEqual(events[-1][1],dict(enable_motion=True,control_release_verified=True,seconds=7.))
        self.assertFalse(result['release_action_unknown'])

    def test_preview_zero_targets_abnormal_stop_unknown_writes_or_error_blocks_release(self):
        for kind in ('zero','stop','unknown','writes','errors','missing_count','bad_final'):
            preview=session()
            remote=preview['remote_result']
            if kind=='zero': remote['accepted_targets']=0
            elif kind=='stop': preview['stop_reason']='RuntimeError: IK failed'
            elif kind=='unknown': remote['last_action_unknown']=True
            elif kind=='writes': remote['actual_write_count']=1
            elif kind=='errors': remote['errors']=['cleanup failed']
            elif kind=='missing_count': del remote['actual_write_count']
            else: remote['type']='runtime_error'
            with self.subTest(kind=kind):
                result,events=self.run_start(preview=preview)
                self.assertFalse(result['completed'])
                self.assertEqual([x[0] for x in events],['preview'])

    def test_release_failure_or_timeout_never_probes_retries_or_moves(self):
        for kind in ('nonzero','nonempty','cleanup','hash','extra_rpc','timeout'):
            report=release_report()
            if kind=='nonzero': report['release_code']=1
            elif kind=='nonempty': report['after']['data']['name']='ai'
            elif kind=='cleanup': report['cleanup_error']='close failed'
            elif kind=='hash': report['sdk_source_sha256']={}
            elif kind=='extra_rpc': report['rpc_calls']+=calls([1003])
            else: report=TimeoutError('remote deadline')
            with self.subTest(kind=kind):
                result,events=self.run_start(release=report)
                self.assertFalse(result['completed'])
                self.assertEqual([x[0] for x in events],['preview','release'])
                self.assertTrue(result['release_action_unknown'])

    def test_already_empty_release_result_is_valid_without_release_rpc(self):
        result,events=self.run_start(release=release_report(False))
        self.assertTrue(result['completed'])
        self.assertFalse(result['release']['report']['release_sent'])

    def test_mode_writer_discovery_failure_nonempty_or_stale_feedback_blocks_motion(self):
        for kind in ('writer','discovery_error','missing_discovery','name','stale','missing_fresh','failed_query','wrong_rpc'):
            report=mode_report()
            if kind=='writer': report['publication_discovery']['lowcmd_writers_observed']=[{'guid':'other'}]
            elif kind=='discovery_error': report['publication_discovery']['errors']=['DDS error']
            elif kind=='missing_discovery': del report['publication_discovery']['lowcmd_writers_observed']
            elif kind=='name': report['check_mode']['data_raw']['name']='ai'
            elif kind=='stale': report['lowstate']['age_s']=.5
            elif kind=='missing_fresh': del report['lowstate']['fresh']
            elif kind=='failed_query': report['check_mode']['code']=3102
            else: report['rpc_calls']=calls([1002])
            with self.subTest(kind=kind):
                result,events=self.run_start(mode=report)
                self.assertFalse(result['completed'])
                self.assertEqual([x[0] for x in events],['preview','release','mode_probe'])

    def test_motion_unknown_or_zero_writes_is_not_success_and_never_restores(self):
        for kind in ('unknown','zero'):
            final=session(True)
            if kind=='unknown': final['remote_result']['last_action_unknown']=True
            else: final['remote_result']['actual_write_count']=0
            result,events=self.run_start(motion_result=final)
            self.assertFalse(result['completed'])
            self.assertEqual([x[0] for x in events],['preview','release','mode_probe','motion'])

    def test_invalid_window_fails_before_io(self):
        for seconds in (0,4.9,10.01,True,float('nan'),float('inf')):
            run,remote=mock.Mock(),mock.Mock()
            with self.assertRaises(ValueError):
                starter.start('/path/must/not/be/created','unused',seconds=seconds,run_fn=run,remote_fn=remote)
            run.assert_not_called();remote.assert_not_called()

    def test_no_reuse_of_existing_evidence_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            run=mock.Mock()
            with self.assertRaises(FileExistsError):
                starter.start(temp,'unused',run_fn=run)
            run.assert_not_called()


class RemoteContractTests(unittest.TestCase):
    def test_memory_bundle_loads_dependency_and_quotes_shell_safely(self):
        with tempfile.TemporaryDirectory(prefix='fake sdk space ') as temp:
            root=Path(temp);(root/'scripts').mkdir()
            (root/'scripts/g1_mode_probe.py').write_text('AUDITED_FILES={"fake":"hash"}\ndef main(): return 0\n')
            (root/'scripts/g1_release_once.py').write_text('from g1_mode_probe import AUDITED_FILES\ndef main():\n assert AUDITED_FILES=={"fake":"hash"}\n return 0\n')
            with mock.patch.object(starter,'ROOT',root):
                command=starter.remote_command('release')
            args=shlex.split(command)
            self.assertEqual(args[:4],['timeout','--signal=TERM','--kill-after=2s','20s'])
            self.assertEqual(args[4],'/home/unitree/miniconda3/envs/tv/bin/python')
            self.assertEqual(args[5:8],['-u','-B','-c'])
            with mock.patch.dict(sys.modules),mock.patch.object(sys,'argv',[]):
                with self.assertRaises(SystemExit) as finished:
                    exec(compile(args[8],'<inert fake bootstrap>','exec'),{})
                self.assertEqual(finished.exception.code,0)
                self.assertEqual(sys.argv,['g1_release_once','--interface','eth0','--release'])

    def test_ssh_is_strict_and_matches_existing_identity(self):
        args=starter.ssh_args('fixed-command')
        for option in ('StrictHostKeyChecking=yes','HostKeyAlias=192.168.123.164',
                       'NumberOfPasswordPrompts=1','ConnectTimeout=5'):
            self.assertIn(option,args)
        self.assertIn('UserKnownHostsFile='+str(ROOT/'diagnostics/g1-known_hosts'),args)
        self.assertEqual(args[-2:],['unitree@192.168.31.65','fixed-command'])
        self.assertNotIn('StrictHostKeyChecking=no',args)

    def test_multiline_report_with_native_noise_is_parsed_once(self):
        report=mode_report()
        text='SDK started\n'+json.dumps(report,indent=2)+'\nSDK stopped\n'
        self.assertEqual(starter.extract_report(text,'mode_probe'),report)
        with self.assertRaises(ValueError): starter.extract_report(text+text,'mode_probe')
        with self.assertRaises(ValueError): starter.extract_report('no result','mode_probe')

    def test_remote_password_once_bounded_deadline_and_cleanup(self):
        eof=object();report=release_report()
        process=mock.Mock()
        process.before=json.dumps(report)
        process.exitstatus=0;process.signalstatus=None;process.isalive.return_value=False
        spawn=mock.Mock(return_value=process)
        fake=SimpleNamespace(spawn=spawn,EOF=eof)
        with mock.patch.dict(sys.modules,{'pexpect':fake}),mock.patch.object(starter,'remote_command',return_value='command'),mock.patch.object(starter.time,'monotonic',side_effect=[100.,101.,102.]):
            response=starter.remote_once('release','fake-secret')
        self.assertEqual(response,{'returncode':0,'report':report})
        process.sendline.assert_called_once_with('fake-secret')
        self.assertNotIn('fake-secret',repr(spawn.call_args))
        self.assertEqual(process.expect.call_args_list[0].kwargs['timeout'],8)
        self.assertEqual(process.expect.call_args_list[1].kwargs['timeout'],28.)
        process.close.assert_called_once_with()

    def test_remote_timeout_forces_local_close_without_retry(self):
        eof=object();process=mock.Mock()
        process.expect.side_effect=[0,TimeoutError('deadline')]
        process.isalive.return_value=True
        spawn=mock.Mock(return_value=process)
        with mock.patch.dict(sys.modules,{'pexpect':SimpleNamespace(spawn=spawn,EOF=eof)}),mock.patch.object(starter,'remote_command',return_value='command'):
            with self.assertRaises(TimeoutError): starter.remote_once('release','fake-secret')
        spawn.assert_called_once()
        process.close.assert_called_once_with(force=True)


if __name__=='__main__':
    unittest.main()
