#!/usr/bin/env python3
"""Laptop dashboard for the pinned PICO branch, adapted to G1 over Wi-Fi SSH."""
import argparse
import copy
from datetime import datetime, timezone
import getpass
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from network_config import laptop_host, robot_host
from dashboard_session import SessionManager
from dashboard_camera import CameraManager
from dashboard_trackers import TrackerManager
from arm_pose_source import validate_source, effective_config, select_frame
from arm_reference_frame import validate_reference_frame, reference_frame, reference_mapping
from dashboard_hands import HandCoordinator
from dashboard_arm_calibration import ArmCalibration, input_readiness
from dashboard_pico_setup import PicoSetup
from dashboard_robot_connection import RobotConnection
from branch_arm_input import live as arm_input_live, selected_sides
from pico_tracker.geometry import pose_valid

ROOT = Path(__file__).resolve().parents[1]


class PicoService:
    def __init__(self, simulation=False):
        self.process = None
        self.log = None
        self.simulation = simulation
        self.sim_running = False
        self.lock = threading.RLock()

    def status(self):
        headset_connected = False
        if self.simulation:
            running = self.sim_running
        else:
            # Probe /proc, never open a client socket that could replace the headset.
            listeners=set()
            for table in ('/proc/net/tcp','/proc/net/tcp6'):
                try:
                    for line in Path(table).read_text().splitlines()[1:]:
                        fields=line.split()
                        if fields[3]=='0A': listeners.add(int(fields[1].rsplit(':',1)[1],16))
                        if fields[3]=='01' and int(fields[1].rsplit(':',1)[1],16)==63901:
                            raw=bytes.fromhex(fields[1].split(':',1)[0])
                            address=ipaddress.ip_address(b''.join(raw[i:i+4][::-1] for i in range(0,len(raw),4)))
                            if isinstance(address,ipaddress.IPv6Address):
                                address=address.ipv4_mapped or address
                            if str(address)==laptop_host():
                                headset_connected = True
                except (OSError,ValueError,IndexError): pass
            running=63901 in listeners and 60061 in listeners
        owned=bool(self.process and self.process.poll() is None)
        return {'running':running,'owned':owned,'headset_connected':headset_connected,'detail':
            ('模拟输入服务' if self.simulation else f'XRoboToolkit PCService · {laptop_host()}:63901')+
            (' · 网页启动' if owned else ' · 已有服务' if running else ' · 未运行')}

    def start(self):
        with self.lock:
            if self.status()['running'] or (self.process and self.process.poll() is None): return self.status()
            if self.simulation:
                self.sim_running=True
                return self.status()
            binary=Path.home()/'.cache/unitree-g1-pico/pc-service/opt/apps/roboticsservice/RoboticsServiceProcess'
            self.log=(ROOT/'diagnostics/dashboard-pico-service.log').open('a')
            env=dict(os.environ)
            env['LD_LIBRARY_PATH']=':'.join((str(binary.parent),str(binary.parent/'lib'),str(binary.parent/'SDK/x64'),env.get('LD_LIBRARY_PATH','')))
            env['QT_PLUGIN_PATH']=str(binary.parent/'plugins')
            env['QT_QML_PATH']=str(binary.parent/'qml')
            self.process=subprocess.Popen([str(binary)],cwd=binary.parent,env=env,
                stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,close_fds=True)
            return self.status()

    def stop(self):
        with self.lock:
            if self.simulation: self.sim_running=False
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait()
            if self.log: self.log.close(); self.log=None
            if self.status()['running']:
                raise RuntimeError('Existing PICO service is owned by another session; this page only stops its own service')
            return self.status()

    def close(self):
        if self.process: self.stop()


