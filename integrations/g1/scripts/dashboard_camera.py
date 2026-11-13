#!/usr/bin/env python3
"""Owned camera lifecycle and G1 source adapter for the unchanged branch bridge.

Construction is inert. Only start() creates a worker or contacts PC2. Credentials
reach the worker through stdin's anonymous pipe, never command arguments or logs.
"""
import argparse
from network_config import robot_host
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

# Matches h2 branch head_camera/receiver.py's displayed-frame freshness rule.
FRAME_FRESHNESS_S = 2.
ROOT = Path(__file__).resolve().parents[1]
_EMIT_LOCK = threading.Lock()


class CameraManager:
    def __init__(self, root=ROOT, python=sys.executable, password='', simulation=False,
                 *, source_url='http://127.0.0.1:8025/api/camera.jpg'):
        self.root, self.python = Path(root).resolve(), str(python)
        self._password, self.simulation = password, simulation
        self.source_url = source_url
        self._lock, self._operations = threading.RLock(), threading.RLock()
        self._process = self._reader = None
        self._closed = False
        self._jpeg, self._received = None, None
        self._state = dict(running=False, live=False, error='', frames=0, fps=0.,
                           pico_video=False, pico_error='', pico_status={},
                           simulation=simulation, source='G1 D435i RGB', device=None)

    def start(self, pico_video=False):
        if type(pico_video) is not bool:
            raise ValueError('pico_video must be boolean')
        with self._operations:
            if self._closed:
                raise RuntimeError('camera manager is closed')
            if self._process is not None and self._process.poll() is None:
                return self.status()
            if self._process is not None:
                self.stop()
            if not self.simulation and (not isinstance(self._password, str) or not self._password
                    or any(c in self._password for c in ('\n', '\r', '\0'))):
                raise ValueError('PC2 credentials are not configured')
            argv = [self.python, '-u', '-B', str(self.root / 'scripts/dashboard_camera.py'),
                    '--worker', '--source-url', self.source_url]
            if self.simulation:
                argv.append('--simulation')
            if pico_video:
                argv.append('--pico-video')
            process = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                start_new_session=True, close_fds=True)
            self._process = process
            with self._lock:
                self._jpeg = self._received = None
                self._state.update(running=True, error='', frames=0, fps=0., device=None,
                                   pico_video=pico_video, pico_error='', pico_status={})
            try:
                process.stdin.write(json.dumps({'password': '' if self.simulation else self._password})+'\n')
                process.stdin.flush()
                self._reader = threading.Thread(target=self._consume, args=(process,), daemon=True)
                self._reader.start()
            except BaseException:
                self.stop()
                raise
            return self.status()

    def _consume(self, process):
        frames, began = 0, time.monotonic()
        for line in process.stdout:
            try:
                record = json.loads(line)
                kind = record.get('type')
                jpeg = None
                if kind == 'camera_frame':
                    jpeg = base64.b64decode(record['jpeg'], validate=True)
                    if len(jpeg) > 8*1024*1024 or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
                        raise ValueError('invalid JPEG frame')
                with self._lock:
                    if process is not self._process:
                        return
                    if jpeg is not None:
                        self._jpeg, self._received = jpeg, time.monotonic()
                        frames += 1
                        self._state.update(frames=self._state['frames']+1, error='')
                        if self._received-began >= 1:
                            self._state['fps'] = round(frames/(self._received-began), 1)
                            frames, began = 0, self._received
                    elif kind == 'camera_error':
                        self._state['error'] = str(record.get('error', 'camera unavailable')).replace(self._password, '[redacted]') if self._password else str(record.get('error'))
                    elif kind == 'camera_ready':
                        self._state['device'] = record.get('device')
                    elif kind == 'pico_video':
                        self._state.update(pico_status=record.get('status', {}), pico_error=record.get('error', ''))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        with self._lock:
            if process is self._process:
                self._state['running'] = False
                if process.poll() not in (None, 0) and not self._state['error']:
                    self._state['error'] = 'Camera worker exited'

    def jpeg(self):
        with self._lock:
            if self._state['running'] and self._received is not None and time.monotonic()-self._received < FRAME_FRESHNESS_S:
                return self._jpeg
            return None

    def status(self):
        with self._lock:
            result = dict(self._state)
            result['age_s'] = None if self._received is None else round(time.monotonic()-self._received, 3)
            result['live'] = self.jpeg() is not None
            result['detail'] = ('SIMULATION: generated test image; no robot camera.' if self.simulation
                                else 'G1 D435i RGB → Wi-Fi → laptop. PICO shows the same image in both eyes.')
            if result['pico_error']:
                result['detail'] += ' PICO: '+result['pico_error']
            return result

    def stop(self):
        with self._operations:
            process = self._process
            if process is not None:
                try:
                    if process.poll() is None:
                        process.stdin.close()  # Owner EOF ends only this camera worker.
                        try:
                            process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait(timeout=2)
                finally:
                    if not process.stdin.closed:
                        process.stdin.close()
                    if self._reader is not None:
                        self._reader.join(timeout=1)
                    process.stdout.close()
                    self._process = self._reader = None
            with self._lock:
                self._jpeg = self._received = None
                self._state.update(running=False, pico_video=False, fps=0.)
            return self.status()

    def close(self):
        with self._operations:
            self.stop()
            self._closed = True
            self._password = ''


