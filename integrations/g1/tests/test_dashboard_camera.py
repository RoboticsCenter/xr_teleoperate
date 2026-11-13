"""Camera integration checks use simulated frames, never PC2 IO."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from dashboard_camera import CameraManager, FRAME_FRESHNESS_S, SIMULATION_JPEG, remote_command
import dashboard_camera as camera


def until(predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('camera condition did not become true')


class CameraLifecycle(unittest.TestCase):
    def test_construction_does_not_start_camera_or_process(self):
        with patch('dashboard_camera.subprocess.Popen') as spawn:
            manager = CameraManager(ROOT, simulation=True)
            self.assertFalse(manager.status()['running'])
            self.assertIsNone(manager.jpeg())
            manager.close()
        spawn.assert_not_called()

    def test_simulated_start_stop_and_restart_own_worker(self):
        manager = CameraManager(ROOT, simulation=True)
        self.addCleanup(manager.close)
        manager.start()
        first = manager._process
        until(lambda: manager.status()['live'])
        self.assertEqual(manager.jpeg(), base64.b64decode(SIMULATION_JPEG))
        self.assertEqual(manager.status()['device'], 'simulation')
        self.assertIn('SIMULATION', manager.status()['detail'])
        manager.start()
        self.assertIs(manager._process, first)
        manager.stop()
        self.assertEqual(first.poll(), 0)
        self.assertIsNone(manager.jpeg())
        self.assertFalse(manager.status()['running'])
        manager.start()
        self.assertNotEqual(manager._process.pid, first.pid)
        until(lambda: manager.status()['live'])

    def test_source_freshness_matches_branch_and_next_frame_recovers(self):
        manager = CameraManager(ROOT, simulation=True)
        manager._state['running'] = True
        manager._jpeg = b'current'
        manager._received = 100.
        with patch('dashboard_camera.time.monotonic', return_value=100.+FRAME_FRESHNESS_S):
            self.assertIsNone(manager.jpeg())
        manager._received = 102.
        with patch('dashboard_camera.time.monotonic', return_value=102.1):
            self.assertEqual(manager.jpeg(), b'current')
            self.assertTrue(manager.status()['live'])
        manager.close()

    def test_worker_death_invalidates_frame(self):
        manager = CameraManager(ROOT, simulation=True)
        self.addCleanup(manager.close)
        manager.start()
        until(lambda: manager.status()['live'])
        manager._process.kill()
        manager._process.wait(timeout=2)
        until(lambda: not manager.status()['running'])
        self.assertIsNone(manager.jpeg())

    def test_credentials_stay_out_of_process_arguments_and_environment(self):
        manager = CameraManager(ROOT, password='private-test-secret')
        with patch('dashboard_camera.subprocess.Popen') as spawn:
            spawn.return_value.stdout = iter(())
            spawn.return_value.poll.return_value = 0
            manager.start()
            call = spawn.call_args
            self.assertNotIn('private-test-secret', repr(call))
            self.assertEqual(json.loads(spawn.return_value.stdin.write.call_args.args[0]),
                             {'password': 'private-test-secret'})
            self.assertNotIn('env', call.kwargs)

    def test_bootstrap_has_no_camera_launch_deadline_or_secret(self):
        command = remote_command(ROOT)
        self.assertNotIn('timeout ', command)
        self.assertNotIn('--seconds', command)
        self.assertIn('/home/unitree/miniconda3/envs/tv/bin/python', command)
        self.assertTrue(command.endswith(' 2>/dev/null'))

    def test_configured_camera_ssh_target_keeps_original_robot_host_key(self):
        import types
        stopped=threading.Event();calls=[];closed=[]
        class EOF(Exception):pass
        class TIMEOUT(Exception):pass
        def spawn(command,args,**kwargs):
            calls.append((command,args));stopped.set()
            return types.SimpleNamespace(sendeof=lambda:None,expect=lambda *a,**k:None,
                close=lambda **k:closed.append(True),
                sendline=lambda *_:self.fail('Cancelled connection must not authenticate'))
        fake=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT,spawn=spawn)
        with patch.dict(sys.modules,{'pexpect':fake}), \
                patch.object(camera,'robot_host',return_value='10.1.10.73') as configured:
            camera.receive_camera(stopped,'unused-test',ROOT)
        configured.assert_called_once_with(ROOT)
        self.assertIn('unitree@10.1.10.73',calls[0][1])
        self.assertIn('StrictHostKeyChecking=yes',calls[0][1])
        self.assertIn('HostKeyAlias=192.168.123.164',calls[0][1])
        self.assertIn('ServerAliveInterval=2',calls[0][1])
        self.assertIn('ServerAliveCountMax=3',calls[0][1])
        self.assertEqual(closed,[True])

    def test_disconnected_camera_ssh_is_closed_then_retried_and_delivers_new_frame(self):
        import types
        processes,records,args_seen=[],[],[]
        secret='fake-secret-never-in-errors'
        class EOF(Exception):pass
        class TIMEOUT(Exception):pass
        class Stop:
            value=False
            waits=[]
            def is_set(self):return self.value
            def wait(self,seconds):self.waits.append(seconds);return self.value
        stopped=Stop()
        class Process:
            def __init__(self,index):
                self.index=index;self.closed=False;self.before='';self.sent=[]
            def expect(self,pattern,**kwargs):
                if pattern==EOF or pattern=='[Pp]assword:':return 0
                if self.index==0:raise EOF('private transcript '+secret)
                self.before=json.dumps({'type':'camera_frame','sequence':1,
                    'width':640,'height':360,'jpeg':SIMULATION_JPEG})
                return 0
            def sendline(self,value):self.sent.append(value)
            def sendeof(self):pass
            def close(self,force=False):self.closed=True
        def spawn(command,args,**kwargs):
            self.assertEqual(command,'ssh')
            self.assertNotIn(secret,repr((args,kwargs)))
            if processes:self.assertTrue(processes[-1].closed)
            if len(processes)>=2:self.fail('Unexpected additional SSH retry')
            process=Process(len(processes));processes.append(process);args_seen.append(args)
            return process
        def emit(record):
            records.append(record)
            if record['type']=='camera_frame':stopped.value=True
        fake=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT,spawn=spawn)
        with patch.dict(sys.modules,{'pexpect':fake}),patch.object(camera,'emit',side_effect=emit):
            camera.receive_camera(stopped,secret,ROOT)
        self.assertEqual(len(processes),2)
        self.assertTrue(all(p.closed for p in processes))
        self.assertEqual([p.sent for p in processes],[[secret],[secret]])
        self.assertEqual([r['type'] for r in records],['camera_error','camera_frame'])
        self.assertNotIn(secret,repr(records))
        self.assertIn('reconnecting',records[0]['error'])
        self.assertEqual(stopped.waits[0],2.)
        for args in args_seen:
            self.assertIn('ServerAliveInterval=2',args)
            self.assertIn('ServerAliveCountMax=3',args)
            self.assertIn('StrictHostKeyChecking=yes',args)


class VendoredSource(unittest.TestCase):
    def test_vendored_source_has_not_been_rewritten(self):
        manifest = json.loads((ROOT/'vendor/xr_h2/SOURCE.json').read_text())
        for name, expected in manifest['files'].items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT/'vendor/xr_h2'/name).read_bytes()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
