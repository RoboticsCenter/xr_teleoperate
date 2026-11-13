"""Fake SSH only; no robot, networking, or hardware services are used."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_robot_connection as robot


class EOF(Exception):
    def __str__(self): raise AssertionError('pexpect error must never be serialized')
class TIMEOUT(Exception):
    def __str__(self): raise AssertionError('pexpect error must never be serialized')


def identity(**changes):
    value={'type':'identity','hostname':'ubuntu','user':'unitree','ssh_server_ip':robot.robot_host(),
           'addresses':['127.0.0.1',robot.robot_host(),'192.168.123.164'],'sequence':0}
    value.update(changes);return value


class FakeSSH:
    def __init__(self,auth_error=None,auth_wait=False,close_gate=None):
        self.before='';self.records=queue.Queue();self.auth_error=auth_error
        self.auth_wait=auth_wait;self.closed=False;self.sent=[];self.close_gate=close_gate
        self.expect_calls=0
    def expect(self,pattern,timeout):
        self.expect_calls+=1
        if pattern=='[Pp]assword:':
            if self.auth_error:
                self.before=self.auth_error;raise EOF('SECRET-ARGV')
            if self.auth_wait:
                time.sleep(.005);raise TIMEOUT('SECRET-ARGV')
            self.before='';return 0
        try:value=self.records.get(timeout=.005)
        except queue.Empty:raise TIMEOUT('SECRET-ARGV')
        if isinstance(value,Exception):
            self.before='Connection closed SECRET-PASSWORD';raise value
        self.before=json.dumps(value) if not isinstance(value,str) else value
        return 0
    def sendline(self,line):self.sent.append(line)
    def close(self,force=False):
        assert force
        if self.close_gate is not None:self.close_gate.wait(1)
        self.closed=True


class RobotConnectionTests(unittest.TestCase):
    def setUp(self):
        # Fake identities and temporary-root managers must use the same address,
        # independently of this laptop's network.json and environment overrides.
        host=mock.patch.object(robot,'robot_host',return_value='192.0.2.10')
        host.start();self.addCleanup(host.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'diagnostics').mkdir();(self.root/'diagnostics/g1-known_hosts').write_text('fake pinned key\n')
        self.process=FakeSSH();self.spawn=mock.Mock(return_value=self.process)
        self.pexpect=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT)

    def manager(self,**kwargs):
        options=dict(spawn=self.spawn,pexpect_module=self.pexpect)
        options.update(kwargs)
        value=robot.RobotConnection(self.root,password='SECRET-PASSWORD',**options)
        self.addCleanup(value.close);return value

    def wait(self,manager,predicate,timeout=2):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            value=manager.status()
            if predicate(value):return value
            time.sleep(.005)
        self.fail(repr(manager.status()))

    def test_constructor_status_and_simulation_have_no_ssh_side_effect(self):
        manager=self.manager();self.assertEqual(manager.status()['stage'],'idle')
        self.spawn.assert_not_called()
        simulated=self.manager(simulation=True)
        state=simulated.connect()
        self.assertTrue(state['simulation']);self.assertFalse(state['connected'])
        self.spawn.assert_not_called()

    def test_identity_heartbeat_and_idempotent_connect(self):
        self.process.records.put(identity())
        manager=self.manager();manager.connect()
        state=self.wait(manager,lambda s:s['connected'])
        self.assertEqual(state['hostname'],'ubuntu');self.assertIn(robot.robot_host(),state['addresses'])
        manager.connect();self.assertEqual(self.spawn.call_count,1)
        args=self.spawn.call_args.args[1]
        for option in ('StrictHostKeyChecking=yes','HostKeyAlias=192.168.123.164','ServerAliveInterval=2'):
            self.assertIn(option,args)
        self.assertNotIn('SECRET-PASSWORD',repr(self.spawn.call_args))
        self.assertEqual(self.process.sent,['SECRET-PASSWORD'])
        self.assertNotIn('SECRET',json.dumps(state))
        self.assertNotIn('subprocess',robot.REMOTE_PROGRAM)
        self.assertNotIn('unitree_sdk',robot.REMOTE_PROGRAM)
        time.sleep(.01);previous=manager.status()['last_seen_age_s']
        self.process.records.put({'type':'heartbeat','sequence':1})
        self.wait(manager,lambda s:s['last_seen_age_s']<previous)
        manager.disconnect();self.wait(manager,lambda s:not s['connected'])
        manager.close();self.assertTrue(self.process.closed)

    def test_configured_robot_address_is_used_for_ssh_display_and_identity(self):
        host='10.1.10.73'
        self.process.records.put(identity(ssh_server_ip=host,addresses=[host,'192.168.123.164']))
        with mock.patch.object(robot,'robot_host',return_value=host) as configured:
            manager=self.manager();manager.connect()
            state=self.wait(manager,lambda s:s['connected'])
            self.assertEqual(state['host'],host)
            self.assertIn('unitree@'+host,self.spawn.call_args.args[1])
            self.assertIn('HostKeyAlias=192.168.123.164',self.spawn.call_args.args[1])
            configured.assert_called_with(self.root)
        with self.assertRaises(robot._Failure):
            robot.validate_identity(identity(ssh_server_ip='192.168.31.65'),host)

    def test_failure_reasons_are_fixed_and_manual_reconnect_works(self):
        for reason,expected in [('No route to host','No route'),('Connection refused','refused'),
                ('Connection timed out','timed out'),('Permission denied','authentication failed'),
                ('Host key verification failed','identity could not'),('unknown output','ended before')]:
            with self.subTest(reason=reason):
                failed=FakeSSH(auth_error=reason+' SECRET-PASSWORD PRIVATE-BOOTSTRAP')
                good=FakeSSH();good.records.put(identity())
                spawn=mock.Mock(side_effect=[failed,good]);manager=self.manager(spawn=spawn)
                manager.connect();state=self.wait(manager,lambda s:s['stage']=='error')
                self.assertIn(expected,state['error']);self.assertNotIn('SECRET',json.dumps(state))
                self.assertNotIn('BOOTSTRAP',json.dumps(state));self.assertTrue(failed.closed)
                manager.connect();self.wait(manager,lambda s:s['connected'])
                self.assertEqual(spawn.call_count,2);manager.close();self.assertTrue(good.closed)

    def test_wrong_identity_never_reports_connected(self):
        for changes in ({'user':'root'},{'ssh_server_ip':'192.168.31.101'},
                        {'addresses':['127.0.0.1']},{'addresses':[True]},{'sequence':False}):
            with self.subTest(changes=changes):
                process=FakeSSH();process.records.put(identity(**changes))
                manager=self.manager(spawn=mock.Mock(return_value=process));manager.connect()
                state=self.wait(manager,lambda s:s['stage']=='error')
                self.assertFalse(state['connected']);self.assertTrue(process.closed)

    def test_eof_after_identity_disconnects_and_releases_ssh(self):
        self.process.records.put(identity());manager=self.manager();manager.connect()
        self.wait(manager,lambda s:s['connected']);self.process.records.put(EOF('SECRET'))
        state=self.wait(manager,lambda s:s['stage']=='disconnected')
        self.assertFalse(state['connected']);self.assertTrue(self.process.closed)
        self.assertNotIn('SECRET',json.dumps(state))

    def test_authentication_wait_is_finite_and_cancellable_without_locking_status(self):
        process=FakeSSH(auth_wait=True)
        manager=self.manager(spawn=mock.Mock(return_value=process),authentication_timeout_s=.03)
        started=time.monotonic();manager.connect();self.assertLess(time.monotonic()-started,.1)
        state=self.wait(manager,lambda s:s['stage']=='error');self.assertIn('timed out',state['error'])
        self.assertTrue(process.closed)
        blocked=FakeSSH(auth_wait=True);manager=self.manager(spawn=mock.Mock(return_value=blocked))
        manager.connect();self.wait(manager,lambda s:blocked.expect_calls>0)
        started=time.monotonic();manager.status();manager.disconnect()
        self.assertLess(time.monotonic()-started,.1);manager.close();self.assertTrue(blocked.closed)
        self.assertEqual(blocked.sent,[])

    def test_stale_or_duplicate_heartbeat_does_not_claim_live_connection(self):
        now=[100.];self.process.records.put(identity())
        manager=self.manager(monotonic=lambda:now[0]);manager.connect()
        self.wait(manager,lambda s:s['connected'])
        now[0]=105.;self.process.records.put({'type':'heartbeat','sequence':0})
        time.sleep(.02);self.assertEqual(manager.status()['last_seen_age_s'],5.)
        now[0]=109.
        self.assertFalse(manager.status()['connected'])
        self.wait(manager,lambda s:self.process.closed)
        self.assertIn('heartbeat timed out',manager.status()['error'])

    def test_old_cancelled_attempt_cannot_close_or_overwrite_reconnected_attempt(self):
        gate=threading.Event();old=FakeSSH(auth_wait=True,close_gate=gate)
        new=FakeSSH();new.records.put(identity())
        manager=self.manager(spawn=mock.Mock(side_effect=[old,new]))
        manager.connect()
        self.wait(manager,lambda s:old.expect_calls>0)
        manager.disconnect();manager.connect();self.wait(manager,lambda s:s['connected'])
        gate.set();time.sleep(.02)
        self.assertTrue(old.closed);self.assertFalse(new.closed);self.assertTrue(manager.status()['connected'])

    def test_missing_known_hosts_does_not_spawn_or_authenticate(self):
        (self.root/'diagnostics/g1-known_hosts').unlink()
        manager=self.manager();manager.connect()
        state=self.wait(manager,lambda s:s['stage']=='error')
        self.assertIn('identity file',state['error']);self.spawn.assert_not_called()

    def test_cancel_before_spawn_does_not_create_an_ssh_process(self):
        entered,release=threading.Event(),threading.Event()
        def delayed_file_check(path):entered.set();release.wait(1);return True
        manager=self.manager()
        with mock.patch.object(Path,'is_file',delayed_file_check):
            manager.connect();self.assertTrue(entered.wait(1))
            manager.disconnect();release.set();manager.close()
        self.spawn.assert_not_called()

    def test_fixed_remote_program_compiles_without_control_or_command_execution(self):
        import ast
        compile(robot.REMOTE_PROGRAM,'readonly-g1-heartbeat','exec')
        tree=ast.parse(robot.REMOTE_PROGRAM)
        imports={alias.name for node in ast.walk(tree) if isinstance(node,ast.Import) for alias in node.names}
        self.assertEqual(imports,{'fcntl','json','os','pwd','socket','struct','time'})
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name):
                self.assertNotIn(node.func.id,{'eval','exec','input','__import__'})
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                self.assertNotIn(node.func.attr,{'system','popen','connect','send','sendto'})


if __name__=='__main__':unittest.main()