class Dashboard:
    def __init__(self,password='',simulation=False,port=8025,*,root=ROOT):
        self.root=Path(root)
        self.token=secrets.token_urlsafe(32)
        self.simulation=simulation
        self.lock=threading.RLock()
        self.done=threading.Event()
        self.pending_hands=None
        self.auto_started_for=None
        self.auto_owned=set()
        self.auto_suppressed=set()
        self.auto_waiting=set()
        self.auto_input_attempted=set()
        self.auto_input_preparing=set()
        self.auto_errors={}
        self.auto_generation=0
        self.coordination_error=None
        self.arm_input_checked_at=None
        self.arm_input_check_error=None
        self.settings=self._load_settings()
        self.arm_calibrations={
            (source, reference):ArmCalibration(self.root, simulation=simulation,
                filename='arm-calibration'+('-controllers' if source=='controllers' else '')+
                    ('-headset' if reference=='headset' else '')+'.json', reference=reference)
            for source in ('trackers','controllers') for reference in ('world','headset')}
        self.session=SessionManager(self.root,python=sys.executable,password=password,simulation=simulation)
        self.camera=CameraManager(self.root,python=sys.executable,password=password,simulation=simulation,
                                  source_url=f'http://127.0.0.1:{port}/api/camera.jpg')
        self.pico=PicoService(simulation)
        self.pico_setup=PicoSetup(self.root,simulation=simulation)
        self.robot=RobotConnection(self.root,password=password,simulation=simulation)
        self.tracker=TrackerManager(self.root,simulation=simulation)
        self.hands=HandCoordinator(self.root,password=password,simulation=simulation)
        self.watcher=threading.Thread(target=self._watch,daemon=True)
        self.watcher.start()

    @property
    def arm_calibration(self):
        return self.arm_calibrations[(self.settings.get('arm_input_source','trackers'),
                                     self.settings.get('arm_reference_frame','world'))]

    def _load_settings(self):
        def read(name, default):
            try:return json.loads((self.root/'config'/name).read_text())
            except FileNotFoundError:return default
        config=read('pico_tracker.json',{'left_sn':'','right_sn':'PC2310MLL9241957G',
                    'offset_tracker_m':[0.,0.,0.],'right_offset_tracker_m':[0.,0.,0.]})
        saved=read('dashboard.json',{})
        mode=saved.get('arms_mode','right')
        source=validate_source(saved.get('arm_input_source','trackers'))
        reference=validate_reference_frame(saved.get('arm_reference_frame','world'))
        selected_sides(mode)
        enabled=saved.get('hands_enabled',{'left':False,'right':False})
        if not isinstance(enabled,dict) or set(enabled)-{'left','right'} or any(type(v) is not bool for v in enabled.values()):
            raise ValueError('Invalid persisted hand selection')
        return {'arms_mode':mode,'arm_input_source':source,'arm_reference_frame':reference,
            'hands_enabled':{'left':False,'right':False,**enabled},'tracker_config':config}

    def _save_settings(self,payload):
        session=self.session.status()
        hands=self.hands.status()
        if session['running'] or any(hands.get(s,{}).get('running') for s in ('left','right')):
            raise RuntimeError('Stop arm and hand following before changing settings')
        if set(payload)-{'arms_mode','arm_input_source','arm_reference_frame','left_sn','right_sn','hands_enabled'}:
            raise ValueError('Unknown dashboard setting')
        settings=copy.deepcopy(self.settings)
        mode=payload.get('arms_mode',settings['arms_mode'])
        source=validate_source(payload.get('arm_input_source',settings['arm_input_source']))
        reference=validate_reference_frame(payload.get('arm_reference_frame',settings['arm_reference_frame']))
        required=selected_sides(mode)
        available=set(self.tracker.status().get('serials',[]))
        config=settings['tracker_config']
        for side in ('left','right'):
            key=side+'_sn'
            if key not in payload:continue
            serial=payload[key]
            if not isinstance(serial,str) or serial!=serial.strip() or len(serial)>128:
                raise ValueError('Tracker serial must be a short, exact string')
            if serial and serial!=config.get(key) and not self.simulation and serial not in available:
                raise ValueError('Choose a currently detected '+side+' wrist tracker')
            config[key]=serial
        if source=='trackers' and any(not config.get(side+'_sn') for side in required):
            raise ValueError('Choose a tracker serial for every selected arm')
        if config.get('left_sn') and config['left_sn']==config.get('right_sn'):
            raise ValueError('Use a different tracker serial for each wrist')
        if 'hands_enabled' in payload:
            enabled=payload['hands_enabled']
            if not isinstance(enabled,dict) or set(enabled)-{'left','right'} or any(type(v) is not bool for v in enabled.values()):
                raise ValueError('hands_enabled must contain left/right booleans')
            settings['hands_enabled'].update(enabled)
        settings['arms_mode']=mode
        settings['arm_input_source']=source
        settings['arm_reference_frame']=reference
        if not self.simulation:
            directory=self.root/'config'
            directory.mkdir(parents=True,exist_ok=True)
            # Each reader sees a complete file; the worker receives this same snapshot.
            for name,value in (('pico_tracker.json',config),('dashboard.json',{
                    'arms_mode':mode,'arm_input_source':source,'arm_reference_frame':reference,
                    'hands_enabled':settings['hands_enabled']})):
                path=directory/name
                temporary=path.with_suffix('.tmp')
                temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
                temporary.replace(path)
        self.settings=settings
        return copy.deepcopy(settings)

    def _cancel_auto_hands(self):
        self.auto_generation+=1
        self.pending_hands=None
        self.auto_waiting.clear()
        self.auto_input_attempted.clear()
        self.auto_input_preparing.clear()
        self.auto_errors.clear()

    def _prepare_auto_glove(self,side,generation):
        # SDK work never holds the dashboard lock: STOP revokes output intent.
        try:
            with self.lock:
                if (self.done.is_set() or generation!=self.auto_generation or
                        side not in self.auto_waiting or side in self.auto_suppressed):return
                if self.hands.status().get(side,{}).get('calibration_busy'):return
            self.hands.glove_input(side,start=True)
        except Exception as exc:
            with self.lock:
                if generation==self.auto_generation and side in self.auto_waiting:
                    self.auto_errors[side]=str(exc)
                    self.auto_waiting.discard(side)
        finally:
            with self.lock:
                if generation==self.auto_generation:self.auto_input_preparing.discard(side)

    def _watch_once(self):
        with self.lock:
            session=self.session.status()
            sid=session.get('session_id')
            if self.auto_owned and (not session['running'] or sid!=self.auto_started_for):
                for side in tuple(self.auto_owned):
                    self.hands.stop(side)
                    self.auto_owned.discard(side)
            if self.pending_hands is None:return
            if not session['running'] or sid!=self.pending_hands or session.get('stop_requested'):
                self._cancel_auto_hands()
                return
            if session.get('stage')=='ready' and session.get('start_requested') is False:
                self._cancel_auto_hands()  # Rejected START may be retried by the user.
                return
            for side in ('left','right'):
                if side not in self.auto_waiting:continue
                state=self.hands.status().get(side,{})
                if (not self.settings['hands_enabled'].get(side) or side in self.auto_suppressed
                        or state.get('calibration_busy') or state.get('running')):
                    self.auto_waiting.discard(side)
                    continue
                if side not in self.auto_input_attempted:
                    self.auto_input_attempted.add(side)
                    if not state.get('input_running'):
                        self.auto_input_preparing.add(side)
                        threading.Thread(target=self._prepare_auto_glove,
                            args=(side,self.auto_generation),daemon=True).start()
                if side in self.auto_input_preparing:continue
                if state.get('reader_error') and not state.get('input_running'):
                    self.auto_errors[side]=state['reader_error']
                    self.auto_waiting.discard(side)
                    continue
                if not (state.get('input_live') and state.get('model_ready')
                        and state.get('calibration_ready') and state.get('command_ready')):continue
                if session.get('stage') not in ('following','holding'):continue
                # SDK preparation holds this lock; do not let it delay STOP.
                operations=getattr(self.hands,'operations',None)
                if operations is not None and not operations.acquire(blocking=False):continue
                try:
                    result=self.hands.start(side)
                    after=self.hands.status().get(side,{})
                    if after.get('running'):
                        self.auto_owned.add(side)
                        self.auto_started_for=sid
                        self.auto_waiting.discard(side)
                    elif after.get('error_kind')!='readiness' and not result.get('errors',{}).get(side):
                        self.auto_errors[side]=after.get('error') or '手部启动未成功；请检查后手动重试。'
                        self.auto_waiting.discard(side)
                except Exception as exc:
                    self.coordination_error=f'{side} hand: {exc}'
                    self.auto_errors[side]=str(exc)
                    self.auto_waiting.discard(side)
                finally:
                    if operations is not None:operations.release()
            if not self.auto_waiting:self.pending_hands=None

    def _watch(self):
        while not self.done.wait(.05):
            try:self._watch_once()
            except Exception as exc:self.coordination_error=str(exc)

    def status(self):
        session=self.session.status()
        robot_connected=not self.simulation and session['running'] and session['stage'] in ('ready','following','holding','stopping')
        history=[]
        for path in sorted((self.root/'diagnostics/dashboard-runs').glob('*/summary.json'),reverse=True)[:10]:
            try:
                item=json.loads(path.read_text())
                item.update(session_id=path.parent.name,output_dir=str(path.parent))
                history.append(item)
            except (OSError,ValueError): pass
        with self.lock:settings=copy.deepcopy(self.settings)
        mode=settings['arms_mode']
        tracker=self.tracker.status()
        source=settings.get('arm_input_source','trackers')
        reference=settings.get('arm_reference_frame','world')
        config=effective_config(settings['tracker_config'],source)
        config['arm_reference_frame']=reference
        selected={side:config.get(side+'_sn','') for side in selected_sides(mode)}
        raw_frame=select_frame(self.tracker.snapshot(),source)
        frame=reference_frame(raw_frame,reference)
        try:ready=bool(all(selected.values()) and arm_input_live(frame,selected) and frame and pose_valid(frame.get('head_pose')))
        except (KeyError,TypeError,ValueError):ready=False
        tracker.update(ready=ready,arms_mode=mode,input_source=source,selected_serials=selected,
            connected=bool(frame),head_ready=bool(frame and pose_valid(frame.get('head_pose'))),
            left_sn=config.get('left_sn',''),right_sn=config.get('right_sn',''),
            scope=({'left':'左手柄 → 左臂','right':'右手柄 → 右臂','both':'双手柄 → 双臂'} if source=='controllers'
                else {'left':'左腕 → 左臂','right':'右腕 → 右臂','both':'双腕 → 双臂'})[mode])
        pico=self.pico.status()
        pico_setup=self.pico_setup.status()
        pico_setup.update(receiver_running=bool(pico.get('running')),reader_running=bool(tracker.get('running')),
            input_source=source,
            headset_connected=bool(pico.get('headset_connected')),input_live=bool(tracker.get('connected')),
            head_ready=bool(tracker.get('head_ready')),ready=bool(ready and pico.get('running') and tracker.get('running')))
        for side in ('left','right'):
            sn=config.get(side+'_sn','')
            try:side_ready=bool(sn and arm_input_live(frame,{side:sn}))
            except (KeyError,TypeError,ValueError):side_ready=False
            pico_setup.update({side+'_sn':sn,side+'_ready':side_ready})
        pico_setup['detail']=(
            '点击一键启动 PICO，准备下载页、接收服务和 Tracker 读取。' if not pico.get('running') else
            '接收服务已启动；点击一键启动 PICO 恢复 Tracker 读取。' if not tracker.get('running') else
            '头显网络已连接，但没有实时姿态；请在头显保持应用前台，并检查 Head + Object + Send。' if pico.get('headset_connected') and not tracker.get('connected') else
            f'电脑接收已准备好，等待头显选择 {laptop_host(self.root)} 并开启 Send。' if not tracker.get('connected') else
            '已收到姿态，但 Head 无效；请在 XRoboToolkit 开启 Head。' if not tracker.get('head_ready') else
            '已收到实时 Head，等待所选腕部 Tracker 的有效数据。' if not ready else
            'Head 和所选腕部 Tracker 实时输入已就绪。')
        if source=='controllers':
            pico_setup['detail']=pico_setup['detail'].replace('Head + Object + Send','Head + Controller + Send').replace('腕部 Tracker','PICO 手柄').replace('Tracker 读取','手柄读取')
        hands=self.hands.status()
        hands['input_running']=any(hands.get(s,{}).get('input_running') for s in ('left','right'))
        if self.coordination_error:hands['coordination_error']=self.coordination_error
        for side in ('left','right'):
            item=hands.get(side,{})
            with self.lock:
                pending=bool(self.pending_hands==session.get('session_id') and session.get('running')
                    and not session.get('stop_requested') and side in self.auto_waiting
                    and side not in self.auto_suppressed)
                preparing=side in self.auto_input_preparing
                auto_error=self.auto_errors.get(side)
            stage=detail=None
            if auto_error:stage,detail='error',auto_error
            elif pending:
                if preparing:stage,detail='preparing_input','正在准备此侧手套输入；收到有效数据后自动接入跟随。'
                elif not item.get('model_ready') or not item.get('calibration_ready'):
                    stage,detail='waiting_calibration','等待此侧手套个人模型和开合校准就绪。'
                elif not item.get('input_live') or not item.get('command_ready'):
                    stage,detail='waiting_input','等待此侧有效手套数据；就绪后自动接入跟随。'
                else:stage,detail='waiting_arm','手套已就绪，等待手臂 START 生效后接入跟随。'
            item.update(auto_start_pending=pending,auto_start_stage=stage,
                auto_start_detail=detail,auto_start_error=auto_error)
            if pending and not item.get('reader_error') and not item.get('motion_error'):item['detail']=detail
            item['can_start']=bool(not item.get('running') and item.get('input_live')
                and item.get('model_ready') and item.get('calibration_ready') and not item.get('calibration_busy'))
        calibration_store=self.arm_calibrations[(source,reference)]
        arm_calibration=calibration_store.status(config,mode)
        arm_calibration.update(input_source=source,reference_frame=reference,calibration_file=calibration_store.path.name)
        arm_input=input_readiness(raw_frame,config,mode)
        arm_input.update(input_source=source,checked_at=self.arm_input_checked_at,
            reader_running=bool(tracker.get('running')),receiver_running=bool(pico.get('running')),
            error=self.arm_input_check_error)
        if source=='controllers':
            arm_input['detail']=arm_input['detail'].replace('Head + Object + Send','Head + Controller + Send').replace('Tracker','手柄')
        changed_session=bool(arm_calibration.get('origin_captured') and arm_input['session'] and
            arm_calibration.get('origin_session') != arm_input['session'])
        arm_input['needs_reset']=bool(changed_session or arm_calibration.get('binding_error'))
        if changed_session:
            arm_input.update(ready=False,missing=arm_input['missing']+['session'],
                detail='Tracker 输入会话已改变；请重置本轮采集，再重新采集原点。')
        elif arm_calibration.get('binding_error'):
            arm_input.update(ready=False,missing=arm_input['missing']+['binding'],
                detail=arm_calibration['binding_error'])
        capture_ready=arm_input['ready']
        capture_detail=arm_input['detail']
        if reference=='world' and arm_calibration.get('origin_captured') and not changed_session:
            # Later axes use the heading already frozen by the origin sample.
            capture_ready=bool(arm_input['input_live'] and
                not any(item not in ('head','heading') for item in arm_input['missing']) and
                not arm_calibration.get('binding_error'))
            if capture_ready and not arm_input['ready']:
                capture_detail='原点已记录头部朝向，腕部输入有效；可以继续三轴采集。'
        arm_input['checking']=bool(self.arm_input_checked_at and not arm_input['ready'] and tracker.get('running') and not self.arm_input_check_error)
        arm_input['calibration_ready']=capture_ready
        calibration_blocked=session['running'] or any(
            hands.get(side,{}).get('running') or hands.get(side,{}).get('calibration_busy')
            for side in ('left','right'))
        arm_input['calibration_available']=bool(capture_ready and not calibration_blocked)
        if arm_input['ready'] and calibration_blocked:
            arm_input['detail']='Head 与所选腕部输入有效；请先结束活动控制或其他校准，再采集手臂校准。'
        arm_calibration.update(input_ready=capture_ready,input_detail=capture_detail,
            can_capture=capture_ready and not calibration_blocked,
            can_reset=not calibration_blocked,
            can_save=bool(arm_calibration.get('can_save') and not calibration_blocked))
        robot=self.robot.status()
        robot['control_connected']=bool(robot_connected)
        return {'control_token':self.token,'session':session,'history':history,'laptop_ip':laptop_host(self.root),
            'simulation':self.simulation,'settings':settings,'arm_calibration':arm_calibration,
            'capabilities':{'left_arm':True,'right_arm':True,'hands':True,'dual_arm':True,'preview':False,
                'arm_input_sources':['trackers','controllers'],
                'arm_reference_frames':['world','headset'],
                'arm_reference_mappings':{mode:reference_mapping(mode) for mode in ('world','headset')},
                'glove_calibration':True,'glove_model_calibration':True,'arm_calibration':True,'pico_setup':True,
                'robot_connection':True,'arm_input_check':True,
                'arms_mode':mode,'selected_arms':list(selected_sides(mode)),
                'scope':tracker['scope']+' · 左右 Wuji / Revo2 独立控制 · 原分支输入容忍逻辑',
                'control_mode':'normal_walking','control_topic':'rt/arm_sdk',
                'control_note':'Regular 模式：行走由原控制器和遥控器负责；LAUNCH 准备连接，START 接入双臂跟随，STOP 交还手臂控制权。'},
            'services':{'pico':pico,'pico_setup':pico_setup,'camera':self.camera.status(),
                'robot':robot,'arm_input':arm_input,
                'tracker':tracker,'hands':hands}}

    def action(self,path,payload):
        with self.lock:
            return self._action(path,payload)

    def _action(self,path,payload):
        if self.done.is_set():raise RuntimeError('Dashboard is closing')
        if path in ('/api/robot/connect','/api/robot/disconnect'):
            if payload:raise ValueError('G1 connection action expects an empty object')
            return self.robot.connect() if path.endswith('/connect') else self.robot.disconnect()
        if path=='/api/arms/input/check':
            if payload:raise ValueError('Arm input check expects an empty object')
            self.arm_input_checked_at=datetime.now(timezone.utc).isoformat()
            self.arm_input_check_error=None
            try:self._prepare_pico()
            except Exception as exc:
                self.arm_input_check_error=str(exc)
                raise
            return self.status()['services']['arm_input']
        if path=='/api/settings':return self._save_settings(payload)
        if path=='/api/launch':
            if self.session.status()['running']:raise RuntimeError('An arm session is already running')
            options=copy.deepcopy(self.settings)
            options.pop('hands_enabled')
            options['tracker_config']=effective_config(options['tracker_config'],options.get('arm_input_source','trackers'))
            options['tracker_config']['arm_reference_frame']=options.get('arm_reference_frame','world')
            if options['arm_reference_frame']=='headset':
                # The headset store below is authoritative; embedded legacy
                # world calibration must never leak into this coordinate mode.
                options['tracker_config'].pop('arm_calibration',None)
            calibration=self.arm_calibration.status(options['tracker_config'],options['arms_mode'])
            if calibration.get('saved_error'):
                raise ValueError(calibration['saved_error'])
            if self.arm_calibration.saved:
                if calibration.get('saved_compatible') is False:
                    raise ValueError('Saved arm calibration does not match selected trackers; recalibrate before LAUNCH')
                options['tracker_config']['arm_calibration']=copy.deepcopy(self.arm_calibration.saved)
            tracker=self.tracker.start()
            options['tracker_frame_path']=tracker.get('frame_path')
            self._cancel_auto_hands()
            self.coordination_error=None
            return self.session.launch(options)
        if path=='/api/start':
            result=self.session.start()
            self._cancel_auto_hands()
            self.auto_suppressed.clear()
            self.pending_hands=result.get('session_id')
            self.auto_waiting={side for side,enabled in self.settings['hands_enabled'].items() if enabled}
            self.coordination_error=None
            return result
        if path=='/api/stop':
            self._cancel_auto_hands()
            self.auto_owned.clear()
            try:session=self.session.stop()
            finally:hands=self.hands.stop('both')
            return {'session':session,'hands':hands}
        if path=='/api/inputs/start':
            results,errors={},[]
            for name,operation in (('tracker',self.tracker.start),('hands',self.hands.input_start)):
                try:results[name]=operation()
                except Exception as exc:errors.append(name+': '+str(exc))
            if errors:raise RuntimeError('; '.join(errors))
            return results
        if path in ('/api/arms/calibration/capture','/api/arms/calibration/save','/api/arms/calibration/reset'):
            hands=self.hands.status()
            if self.session.status()['running'] or any(
                    hands.get(side,{}).get('running') or hands.get(side,{}).get('calibration_busy')
                    for side in ('left','right')):
                raise RuntimeError('Stop following and finish other calibration before capturing arm calibration')
            source=self.settings.get('arm_input_source','trackers')
            config,mode=effective_config(self.settings['tracker_config'],source),self.settings['arms_mode']
            config['arm_reference_frame']=self.settings.get('arm_reference_frame','world')
            if path.endswith('/capture'):
                if set(payload)-{'stage','distance_cm'}:raise ValueError('Unknown arm calibration field')
                self.arm_calibration.capture(payload.get('stage'),select_frame(self.tracker.snapshot(),source),config,mode,
                                             payload.get('distance_cm',20))
            elif path.endswith('/save'):
                if payload:raise ValueError('Arm calibration save expects an empty object')
                self.arm_calibration.save(config,mode)
            else:
                if payload:raise ValueError('Arm calibration reset expects an empty object')
                self.arm_calibration.reset()
            return self.status()['arm_calibration']
        if path=='/api/trackers/start':
            return self.tracker.start()
        if path=='/api/trackers/stop':
            if self.session.status()['running']:raise RuntimeError('Stop arm teleop before stopping its shared input')
            return self.tracker.stop()
        if path=='/api/inputs/stop':
            if self.session.status()['running']:raise RuntimeError('Stop arm teleop before stopping its shared input')
            self._cancel_auto_hands()
            self.auto_owned.clear()
            try:hands=self.hands.input_stop()
            finally:tracker=self.tracker.stop()
            return {'tracker':tracker,'hands':hands}
        if path in ('/api/hands/start','/api/hands/stop'):
            side=payload.get('side')
            if side not in ('left','right','both'):raise ValueError('Choose left, right, or both hands')
            for name in (('left','right') if side=='both' else (side,)):
                self.auto_owned.discard(name)
                self.auto_waiting.discard(name)
                if path.endswith('/stop'):self.auto_suppressed.add(name)
            return self.hands.start(side) if path.endswith('/start') else self.hands.stop(side)
        if path=='/api/hands/calibrate':
            side,pose=payload.get('side'),payload.get('pose')
            if side not in ('left','right') or pose not in ('open','closed'):raise ValueError('Choose one hand and open or closed pose')
            self.auto_suppressed.add(side)
            self.auto_waiting.discard(side)
            return self.hands.calibrate(side,pose)
        if path=='/api/hands/calibrate/cancel':
            return self.hands.cancel_calibration(payload.get('side'))
        if path in ('/api/gloves/input/start','/api/gloves/input/stop'):
            if path.endswith('/stop'):
                self.auto_suppressed.add(payload.get('side'))
                self.auto_waiting.discard(payload.get('side'))
            return self.hands.glove_input(payload.get('side'),start=path.endswith('/start'))
        if path=='/api/gloves/refresh':
            return self.hands.refresh_gloves()
        if path=='/api/gloves/model/start':
            self.auto_suppressed.update(('left','right'))
            self._cancel_auto_hands()
            return self.hands.model_start(payload.get('side'))
        if path=='/api/gloves/model/continue':
            return self.hands.model.continue_calibration(payload.get('side'),payload.get('prompt_id'))
        if path=='/api/gloves/model/cancel':
            side=payload.get('side')
            if side not in ('left','right'):raise ValueError('Choose one glove')
            return self.hands.model.cancel(side)
        if path=='/api/camera/start':
            flag=payload.get('pico_video',False)
            if type(flag) is not bool: raise ValueError('pico_video must be boolean')
            return self.camera.start(pico_video=flag)
        if path=='/api/camera/stop': return self.camera.stop()
        if path in ('/api/pico/start','/api/services/pico/start'):
            if payload:raise ValueError('PICO start expects an empty object')
            self._prepare_pico()
            return self.status()['services']['pico_setup']
        if path in ('/api/pico/stop','/api/services/pico/stop'):
            if payload:raise ValueError('PICO stop expects an empty object')
            if self.session.status()['running']: raise RuntimeError('Stop teleop before stopping its input service')
            self.tracker.stop()
            try:self.pico.stop()
            finally:self.pico_setup.stop_monitor()
            return self.status()['services']['pico_setup']
        raise ValueError('Unknown action')

    def _prepare_pico(self):
        errors=[]
        for name,operation in (('下载页',self.pico_setup.start),('接收服务',self.pico.start),('Tracker',self.tracker.start)):
            try:operation()
            except Exception as exc:errors.append(name+': '+str(exc))
        if errors:raise RuntimeError('; '.join(errors))

    def close(self):
        self.done.set()
        self.watcher.join(timeout=3)
        errors=[]
        try:self.session.stop()
        except Exception as exc:errors.append(str(exc))
        for manager in (self.hands,self.session,self.tracker,self.camera,self.pico,self.pico_setup,self.robot):
            try:manager.close()
            except Exception as exc:errors.append(str(exc))
        if errors:raise RuntimeError('; '.join(errors))


