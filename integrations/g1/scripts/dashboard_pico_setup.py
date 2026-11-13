"""Read-only PICO installation page and passive discovery monitor.

Construct/status do not open sockets or start services. The PC receiver and
Tracker reader belong to the dashboard coordinator, not this helper.
"""
import copy
import errno
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from network_config import laptop_host

ROOT = Path(__file__).resolve().parents[1]
HOST = laptop_host()
PORT = 8027
DISCOVERY_PORT = 29888
APK_SHA256 = '31e8344f6c80bb80234b48817800854005a35f35d023c642a4ce36e7f9a2de32'
APK_RELATIVE = 'build/pico-two-object/package/XRoboToolkit-G1-Wrists-1.1.1-two-object.apk'
URL = f'http://{HOST}:{PORT}/'
APK_URL = URL+'XRoboToolkit-G1-Wrists-TwoObject.apk'


def advertised_ip(packet):
    """Parse the observed PCService discovery envelope, without trusting text matches."""
    if not isinstance(packet, bytes) or len(packet) < 16 or packet[:2] != b'\xcf\x7e':
        return None
    size = int.from_bytes(packet[2:6], 'little')
    if not 7 <= size <= 15 or len(packet) != 6+size+8+1 or packet[-1:] != b'\xa5':
        return None
    try:
        value = packet[6:6+size].decode('ascii')
        if socket.inet_ntop(socket.AF_INET, socket.inet_pton(socket.AF_INET, value)) != value:
            return None
        return value
    except (UnicodeDecodeError, OSError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_download(host=None):
    # No proxies, redirects, APK transfers, or receiver-port probes.
    url = f'http://{host or laptop_host()}:{PORT}/'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url+'sha256', timeout=1.5) as response:
            body = response.read(1025)
        tokens = body.decode('ascii').split()
        valid = len(body) <= 1024 and bool(tokens) and tokens[0] == APK_SHA256
        return {'running': True, 'available': valid,
                'error': None if valid else 'APK下载页校验不匹配；请检查8027端口上的服务，未替换该服务。'}
    except urllib.error.HTTPError as exc:
        return {'running': True, 'available': False,
                'error': f'8027端口服务未提供正确的/sha256（HTTP {exc.code}）；未替换该服务。'}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return {'running': False, 'available': False, 'error': '下载页暂不可达：'+str(exc)}


