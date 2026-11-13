"""Packet freshness, stationary poses and fake HOLD/resume; no hardware IO."""
import copy
import io
import json
from pathlib import Path
import queue
import signal
import shlex
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import dashboard_worker as worker

SERIAL = 'PC2310MLL9241957G'
ARM_SDK = {'control_topic':'rt/arm_sdk','control_mode':'normal_walking'}


def snapshot(**changes):
    frame = {'version': 1, 'source': 'xrobotoolkit_sdk', 'session': 'original-session',
             'seq': 1, 'trackers': {SERIAL: [0., 1., 0., 0., 0., 0., 1.]},
             'head_pose': [0., 1.5, 0., 0., 0., 0., 1.], 'sample_mono': 100.,
             'received_mono': 100.1, 'sample_mono_on_robot': 100.,
             'pose_age_ms': {SERIAL: 0.}, 'timestamp_source': 'packet'}
    frame.update(changes)
    return frame


class SourceFrameAge(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'latest.json'

    def read_at(self, frame, now):
        self.path.write_text(json.dumps(frame))
        with patch('pico_tracker.input.time.monotonic', return_value=now):
            return worker.read_frame(self.path)

    def test_exact_half_second_is_accepted_and_older_is_held(self):
        frame = snapshot()
        self.assertEqual(self.read_at(frame, 100.5), frame)
        self.assertIsNone(self.read_at(frame, 100.500001))

    def test_recent_receipt_does_not_relabel_old_sample_as_fresh(self):
        self.assertIsNone(self.read_at(snapshot(received_mono=105.), 105.))

    def test_future_sample_is_rejected(self):
        self.assertIsNone(self.read_at(snapshot(), 99.999))

    def test_original_received_timestamp_fallback_is_supported(self):
        frame = snapshot()
        del frame['sample_mono_on_robot']
        self.assertEqual(self.read_at(frame, 100.6), frame)
        self.assertIsNone(self.read_at(frame, 100.600001))

    def test_missing_or_unparseable_frame_is_unavailable(self):
        self.assertIsNone(worker.read_frame(self.path))
        self.path.write_text('{interrupted input')
        self.assertIsNone(worker.read_frame(self.path))

    def test_same_session_fresh_frame_recovers_after_transport_gap(self):
        self.assertFalse(worker.input_live(self.read_at(snapshot(), 101.), SERIAL, 'original-session'))
        recovered = snapshot(seq=2, sample_mono=101., sample_mono_on_robot=101., received_mono=101.1)
        self.assertTrue(worker.input_live(self.read_at(recovered, 101.1), SERIAL, 'original-session'))


class BranchLivePredicate(unittest.TestCase):
    def test_raw_input_snapshot_preserves_world_pose_pairing_and_source_times(self):
        frame=snapshot(controller_sample_mono_by_side={'right':100.},head_sample_mono=100.)
        frame['head_pose_by_tracker']={SERIAL:list(frame['head_pose'])}
        before=copy.deepcopy(frame)
        raw=worker.raw_input_record(frame,{'right':SERIAL},'controllers','headset')
        self.assertEqual(raw['head_pose'],before['head_pose'])
        self.assertEqual(raw['trackers'],before['trackers'])
        self.assertEqual(raw['head_pose_by_tracker'],before['head_pose_by_tracker'])
        self.assertEqual(raw['controller_sample_mono_by_side'],{'right':100.})
        self.assertEqual(raw['arm_reference_frame'],'world')
        self.assertEqual(raw['target_reference_frame'],'headset')
        self.assertEqual(raw['serials'],{'right':SERIAL})
        expected=worker.reference_frame(frame,'headset')
        replay=worker.reference_frame(raw,raw['target_reference_frame'])
        self.assertEqual(replay['trackers'],expected['trackers'])
        frame['head_pose'][0]=99.
        frame['trackers'][SERIAL][0]=99.
        frame['head_pose_by_tracker'][SERIAL][0]=99.
        self.assertEqual(raw['head_pose'],before['head_pose'])
        self.assertEqual(raw['trackers'],before['trackers'])
        self.assertEqual(raw['head_pose_by_tracker'],before['head_pose_by_tracker'])

    def test_raw_input_snapshot_distinguishes_absent_and_empty_pair_map(self):
        frame=snapshot()
        self.assertNotIn('head_pose_by_tracker',worker.raw_input_record(frame,{'right':SERIAL},'trackers','world'))
        frame['head_pose_by_tracker']={}
        self.assertEqual(worker.raw_input_record(frame,{'right':SERIAL},'trackers','headset')['head_pose_by_tracker'],{})

    def test_stationary_pose_does_not_require_recent_motion_or_pose_age_field(self):
        for ages in ({SERIAL: 999.999}, {SERIAL: 1000.}, {SERIAL: 3_600_000.}, {}):
            with self.subTest(ages=ages):
                self.assertTrue(worker.input_live(snapshot(pose_age_ms=ages), SERIAL))
        frame = snapshot()
        del frame['pose_age_ms']
        self.assertTrue(worker.input_live(frame, SERIAL))

    def test_session_restart_holds_until_explicit_new_neutral_session(self):
        frame = snapshot(session='reconnected-producer')
        self.assertFalse(worker.input_live(frame, SERIAL, 'original-session'))
        self.assertTrue(worker.input_live(frame, SERIAL, 'reconnected-producer'))

    def test_invalid_or_missing_tracker_holds_then_valid_pose_recovers(self):
        self.assertFalse(worker.input_live(None, SERIAL, 'original-session'))
        self.assertFalse(worker.input_live(snapshot(trackers={}), SERIAL, 'original-session'))
        for invalid in ([0.]*7, [float('nan'), 0., 0., 0., 0., 0., 1.], [0.]*6):
            with self.subTest(pose=invalid):
                self.assertFalse(worker.input_live(snapshot(trackers={SERIAL: invalid}), SERIAL, 'original-session'))
        self.assertTrue(worker.input_live(snapshot(), SERIAL, 'original-session'))

    def test_no_added_jump_trigger_or_focus_gate(self):
        frame = snapshot()
        frame['trackers'][SERIAL][0] = 2.
        frame['appState'] = {'focus': False}
        frame['right_trigger'] = 0.
        self.assertTrue(worker.input_live(frame, SERIAL, 'original-session'))

    def test_unchanged_pose_remains_valid_across_new_packets(self):
        frame = snapshot(pose_age_ms={SERIAL: 3_600_000.})
        self.assertTrue(worker.input_live(frame, SERIAL, 'original-session'))
        updated = copy.deepcopy(frame)
        updated['seq'] += 1
        updated['pose_age_ms'][SERIAL] += 1000.
        self.assertTrue(worker.input_live(updated, SERIAL, 'original-session'))
        self.assertEqual(frame['trackers'], updated['trackers'])


class SSHHandshakeErrors(unittest.TestCase):
    def test_known_failures_are_concise_and_never_serialize_pexpect_state(self):
        class EOF(Exception):
            def __str__(self): raise AssertionError('Never stringify pexpect errors')
        class TIMEOUT(Exception):
            def __str__(self): raise AssertionError('Never stringify pexpect errors')
        cases = [
            ('ssh: No route to host', EOF, 'no route'),
            ('ssh: Network is unreachable', EOF, 'no route'),
            ('ssh: Connection timed out', EOF, 'timed out'),
            ('', TIMEOUT, 'timed out'),
            ('ssh: Connection refused', EOF, 'refused'),
            ('Permission denied (password)', EOF, 'authentication failed'),
            ('Host key verification failed', EOF, 'identity could not be verified'),
            ('REMOTE HOST IDENTIFICATION HAS CHANGED', EOF, 'identity could not be verified'),
            ('unrecognized diagnostic', EOF, 'ended before authentication'),
        ]
        for transcript, error_type, expected in cases:
            with self.subTest(expected=expected, transcript=transcript):
                process = types.SimpleNamespace(before=transcript+' SECRET-PASSWORD PRIVATE-BOOTSTRAP',
                    closed=False)
                def expect(*args, **kwargs): raise error_type('SECRET-ARGV-'+'x'*100000)
                def close(force=False): process.closed=force
                process.expect, process.close = expect, close
                process.sendline = lambda *_: self.fail('Failed handshake must not send a password')
                fake = types.SimpleNamespace(EOF=EOF, TIMEOUT=TIMEOUT, spawn=lambda *a, **k: process)
                log = io.StringIO()
                with patch.dict(sys.modules, {'pexpect':fake}), patch.object(worker.threading,'Thread') as thread:
                    with self.assertRaises(RuntimeError) as raised:
                        worker.RemoteArm('SECRET-PASSWORD',log)
                    thread.assert_not_called()
                message=str(raised.exception)
                self.assertIn(expected,message)
                self.assertLess(len(message),180)
                self.assertNotIn('SECRET',message)
                self.assertNotIn('BOOTSTRAP',message)
                self.assertEqual(log.getvalue(),'')
                self.assertTrue(process.closed)

    def test_classifier_accepts_bytes_but_never_relays_unknown_text(self):
        self.assertIn('no route',worker.ssh_handshake_error(b'No route to host'))
        self.assertNotIn('sensitive',worker.ssh_handshake_error('sensitive remote output'))
        self.assertIn('ended',worker.ssh_handshake_error(None))

    def test_interrupt_still_cleans_up_and_propagates(self):
        class EOF(Exception): pass
        class TIMEOUT(Exception): pass
        process=types.SimpleNamespace(before='',closed=False)
        def expect(*args,**kwargs):raise KeyboardInterrupt()
        def close(force=False):process.closed=force
        process.expect,process.close=expect,close
        fake=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT,spawn=lambda *a,**k:process)
        with patch.dict(sys.modules,{'pexpect':fake}):
            with self.assertRaises(KeyboardInterrupt):worker.RemoteArm('unused',io.StringIO())
        self.assertTrue(process.closed)


