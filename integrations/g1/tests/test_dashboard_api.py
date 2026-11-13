"""API integration with inert managers: no SDK readers, SSH, or robot commands."""
from contextlib import ExitStack
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import teleop_dashboard as api


class Session:
    def __init__(self,*args,**kwargs):
        self.value={'running':False,'stage':'idle','session_id':None,'start_requested':False,'stop_requested':False}
        self.launches=[];self.starts=0;self.stops=0
    def status(self):return dict(self.value)
    def launch(self,options=None):
        self.launches.append(copy.deepcopy(options))
        self.value.update(running=True,stage='ready',session_id=str(len(self.launches)),start_requested=False,stop_requested=False)
        return self.status()
    def start(self):
        self.starts+=1;self.value['start_requested']=True
        return self.status()
    def stop(self):
        self.stops+=1;self.value.update(running=False,stage='stopped',stop_requested=True)
        return self.status()
    close=stop


class Camera:
    def __init__(self,*args,**kwargs):self.running=False
    def status(self):return {'running':self.running,'live':False}
    def start(self,**kwargs):self.running=True;return self.status()
    def stop(self):self.running=False;return self.status()
    def close(self):self.stop()
    def jpeg(self):return None


class Tracker(Camera):
    def __init__(self,*args,**kwargs):
        super().__init__();self.starts=0;self.stops=0
        self.frame={'session':'tracker-session','trackers':{s:[0.,0.,0.,0.,0.,0.,1.] for s in ('L','R','R2')},
                    'head_pose':[0.,0.,0.,0.,0.,0.,1.],'pose_age_ms':{s:0. for s in ('L','R','R2')}}
    def status(self):
        return {'running':self.running,'connected':True,'serials':list(self.frame['trackers']),
                'head_ready':True,'frame_path':'/dev/shm/fake-trackers/latest.json'}
    def snapshot(self):return copy.deepcopy(self.frame)
    def start(self):self.starts+=1;self.running=True;return self.status()
    def stop(self):self.stops+=1;self.running=False;return self.status()


class Setup:
    def __init__(self,*args,**kwargs):self.starts=0;self.monitor_stops=0;self.download=False
    def status(self):return {'host':'192.168.31.92','download':{'running':self.download,'available':self.download},'broadcast':{'observed':False}}
    def start(self):self.starts+=1;self.download=True;return self.status()
    def stop_monitor(self):self.monitor_stops+=1;return self.status()
    def close(self):pass


class Robot:
    def __init__(self,*args,**kwargs):self.connects=0;self.disconnects=0;self.connected=False
    def status(self):return {'host':'192.168.31.65','connected':self.connected,'connecting':False,'stage':'connected' if self.connected else 'idle','addresses':[],'detail':'test SSH monitor'}
    def connect(self):self.connects+=1;self.connected=True;return self.status()
    def disconnect(self):self.disconnects+=1;self.connected=False;return self.status()
    def close(self):pass


