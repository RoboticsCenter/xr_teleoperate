"""Fake SDK only; these tests cannot initialize real DDS or move a robot."""
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0,str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('release_under_test',SCRIPTS/'g1_release_once.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class FakeSDK:
    def __init__(self, directory, replies, *, query_api=1001, query_parameter='{}', repeat_release=False):
        self.calls = []
        self.initialized = []
        self.client_created = 0
        self.close_calls = []
        self.select_calls = []
        self.replies = list(replies)
        self.modules = {}
        self.hashes = {}
        root = Path(directory)/'unitree_sdk2py'
        for name in release.AUDITED_FILES:
            path = root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            content = ('# inert test fixture '+name+'\n').encode()
            path.write_bytes(content)
            self.hashes[name] = hashlib.sha256(content).hexdigest()
        for name in ('unitree_sdk2py','unitree_sdk2py.comm','unitree_sdk2py.comm.motion_switcher',
                     'unitree_sdk2py.core','unitree_sdk2py.core.channel',
                     'unitree_sdk2py.comm.motion_switcher.motion_switcher_client'):
            module = ModuleType(name)
            module.__path__ = []
            self.modules[name] = module
            if '.' in name:
                parent, leaf = name.rsplit('.',1)
                setattr(self.modules[parent],leaf,module)
        channel = self.modules['unitree_sdk2py.core.channel']
        channel.ChannelFactoryInitialize = lambda *args: self.initialized.append(args)
        motion = self.modules['unitree_sdk2py.comm.motion_switcher.motion_switcher_client']
        motion.__file__ = str(root/'comm/motion_switcher/motion_switcher_client.py')
        fixture = self

        class Client:
            def __init__(self):
                fixture.client_created += 1
                self._ClientBase__stub = SimpleNamespace(
                    _ClientStub__recvChannel=SimpleNamespace(CloseReader=lambda:fixture.close_calls.append('reader')),
                    _ClientStub__sendChannel=SimpleNamespace(CloseWriter=lambda:fixture.close_calls.append('writer')))
            def SetTimeout(self,timeout): fixture.timeout = timeout
            def Init(self): fixture.init_called = True
            def CheckMode(self): return self._Call(query_api,query_parameter)
            def ReleaseMode(self):
                answer = self._Call(1003,'{}')
                if repeat_release:
                    self._Call(1003,'{}')
                return answer
            def SelectMode(self,*args):
                fixture.select_calls.append(args)
                raise AssertionError('must never restore/select a mode')
            def _Call(self,api,parameter):
                fixture.calls.append((api,json.loads(parameter)))
                if not fixture.replies:
                    raise AssertionError('unexpected extra RPC')
                answer = fixture.replies.pop(0)
                if isinstance(answer,BaseException):
                    raise answer
                return answer
        motion.MotionSwitcherClient = Client


class ReleaseOnceTests(unittest.TestCase):
    def run_fake(self,replies,*,tamper_hash=False,**sdk_options):
        with tempfile.TemporaryDirectory(prefix='g1-release-fake-') as directory:
            sdk = FakeSDK(directory,replies,**sdk_options)
            hashes = dict(sdk.hashes)
            if tamper_hash:
                hashes[next(iter(hashes))] = '0'*64
            output = io.StringIO()
            # Replace the module's OS object, not os.dup2 process-wide. No real
            # descriptor changes, hard exits, sleepers or timer threads occur.
            fake_os = SimpleNamespace(dup=mock.Mock(return_value=123),
                                      fdopen=mock.Mock(return_value=output),
                                      dup2=mock.Mock(),_exit=mock.Mock())
            timer = SimpleNamespace(start=mock.Mock(),cancel=mock.Mock(),daemon=False)
            fake_timer = mock.Mock(return_value=timer)
            fake_time = SimpleNamespace(sleep=mock.Mock())
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(sys.modules,sdk.modules))
                stack.enter_context(mock.patch.object(release,'AUDITED_FILES',hashes))
                stack.enter_context(mock.patch.object(release,'os',fake_os))
                stack.enter_context(mock.patch.object(release,'threading',SimpleNamespace(Timer=fake_timer)))
                stack.enter_context(mock.patch.object(release,'time',fake_time))
                code = release.run('fake0')
            report = json.loads(output.getvalue())
            timer.start.assert_called_once()
            timer.cancel.assert_called_once()
            fake_os._exit.assert_not_called()
            self.assertEqual(fake_timer.call_args.args[0],12)
            self.assertTrue(timer.daemon)
            self.assertEqual(report['motor_commands_published'],0)
            self.assertIs(report['mode_restored'],False)
            self.assertEqual(sdk.select_calls,[])
            self.assertLessEqual(sum(api==1003 for api,_ in sdk.calls),1)
            self.assertTrue(all(api in (1001,1003) and params=={} for api,params in sdk.calls))
            self.assertEqual([x['api_id'] for x in report['rpc_calls']],[x[0] for x in sdk.calls])
            if sdk.client_created:
                self.assertEqual(sdk.close_calls,['reader','writer'])
                self.assertEqual(sdk.initialized,[(0,'fake0')])
                self.assertEqual(sdk.timeout,2)
            return code,report,sdk,fake_time

    def test_default_plan_imports_no_robot_sdk_and_does_no_rpc(self):
        # A fresh interpreter also catches SDK imports in transitive modules.
        program = '''import importlib.abc,json,runpy,sys
class ForbidRobotImport(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.startswith(('unitree_sdk2py','cyclonedds')):
            raise AssertionError('robot SDK imported during plan: '+fullname)
sys.meta_path.insert(0,ForbidRobotImport())
script=sys.argv[1]
sys.path.insert(0,str(__import__('pathlib').Path(script).parent))
sys.argv=[script]
runpy.run_path(script,run_name='__main__')
'''
        proc = subprocess.run([sys.executable,'-c',program,str(SCRIPTS/'g1_release_once.py')],
                              capture_output=True,text=True,timeout=5)
        self.assertEqual(proc.returncode,0,proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result['plan_only'])
        self.assertEqual(result['motor_commands_published'],0)
        self.assertTrue(result['no_automatic_retry'])

    def test_ai_releases_once_then_verifies_empty(self):
        code,report,sdk,clock = self.run_fake([(0,{'name':'ai'}),(0,''),(0,{'name':''})])
        self.assertEqual(code,0)
        self.assertEqual(sdk.calls,[(1001,{}),(1003,{}),(1001,{})])
        self.assertTrue(report['release_sent'])
        self.assertTrue(report['service_inactive_verified'])
        clock.sleep.assert_called_once_with(1)

    def test_already_empty_does_not_release_or_requery(self):
        code,report,sdk,clock = self.run_fake([(0,{'name':''})])
        self.assertEqual(code,0)
        self.assertEqual(sdk.calls,[(1001,{})])
        self.assertFalse(report['release_sent'])
        self.assertEqual(report['before'],report['after'])
        clock.sleep.assert_not_called()

    def test_unknown_service_or_malformed_response_never_releases(self):
        for before in ({'name':'sport'},{'name':'unknown'},{'name':None},{},None,'ai',[]):
            with self.subTest(before=before):
                code,report,sdk,clock = self.run_fake([(0,before)])
                self.assertEqual(code,1)
                self.assertEqual(sdk.calls,[(1001,{})])
                self.assertIn('error',report)
                clock.sleep.assert_not_called()

    def test_failed_initial_query_never_releases(self):
        for before in ((3102,{'name':'ai'}),(3102,{'name':''}),TimeoutError('query timeout')):
            with self.subTest(before=before):
                code,report,sdk,_ = self.run_fake([before])
                self.assertEqual(code,1)
                self.assertEqual(sdk.calls,[(1001,{})])
                self.assertIn('error',report)

    def test_release_rpc_failure_still_only_checks_once_and_never_retries(self):
        code,report,sdk,_ = self.run_fake([(0,{'name':'ai'}),(123,'failed'),(0,{'name':'ai'})])
        self.assertEqual(code,1)
        self.assertEqual(report['release_code'],123)
        self.assertFalse(report['service_inactive_verified'])
        self.assertEqual(sdk.calls,[(1001,{}),(1003,{}),(1001,{})])

    def test_release_exception_never_retries_or_restores(self):
        code,report,sdk,clock = self.run_fake([(0,{'name':'ai'}),TimeoutError('release timeout')])
        self.assertEqual(code,1)
        self.assertEqual(sdk.calls,[(1001,{}),(1003,{})])
        self.assertIn('release timeout',report['error'])
        clock.sleep.assert_not_called()

    def test_final_nonempty_or_failed_query_never_releases_again(self):
        for after in ((0,{'name':'ai'}),(0,{'name':'sport'}),(3102,{'name':''}),
                      (0,None),TimeoutError('final query timeout')):
            with self.subTest(after=after):
                code,report,sdk,_ = self.run_fake([(0,{'name':'ai'}),(0,''),after])
                self.assertEqual(code,1)
                self.assertEqual(sdk.calls,[(1001,{}),(1003,{}),(1001,{})])
                self.assertIn('error',report)

    def test_hash_mismatch_has_no_channel_client_or_rpc(self):
        code,report,sdk,clock = self.run_fake([],tamper_hash=True)
        self.assertEqual(code,1)
        self.assertIn('differs from reviewed',report['error'])
        self.assertEqual(sdk.calls,[])
        self.assertEqual(sdk.initialized,[])
        self.assertEqual(sdk.client_created,0)
        clock.sleep.assert_not_called()

    def test_wrong_rpc_or_nonempty_parameter_blocked_before_backend(self):
        for options in ({'query_api':1002},{'query_parameter':'{"name":"ai"}'}):
            with self.subTest(options=options):
                code,report,sdk,_ = self.run_fake([],**options)
                self.assertEqual(code,1)
                self.assertEqual(sdk.calls,[])
                self.assertIn('unexpected RPC',report['error'])

    def test_duplicate_release_blocked_before_backend(self):
        code,report,sdk,_ = self.run_fake([(0,{'name':'ai'}),(0,'')],repeat_release=True)
        self.assertEqual(code,1)
        self.assertEqual(sdk.calls,[(1001,{}),(1003,{})])
        self.assertIn('repeated release blocked',report['error'])

    def test_nonzero_release_code_not_hidden_by_later_empty_state(self):
        code,report,sdk,_ = self.run_fake([(0,{'name':'ai'}),(123,'failed'),(0,{'name':''})])
        self.assertEqual(sdk.calls,[(1001,{}),(1003,{}),(1001,{})])
        self.assertEqual(report['release_code'],123)
        self.assertEqual(code,1,'a failed RPC must not become exit 0 merely because final state is empty')


if __name__=='__main__':
    unittest.main()