class ArmSDKProtocol(unittest.TestCase):
    def arm(self,verified=False):
        arm=worker.RemoteArm.__new__(worker.RemoteArm)
        arm.control_verified=verified
        arm.latest=dict(ARM_SDK,q=[0.]*14,dq=[0.]*14,arm_weight=0.)
        arm.records=queue.Queue();arm.error=arm.final=None
        arm._lock=threading.Lock()
        arm.sent=[];arm.closed=False
        def close(force=False):arm.closed=True
        arm.process=types.SimpleNamespace(isalive=lambda:not arm.closed,
            sendline=lambda line:arm.sent.append(json.loads(line)),close=close)
        return arm

    def test_startup_stderr_survives_closed_notification(self):
        class EOF(Exception):pass
        class TIMEOUT(Exception):pass
        arm=self.arm();arm.log=io.StringIO();arm.startup_failure=None
        arm.pexpect=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT)
        lines=iter(['usage: pc2_branch_arm.py ...',
            'pc2_branch_arm.py: error: unrecognized arguments: --control-mode normal_walking'])
        def expect(*args,**kwargs):
            try:arm.process.before=next(lines)
            except StopIteration:raise EOF()
        arm.process.expect=expect
        arm._read()
        with self.assertRaisesRegex(RuntimeError,'版本不匹配'):arm.ready()
        self.assertFalse(arm.control_verified)
        self.assertEqual(arm.sent,[])

    def test_closed_without_stderr_has_actionable_message(self):
        arm=self.arm();arm.records.put({'type':'closed'})
        with self.assertRaisesRegex(RuntimeError,'就绪前退出.*runtime.log'):arm.ready()
        self.assertEqual(arm.sent,[])

    def test_missing_debug_or_changed_mode_is_rejected_before_any_command(self):
        for record in ({},dict(ARM_SDK,control_topic='rt/lowcmd'),
                       dict(ARM_SDK,control_mode='debug')):
            with self.subTest(record=record):
                arm=self.arm();arm.records.put(dict(record,type='ready'))
                with self.assertRaisesRegex(RuntimeError,'normal walking'):arm.ready()
                with self.assertRaises(RuntimeError):arm.send({'command':'start'})
                arm.close()
                self.assertEqual(arm.sent,[])
                self.assertTrue(arm.closed)

    def test_verified_start_waits_for_remote_ack_and_stop_cancels_wait(self):
        arm=self.arm();arm.records.put(dict(ARM_SDK,type='ready',arm_weight=0.))
        arm.ready()
        stopping=threading.Event();done=threading.Event();result=[]
        def start():
            result.append(arm.start(stopping));done.set()
        thread=threading.Thread(target=start);thread.start()
        self.assertFalse(done.wait(.03))
        self.assertEqual(arm.sent,[{'command':'start'}])
        arm.records.put({'type':'state','stage':'following'})
        self.assertTrue(done.wait(1));thread.join()
        self.assertEqual(result,[True])
        arm=self.arm(verified=True);stopping=threading.Event()
        thread=threading.Thread(target=lambda:result.append(arm.start(stopping)))
        thread.start();stopping.set();thread.join(.5)
        self.assertFalse(thread.is_alive())
        self.assertIs(result[-1],False)
        self.assertFalse(any(x.get('command')=='target' for x in arm.sent))

    def test_feedback_protocol_switch_revokes_command_authorization(self):
        class EOF(Exception):pass
        class TIMEOUT(Exception):pass
        arm=self.arm(verified=True);arm.log=io.StringIO()
        arm.pexpect=types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT)
        lines=iter([json.dumps(dict(ARM_SDK,type='feedback',control_topic='rt/lowcmd'))])
        def expect(*args,**kwargs):
            try:arm.process.before=next(lines)
            except StopIteration:raise EOF()
        arm.process.expect=expect
        arm._read()
        self.assertFalse(arm.control_verified)
        self.assertIn('normal walking',arm.error['error'])
        with self.assertRaises(RuntimeError):arm.send({'command':'target','q':[0.]*14})
        self.assertEqual(arm.sent,[])

    def test_bootstrap_requires_mode_flag_before_old_adapter_can_initialize(self):
        class EOF(Exception):pass
        class TIMEOUT(Exception):pass
        calls=[]
        def spawn(command,args,**kwargs):
            calls.append((command,args))
            def expect(*a,**k):raise EOF()
            return types.SimpleNamespace(expect=expect,before='',close=lambda **k:None)
        with patch.dict(sys.modules,{'pexpect':types.SimpleNamespace(EOF=EOF,TIMEOUT=TIMEOUT,spawn=spawn)}), \
                patch.object(worker,'robot_host',return_value='10.1.10.73') as configured:
            with self.assertRaises(RuntimeError):worker.RemoteArm('SECRET',io.StringIO())
        configured.assert_called_once_with(worker.ROOT)
        self.assertIn('unitree@10.1.10.73',calls[0][1])
        self.assertIn('StrictHostKeyChecking=yes',calls[0][1])
        self.assertIn('HostKeyAlias=192.168.123.164',calls[0][1])
        bootstrap=shlex.split(calls[0][1][-1])[-1]
        self.assertIn("'--control-mode','normal_walking'",bootstrap)
        self.assertNotIn('SECRET',str(calls))

    def test_launch_requires_explicit_zero_arm_weight(self):
        for weight in (None,True,.01,1.,'0'):
            with self.subTest(weight=weight):
                arm=self.arm();arm.records.put(dict(ARM_SDK,type='ready',arm_weight=weight))
                with self.assertRaisesRegex(RuntimeError,'zero arm_sdk weight'):arm.ready()
                self.assertFalse(arm.control_verified)
                self.assertEqual(arm.sent,[])