def remote_command(root):
    source = base64.b64encode((root / 'scripts/pc2_camera_source.py').read_bytes()).decode()
    bootstrap = "import base64;exec(compile(base64.b64decode("+repr(source)+"),'<g1-camera>','exec'),{'__name__':'__main__'})"
    # The source redirects SDK diagnostics to stderr. Exclude that stream before
    # SSH reaches pexpect's PTY, where stdout/stderr would otherwise be merged.
    return ' '.join(shlex.quote(arg) for arg in (
        '/home/unitree/miniconda3/envs/tv/bin/python', '-u', '-B', '-c', bootstrap)) + ' 2>/dev/null'


def emit(record):
    with _EMIT_LOCK:
        print(json.dumps(record, separators=(',', ':')), flush=True)


def receive_camera(stopped, password, root=ROOT):
    import pexpect
    while not stopped.is_set():
        process = None
        try:
            args = ['-T', '-o', 'ConnectTimeout=5', '-o', 'NumberOfPasswordPrompts=1',
                    '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=3',
                    '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                    '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=192.168.123.164',
                    '-o', 'UserKnownHostsFile='+str(root/'diagnostics/g1-known_hosts'),
                    'unitree@'+robot_host(root), remote_command(root)]
            process = pexpect.spawn('ssh', args, encoding='utf-8', echo=False,
                                    timeout=.5, maxread=256*1024)
            process.delaybeforesend = 0
            # Poll authentication so STOP stays responsive during network outage.
            deadline = time.monotonic()+8
            authenticated = False
            while not stopped.is_set() and time.monotonic() < deadline:
                try:
                    process.expect('[Pp]assword:', timeout=.25)
                    process.sendline(password)
                    authenticated = True
                    break
                except pexpect.TIMEOUT:
                    pass
            if stopped.is_set():
                break
            if not authenticated:
                raise TimeoutError('PC2 SSH authentication timed out')
            while not stopped.is_set():
                try:
                    process.expect(r'\r?\n', timeout=.25)
                except pexpect.TIMEOUT:
                    continue
                try:
                    record = json.loads(process.before.strip())
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get('type') in ('camera_frame', 'camera_error', 'camera_ready'):
                    emit(record)
        except (pexpect.EOF, OSError, TimeoutError):
            # Do not expose pexpect's transcript: authentication may be present.
            emit({'type': 'camera_error', 'error': 'PC2 camera SSH disconnected; reconnecting'})
        finally:
            if process is not None:
                try:
                    process.sendeof()
                    process.expect(pexpect.EOF, timeout=1)
                except (pexpect.EOF, pexpect.TIMEOUT, OSError):
                    pass
                process.close(force=True)
        stopped.wait(2.)


