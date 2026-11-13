"""One native tracker reader shared by dashboard checks and arm sessions."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
VENDOR=ROOT/'vendor/xr_h2'
sys.path.insert(0,str(VENDOR))
from pico_tracker.input import read_frame
from pico_tracker.geometry import pose_valid
from arm_pose_source import select_frame


class TrackerManager:
    def __init__(self,root=ROOT,simulation=False):
        self.root=Path(root);self.simulation=simulation
        self.process=self._log=self._lease=self._temporary=None
        self.frame_path=None;self.error=None
        self.lock=threading.RLock()

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None: return self.status()
            self.stop()
            if not self.simulation:
                from dashboard_session import _conflicts
                conflicts=_conflicts()
                if conflicts:raise RuntimeError('Another tracker/control reader is active: '+', '.join(conflicts))
            directory=(self.root/'diagnostics/simulation-trackers' if self.simulation else
                       Path('/dev/shm')/f'g1-dashboard-tracker-{os.getuid()}')
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            self._lease=(directory/'reader.lock').open('a')
            try: fcntl.flock(self._lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self._lease.close();self._lease=None
                raise RuntimeError('Another dashboard owns the native PICO reader')
            self._temporary=tempfile.TemporaryDirectory(prefix='stream-',dir=directory)
            self.frame_path=Path(self._temporary.name)/'latest.json'
            self.error=None
            self._log=(self.root/'diagnostics/dashboard-trackers.log').open('a',buffering=1)
            python=Path.home()/'.cache/unitree-g1-pico/venv/bin/python'
            argv=[str(python),'-u','-B',str(self.root/'scripts/pico_arm_stream.py')]
            if self.simulation:
                code="import json,time\nseq=0\nwhile True:\n t=time.monotonic(); print(json.dumps(dict(session='simulation',seq=seq,trackers={'SIM-LEFT':[0,1,0,0,0,0,1],'SIM-RIGHT':[0,1,0,0,0,0,1]},controllers={'left':[0,1,0,0,0,0,1],'right':[0,1,0,0,0,0,1]},controller_sample_mono=t,tracker_sample_mono=t,head_pose=[0,1.5,0,0,0,0,1],pose_age_ms={'SIM-LEFT':0,'SIM-RIGHT':0},sample_mono=t)),flush=True);seq+=1;time.sleep(.05)"
                argv=[sys.executable,'-u','-c',code]
            try:
                self.process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=self._log,text=True,bufsize=1)
            except BaseException:
                self.stop();raise
            process,path=self.process,self.frame_path
            threading.Thread(target=self._read,args=(process,path),daemon=True).start()
            return self.status()

    def _read(self,process,path):
        try:
            for line in process.stdout:
                try:
                    frame=json.loads(line)
                    if not isinstance(frame,dict) or 'trackers' not in frame: continue
                    frame['received_mono']=time.monotonic()
                    frame['sample_mono_on_robot']=frame['sample_mono']
                    temporary=path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(frame,allow_nan=False));temporary.replace(path)
                except (ValueError,KeyError,TypeError): continue
                except OSError: break
        finally:
            process.stdout.close()

    def snapshot(self):
        return read_frame(self.frame_path) if self.frame_path else None

    def status(self):
        frame=self.snapshot()
        wrists=select_frame(frame,'trackers')
        controllers=select_frame(frame,'controllers')
        running=bool(self.process and self.process.poll() is None)
        return {'running':running,'connected':bool(frame),'serials':list(wrists.get('trackers',{})) if wrists else [],
            'controllers':{side:{'ready':bool(controllers and pose_valid(controllers.get('trackers',{}).get('pico-controller-'+side)))} for side in ('left','right')},
            'head_ready':bool(frame and pose_valid(frame.get('head_pose'))),'pose_age_ms':frame.get('pose_age_ms',{}) if frame else {},
            'session':frame.get('session') if frame else None,'frame_path':str(self.frame_path) if self.frame_path else None,
            'detail':'等待 PICO Head + Object／Controller + Send' if running and not frame else 'PICO 位姿输入已连接' if frame else '点击检查输入，读取腕部 Tracker 或手柄',
            'error':self.error or ('PICO native reader exited' if self.process and not running else None)}

    def stop(self):
        with self.lock:
            if self.process:
                if self.process.poll() is None:
                    self.process.send_signal(signal.SIGINT)
                    try:self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        try:self.process.wait(timeout=1)
                        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
                self.process=None
            if self._temporary:self._temporary.cleanup();self._temporary=None
            self.frame_path=None
            if self._lease:self._lease.close();self._lease=None
            if self._log:self._log.close();self._log=None
        return self.status()

    close=stop
