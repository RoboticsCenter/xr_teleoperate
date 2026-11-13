"""Owned, read-only SSH identity/heartbeat connection for the dashboard."""
import copy
import ipaddress
import json
from pathlib import Path
import re
import shlex
import threading
import time
from network_config import robot_host

ROOT = Path(__file__).resolve().parents[1]
USER, HOST_ALIAS = 'unitree', '192.168.123.164'

# Fixed stdlib-only program: no input commands, subprocesses, DDS or controllers.
REMOTE_PROGRAM = r'''
import fcntl,json,os,pwd,socket,struct,time
def addresses():
    result=[]
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
        for _,name in socket.if_nameindex():
            try:
                raw=fcntl.ioctl(probe.fileno(),0x8915,struct.pack('256s',name.encode()[:15]))
                result.append(socket.inet_ntoa(raw[20:24]))
            except OSError:
                pass
    return sorted(set(result))
connection=os.environ.get('SSH_CONNECTION','').split()
identity={'type':'identity','hostname':socket.gethostname(),'user':pwd.getpwuid(os.getuid()).pw_name,
          'addresses':addresses(),'ssh_server_ip':connection[2] if len(connection)==4 else None,'sequence':0}
try:
    print(json.dumps(identity),flush=True)
    sequence=0
    while True:
        time.sleep(2)
        sequence+=1
        print(json.dumps({'type':'heartbeat','sequence':sequence}),flush=True)
except (BrokenPipeError,ConnectionResetError):
    pass
'''


class _Failure(Exception):
    """Only fixed, safe messages may be wrapped in this private exception."""


def failure_message(transcript, *, timed_out=False, established=False):
    if isinstance(transcript, bytes):
        transcript = transcript.decode('utf-8', errors='replace')
    text = transcript.lower() if isinstance(transcript, str) else ''
    if 'host key verification failed' in text or 'remote host identification has changed' in text:
        return 'G1 SSH identity could not be verified. Check the saved robot host key.'
    if any(value in text for value in ('permission denied','authentication failed','too many authentication failures')):
        return 'G1 SSH authentication failed. Check the robot login credential.'
    if 'no route to host' in text or 'network is unreachable' in text:
        return 'No route to G1. Check robot power, Wi-Fi and its current address.'
    if 'connection refused' in text:
        return 'G1 SSH connection refused. Wait for the robot computer to finish booting.'
    if timed_out or 'timed out' in text:
        return 'G1 SSH connection timed out. Check robot power, Wi-Fi and its current address.'
    return ('G1 SSH connection closed. Click Connect to retry.' if established else
            'G1 SSH connection ended before identity verification. Check robot power and Wi-Fi.')