def pico_video(stopped, source_url):
    process = None
    try:
        if not all(importlib.util.find_spec(name) for name in ('av', 'numpy')):
            raise RuntimeError('Install av and numpy in the dashboard Python environment')
        process = subprocess.Popen([sys.executable, '-u', '-B',
            str(ROOT/'vendor/xr_h2/head_camera/pico_video.py'), '--source', source_url,
            '--host', '0.0.0.0', '--port', '13579', '--status-port', '13581'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not stopped.wait(.5):
            if process.poll() is not None:
                raise RuntimeError('PICO video bridge exited; check ports 13579 and 13581')
            try:
                with urlopen('http://127.0.0.1:13581/', timeout=.3) as response:
                    state = json.load(response)
                # The unchanged upstream bridge has an H2-specific display label.
                state['source'] = 'G1 D435i RGB, repeated for both eyes'
                emit({'type': 'pico_video', 'status': state})
            except OSError:
                continue
    except (OSError, RuntimeError) as exc:
        emit({'type': 'pico_video', 'error': str(exc)})
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def worker(args):
    password = json.loads(sys.stdin.readline()).get('password', '')
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    def owner():
        for _ in sys.stdin:
            stopped.set()
            return
        stopped.set()
    threading.Thread(target=owner, daemon=True).start()
    video = None
    if args.pico_video:
        video = threading.Thread(target=pico_video, args=(stopped, args.source_url), daemon=True)
        video.start()
    try:
        if args.simulation:
            emit({'type': 'camera_ready', 'device': 'simulation'})
            while not stopped.is_set():
                emit({'type': 'camera_frame', 'jpeg': SIMULATION_JPEG})
                stopped.wait(.1)
        else:
            receive_camera(stopped, password)
    finally:
        stopped.set()
        if video:
            video.join(timeout=6)


# A generated test pattern, never a cached robot frame.
SIMULATION_JPEG = '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zkr/AMal6v8AJnC0UUVJ1hRRRQB3Xw+/5Fnxf/15/wDsktcLXdfD7/kWfF//AF5/+yS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/yLPhD/rz/APZIq4Wu6+IP/Is+EP8Arz/9kiqlszkr/wAal6v8mcLRRRUnWFFFFAHdfD7/AJFnxf8A9ef/ALJLXC13Xw+/5Fnxf/15/wDsktcLVPZHJQ/jVfVfkgoooqTrCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAK7r4g/wDIs+EP+vP/ANkirha7r4g/8iz4Q/68/wD2SKqWzOSv/Gper/JnC0UUVJ1hRRRQB3Xw+/5Fnxf/ANef/sktcLXdfD7/AJFnxf8A9ef/ALJLXC1T2RyUP41X1X5IKKKKk6wooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACu6+IP/ACLPhD/rz/8AZIq4Wu6+IP8AyLPhD/rz/wDZIqpbM5K/8al6v8mcLRRRUnWFFFFAHdfD7/kWfF//AF5/+yS1wtd18Pv+RZ8X/wDXn/7JLXC1T2RyUP41X1X5IKKKKk6wooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACu6+IP/Is+EP8Arz/9kirha7r4g/8AIs+EP+vP/wBkiqlszkr/AMal6v8AJnC0UUVJ1hRRRQB3Xw+/5Fnxf/15/wDsktcLXdfD7/kWfF//AF5/+yS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/yLPhD/rz/APZIq4Wu6+IP/Is+EP8Arz/9kiqlszkr/wAal6v8mcLRRRUnWFFFFAHdfD7/AJFnxf8A9ef/ALJLXC13Xw+/5Fnxf/15/wDsktcLVPZHJQ/jVfVfkgoooqTrCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAK7r4g/wDIs+EP+vP/ANkirha7r4g/8iz4Q/68/wD2SKqWzOSv/Gper/JnC0UUVJ1hRRRQB3Xw+/5Fnxf/ANef/sktcLXdfD7/AJFnxf8A9ef/ALJLXC1T2RyUP41X1X5IKKKKk6woorT8LXNrZ+J9IudRXdZQ3kMk64zmMOCwx34BoA7LS/hB4hutOt73UrrSNDiuBuhXVbvyXkHsuCR9Dg1z/jbwTrfgy7hh1u2VY5xuguIn3xSjvtYfUcHB5Feq/HrwL4i8Q+Kn8TaBA+taNdwRGFrNhKYwFAwFHJBILZAI+Y153qnjTUG+H0XgrV9OybK482GecsssHJ+TaR0wzD6H2pJ3GUb/AME6jY+ANN8Xyz2h02/uDbRRK7ecGBkGWG3GP3TdCeo/Da+Hvwm13x3ok+qaPdaZDBDcNbMt1I6sWCq3G1GGMOO/rXU+Kf8Ak1fwd/2FZP8A0O6rb+DutN4c+A2taupO2016GRwOpTfaBx+Kkj8aV3YD58nhkt55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ooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACu6+IP8AyLPhD/rz/wDZIq4Wu6+IP/Is+EP+vP8A9kiqlszkr/xqXq/yZwtFFFSdYUUUUAd18Pv+RZ8X/wDXn/7JLXC13Xw+/wCRZ8X/APXn/wCyS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/wAiz4Q/68//AGSKuFruviD/AMiz4Q/68/8A2SKqWzOSv/Gper/JnC0UUVJ1hRRRQB3Xw+/5Fnxf/wBef/sktcLXdfD7/kWfF/8A15/+yS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/yLPhD/AK8//ZIq4Wu6+IP/ACLPhD/rz/8AZIqpbM5K/wDGper/ACZwtFFFSdYUUUUAd18Pv+RZ8X/9ef8A7JLXC13Xw+/5Fnxf/wBef/sktcLVPZHJQ/jVfVfkgoooqTrCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAK7r4g/8iz4Q/68/wD2SKuFruviD/yLPhD/AK8//ZIqpbM5K/8AGper/JnC0UUVJ1hRRRQB3Xw+/wCRZ8X/APXn/wCyS1wtd18Pv+RZ8X/9ef8A7JLXC1T2RyUP41X1X5IKKKKk6wooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACu6+IP8AyLPhD/rz/wDZIq4Wu6+IP/Is+EP+vP8A9kiqlszkr/xqXq/yZwtFFFSdYUUUUAd18Pv+RZ8X/wDXn/7JLXC13Xw+/wCRZ8X/APXn/wCyS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/wAiz4Q/68//AGSKuFruviD/AMiz4Q/68/8A2SKqWzOSv/Gper/JnC0UUVJ1hRRRQB3Xw+/5Fnxf/wBef/sktcLXdfD7/kWfF/8A15/+yS1wtU9kclD+NV9V+SCiiipOsKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAruviD/yLPhD/AK8//ZIq4Wu6+IP/ACLPhD/rz/8AZIqpbM5K/wDGper/ACZwtFFFSdYUUUUAdJ4P8TJ4eiv45bBb2O7CqytJtGBuyCNpyDurW/4TTRv+hQ0780/+N1wtFVzNHNPB0pyc5LV+b/zO6/4TTRv+hQ0780/+N0f8Jpo3/Qoad+af/G64WijmZP1Gj2f3v/M7r/hNNG/6FDTvzT/43R/wmmjf9Chp35p/8brhaKOZh9Ro9n97/wAzuv8AhNNG/wChQ0780/8AjdH/AAmmjf8AQoad+af/ABuuFoo5mH1Gj2f3v/M7r/hNNG/6FDTvzT/43R/wmmjf9Chp35p/8brhaKOZh9Ro9n97/wAzuv8AhNNG/wChQ0780/8AjdH/AAmmjf8AQoad+af/ABuuFoo5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'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', required=True)
    parser.add_argument('--simulation', action='store_true')
    parser.add_argument('--pico-video', action='store_true')
    parser.add_argument('--source-url', default='http://127.0.0.1:8025/api/camera.jpg')
    try:
        worker(parser.parse_args())
    except BrokenPipeError:
        pass
