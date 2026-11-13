"""Authenticated SSH transport for the bounded PC2 right-arm runtime.

No network or publishing at import/construction. Remote source runs in memory;
the caller must consume a fresh PICO packet after each received challenge.
"""
import base64
import json
from pathlib import Path
from network_config import robot_host
import shlex
import time

ROOT = Path(__file__).resolve().parents[1]


def remote_command(reference_id, config, seconds, enable_motion=False):
    if type(enable_motion) is not bool or not 0 < seconds <= 60:
        raise ValueError('invalid runtime limits')
    sources = {name: base64.b64encode((ROOT/'scripts'/filename).read_bytes()).decode()
               for name,filename in [('g1_lowcmd_session','g1_lowcmd_session.py'),
                                     ('__main__','g1_right_arm_runtime.py')]}
    # Decode data into separate module globals; shell arguments use shlex.quote.
    bootstrap = ("import base64,sys,types; sources="+repr(sources)+"; "
                 "m=types.ModuleType('g1_lowcmd_session'); "
                 "sys.modules[m.__name__]=m; "
                 "exec(compile(base64.b64decode(sources[m.__name__]),'<g1_lowcmd_session>','exec'),m.__dict__); "
                 "exec(compile(base64.b64decode(sources['__main__']),'<g1_right_arm_runtime>','exec'),"
                 "{'__name__':'__main__','__file__':'<g1_right_arm_runtime>'})")
    args = ['timeout','--signal=TERM','--kill-after=2s',str(seconds+15)+'s',
            '/home/unitree/miniconda3/envs/tv/bin/python','-u','-B','-c',bootstrap,
            '--interface','eth0','--seconds',str(seconds),'--reference-id',reference_id,
            '--config-json',json.dumps(config,allow_nan=False,separators=(',',':'))]
    if enable_motion:
        # These are the current session's already-confirmed hanging/operator
        # conditions. Ownership must be verified by the caller before this call.
        args += ['--enable-motion','--suspended-confirmed','--operator-ready',
                 '--exclusive-control-confirmed']
    return ' '.join(shlex.quote(arg) for arg in args)


class PC2ArmClient:
    def __init__(self, reference_id, config, *, seconds=10, enable_motion=False,
                 control_release_verified=False, emit=lambda record: None):
        if enable_motion and control_release_verified is not True:
            raise ValueError('current control release must be verified before motion runtime')
        self.reference_id, self.config = reference_id, config
        self.seconds, self.enable_motion = seconds, enable_motion
        self.process, self.emit, self.ready = None, emit, None

    def open(self, password):
        import pexpect
        if self.process is not None:
            raise RuntimeError('runtime already opened')
        remote = remote_command(self.reference_id,self.config,self.seconds,self.enable_motion)
        args = ['-T','-o','ConnectTimeout=5','-o','NumberOfPasswordPrompts=1',
                '-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no',
                '-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=192.168.123.164',
                '-o','UserKnownHostsFile='+str(ROOT/'diagnostics/g1-known_hosts'),
                'unitree@'+robot_host(),remote]
        self.process = pexpect.spawn('ssh',args,encoding='utf-8',echo=False,timeout=12,maxread=65536)
        self.process.delaybeforesend=0
        try:
            self.process.expect('[Pp]assword:',timeout=8)
            self.process.sendline(password)
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                item=self.read_record(max(.001,deadline-time.monotonic()))
                if item.get('type')=='runtime_ready':
                    self.ready=item
                    return item
                if item.get('type') in ('error','runtime_error','runtime_final','runtime_summary','summary'):
                    raise RuntimeError('PC2 runtime did not become ready: '+str(item))
            raise TimeoutError('runtime ready timeout')
        except BaseException:
            self.close()
            raise

    def read_record(self,timeout=1):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.process.expect(r'\r?\n',timeout=max(.001,deadline-time.monotonic()))
            try:
                item=json.loads(self.process.before.strip())
            except ValueError:
                continue
            if isinstance(item,dict) and isinstance(item.get('type'),str):
                item['_local_received_monotonic_ns']=time.monotonic_ns()
                self.emit({'type':'pc2_runtime','record':item})
                return item
        raise TimeoutError('runtime JSON timeout')

    def send(self,record):
        if self.process is None or self.ready is None:
            raise RuntimeError('runtime not ready')
        self.process.sendline(json.dumps(record,allow_nan=False,separators=(',',':')))

    def close(self, *, drain=True):
        if self.process is None:
            return
        import pexpect
        try:
            if self.process.isalive():
                self.process.sendeof()
                if drain:
                    deadline=time.monotonic()+4
                    while time.monotonic()<deadline:
                        self.read_record(max(.001,deadline-time.monotonic()))
        except (pexpect.EOF,pexpect.TIMEOUT,TimeoutError):
            pass
        finally:
            self.process.close(force=True)
            self.process=self.ready=None