def validate_identity(record, expected_host=None):
    expected_host = robot_host() if expected_host is None else expected_host
    hostname, addresses = record.get('hostname'), record.get('addresses')
    if (record.get('type') != 'identity' or record.get('user') != USER or
            record.get('ssh_server_ip') != expected_host or type(record.get('sequence')) is not int or record['sequence'] != 0 or
            not isinstance(hostname, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',hostname) or
            not isinstance(addresses,list) or not 1 <= len(addresses) <= 64):
        raise _Failure('G1 SSH returned an unexpected robot identity; connection closed.')
    try:
        if any(not isinstance(address,str) for address in addresses):
            raise ValueError('addresses must be text')
        values = sorted(set(str(ipaddress.IPv4Address(address)) for address in addresses))
    except (ValueError, TypeError):
        raise _Failure('G1 SSH returned invalid robot addresses; connection closed.') from None
    if expected_host not in values:
        raise _Failure('G1 SSH identity does not contain the expected Wi-Fi address; connection closed.')
    return hostname, values


class RobotConnection:
    def __init__(self, root=ROOT, password='', simulation=False, *, spawn=None,
                 pexpect_module=None, monotonic=time.monotonic,
                 authentication_timeout_s=8., heartbeat_timeout_s=8.):
        if not isinstance(password,str) or any(value in password for value in ('\n','\r','\0')):
            raise ValueError('Invalid SSH credential')
        self.root, self._password, self.simulation = Path(root), password, bool(simulation)
        self.host = robot_host(self.root)
        self._spawn, self._pexpect, self._clock = spawn, pexpect_module, monotonic
        self._auth_timeout, self._heartbeat_timeout = authentication_timeout_s, heartbeat_timeout_s
        self._lock = threading.RLock()
        self._attempt, self._attempts = None, []
        self._closed, self._last_seen = False, None
        self._state = {'host':self.host,'connected':False,'connecting':False,'stage':'idle',
            'detail':'Click Connect to check G1 over Wi-Fi.','error':None,'hostname':None,
            'addresses':[],'simulation':self.simulation}

    def status(self):
        with self._lock:
            value = copy.deepcopy(self._state)
            age = None if self._last_seen is None else max(0.,self._clock()-self._last_seen)
            value['last_seen_age_s'] = age
            if value['connected'] and age is not None and age > self._heartbeat_timeout:
                value.update(connected=False,connecting=False,stage='disconnected',
                    detail='G1 SSH heartbeat timed out. Click Connect to retry.',
                    error='G1 SSH heartbeat timed out. Click Connect to retry.')
            return value

    def connect(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Robot connection manager is closed')
            if self.simulation:
                self._state.update(stage='idle',detail='Simulation: no robot SSH connection opened.')
                return self.status()
            host = robot_host(self.root)
            if self._attempt is not None and self._attempt['thread'].is_alive():
                if (self._attempt['host']==host and not self._attempt['cancel'].is_set()
                        and self.status()['stage'] in ('connecting','connected')):
                    return self.status()
                self._attempt['cancel'].set()
            self._last_seen = None
            self.host = host
            self._state.update(host=host,connected=False,connecting=True,stage='connecting',
                detail='Connecting to G1 over Wi-Fi…',error=None,hostname=None,addresses=[])
            attempt = {'cancel':threading.Event(), 'process':None, 'thread':None, 'host':host}
            attempt['thread'] = threading.Thread(target=self._run,args=(attempt,),daemon=True,name='g1-readonly-ssh')
            self._attempt = attempt
            self._attempts = [item for item in self._attempts if item['thread'].is_alive()]+[attempt]
            attempt['thread'].start()
            return self.status()

    def _update(self, attempt, *, seen=False, **values):
        with self._lock:
            if self._attempt is not attempt or attempt['cancel'].is_set() or self._closed:
                return False
            self._state.update(values)
            if seen:
                self._last_seen = self._clock()
            return True

    def _run(self, attempt):
        process, established, message = None, False, None
        cancelled = attempt['cancel']
        try:
            if cancelled.is_set():
                return
            if not (self.root/'diagnostics/g1-known_hosts').is_file():
                raise _Failure('Saved G1 SSH identity file is missing; verify the robot host key first.')
            if not self._password:
                raise _Failure('G1 SSH login credential is unavailable.')
            pexpect = self._pexpect
            if pexpect is None:
                import pexpect
            command = 'python3 -u -B -c '+shlex.quote(REMOTE_PROGRAM)
            args = ['-T','-o','ConnectTimeout=5','-o','NumberOfPasswordPrompts=1',
                '-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
                '-o','ServerAliveInterval=2','-o','ServerAliveCountMax=3',
                '-o','StrictHostKeyChecking=yes','-o','HostKeyAlias='+HOST_ALIAS,
                '-o','UserKnownHostsFile='+str(self.root/'diagnostics/g1-known_hosts'),
                USER+'@'+attempt['host'],command]
            if cancelled.is_set():
                return
            process = (self._spawn or pexpect.spawn)('ssh',args,encoding='utf-8',echo=False,
                timeout=.25,maxread=4096)
            attempt['process'] = process
            process.delaybeforesend = 0
            deadline = self._clock()+self._auth_timeout
            while not cancelled.is_set():
                if self._clock() >= deadline:
                    raise _Failure(failure_message('',timed_out=True))
                try:
                    process.expect('[Pp]assword:',timeout=.25)
                    break
                except pexpect.TIMEOUT:
                    continue
                except pexpect.EOF:
                    raise _Failure(failure_message(process.before)) from None
            if cancelled.is_set():
                return
            process.sendline(self._password)
            sequence, last_seen = -1, self._clock()
            while not cancelled.is_set():
                now = self._clock()
                if not established and now >= deadline:
                    raise _Failure('G1 SSH identity response timed out. Click Connect to retry.')
                if established and now-last_seen > self._heartbeat_timeout:
                    raise _Failure('G1 SSH heartbeat timed out. Click Connect to retry.')
                try:
                    process.expect(r'\r?\n',timeout=.25)
                except pexpect.TIMEOUT:
                    continue
                except pexpect.EOF:
                    raise _Failure(failure_message(process.before,established=established)) from None
                line = process.before.strip()
                if len(line) > 8192:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError,TypeError):
                    if not established and any(token in line.lower() for token in ('permission denied','authentication failed')):
                        raise _Failure(failure_message(line)) from None
                    continue
                if not isinstance(record,dict):
                    continue
                if not established:
                    if record.get('type') != 'identity':
                        continue
                    hostname, addresses = validate_identity(record,attempt['host'])
                    established, sequence, last_seen = True, 0, self._clock()
                    self._update(attempt,seen=True,connected=True,connecting=False,stage='connected',
                        detail='G1 SSH connected; read-only heartbeat active.',error=None,
                        hostname=hostname,addresses=addresses)
                elif record.get('type') == 'heartbeat' and type(record.get('sequence')) is int and record['sequence'] > sequence:
                    sequence, last_seen = record['sequence'], self._clock()
                    self._update(attempt,seen=True)
        except _Failure as exc:
            message = str(exc)
        except Exception:
            # Third-party exception strings may include passwords and complete argv.
            message = 'G1 SSH connection failed. Check robot power, Wi-Fi and login settings.'
        finally:
            if process is not None:
                try:
                    process.close(force=True)
                except Exception:
                    pass
            if not cancelled.is_set():
                self._update(attempt,connected=False,connecting=False,
                    stage='disconnected' if established else 'error',
                    detail=message or 'G1 SSH connection closed.',error=message)

    def disconnect(self):
        with self._lock:
            for attempt in self._attempts:
                attempt['cancel'].set()
            self._state.update(connected=False,connecting=False,stage='disconnected',
                               detail='G1 SSH connection closed.',error=None)
            return self.status()

    def close(self):
        with self._lock:
            self._closed = True
            self.disconnect()
            threads = [attempt['thread'] for attempt in self._attempts]
        deadline = time.monotonic()+2
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.,deadline-time.monotonic()))
        with self._lock:
            self._password = ''
        return self.status()