def port_free(host=None):
    host = host or laptop_host()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, PORT))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            if exc.errno == errno.EADDRNOTAVAIL:
                raise RuntimeError(f'本机当前没有配置的电脑地址 {host}；请核对当前网络与网络配置。') from None
            raise
    return True


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class PicoSetup:
    def __init__(self, root=ROOT, simulation=False, *, verify_page=None, free_port=None,
                 socket_factory=socket.socket, popen=subprocess.Popen, monotonic=time.monotonic,
                 apk_hash=file_hash, verify_interval_s=20., broadcast_max_age_s=15., startup_timeout_s=6., host=None):
        self.root, self.simulation = Path(root), bool(simulation)
        self.host = host or laptop_host(self.root)
        self._verify = verify_page or (lambda: verify_download(self.host))
        self._free_port = free_port or (lambda: port_free(self.host))
        self._socket_factory, self._popen, self._clock, self._apk_hash = socket_factory, popen, monotonic, apk_hash
        self._verify_interval, self._max_broadcast_age, self._startup_timeout = verify_interval_s, broadcast_max_age_s, startup_timeout_s
        self._lock = threading.RLock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._closed = False
        self._process = self._download_thread = self._observer_thread = self._observer_socket = None
        self._observe_stop = threading.Event()
        self._verified_at = self._last_seen = None
        url = f'http://{self.host}:{PORT}/'
        self._download = {'running':False,'owned':False,'url':url,'apk_url':url+'XRoboToolkit-G1-Wrists-TwoObject.apk',
                          'available':False,'error':None,'verifying':False,'pid':None}
        self._broadcast = {'monitoring':False,'ip':None,'error':None}

    def status(self):
        with self._lock:
            download = dict(self._download)
            if self._process is not None and self._process.poll() is not None:
                download.update(running=False, owned=False, available=False, pid=None,
                                error='本次启动的APK下载服务已退出；可再次点击准备。')
            now = self._clock()
            age = max(0., now-self._last_seen) if self._last_seen is not None else None
            download['verification_age_s'] = None if self._verified_at is None else max(0., now-self._verified_at)
            return copy.deepcopy({'host':self.host,'simulation':self.simulation,'download':download,
                'broadcast':dict(self._broadcast, observed=bool(self._broadcast['monitoring'] and
                    age is not None and age <= self._max_broadcast_age), age_s=age)})

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('PICO setup manager is closed')
            if self.simulation:
                # Simulation has no observed broadcast or verified real server.
                return self.status()
            self._download['verifying'] = True
            self._wake.set()
            if self._download_thread is None or not self._download_thread.is_alive():
                self._download_thread = threading.Thread(target=self._downloads, daemon=True, name='pico-apk-page')
                self._download_thread.start()
            if self._observer_thread is None or not self._observer_thread.is_alive() or self._observe_stop.is_set():
                self._observe_stop = threading.Event()
                self._last_seen = None
                self._broadcast.update(monitoring=False, ip=None, error=None)
                event = self._observe_stop
                self._observer_thread = threading.Thread(target=self._observe, args=(event,), daemon=True, name='pico-discovery-monitor')
                self._observer_thread.start()
            return self.status()

    def _cache_verification(self, result):
        with self._lock:
            owned = bool(self._process is not None and self._process.poll() is None)
            self._download.update(running=result.get('running') is True, available=result.get('available') is True,
                error=result.get('error'), verifying=False, owned=owned,
                pid=self._process.pid if owned else None)
            self._verified_at = self._clock()

    def _ensure_download(self):
        result = self._verify()
        if result.get('available') is True or self._stop.is_set():
            self._cache_verification(result)
            return
        with self._lock:
            owned_alive = self._process is not None and self._process.poll() is None
        if not owned_alive:
            if not self._free_port():
                result['error'] = result.get('error') or '8027端口被其他服务占用；未停止或替换该服务。'
                self._cache_verification(result)
                return
            apk = self.root/APK_RELATIVE
            if self._apk_hash(apk) != APK_SHA256:
                raise ValueError('本地APK校验不匹配；未启动下载服务。')
            with self._lock:
                if self._stop.is_set():
                    return
                self._process = self._popen([sys.executable,'-u','-B',str(self.root/'scripts/serve_pico_apk.py'),
                    '--apk',str(apk),'--host',self.host,'--port',str(PORT)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                self._download.update(owned=True, pid=self._process.pid, running=True)
        deadline = self._clock()+self._startup_timeout
        while not self._stop.is_set() and self._clock() < deadline:
            with self._lock:
                if self._process.poll() is not None:
                    raise RuntimeError('APK下载服务启动失败；可能8027端口已被占用，未操作其他服务。')
            result = self._verify()
            if result.get('available') is True:
                break
            self._stop.wait(.1)
        self._cache_verification(result)

    def _downloads(self):
        next_check = 0.
        while not self._stop.is_set():
            requested = self._wake.is_set()
            if requested:
                self._wake.clear()
            if requested or self._clock() >= next_check:
                try:
                    if requested:
                        self._ensure_download()
                    else:
                        self._cache_verification(self._verify())
                except Exception as exc:
                    with self._lock:
                        self._download.update(available=False, verifying=False, error=str(exc))
                next_check = self._clock()+self._verify_interval
            self._wake.wait(.25)

    def _accept_datagram(self, packet, source):
        ip = advertised_ip(packet)
        if ip != self.host or not source or source[0] != self.host:
            return False
        with self._lock:
            self._last_seen = self._clock()
            self._broadcast.update(ip=ip, error=None)
        return True

    def _observe(self, stopped):
        listener = None
        try:
            listener = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(.25)
            listener.bind(('', DISCOVERY_PORT))
            with self._lock:
                if stopped.is_set() or self._stop.is_set():
                    return
                self._observer_socket = listener
                self._broadcast.update(monitoring=True, error=None)
            while not stopped.is_set() and not self._stop.is_set():
                try:
                    packet, source = listener.recvfrom(4096)
                except socket.timeout:
                    continue
                if not stopped.is_set() and not self._stop.is_set():
                    self._accept_datagram(packet, source)
        except OSError as exc:
            if not stopped.is_set() and not self._stop.is_set():
                with self._lock:
                    self._broadcast['error'] = '无法监听UDP29888广播：'+str(exc)
        finally:
            if listener is not None:
                listener.close()
            with self._lock:
                if self._observer_socket is listener:
                    self._observer_socket = None
                    self._broadcast['monitoring'] = False

    def stop_monitor(self):
        with self._lock:
            self._observe_stop.set()
            listener, thread = self._observer_socket, self._observer_thread
            self._broadcast['monitoring'] = False
        if listener is not None:
            listener.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        return self.status()

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
            thread = self._download_thread
        self.stop_monitor()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._lock:
            process = self._process
            self._process = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            with self._lock:
                self._download.update(running=False, owned=False, available=False, pid=None, verifying=False)
        return self.status()
