"""Injected sockets/processes/HTTP only: these tests never access the network."""
import errno
import json
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_pico_setup as setup


def packet(ip=setup.HOST):
    address=ip.encode('ascii')
    return b'\xcf\x7e'+len(address).to_bytes(4,'little')+address+b'\0'*8+b'\xa5'


class FakeSocket:
    def __init__(self):
        self.queue=queue.Queue();self.closed=False;self.bound=None
    def setsockopt(self,*args): pass
    def settimeout(self,value): pass
    def bind(self,address): self.bound=address
    def recvfrom(self,size):
        if self.closed:raise OSError('closed')
        try:return self.queue.get(timeout=.01)
        except queue.Empty:raise socket.timeout()
    def close(self): self.closed=True


class FakeProcess:
    pid=12345
    def __init__(self,stubborn=False):
        self.code=None;self.terminated=self.killed=0;self.stubborn=stubborn
    def poll(self): return self.code
    def terminate(self):
        self.terminated+=1
        if not self.stubborn:self.code=0
    def kill(self):self.killed+=1;self.code=-9
    def wait(self,timeout):
        if self.code is None:raise subprocess.TimeoutExpired('fake',timeout)
        return self.code


class PicoSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=100.;self.sockets=[];self.process=FakeProcess()
        self.verify=mock.Mock(return_value={'running':True,'available':True,'error':None})
        self.free=mock.Mock(return_value=True);self.spawn=mock.Mock(return_value=self.process)
        self.digest=mock.Mock(return_value=setup.APK_SHA256)

    def factory(self,*args):
        self.assertEqual(args,(socket.AF_INET,socket.SOCK_DGRAM))
        value=FakeSocket();self.sockets.append(value);return value

    def manager(self,**overrides):
        args=dict(verify_page=self.verify,free_port=self.free,popen=self.spawn,
            socket_factory=self.factory,monotonic=lambda:self.now,apk_hash=self.digest,host=setup.HOST)
        args.update(overrides)
        value=setup.PicoSetup(self.temp.name,**args);self.addCleanup(value.close)
        return value

    def wait(self,manager,predicate,timeout=2):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            value=manager.status()
            if predicate(value):return value
            time.sleep(.005)
        self.fail(repr(manager.status()))

    def test_constructor_and_status_do_not_probe_spawn_or_write(self):
        manager=self.manager()
        for _ in range(5):self.assertFalse(manager.status()['download']['available'])
        self.verify.assert_not_called();self.free.assert_not_called();self.spawn.assert_not_called()
        self.assertEqual(self.sockets,[])
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_existing_verified_page_reused_and_never_terminated(self):
        manager=self.manager();manager.start()
        state=self.wait(manager,lambda s:s['download']['available'] and s['broadcast']['monitoring'])
        self.assertFalse(state['download']['owned']);self.assertEqual(state['host'],setup.HOST)
        self.spawn.assert_not_called();self.free.assert_not_called();self.digest.assert_not_called()
        calls=self.verify.call_count
        for _ in range(10):manager.status()
        self.assertEqual(self.verify.call_count,calls)
        manager.close();self.assertEqual(self.process.terminated,0)
        self.assertTrue(manager.status()['download']['available'])

    def test_unknown_occupied_port_is_not_replaced(self):
        self.verify.return_value={'running':True,'available':False,'error':'wrong hash'}
        self.free.return_value=False
        manager=self.manager();manager.start()
        state=self.wait(manager,lambda s:not s['download']['verifying'])
        self.assertFalse(state['download']['available']);self.assertIn('wrong hash',state['download']['error'])
        self.spawn.assert_not_called();self.digest.assert_not_called()

    def test_free_port_starts_verified_local_apk_and_closes_only_owned_process(self):
        self.verify.side_effect=[{'running':False,'available':False,'error':'offline'},
                                 {'running':True,'available':True,'error':None}]
        manager=self.manager();manager.start()
        state=self.wait(manager,lambda s:s['download']['available'])
        self.assertTrue(state['download']['owned']);self.assertEqual(state['download']['pid'],12345)
        args=self.spawn.call_args.args[0]
        self.assertIn('serve_pico_apk.py',args[3]);self.assertEqual(args[-3:],[setup.HOST,'--port','8027'])
        self.assertEqual(self.sockets[0].bound,('',29888))
        manager.close();self.assertEqual(self.process.terminated,1);self.assertEqual(self.process.killed,0)

    def test_wrong_local_apk_cannot_start_server(self):
        self.verify.return_value={'running':False,'available':False,'error':'offline'}
        self.digest.return_value='0'*64
        manager=self.manager();manager.start()
        state=self.wait(manager,lambda s:not s['download']['verifying'])
        self.assertIn('APK校验不匹配',state['download']['error']);self.spawn.assert_not_called()

    def test_fixed_address_missing_is_actionable_and_no_network_changes(self):
        self.verify.return_value={'running':False,'available':False,'error':'offline'}
        self.free.side_effect=RuntimeError('固定地址192.168.31.92不可用')
        manager=self.manager();manager.start()
        state=self.wait(manager,lambda s:not s['download']['verifying'])
        self.assertIn('192.168.31.92',state['download']['error']);self.spawn.assert_not_called()

    def test_broadcast_requires_valid_envelope_expected_ip_and_source_then_expires(self):
        manager=self.manager();manager.start()
        self.wait(manager,lambda s:s['broadcast']['monitoring'])
        invalid=[b'192.168.31.92',packet()[:-1],packet()+b'extra',b'xx'+packet()[2:],packet('192.168.31.101')]
        for value in invalid:self.assertFalse(manager._accept_datagram(value,(setup.HOST,1234)))
        self.assertFalse(manager._accept_datagram(packet(),('192.168.31.101',1234)))
        self.assertFalse(manager.status()['broadcast']['observed'])
        self.sockets[0].queue.put((packet(),(setup.HOST,1234)))
        self.wait(manager,lambda s:s['broadcast']['observed'])
        self.now+=16
        state=manager.status()['broadcast']
        self.assertFalse(state['observed']);self.assertEqual(state['age_s'],16.)
        self.assertEqual(state['ip'],setup.HOST)

    def test_stop_monitor_leaves_external_page_and_can_restart_observer(self):
        manager=self.manager();manager.start()
        self.wait(manager,lambda s:s['broadcast']['monitoring'] and s['download']['available'])
        state=manager.stop_monitor()
        self.assertFalse(state['broadcast']['monitoring']);self.assertTrue(state['download']['available'])
        manager.start();self.wait(manager,lambda s:s['broadcast']['monitoring'])
        self.assertEqual(len(self.sockets),2);self.assertTrue(self.sockets[0].closed)
        self.spawn.assert_not_called()

    def test_start_and_status_do_not_wait_for_http_and_close_prevents_delayed_spawn(self):
        entered,release=threading.Event(),threading.Event()
        def blocking():entered.set();release.wait(2);return {'running':False,'available':False,'error':'offline'}
        manager=self.manager(verify_page=blocking)
        before=time.monotonic();manager.start();self.assertLess(time.monotonic()-before,.2)
        self.assertTrue(entered.wait(1));before=time.monotonic();manager.status()
        self.assertLess(time.monotonic()-before,.1)
        closer=threading.Thread(target=manager.close);closer.start()
        self.assertTrue(manager._stop.wait(1));release.set();closer.join(2)
        self.assertFalse(closer.is_alive());self.spawn.assert_not_called()

    def test_simulation_has_no_network_process_file_or_fake_observations(self):
        manager=self.manager(simulation=True);state=manager.start()
        self.assertTrue(state['simulation']);self.assertFalse(state['download']['available'])
        self.assertFalse(state['broadcast']['observed']);manager.close()
        self.verify.assert_not_called();self.spawn.assert_not_called();self.assertEqual(self.sockets,[])

    def test_owned_shutdown_escalates_only_owned_process(self):
        self.process.stubborn=True
        self.verify.side_effect=[{'running':False,'available':False,'error':'offline'},
                                 {'running':True,'available':True,'error':None}]
        manager=self.manager();manager.start();self.wait(manager,lambda s:s['download']['available'])
        manager.close();self.assertEqual((self.process.terminated,self.process.killed),(1,1))

    def test_http_sha_check_is_bounded_and_rejects_wrong_hash(self):
        response=mock.MagicMock();response.__enter__.return_value=response
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch.object(setup.urllib.request,'build_opener',return_value=opener):
            response.read.return_value=(setup.APK_SHA256+'  file.apk\n').encode()
            self.assertTrue(setup.verify_download()['available'])
            opener.open.assert_called_with(setup.URL+'sha256',timeout=1.5)
            response.read.assert_called_with(1025)
            response.read.return_value=b'wrong  file.apk\n'
            self.assertFalse(setup.verify_download()['available'])
            response.read.return_value=(setup.APK_SHA256+' '*1100).encode()
            self.assertFalse(setup.verify_download()['available'])

    def test_configured_laptop_host_drives_status_urls_without_network_actions(self):
        root=Path(self.temp.name);(root/'config').mkdir()
        (root/'config/network.json').write_text(json.dumps({'laptop_ip':'10.1.10.62'}))
        with mock.patch.dict('os.environ',{},clear=True):manager=self.manager(host=None)
        state=manager.status()
        self.assertEqual(state['host'],'10.1.10.62')
        self.assertEqual(state['download']['url'],'http://10.1.10.62:8027/')
        self.assertEqual(state['download']['apk_url'],'http://10.1.10.62:8027/XRoboToolkit-G1-Wrists-TwoObject.apk')
        self.verify.assert_not_called();self.spawn.assert_not_called();self.assertEqual(self.sockets,[])

    def test_migrated_download_process_and_discovery_use_new_host_only(self):
        self.verify.side_effect=[{'running':False,'available':False,'error':'offline'},
                                 {'running':True,'available':True,'error':None}]
        manager=self.manager(host='10.1.10.62');manager.start()
        self.wait(manager,lambda state:state['download']['available'])
        self.assertEqual(self.spawn.call_args.args[0][-3:],['10.1.10.62','--port','8027'])
        self.assertFalse(manager._accept_datagram(packet('192.168.31.92'),('192.168.31.92',1234)))
        self.assertFalse(manager._accept_datagram(packet('10.1.10.62'),('192.168.31.92',1234)))
        self.assertTrue(manager._accept_datagram(packet('10.1.10.62'),('10.1.10.62',1234)))

    def test_migrated_sha_probe_and_unavailable_address_message_use_configured_host(self):
        response=mock.MagicMock();response.__enter__.return_value=response
        response.read.return_value=(setup.APK_SHA256+' file.apk\n').encode()
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch.object(setup.urllib.request,'build_opener',return_value=opener):
            self.assertTrue(setup.verify_download('10.1.10.62')['available'])
        opener.open.assert_called_with('http://10.1.10.62:8027/sha256',timeout=1.5)
        probe=mock.MagicMock();probe.__enter__.return_value=probe
        probe.bind.side_effect=OSError(errno.EADDRNOTAVAIL,'not assigned')
        with mock.patch.object(setup.socket,'socket',return_value=probe):
            with self.assertRaisesRegex(RuntimeError,'10.1.10.62'):setup.port_free('10.1.10.62')
        probe.bind.assert_called_once_with(('10.1.10.62',8027))


if __name__=='__main__':unittest.main()