def handler_for(app,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,status,body,kind='application/json; charset=utf-8'):
            if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError): pass
        def local(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')
        def do_GET(self):
            if not self.local(): return self.respond(403,{'error':'Loopback host required'})
            path=urlsplit(self.path).path
            if path=='/favicon.ico': return self.respond(204,b'','image/x-icon')
            if path=='/api/health': return self.respond(200,{'ok':True,'simulation':app.simulation})
            if path=='/api/status': return self.respond(200,app.status())
            if path=='/api/camera.jpg':
                jpeg=app.camera.jpeg()
                return self.respond(200,jpeg,'image/jpeg') if jpeg else self.respond(503,{'error':'No live camera frame'})
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/styles.css':('styles.css','text/css; charset=utf-8')}
            if path not in files: return self.respond(404,{'error':'Not found'})
            filename,kind=files[path]
            self.respond(200,(app.root/'web'/filename).read_bytes(),kind)
        def do_POST(self):
            origin=self.headers.get('Origin')
            valid_origin=origin in (None,f'http://127.0.0.1:{port}',f'http://localhost:{port}')
            token=self.headers.get('X-Control-Token','')
            if not self.local() or not valid_origin or not secrets.compare_digest(token,app.token):
                return self.respond(403,{'error':'Invalid local control token/origin'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=4096 or self.headers.get_content_type()!='application/json':
                    raise ValueError('Expected small JSON request')
                data=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data,dict): raise ValueError('Expected JSON object')
                result=app.action(urlsplit(self.path).path,data)
                self.respond(200,{'ok':True,'result':result})
            except (ValueError,RuntimeError,OSError) as exc:
                self.respond(409,{'ok':False,'error':str(exc)})
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8025)
    parser.add_argument('--simulation',action='store_true')
    parser.add_argument('--credentials-stdin',action='store_true')
    args=parser.parse_args()
    password=''
    if not args.simulation:
        password=json.loads(sys.stdin.readline()).get('ssh_password','') if args.credentials_stdin else getpass.getpass('G1 SSH password: ')
    app=Dashboard(password,args.simulation,args.port)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler_for(app,args.port))
    server.daemon_threads=True
    def stop(*_): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    print(json.dumps({'url':f'http://127.0.0.1:{args.port}/','pid':os.getpid(),'simulation':args.simulation}),flush=True)
    try: server.serve_forever(poll_interval=.2)
    finally:
        try:app.close()
        finally:server.server_close()


if __name__=='__main__': main()