class FakeWorkerIntegration(unittest.TestCase):
    def test_launch_without_wearable_input_and_start_waits_without_exiting(self):
        commands, records, sent = queue.Queue(), [], []
        class Incoming:
            def __iter__(self):
                while True:
                    command = commands.get()
                    if command is None: return
                    yield json.dumps({'command': command}) + '\n'
        class Output(io.StringIO):
            def write(self, text):
                if text.strip():
                    record = json.loads(text)
                    records.append(record)
                    if record.get('stage') == 'ready':
                        if record.get('start_requested') is False:
                            commands.put('stop'); commands.put(None)
                        else:
                            commands.put('start')
                return super().write(text)
        class Child:
            def __init__(self, argv, log): pass
            def get(self, timeout=30): return {'type': 'ready'}
            def request(self, record): raise AssertionError('No input: no FK/IK request expected')
            def close(self): pass
        class Arm:
            error = final = None
            latest = dict(ARM_SDK,q=[0.]*14,dq=[0.]*14,actual_write_count=None,arm_weight=0.)
            process = types.SimpleNamespace(isalive=lambda: True)
            def __init__(self, password, log): pass
            def ready(self): return self.latest
            def send(self, record): sent.append(record)
            def close(self): return dict(ARM_SDK,completed=True,last_action_unknown=True,arm_weight_released=True)
        guard = threading.Timer(3, lambda: (commands.put('stop'), commands.put(None)))
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        guard.start()
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(worker, 'JsonChild', Child), patch.object(worker, 'RemoteArm', Arm), \
                    patch.object(worker, 'read_frame', return_value=None), \
                    patch.object(worker.sys, 'stdin', Incoming()), patch.object(worker.sys, 'stdout', Output()):
                worker.run(Path(directory), 'unused', options={
                    'tracker_frame_path': '/fake/missing',
                    'tracker_config': {'left_sn': '', 'right_sn': SERIAL,
                        'offset_tracker_m': [0., 0., 0.],
                        'right_offset_tracker_m': [0., 0., 0.]}})
                summary = json.loads((Path(directory) / 'summary.json').read_text())
        finally:
            guard.cancel(); commands.put(None)
            for sig, handler in handlers.items(): signal.signal(sig, handler)
        ready = [r for r in records if r.get('stage') == 'ready']
        self.assertGreaterEqual(len(ready), 2)
        self.assertTrue(any(r.get('start_requested') is False and r.get('error') is None for r in ready))
        self.assertFalse(any(r.get('stage') == 'error' for r in records))
        self.assertEqual(sent, [])
        self.assertEqual(summary['accepted_targets'], 0)
        self.assertTrue(summary['completed'])

    def test_follow_hold_same_session_resume_and_stop(self):
        self._exercise_follow_hold('trackers')

    def test_controller_capture_to_target_and_stale_source_hold(self):
        self._exercise_follow_hold('controllers')

    def test_headset_controller_head_loss_holds_and_valid_head_resumes(self):
        self._exercise_follow_hold('controllers', reference='headset')

    def _exercise_follow_hold(self,source,reference='world'):
        commands, frames = queue.Queue(), queue.Queue()
        records, sent, children = [], [], []
        options={'arm_input_source':source,'arm_reference_frame':reference,
                 'tracker_config':{'right_sn':SERIAL}}
        if source=='controllers':
            options['tracker_config']['arm_calibration']={'schema':'g1-arm-calibration-v1','sides':{
                'right':{'serial':'pico-controller-right','rotation_from_heading':
                    [[1.,0.,0.],[0.,1.,0.],[0.,0.,1.]],'translation_scale':2.}}}
            if reference=='headset':
                saved=options['tracker_config']['arm_calibration']
                saved['arm_reference_frame']='headset'
                saved['arm_reference_mapping']=worker.reference_mapping('headset')
                saved['sides']['right']['capture_binding']={
                    'serial':'pico-controller-right','offset_tracker_m':[0.,0.,0.],
                    'arm_reference_frame':'headset',
                    'arm_reference_mapping':worker.reference_mapping('headset')}
        def put_frame(seq, sample_age):
            now=time.monotonic()
            frame=snapshot(seq=seq,sample_mono=now-sample_age,pose_age_ms={SERIAL:3_600_000.})
            if source=='controllers':
                if reference=='headset': sample_age=0.  # Head loss alone causes HOLD.
                frame.update(sample_mono=now,tracker_sample_mono=now,
                    controllers={'right':[.05 if seq==3 else 0.,1.,0.,0.,0.,0.,1.]},
                    controller_sample_mono=now-sample_age,
                    controller_sample_mono_by_side={'right':now-sample_age})
                if reference=='headset':
                    if seq==2:frame['head_pose']=None
                    if seq==3:
                        # A common body translation must not contaminate the
                        # controller's .05 m relative displacement.
                        for pose in (frame['head_pose'],frame['controllers']['right']):
                            pose[:3]=[pose[i]+[1.,.3,-.4][i] for i in range(3)]
                if frame['head_pose'] is not None:
                    frame['controller_head_poses']={'right':list(frame['head_pose'])}
            frames.put(frame)
        put_frame(1, 0.)
        class Incoming:
            def __iter__(self):
                while True:
                    line = commands.get()
                    if line is None: return
                    yield json.dumps({'command': line})+'\n'
        class Output(io.StringIO):
            pending = ''
            def write(self, text):
                result = super().write(text)
                self.pending += text
                while '\n' in self.pending:
                    line, self.pending = self.pending.split('\n', 1)
                    record = json.loads(line)
                    records.append(record)
                    if record.get('stage') == 'ready': commands.put('start')
                    if record.get('stage') == 'holding': put_frame(3, 0.)
                return result
        identity = [[1.,0.,0.,0.], [0.,1.,0.,0.], [0.,0.,1.,0.], [0.,0.,0.,1.]]
        class Child:
            def __init__(self, argv, log):
                self.is_tracker = 'pico_arm_stream.py' in argv[-1]
                self.closed = False
                self.process = types.SimpleNamespace(poll=lambda: 0 if self.closed else None)
                children.append(self)
            def get(self, timeout=30):
                return frames.get(timeout=timeout) if self.is_tracker else {'type': 'ready'}
            def request(self, record):
                if record['command'] == 'fk': return {'type': 'fk', 'left': identity, 'right': identity}
                return {'type': 'target', 'q': [0.]*14, 'tau': [0.]*14}
            def close(self): self.closed = True
        class Arm:
            def __init__(self, password, log):
                self.error = None
                self.final = None
                self.closed = False
                self.process = types.SimpleNamespace(isalive=lambda: not self.closed)
                self.latest = dict(ARM_SDK,type='ready',q=[0.]*14,dq=[0.]*14,actual_write_count=None,arm_weight=0.)
            def ready(self): return self.latest
            def start(self, stopping):
                self.send({'command':'start'})
                return not stopping.is_set()
            def send(self, record):
                sent.append(record)
                count = sum(item['command'] == 'target' for item in sent)
                if record['command'] == 'target' and count == 1: put_frame(2, 1.)
                if record['command'] == 'target' and count == 2:
                    commands.put('stop'); commands.put(None)
            def close(self):
                self.closed = True
                return dict(ARM_SDK,type='final',completed=True,actual_write_count=None,
                        last_action_unknown=True,physical_stop_verified=False,arm_weight_released=True)
        # The guard only prevents a broken fake from hanging the test process.
        guard = threading.Timer(5, lambda: (commands.put('stop'), commands.put(None)))
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        guard.start()
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(worker, 'JsonChild', Child), patch.object(worker, 'RemoteArm', Arm), \
                    patch.object(worker.sys, 'stdin', Incoming()), patch.object(worker.sys, 'stdout', Output()):
                worker.run(Path(directory), 'unused-fake-password',options=options)
                summary = json.loads((Path(directory)/'summary.json').read_text())
                recorded=[json.loads(line) for line in (Path(directory)/'targets.jsonl').read_text().splitlines()]
        finally:
            guard.cancel()
            commands.put(None)
            for sig, handler in handlers.items(): signal.signal(sig, handler)
        stages = [r['stage'] for r in records if r.get('type') == 'state']
        self.assertIn('holding', stages, records)
        held = stages.index('holding')
        self.assertIn('following', stages[:held])
        self.assertIn('following', stages[held+1:])
        self.assertEqual(sum(item['command'] == 'target' for item in sent), 2)
        self.assertEqual(summary['accepted_targets'], 2)
        self.assertTrue(summary['completed'])
        self.assertTrue(summary['last_action_unknown'])
        self.assertFalse(summary['physical_stop_verified'])
        self.assertEqual(summary['control_mode'],'normal_walking')
        self.assertEqual(summary['control_topic'],'rt/arm_sdk')
        self.assertEqual(summary['stop_behavior'],'release_arm_weight')
        self.assertTrue(summary['arm_weight_released'])
        self.assertEqual(summary['arm_input_source'],source)
        self.assertEqual(summary['arm_reference_frame'],reference)
        self.assertEqual(recorded[0]['arm_input_source'],source)
        self.assertEqual(recorded[0]['arm_reference_frame'],reference)
        for row in recorded:
            raw=row['raw_input']
            self.assertEqual(raw['arm_reference_frame'],'world')
            self.assertEqual(raw['target_reference_frame'],reference)
            self.assertEqual(raw['arm_input_source'],source)
            replay=worker.reference_frame(raw,reference)
            self.assertEqual(replay['trackers'][recorded[0]['serials']['right']],row['trackers']['right'])
            if 'seq' in row:self.assertEqual(raw['seq'],row['seq'])
        if reference=='headset':
            self.assertEqual(recorded[0]['raw_input']['head_pose'],[0.,1.5,0.,0.,0.,0.,1.])
            self.assertEqual(recorded[-1]['raw_input']['head_pose'][:3],[1.,1.8,-.4])
            self.assertEqual(recorded[-1]['head_pose'],[0.,0.,0.,0.,0.,0.,1.])
        if source=='controllers':
            self.assertEqual(recorded[0]['serials'],{'right':'pico-controller-right'})
            self.assertEqual(recorded[0]['arm_calibration']['sides']['right']['translation_scale'],2.)
            self.assertAlmostEqual(recorded[-1]['wrist_targets']['right'][1][3],-.1)
        self.assertTrue(all(child.closed for child in children))


if __name__ == '__main__':
    unittest.main()
