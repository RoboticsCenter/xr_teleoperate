"""Fake feed and localhost HTTP contract; no SDK, robot or dashboard actions."""
import copy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from pico_distance_server import DistanceFeed, sanitize_dashboard, shared_frame
from serve_pico_apk import handler_for


def dashboard():
    return {'control_token':'DO-NOT-EXPOSE','history':[{'secret':'HISTORY'}],
        'simulation':False,'settings':{'arms_mode':'both','hands_enabled':{'left':True},
            'tracker_config':{'left_sn':'L','right_sn':'R','offset_tracker_m':[0,0,0],
                'right_offset_tracker_m':[0,0,0],'arm_calibration':{'secret':'CALIBRATION'}}},
        'arm_calibration':{'distance_cm':20,'captured_stages':['origin'],
            'origin_session':'input','saved':{'secret':'SAVED'},'error':None},
        'services':{'tracker':{'frame_path':'fake-shared-frame'}}}


def frame():
    return {'session':'input','seq':1,'head_pose':[0,0,0,0,0,0,1],
        'trackers':{'L':[0,0,0,0,0,0,1],'R':[0,0,0,0,0,0,1]}}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.value=dashboard();self.frame=frame()
        self.fetch=Mock(side_effect=lambda:copy.deepcopy(self.value))
        self.feed=DistanceFeed(fetch=self.fetch,reader=lambda _:copy.deepcopy(self.frame),clock=lambda:self.now)
        self.addCleanup(self.feed.close)

    def test_constructor_and_status_do_not_connect_or_expose_control_state(self):
        result=self.feed.status();self.fetch.assert_not_called()
        self.assertFalse(result['ready'])
        self.feed.refresh();result=self.feed.status()
        self.assertTrue(result['zero_ready']);self.assertFalse(result['has_origin'])
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(result))
        clean=json.dumps(sanitize_dashboard(self.value))
        for secret in ('DO-NOT-EXPOSE','HISTORY','CALIBRATION','SAVED','hands_enabled'):
            self.assertNotIn(secret,clean)

    def test_measurement_zero_never_modifies_actual_calibration_or_frame(self):
        original=copy.deepcopy(self.value);self.feed.refresh();self.feed.zero()
        self.frame['trackers']['L'][2]=-.2
        result=self.feed.status()
        self.assertAlmostEqual(result['sides']['left']['displacement_cm'],20)
        self.assertAlmostEqual(result['sides']['left']['forward_cm'],20)
        self.assertEqual(self.value,original)

    def test_headset_reference_is_forwarded_without_exposing_control_data(self):
        self.value['settings']['arm_reference_frame']='headset'
        clean=sanitize_dashboard(self.value)
        self.assertEqual(clean['settings']['arm_reference_frame'],'headset')
        self.feed.refresh();self.feed.zero()
        for pose in [self.frame['head_pose'],*self.frame['trackers'].values()]:pose[0]+=3.
        result=self.feed.status()
        self.assertEqual(result['reference_frame'],'headset')
        self.assertAlmostEqual(result['sides']['left']['displacement_cm'],0.)
        self.value['settings']['arm_reference_frame']='world'
        self.feed.refresh()
        self.assertTrue(self.feed.status()['needs_zero'])

    def test_controller_distance_uses_selected_source_and_resets_on_switch(self):
        self.value['settings']['arm_input_source']='controllers'
        self.frame.update(controllers={side:[0,0,0,0,0,0,1] for side in ('left','right')},
            controller_sample_mono=time.monotonic())
        self.feed.refresh();self.feed.zero()
        self.frame['trackers']['L'][2]=-9
        self.frame['controllers']['left'][2]=-.2
        result=self.feed.status()
        self.assertEqual(result['input_source'],'controllers')
        self.assertAlmostEqual(result['sides']['left']['displacement_cm'],20)
        self.assertEqual(result['sides']['left']['sn'],'pico-controller-left')
        self.value['settings']['arm_input_source']='trackers';self.feed.refresh()
        self.assertTrue(self.feed.status()['needs_zero'])

    def test_controller_mode_does_not_use_fresh_wrist_pose_as_fallback(self):
        self.value['settings']['arm_input_source']='controllers'
        self.feed.refresh()
        self.assertFalse(self.feed.status()['zero_ready'])

    def test_stale_dashboard_or_missing_frame_never_shows_old_distance(self):
        self.feed.refresh();self.feed.zero();self.now+=2.001
        result=self.feed.status()
        self.assertFalse(result['ready']);self.assertIsNone(result['sides']['left']['displacement_cm'])
        self.feed.refresh();self.frame=None
        self.assertFalse(self.feed.status()['zero_ready'])
        with self.assertRaises(ValueError):self.feed.zero()

    def test_deferred_zero_waits_for_next_valid_sample_then_fires_once(self):
        self.feed.refresh();result=self.feed.arm_zero()
        self.assertTrue(result['zero_pending']);self.assertFalse(result['has_origin'])
        self.frame['head_pose']=[0]*7;self.frame['seq']=2
        self.assertTrue(self.feed.status()['zero_pending'])
        self.frame['head_pose']=[0,0,0,0,0,0,1];self.frame['seq']=3
        self.assertFalse(self.feed.status()['zero_pending'])
        self.frame['trackers']['R'][2]=-.1
        self.assertAlmostEqual(self.feed.status()['sides']['right']['displacement_cm'],10)

    def test_deferred_zero_can_be_cancelled_or_invalidated_by_selection(self):
        self.feed.refresh();self.frame=None;self.feed.arm_zero();self.feed.cancel_zero()
        self.frame=frame();self.assertFalse(self.feed.status()['has_origin'])
        self.feed.arm_zero();self.value['settings']['arms_mode']='left';self.feed.refresh()
        self.assertFalse(self.feed.status()['zero_pending'])
        self.assertFalse(self.feed.status()['has_origin'])

    def test_simulated_dashboard_cannot_be_served_as_real_distance(self):
        self.value['simulation']=True;self.feed.refresh()
        self.assertFalse(self.feed.status()['zero_ready'])

    def test_failed_video_child_is_not_reported_as_enabled(self):
        process=Mock();process.poll.return_value=1
        self.feed.set_video(process=process,enabled=True)
        self.assertFalse(self.feed.status()['remote_vision']['enabled'])

    def test_jpeg_cache_drops_values_immediately_when_frame_becomes_unavailable(self):
        self.feed.refresh();self.feed.zero()
        with patch('pico_distance_image.render_distance_jpeg',side_effect=[b'live',b'waiting']) as render:
            self.assertEqual(self.feed.jpeg(),b'live')
            self.frame=None;self.now+=.01
            self.assertEqual(self.feed.jpeg(),b'waiting')
            self.assertFalse(render.call_args.args[0]['ready'])

    def test_only_private_shared_frame_path_is_read(self):
        with patch('pico_distance_server.read_frame',return_value={'test':True}) as read:
            self.assertIsNone(shared_frame('/etc/passwd'))
            read.assert_not_called()
            valid=Path('/dev/shm')/f'g1-dashboard-tracker-{os.getuid()}'/'stream-test'/'latest.json'
            with patch.object(Path,'resolve',return_value=valid):
                self.assertEqual(shared_frame(str(valid)),{'test':True})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.apk=Path(self.temp.name)/'test.apk';self.apk.write_bytes(b'fake-apk')
        self.feed=DistanceFeed(fetch=dashboard,reader=lambda _:frame())
        self.feed.refresh();self.addCleanup(self.feed.close)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.apk,self.feed,token='measure-only'))
        self.port=self.server.server_address[1]
        self.server.RequestHandlerClass=handler_for(self.apk,self.feed,host='127.0.0.1',port=self.port,token='measure-only')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.finish)
        self.url=f'http://127.0.0.1:{self.port}'

    def finish(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=1)

    def post(self,path,payload=b'{}',**headers):
        return urlopen(Request(self.url+path,data=payload,headers={
            'Content-Type':'application/json','X-Control-Token':'measure-only',**headers}),timeout=2)

    def test_public_routes_only_expose_measurement_and_existing_apk(self):
        for path in ('/','/distance','/distance.js','/distance.css','/sha256','/XRoboToolkit-G1-Wrists-TwoObject.apk','/distance.jpg'):
            with urlopen(self.url+path,timeout=3) as response:self.assertEqual(response.status,200)
        with urlopen(self.url+'/api/distance') as response:state=json.load(response)
        self.assertEqual(state['token'],'measure-only');self.assertNotIn('DO-NOT-EXPOSE',json.dumps(state))
        for path in ('/api/status','/api/launch','/api/arms/calibration/save'):
            with self.assertRaises(HTTPError) as error:self.post(path)
            self.assertEqual(error.exception.code,404)

    def test_measurement_mutations_require_correct_token_and_origin(self):
        for headers in ({'X-Control-Token':''},{'Origin':'http://evil.example'},{'Host':'evil.example'}):
            with self.assertRaises(HTTPError) as error:self.post('/api/distance/zero',**headers)
            self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:self.post('/api/distance/zero',b'{"command":"launch"}')
        self.assertEqual(error.exception.code,409)
        self.assertFalse(self.feed.status()['has_origin'])
        with self.post('/api/distance/zero') as response:self.assertTrue(json.load(response)['result']['has_origin'])
        with self.post('/api/distance/arm-zero') as response:self.assertTrue(json.load(response)['result']['zero_pending'])
        with self.post('/api/distance/cancel-zero') as response:self.assertFalse(json.load(response)['result']['zero_pending'])

    def test_configured_lan_host_drives_page_metadata_and_origin_guard(self):
        host='10.1.10.62'
        self.server.RequestHandlerClass=handler_for(self.apk,self.feed,host=host,port=self.port,token='measure-only')
        with urlopen(self.url+'/') as response:page=response.read().decode()
        self.assertIn(host,page);self.assertNotIn('192.168.31.92',page)
        with urlopen(self.url+'/api/distance') as response:state=json.load(response)
        self.assertEqual(state['laptop_ip'],host)
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(state))
        with self.post('/api/distance/zero',Origin=f'http://{host}:{self.port}') as response:
            self.assertTrue(json.load(response)['ok'])
        with self.assertRaises(HTTPError) as error:
            self.post('/api/distance/zero',Origin=f'http://192.168.31.92:{self.port}')
        self.assertEqual(error.exception.code,403)


if __name__=='__main__':unittest.main()