class Hands:
    def __init__(self,*args,**kwargs):
        self.values={s:{'running':False,'input_running':True,'input_live':True,'model_ready':True,
                        'calibration_ready':True,'command_ready':True} for s in ('left','right')}
        self.started=[];self.stopped=[];self.captures=[];self.input_starts=0;self.input_stops=0;self.unready=set()
        self.glove_requests=[];self.model_requests=[];self.capture_cancellations=[]
        self.model=self
    def status(self):return {**copy.deepcopy(self.values),'running':any(v['running'] for v in self.values.values()),'command_ready':True}
    def input_start(self):
        self.input_starts+=1
        for value in self.values.values():value['input_running']=True
        return self.status()
    def input_stop(self):
        self.input_stops+=1;self.stop('both')
        for value in self.values.values():value['input_running']=False
        return self.status()
    def start(self,side):
        self.started.append(side)
        for name in (('left','right') if side=='both' else (side,)):
            if name not in self.unready:self.values[name]['running']=True
        return self.status()
    def stop(self,side='both'):
        self.stopped.append(side)
        for name in (('left','right') if side=='both' else (side,)):self.values[name]['running']=False
        return self.status()
    def calibrate(self,side,pose):self.captures.append((side,pose));return {'side':side,'pose':pose}
    def glove_input(self,side,start=True):
        if side not in ('left','right'):raise ValueError('Choose one glove')
        self.glove_requests.append((side,start));self.values[side]['input_running']=start
        return self.status()
    def refresh_gloves(self):return self.status()
    def model_start(self,side):
        self.model_requests.append(('start',side));return {'active':True,'side':side}
    def continue_calibration(self,side,prompt_id=None):
        self.model_requests.append(('continue',side,prompt_id));return {'active':True}
    def cancel(self,side):self.model_requests.append(('cancel',side));return {'active':False}
    def cancel_calibration(self,side):self.capture_cancellations.append(side);return self.status()
    def close(self):self.input_stop()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        directory=self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root=Path(directory);(self.root/'config').mkdir()
        (self.root/'config/pico_tracker.json').write_text(json.dumps({'left_sn':'','right_sn':'R',
            'offset_tracker_m':[0,0,0],'right_offset_tracker_m':[0,0,0]}))
        for name,kind in [('SessionManager',Session),('CameraManager',Camera),('TrackerManager',Tracker),('HandCoordinator',Hands),('PicoService',Camera),('PicoSetup',Setup),('RobotConnection',Robot)]:
            self.stack.enter_context(patch.object(api,name,kind))
        self.app=api.Dashboard(root=self.root)
        self.addCleanup(self.app.close)
        # Exercise the actual watcher step deterministically, without timing races.
        self.app.done.set();self.app.watcher.join(timeout=1);self.app.done.clear()

    def launch_start(self):
        self.app.action('/api/launch',{})
        self.app.action('/api/start',{})

    def wait_prepared(self):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with self.app.lock:
                if not self.app.auto_input_preparing:return
            time.sleep(.001)
        self.fail('fake glove preparation did not complete')

    def test_constructor_is_inert_and_status_exposes_dynamic_capabilities(self):
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.tracker.starts,0)
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.pico_setup.starts,0)
        self.assertEqual(self.app.robot.connects,0)
        status=self.app.status()
        self.assertTrue(status['capabilities']['hands'])
        self.assertTrue(status['capabilities']['left_arm'])
        self.assertEqual(status['capabilities']['control_mode'],'normal_walking')
        self.assertEqual(status['capabilities']['control_topic'],'rt/arm_sdk')
        self.assertEqual(status['settings']['arms_mode'],'right')
        self.assertTrue(status['services']['tracker']['ready'])
        self.app.tracker.frame['pose_age_ms']['R']=1000.
        self.assertTrue(self.app.status()['services']['tracker']['ready'])
        self.app.tracker.frame['trackers']['R']=[0.,0.,0.,0.,0.,0.,0.]
        self.assertFalse(self.app.status()['services']['tracker']['ready'])

    def test_settings_require_selected_distinct_detected_serials(self):
        for payload in ({'arms_mode':'both','left_sn':''},{'arms_mode':'both','left_sn':'R'},
                        {'arms_mode':'left','left_sn':'unknown'},{'hands_enabled':{'left':'yes'}}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.app.action('/api/settings',payload)
        self.assertEqual(self.app.settings['arms_mode'],'right')
        result=self.app.action('/api/settings',{'arms_mode':'both','left_sn':'L','hands_enabled':{'left':True}})
        self.assertEqual(result['tracker_config']['right_sn'],'R')
        self.assertEqual(json.loads((self.root/'config/dashboard.json').read_text())['arms_mode'],'both')
        self.assertEqual(json.loads((self.root/'config/pico_tracker.json').read_text())['left_sn'],'L')

    def test_settings_forbidden_when_arm_or_hand_is_running(self):
        self.app.session.value['running']=True
        with self.assertRaises(RuntimeError):self.app.action('/api/settings',{'arms_mode':'right'})
        self.app.session.value['running']=False;self.app.hands.values['left']['running']=True
        with self.assertRaises(RuntimeError):self.app.action('/api/settings',{'arm_input_source':'controllers'})

    def controller_frame(self):
        self.app.tracker.frame.update(controllers={
            'left':[-.2,1.,-.3,0.,0.,0.,1.],
            'right':[.2,1.,-.3,0.,0.,0.,1.]},controller_sample_mono=time.monotonic())
        return self.app.tracker.frame

    def test_controller_selection_needs_no_wrist_serial_and_retains_bindings(self):
        original=copy.deepcopy(self.app.settings['tracker_config'])
        self.app.action('/api/settings',{'arm_input_source':'controllers','arms_mode':'both'})
        self.assertEqual(self.app.settings['tracker_config'],original)
        self.assertEqual(json.loads((self.root/'config/dashboard.json').read_text())['arm_input_source'],'controllers')
        self.controller_frame()
        self.app.tracker.frame['trackers']={}
        status=self.app.status()
        self.assertTrue(status['services']['arm_input']['ready'])
        self.assertEqual(status['services']['arm_input']['input_source'],'controllers')
        self.assertEqual(status['services']['tracker']['selected_serials'],
            {'left':'pico-controller-left','right':'pico-controller-right'})
        self.assertEqual(status['arm_calibration']['calibration_file'],'arm-calibration-controllers.json')
        with self.assertRaises(ValueError):self.app.action('/api/settings',{'arm_input_source':'joystick'})

    def test_controller_launch_without_live_input_does_not_gain_a_strict_gate(self):
        self.app.action('/api/settings',{'arm_input_source':'controllers','arms_mode':'both'})
        self.app.tracker.frame['trackers']={}
        self.app.action('/api/launch',{})
        options=self.app.session.launches[-1]
        self.assertEqual(options['arm_input_source'],'controllers')
        self.assertEqual(options['tracker_config']['left_sn'],'pico-controller-left')
        self.assertNotIn('arm_calibration',options['tracker_config'])

    def test_headset_selection_persists_and_launch_keeps_missing_input_tolerance(self):
        self.assertEqual(self.app.settings['arm_reference_frame'],'world')
        self.app.action('/api/settings',{'arm_input_source':'controllers','arm_reference_frame':'headset','arms_mode':'both'})
        saved=json.loads((self.root/'config/dashboard.json').read_text())
        self.assertEqual(saved['arm_reference_frame'],'headset')
        self.assertEqual(self.app.status()['capabilities']['arm_reference_frames'],['world','headset'])
        status=self.app.status()['arm_calibration']
        self.assertEqual(status['calibration_file'],'arm-calibration-controllers-headset.json')
        self.assertIsNone(status['saved'])
        self.app.tracker.frame={'trackers':{},'head_pose':None}
        self.app.action('/api/launch',{})
        options=self.app.session.launches[-1]
        self.assertEqual(options['arm_reference_frame'],'headset')
        self.assertEqual(options['tracker_config']['arm_reference_frame'],'headset')
        self.assertNotIn('arm_calibration',options['tracker_config'])
        with self.assertRaises(RuntimeError):self.app.action('/api/settings',{'arm_reference_frame':'world'})
        self.app.action('/api/stop',{})
        with self.assertRaises(ValueError):self.app.action('/api/settings',{'arm_reference_frame':'room'})

    def test_reference_switch_preserves_world_calibration_and_unsaved_draft(self):
        self.capture_arm_axes();self.app.action('/api/arms/calibration/save',{})
        path=self.root/'config/arm-calibration.json';original=path.read_bytes()
        self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        self.app.action('/api/settings',{'arm_reference_frame':'headset'})
        self.assertEqual(self.app.status()['arm_calibration']['captured_stages'],[])
        self.assertIsNone(self.app.arm_calibration.saved)
        self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        self.app.tracker.frame['head_pose']=None
        status=self.app.status()
        self.assertFalse(status['arm_calibration']['can_capture'])
        self.assertFalse(status['services']['arm_input']['calibration_ready'])
        self.app.action('/api/settings',{'arm_reference_frame':'world'})
        self.assertEqual(self.app.status()['arm_calibration']['captured_stages'],['origin'])
        self.assertTrue(self.app.status()['arm_calibration']['can_capture'])
        self.assertEqual(path.read_bytes(),original)

    def test_headset_missing_packet_pair_is_not_reported_ready(self):
        self.app.action('/api/settings',{'arm_input_source':'controllers','arm_reference_frame':'headset','arms_mode':'both'})
        self.controller_frame()
        self.app.tracker.frame['controller_head_poses']={}
        self.assertFalse(self.app.status()['services']['arm_input']['ready'])
        self.app.tracker.frame['controller_head_poses']={s:list(self.app.tracker.frame['head_pose']) for s in ('left','right')}
        self.assertTrue(self.app.status()['services']['arm_input']['ready'])

    def test_source_cannot_switch_during_an_active_arm_session(self):
        self.app.session.value['running']=True
        with self.assertRaises(RuntimeError):
            self.app.action('/api/settings',{'arm_input_source':'controllers'})

    def test_controller_and_tracker_calibration_are_isolated_and_switch_back_restores(self):
        self.capture_arm_axes()
        self.app.action('/api/arms/calibration/save',{})
        tracker_path=self.root/'config/arm-calibration.json'
        original=tracker_path.read_bytes()
        original_id=self.app.arm_calibration.saved['calibration_id']
        self.app.action('/api/settings',{'arm_input_source':'controllers'})
        self.assertIsNone(self.app.arm_calibration.saved)
        self.assertEqual(self.app.status()['arm_calibration']['captured_stages'],[])
        self.controller_frame()
        initial=copy.deepcopy(self.app.tracker.frame)
        self.app.action('/api/arms/calibration/capture',{'stage':'origin','distance_cm':20})
        for stage,delta in (('forward',[0.,0.,-.2]),('left',[-.2,0.,0.]),('up',[0.,.2,0.])):
            self.app.tracker.frame=copy.deepcopy(initial)
            self.app.tracker.frame['controller_sample_mono']=time.monotonic()
            for side in ('left','right'):
                p=self.app.tracker.frame['controllers'][side]
                p[:3]=[p[i]+delta[i] for i in range(3)]
            self.app.action('/api/arms/calibration/capture',{'stage':stage,'distance_cm':20})
        self.app.action('/api/arms/calibration/save',{})
        controller_path=self.root/'config/arm-calibration-controllers.json'
        self.assertTrue(controller_path.exists())
        self.assertEqual(tracker_path.read_bytes(),original)
        self.app.action('/api/launch',{})
        options=self.app.session.launches[-1]
        self.assertEqual(options['tracker_config']['arm_calibration']['sides']['left']['serial'],'pico-controller-left')
        self.app.action('/api/stop',{})
        self.app.action('/api/settings',{'arm_input_source':'trackers'})
        self.assertEqual(self.app.arm_calibration.saved['calibration_id'],original_id)
        self.assertEqual(tracker_path.read_bytes(),original)

    def test_simulation_fake_assignments_do_not_overwrite_real_settings(self):
        self.app.simulation=True
        self.app.action('/api/settings',{'arms_mode':'both','left_sn':'SIM-LEFT'})
        self.assertEqual(self.app.settings['tracker_config']['left_sn'],'SIM-LEFT')
        self.assertEqual(json.loads((self.root/'config/pico_tracker.json').read_text())['left_sn'],'')
        self.assertFalse((self.root/'config/dashboard.json').exists())

    def test_launch_passes_shared_input_and_selected_arm_options(self):
        self.app.action('/api/settings',{'arms_mode':'left','left_sn':'L'})
        self.app.action('/api/launch',{})
        self.assertEqual(self.app.tracker.starts,1)
        options=self.app.session.launches[0]
        self.assertEqual(options['arms_mode'],'left')
        self.assertEqual(options['tracker_config']['left_sn'],'L')
        self.assertEqual(options['tracker_frame_path'],'/dev/shm/fake-trackers/latest.json')
        self.assertNotIn('hands_enabled',options)

    def capture_arm_axes(self):
        self.app.action('/api/settings',{'arms_mode':'both','left_sn':'L'})
        initial=copy.deepcopy(self.app.tracker.frame)
        self.app.action('/api/arms/calibration/capture',{'stage':'origin','distance_cm':20})
        for stage,delta in (('forward',[0.,0.,-.2]),('left',[-.2,0.,0.]),('up',[0.,.2,0.])):
            self.app.tracker.frame=copy.deepcopy(initial)
            for sn in ('L','R'):
                self.app.tracker.frame['trackers'][sn][:3]=delta
            self.app.action('/api/arms/calibration/capture',{'stage':stage,'distance_cm':20})

    def test_arm_calibration_is_readonly_until_explicit_save_and_launch_uses_it(self):
        self.capture_arm_axes()
        value=self.app.status()['arm_calibration']
        self.assertTrue(value['can_save'])
        self.assertFalse((self.root/'config/arm-calibration.json').exists())
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.tracker.starts,0)
        self.app.action('/api/arms/calibration/save',{})
        saved=json.loads((self.root/'config/arm-calibration.json').read_text())
        self.assertEqual(set(saved['sides']),{'left','right'})
        self.app.action('/api/arms/calibration/reset',{})
        self.assertEqual(json.loads((self.root/'config/arm-calibration.json').read_text()),saved)
        self.app.action('/api/launch',{})
        self.assertEqual(self.app.session.launches[0]['tracker_config']['arm_calibration'],saved)

    def test_operator_distance_can_save_despite_small_unequal_skewed_pico_readings(self):
        self.app.action('/api/settings',{'arms_mode':'both','left_sn':'L'})
        initial=copy.deepcopy(self.app.tracker.frame)
        self.app.action('/api/arms/calibration/capture',{'stage':'origin','distance_cm':20})
        for stage,delta in (('forward',[0.,0.,-.01]),('left',[-.1,0.,-.02]),('up',[.03,.4,0.])):
            self.app.tracker.frame=copy.deepcopy(initial)
            for sn in ('L','R'):
                self.app.tracker.frame['trackers'][sn][:3]=delta
            self.app.action('/api/arms/calibration/capture',{'stage':stage,'distance_cm':20})
        state=self.app.status()['arm_calibration']
        self.assertTrue(state['can_save'])
        self.assertIsNone(state['error'])
        self.app.action('/api/arms/calibration/save',{})
        self.assertTrue((self.root/'config/arm-calibration.json').exists())
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.tracker.starts,0)

    def test_arm_capture_rejects_missing_input_and_running_outputs(self):
        with patch.object(self.app.tracker,'snapshot',return_value=None), self.assertRaises(ValueError):
            self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        self.assertEqual(self.app.session.launches,[])
        for busy in ('arms','hands','model'):
            if busy=='arms':self.app.session.value['running']=True
            elif busy=='hands':self.app.hands.values['left']['running']=True
            else:self.app.hands.values['right']['calibration_busy']=True
            with self.subTest(busy=busy),self.assertRaises(RuntimeError):
                self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
            self.app.session.value['running']=False
            self.app.hands.values['left']['running']=False
            self.app.hands.values['right']['calibration_busy']=False

    def test_arm_calibration_draft_binding_change_cannot_save(self):
        self.capture_arm_axes()
        self.app.action('/api/settings',{'right_sn':'R2'})
        self.assertFalse(self.app.status()['arm_calibration']['can_save'])
        with self.assertRaises(ValueError):self.app.action('/api/arms/calibration/save',{})
        self.assertFalse((self.root/'config/arm-calibration.json').exists())

    def test_launch_does_not_require_live_tracker_or_head(self):
        with patch.object(self.app.tracker,'snapshot',return_value=None):
            self.app.action('/api/launch',{})
        self.assertEqual(len(self.app.session.launches),1)

    def test_auto_hands_wait_for_actual_start_and_attempt_unready_only_once(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.app.hands.unready.add('left')
        self.launch_start();self.app._watch_once()
        self.assertEqual(self.app.hands.started,[])
        self.app.session.value['stage']='following'
        self.app._watch_once();self.app._watch_once()
        self.assertEqual(self.app.hands.started,['left','right'])
        self.assertFalse(self.app.hands.values['left']['running'])
        self.assertTrue(self.app.session.value['running'])
        self.assertEqual(self.app.auto_owned,{'right'})

    def test_cold_start_prepares_only_enabled_glove_and_waits_for_fresh_input(self):
        self.app.action('/api/settings',{'hands_enabled':{'right':True}})
        self.app.hands.values['right'].update(input_running=False,input_live=False,command_ready=False)
        self.launch_start();self.app._watch_once();self.wait_prepared()
        self.assertEqual(self.app.hands.glove_requests,[('right',True)])
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.hands.started,[])
        self.app.session.value['stage']='following'
        for _ in range(3):self.app._watch_once()
        self.assertEqual(self.app.hands.started,[])
        state=self.app.status()['services']['hands']['right']
        self.assertTrue(state['auto_start_pending'])
        self.assertEqual(state['auto_start_stage'],'waiting_input')
        self.app.hands.values['right'].update(input_live=True,command_ready=True)
        self.app._watch_once();self.app._watch_once()
        self.assertEqual(self.app.hands.started,['right'])
        self.assertIsNone(self.app.pending_hands)
        self.assertEqual(self.app.auto_owned,{'right'})

    def test_one_waiting_hand_does_not_block_ready_side_or_arms(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.app.hands.values['left'].update(input_live=False,command_ready=False)
        self.launch_start();self.app.session.value['stage']='holding';self.app._watch_once()
        self.assertEqual(self.app.hands.started,['right'])
        self.assertTrue(self.app.status()['services']['hands']['left']['auto_start_pending'])
        self.assertTrue(self.app.session.value['running'])
        self.app.hands.values['left'].update(input_live=True,command_ready=True)
        self.app._watch_once();self.app._watch_once()
        self.assertEqual(self.app.hands.started,['right','left'])

    def test_exited_reader_reports_error_and_is_not_restarted_by_polling(self):
        self.app.action('/api/settings',{'hands_enabled':{'right':True}})
        self.app.hands.values['right'].update(input_running=False,input_live=False,command_ready=False)
        self.launch_start();self.app._watch_once();self.wait_prepared()
        self.app.session.value['stage']='following'
        self.app.hands.values['right'].update(input_running=False,reader_error='SDK connection timeout')
        for _ in range(4):self.app._watch_once()
        state=self.app.status()['services']['hands']['right']
        self.assertFalse(state['auto_start_pending'])
        self.assertEqual(state['auto_start_error'],'SDK connection timeout')
        self.assertEqual(self.app.hands.glove_requests,[('right',True)])
        self.assertEqual(self.app.hands.started,[])

    def test_output_failure_is_not_retried_while_other_side_waits(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.app.hands.values['left'].update(input_live=False,command_ready=False)
        self.launch_start();self.app.session.value['stage']='following';self.app._watch_once()
        self.app.hands.values['right'].update(running=False,error='bridge EOF',error_kind='motion')
        for _ in range(3):self.app._watch_once()
        self.assertEqual(self.app.hands.started,['right'])

    def test_stop_revokes_pending_hand_during_slow_sdk_preparation(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':False,'right':True}})
        self.app.hands.values['right'].update(input_running=False,input_live=False,command_ready=False)
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def slow_input(side,start=True):
            entered.set()
            if not release.wait(2):raise RuntimeError('test preparation timeout')
            self.app.hands.values[side].update(input_running=True,input_live=True,command_ready=True)
            finished.set()
            return self.app.hands.status()
        with patch.object(self.app.hands,'glove_input',side_effect=slow_input):
            try:
                self.launch_start();self.app.session.value['stage']='following';self.app._watch_once()
                self.assertTrue(entered.wait(1))
                stopped=threading.Event()
                thread=threading.Thread(target=lambda:(self.app.action('/api/stop',{}),stopped.set()))
                thread.start()
                self.assertTrue(stopped.wait(.5),'STOP waited on SDK preparation')
                release.set();self.assertTrue(finished.wait(1));thread.join(1)
                self.app._watch_once()
                self.assertIsNone(self.app.pending_hands)
                self.assertEqual(self.app.hands.started,[])
            finally:release.set()

    def test_session_change_cancels_old_pending_hand_preparation(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':False,'right':True}})
        self.app.hands.values['right'].update(input_running=False,input_live=False,command_ready=False)
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def slow_input(side,start=True):
            entered.set()
            if not release.wait(2):raise RuntimeError('test preparation timeout')
            self.app.hands.values[side].update(input_running=True,input_live=True,command_ready=True)
            finished.set()
            return self.app.hands.status()
        with patch.object(self.app.hands,'glove_input',side_effect=slow_input):
            try:
                self.launch_start();self.app._watch_once();self.assertTrue(entered.wait(1))
                self.app.session.value.update(session_id='replacement-session',stage='following')
                self.app._watch_once()
                release.set();self.assertTrue(finished.wait(1));self.app._watch_once()
                self.assertEqual(self.app.hands.started,[])
                self.assertIsNone(self.app.pending_hands)
            finally:release.set()

    def test_rejected_start_can_be_retried_without_auto_hand_start(self):
        self.app.action('/api/settings',{'hands_enabled':{'right':True}})
        self.launch_start();self.app.session.value['start_requested']=False
        self.app._watch_once();self.assertIsNone(self.app.pending_hands)
        self.app.action('/api/start',{});self.app.session.value['stage']='holding'
        self.app._watch_once();self.assertEqual(self.app.hands.started,['right'])

    def test_explicit_hand_stop_cancels_pending_auto_start_for_that_side(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.launch_start()
        self.app.action('/api/hands/stop',{'side':'right'})
        self.app.session.value['stage']='following'
        self.app._watch_once()
        self.assertEqual(self.app.hands.started,['left'])

    def test_arm_exit_stops_only_auto_owned_hands(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.app.action('/api/hands/start',{'side':'right'})
        self.launch_start();self.app.session.value['stage']='following';self.app._watch_once()
        self.assertEqual(self.app.auto_owned,{'left'})
        self.app.session.value['running']=False;self.app._watch_once()
        self.assertFalse(self.app.hands.values['left']['running'])
        self.assertTrue(self.app.hands.values['right']['running'])

    def test_global_stop_stops_arms_even_if_hand_cleanup_raises(self):
        self.launch_start()
        with patch.object(self.app.hands,'stop',side_effect=RuntimeError('hand failure')):
            with self.assertRaises(RuntimeError):self.app.action('/api/stop',{})
        self.assertFalse(self.app.session.value['running'])
        self.assertIsNone(self.app.pending_hands)

    def test_readonly_inputs_and_independent_calibration_routes(self):
        self.app.action('/api/inputs/start',{})
        self.assertEqual(self.app.hands.input_starts,1)
        self.assertEqual(self.app.session.launches,[])
        self.app.action('/api/hands/calibrate',{'side':'left','pose':'closed'})
        self.assertEqual(self.app.hands.captures,[('left','closed')])
        self.app.action('/api/launch',{})
        with self.assertRaises(RuntimeError):self.app.action('/api/inputs/stop',{})
        self.assertEqual(self.app.hands.input_stops,0)

    def test_http_token_protects_new_settings_and_control_routes(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),api.handler_for(self.app,0))
        port=server.server_address[1];server.RequestHandlerClass=api.handler_for(self.app,port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{port}'
            with urlopen(url+'/api/status') as response:status=json.load(response)
            payload=json.dumps({'arms_mode':'left','left_sn':'L'}).encode()
            headers={'Content-Type':'application/json'}
            with self.assertRaises(HTTPError) as denied:urlopen(Request(url+'/api/settings',data=payload,headers=headers))
            self.assertEqual(denied.exception.code,403)
            for route in ('/api/gloves/model/start','/api/gloves/model/cancel',
                          '/api/gloves/model/continue','/api/gloves/input/start',
                          '/api/hands/calibrate/cancel','/api/arms/calibration/capture',
                          '/api/arms/calibration/save','/api/arms/calibration/reset',
                          '/api/pico/start','/api/pico/stop','/api/robot/connect',
                          '/api/robot/disconnect','/api/arms/input/check'):
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(url+route,data=b'{"side":"left"}',headers=headers))
                self.assertEqual(denied.exception.code,403)
            self.assertEqual(self.app.hands.model_requests,[])
            headers['X-Control-Token']=status['control_token']
            with urlopen(Request(url+'/api/settings',data=payload,headers=headers)) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertEqual(self.app.settings['arms_mode'],'left')
            self.assertEqual(self.app.session.launches,[])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=1)


    def test_glove_workflow_does_not_launch_robot_or_start_other_input_services(self):
        for suffix in ('start','stop'):
            self.app.action('/api/gloves/input/'+suffix,{'side':'left'})
        self.app.action('/api/gloves/refresh',{})
        self.app.action('/api/gloves/model/start',{'side':'left'})
        self.app.action('/api/gloves/model/continue',{'side':'left','prompt_id':'pose-1'})
        self.app.action('/api/gloves/model/cancel',{'side':'left'})
        self.app.action('/api/hands/calibrate/cancel',{'side':'right'})
        self.assertEqual(self.app.hands.glove_requests,[('left',True),('left',False)])
        self.assertEqual(self.app.hands.model_requests,[('start','left'),('continue','left','pose-1'),('cancel','left')])
        self.assertEqual(self.app.hands.capture_cancellations,['right'])
        self.assertEqual(self.app.tracker.starts,0)
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.auto_suppressed,{'left','right'})

    def test_tracker_service_restore_does_not_start_gloves_or_robot(self):
        self.app.action('/api/trackers/start',{})
        self.assertEqual(self.app.tracker.starts,1)
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.session.launches,[])
        self.app.session.value['running']=True
        with self.assertRaises(RuntimeError):self.app.action('/api/trackers/stop',{})
        self.assertEqual(self.app.tracker.stops,0)

    def test_pico_oneclick_and_legacy_alias_only_prepare_pico_inputs(self):
        for route in ('/api/pico/start','/api/services/pico/start'):
            result=self.app.action(route,{})
            self.assertTrue(result['download']['available'])
            self.assertTrue(result['receiver_running'])
            self.assertTrue(result['reader_running'])
        self.assertEqual(self.app.pico_setup.starts,2)
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.hands.input_starts,0)

    def test_pico_stop_keeps_download_and_blocks_during_arm_ready(self):
        self.app.action('/api/pico/start',{})
        self.app.session.value.update(running=True,stage='ready')
        with self.assertRaises(RuntimeError):self.app.action('/api/pico/stop',{})
        self.assertEqual(self.app.tracker.stops,0)
        self.assertEqual(self.app.pico_setup.monitor_stops,0)
        self.app.session.value['running']=False
        result=self.app.action('/api/services/pico/stop',{})
        self.assertFalse(result['receiver_running'])
        self.assertFalse(result['reader_running'])
        self.assertTrue(result['download']['available'])
        self.assertEqual(self.app.pico_setup.monitor_stops,1)

    def test_pico_partial_page_failure_does_not_block_existing_headset(self):
        with patch.object(self.app.pico_setup,'start',side_effect=RuntimeError('page unavailable')):
            with self.assertRaisesRegex(RuntimeError,'page unavailable'):self.app.action('/api/pico/start',{})
        self.assertTrue(self.app.pico.running)
        self.assertTrue(self.app.tracker.running)
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.input_starts,0)

    def test_pico_transport_connection_and_pose_readiness_are_separate(self):
        self.app.action('/api/settings',{'arms_mode':'both','left_sn':'L'})
        self.app.action('/api/pico/start',{})
        self.app.tracker.frame['trackers']['L']=[0.]*7
        with patch.object(self.app.pico,'status',return_value={'running':True,'headset_connected':True}):
            setup=self.app.status()['services']['pico_setup']
        self.assertTrue(setup['headset_connected'])
        self.assertTrue(setup['right_ready'])
        self.assertFalse(setup['left_ready'])
        self.assertFalse(setup['ready'])
        with patch.object(self.app.tracker,'snapshot',return_value=None), patch.object(self.app.tracker,'status',return_value={'running':True,'connected':False,'head_ready':False}), patch.object(self.app.pico,'status',return_value={'running':True,'headset_connected':True}):
            setup=self.app.status()['services']['pico_setup']
        self.assertTrue(setup['headset_connected'])
        self.assertFalse(setup['input_live'])
        self.assertIn('没有实时姿态',setup['detail'])

    def test_pico_recovery_is_allowed_while_arm_session_running(self):
        self.app.session.value.update(running=True,stage='holding')
        self.app.action('/api/pico/start',{})
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.session.starts,0)
        self.assertEqual(self.app.hands.started,[])
        for path in ('/api/pico/start','/api/pico/stop'):
            with self.assertRaises(ValueError):self.app.action(path,{'unexpected':True})

    def test_calibration_blocks_hand_autostart_and_readiness(self):
        self.app.action('/api/settings',{'hands_enabled':{'left':True,'right':True}})
        self.app.hands.values['left']['calibration_busy']=True
        self.assertFalse(self.app.status()['services']['hands']['left']['can_start'])
        self.launch_start();self.app.session.value['stage']='following'
        self.app._watch_once()
        self.assertEqual(self.app.hands.started,['right'])

    def test_robot_connection_does_not_start_or_stop_controls(self):
        self.app.action('/api/robot/connect',{})
        self.assertTrue(self.app.status()['services']['robot']['connected'])
        self.app.session.value.update(running=True,stage='following')
        self.app.action('/api/robot/disconnect',{})
        robot=self.app.status()['services']['robot']
        self.assertFalse(robot['connected'])
        self.assertTrue(robot['control_connected'])
        self.assertEqual(self.app.session.stops,0)
        self.assertEqual(self.app.session.starts,0)
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.tracker.starts,0)

    def test_arm_input_check_prepares_only_pico_and_allows_offline_robot_calibration(self):
        result=self.app.action('/api/arms/input/check',{})
        self.assertTrue(result['ready'])
        self.assertIsNotNone(result['checked_at'])
        self.assertEqual(self.app.tracker.starts,1)
        self.assertEqual(self.app.pico_setup.starts,1)
        self.assertEqual(self.app.robot.connects,0)
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertEqual(self.app.hands.started,[])
        self.assertEqual(self.app.session.launches,[])
        self.assertTrue(self.app.status()['arm_calibration']['can_capture'])
        self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        self.assertTrue(self.app.status()['arm_calibration']['origin_captured'])

    def test_input_check_uses_actual_snapshot_and_does_not_latch_success(self):
        self.app.action('/api/arms/input/check',{})
        with patch.object(self.app.tracker,'snapshot',return_value=None):
            status=self.app.status()
            self.assertFalse(status['services']['arm_input']['ready'])
            self.assertFalse(status['arm_calibration']['can_capture'])
            self.assertIn('stream',status['services']['arm_input']['missing'])
        self.app.tracker.frame['head_pose']=[0.]*7
        status=self.app.status()
        self.assertFalse(status['services']['arm_input']['head_ready'])
        self.assertFalse(status['arm_calibration']['can_capture'])
        self.assertTrue(status['services']['arm_input']['right_ready'])

    def test_head_heading_and_selected_wrist_are_checked_without_motion_requirement(self):
        self.app.tracker.frame['trackers']={'R':[0.,0.,0.,0.,0.,0.,1.]}
        self.app.tracker.frame['pose_age_ms']={'R':3600000}
        self.assertTrue(self.app.action('/api/arms/input/check',{})['ready'])
        self.app.tracker.frame['head_pose']=[0.,0.,0.,2**-.5,0.,0.,2**-.5]
        status=self.app.status()
        self.assertTrue(status['services']['arm_input']['head_ready'])
        self.assertFalse(status['services']['arm_input']['heading_ready'])
        self.assertFalse(status['arm_calibration']['can_capture'])

    def test_existing_origin_uses_frozen_heading_but_new_reader_requires_reset(self):
        self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        self.app.tracker.frame['head_pose']=[0.]*7
        self.app.tracker.frame['trackers']['R'][2]=-.2
        status=self.app.status()
        self.assertFalse(status['services']['arm_input']['ready'])
        self.assertTrue(status['arm_calibration']['can_capture'])
        self.app.action('/api/arms/calibration/capture',{'stage':'forward'})
        self.app.tracker.frame['session']='new-reader'
        status=self.app.status()
        self.assertTrue(status['services']['arm_input']['needs_reset'])
        self.assertFalse(status['arm_calibration']['can_capture'])
        self.assertTrue(status['arm_calibration']['can_reset'])

    def test_bad_measurement_does_not_block_retrying_current_axis(self):
        self.app.action('/api/arms/calibration/capture',{'stage':'origin'})
        with self.assertRaises(ValueError):self.app.action('/api/arms/calibration/capture',{'stage':'forward'})
        self.assertTrue(self.app.status()['arm_calibration']['can_capture'])
        self.app.tracker.frame['trackers']['R'][2]=-.2
        self.app.action('/api/arms/calibration/capture',{'stage':'forward'})

    def test_check_routes_reject_arguments_and_input_preparation_failure_keeps_outputs_idle(self):
        for route in ('/api/robot/connect','/api/robot/disconnect','/api/arms/input/check'):
            with self.assertRaises(ValueError):self.app.action(route,{'host':'untrusted'})
        with patch.object(self.app.pico,'start',side_effect=RuntimeError('receiver failed')):
            with self.assertRaisesRegex(RuntimeError,'receiver failed'):self.app.action('/api/arms/input/check',{})
        self.assertEqual(self.app.session.launches,[])
        self.assertEqual(self.app.hands.input_starts,0)
        self.assertFalse(self.app.status()['services']['arm_input']['checking'])


if __name__=='__main__':unittest.main()
