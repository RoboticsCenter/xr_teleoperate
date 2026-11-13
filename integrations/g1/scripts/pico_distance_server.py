"""Read-only dashboard adapter for the PICO distance display.

The only write is an in-memory measurement origin; no dashboard POST, SDK,
robot connection, calibration save or controller exists here.
"""
import copy
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

from network_config import laptop_host
from pico_distance import DistanceMeter
from pico_tracker.input import read_frame
from arm_pose_source import validate_source, effective_config, select_frame
from arm_reference_frame import validate_reference_frame, reference_mapping


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_dashboard():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open('http://127.0.0.1:8025/api/status', timeout=1) as response:
        data = response.read(4*1024*1024+1)
    if len(data) > 4*1024*1024:
        raise ValueError('Dashboard response too large')
    return json.loads(data)


def sanitize_dashboard(value):
    """Allow-list the fields needed by the display; never expose control tokens."""
    settings = value['settings']
    source = validate_source(settings.get('arm_input_source','trackers'))
    config = effective_config(settings['tracker_config'],source)
    calibration = value['arm_calibration']
    tracker = value['services']['tracker']
    return copy.deepcopy({
        'settings': {'arms_mode': settings['arms_mode'], 'arm_input_source':source,
            'arm_reference_frame':validate_reference_frame(settings.get('arm_reference_frame','world')), 'tracker_config': {
            key: config.get(key) for key in
            ('left_sn', 'right_sn', 'offset_tracker_m', 'right_offset_tracker_m')}},
        'calibration': {key: calibration.get(key) for key in
            ('distance_cm', 'captured_stages', 'origin_session', 'error')},
        'frame_path': tracker.get('frame_path'),
        'simulation': value.get('simulation') is True,
    })


def shared_frame(path):
    if not isinstance(path, str):
        return None
    candidate = Path(path)
    expected = Path('/dev/shm')/f'g1-dashboard-tracker-{os.getuid()}'
    try:
        resolved = candidate.resolve(strict=True)
        if (resolved.name != 'latest.json' or resolved.parent.parent != expected or
                not resolved.parent.name.startswith('stream-')):
            return None
        return read_frame(resolved)
    except (OSError, ValueError):
        return None


class DistanceFeed:
    def __init__(self, *, fetch=fetch_dashboard, reader=shared_frame, clock=time.monotonic):
        self._fetch, self._reader, self._clock = fetch, reader, clock
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._thread = None
        self._data, self._received = None, None
        self._error = '等待电脑控制台提供输入。'
        self._meter = DistanceMeter()
        self._pending = False
        self._armed_sample = None
        self._pending_binding = None
        self._video = {'enabled': False, 'error': None,
                       'detail': '可在浏览器查看；头显应用内显示尚未启用。'}
        self._video_process = None
        self._jpeg, self._jpeg_at = None, None
        self._jpeg_validity = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name='pico-distance-feed')
            self._thread.start()

    def refresh(self):
        try:
            data = sanitize_dashboard(self._fetch())
            if data['simulation']:
                raise ValueError('Production distance display cannot use simulated input')
            with self._lock:
                self._data, self._received, self._error = data, self._clock(), None
        except Exception:
            with self._lock:
                self._error = '控制台状态暂不可用；请恢复电脑控制台及 PICO 输入。'

    def _run(self):
        next_refresh = 0.
        while not self._stop.is_set():
            if self._clock() >= next_refresh:
                self.refresh()
                next_refresh = self._clock()+.5
            # Pending measurement zero works after returning to XRoboToolkit,
            # even if no browser remains open to poll this endpoint.
            self.status()
            self._stop.wait(.1)

    def _snapshot(self):
        data = self._data or {'settings': {'arms_mode': 'both', 'tracker_config': {}}, 'calibration': {}}
        fresh = self._received is not None and self._clock()-self._received <= 2 and not self._error
        frame = self._reader(data.get('frame_path')) if fresh else None
        frame = select_frame(frame,data['settings'].get('arm_input_source','trackers'))
        return frame, data['settings'], data['calibration']

    @staticmethod
    def _identity(frame):
        return (frame.get('session'), frame.get('seq')) if isinstance(frame, dict) else None

    def _status(self):
        frame, settings, calibration = self._snapshot()
        result = self._meter.sample(frame, settings, calibration)
        if self._pending and self._pending_binding is not None and settings != self._pending_binding:
            self._pending = False
            self._error = '腕部选择已改变；请重新设置测量起点。'
        if (self._pending and result.get('zero_ready') and self._identity(frame) != self._armed_sample):
            result = self._meter.zero(frame, settings, calibration)
            self._pending = False
        if self._video_process is not None and self._video_process.poll() is not None:
            self._video.update(enabled=False,error='头显显示服务已退出；浏览器测距页面仍可使用。')
        result.update(zero_pending=self._pending, remote_vision=copy.deepcopy(self._video),
                      calibration_error=calibration.get('error'), source_error=self._error)
        if self._error:
            result['detail'] = self._error
        elif self._pending:
            result['detail'] = '已准备归零：返回 XRoboToolkit，保持测量起始姿态并开启 Head + Object + Send。'
        if settings.get('arm_input_source')=='controllers':
            result['detail']=result['detail'].replace('Head + Object + Send','Head + Controller + Send').replace('Tracker','手柄')
        result['input_source']=settings.get('arm_input_source','trackers')
        result['reference_frame']=settings.get('arm_reference_frame','world')
        result['reference_mapping']=reference_mapping(result['reference_frame'])
        result['laptop_ip']=laptop_host()
        return result

    def status(self):
        with self._lock:
            return self._status()

    def zero(self):
        with self._lock:
            frame, settings, calibration = self._snapshot()
            self._meter.zero(frame, settings, calibration)
            self._pending = False
            self._jpeg = None
            return self._status()

    def arm_zero(self):
        with self._lock:
            frame, settings, _ = self._snapshot()
            self._armed_sample = self._identity(frame)
            self._pending_binding = copy.deepcopy(settings) if self._data else None
            self._pending = True
            self._jpeg = None
            return self._status()

    def cancel_zero(self):
        with self._lock:
            self._pending = False
            return self._status()

    def set_video(self, *, process=None, **values):
        with self._lock:
            if process is not None:self._video_process=process
            self._video.update(values)

    def jpeg(self):
        from pico_distance_image import render_distance_jpeg
        with self._lock:
            now = self._clock()
            current = self._status()
            validity = (current.get('session'), current.get('has_origin'), current.get('needs_zero'),
                current.get('zero_pending'), tuple((side, value.get('live')) for side,value in current.get('sides',{}).items()))
            if self._jpeg is None or now-self._jpeg_at >= .1 or validity != self._jpeg_validity:
                self._jpeg = render_distance_jpeg(current)
                self._jpeg_at = now
                self._jpeg_validity = validity
            return self._jpeg

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
