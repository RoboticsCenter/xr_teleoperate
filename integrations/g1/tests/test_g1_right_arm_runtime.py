import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import g1_lowcmd_session as g1
import g1_right_arm_runtime as runner


def config(supervised=False, tracker=False):
    return g1.Config(right_kp=(10.,)*7, right_kd=(1.,)*7, nonright_kd=(3.,)*22,
                     calibration_id='cal', enable_source='authorized_ten_second_window' if tracker else 'trigger', gain_review_id='fake-only',
                     input_policy_id=('supervised_tracker_window_v1' if tracker else
                                      'supervised_position_only_v1' if supervised else 'vendor_tracking'),
                     max_session_s=10 if supervised or tracker else 30, max_command_hz=200)


class Clock:
    def __init__(self): self.now=100.
    def __call__(self): return self.now
    def advance(self, seconds=.005): self.now+=seconds


class Source:
    def __init__(self, clock):
        self.clock, self.sequence, self.previous = clock, 0, None
        self.frozen = self.error = False
        self.cached = None
    def snapshot(self):
        if self.error: raise ValueError('bad_callback_crc')
        if self.cached is not None and self.frozen: return copy.deepcopy(self.cached)
        if self.clock()!=self.previous: self.sequence+=1;self.previous=self.clock()
        self.cached=dict(received_t=self.clock(),sequence=self.sequence,tick=self.sequence,
                         q=[0.]*22+[.1]*7,dq=[0.]*29,motorstate=[0]*29,
                         crc_valid=True,mode_machine=5,mode_pr=0)
        return copy.deepcopy(self.cached)


class Publisher:
    def __init__(self): self.messages=[];self.closed=False;self.success=True
    def Write(self, msg): self.messages.append(copy.deepcopy(msg));return self.success
    def Close(self): self.closed=True


class CRC:
    def Crc(self, msg): return 1234


def factory():
    return SimpleNamespace(motor_cmd=[SimpleNamespace() for _ in range(35)])


def response(challenge, *, held=True, source_seq=None, supervised=False, tracker=False):
    seq=challenge['sequence'] if source_seq is None else source_seq
    out = dict(type='target',session_id=challenge['session_id'],nonce=challenge['nonce'],
                sequence=challenge['sequence'],input_sequence=seq,enable_sequence=seq,
                generated_after_challenge=True,q=[.1]*7,tracking_valid=None if supervised else True,
                held=held,calibration_id='cal',reference_id='fixed-ref',enable_source='trigger',
                input_policy_id='supervised_position_only_v1' if supervised else 'vendor_tracking',
                supervised_input_accepted=True if supervised else False)
    if tracker:
        for field in ('held','enable_sequence','enable_source'):del out[field]
        out.update(type='tracker_target',input_policy_id='supervised_tracker_window_v1',
                   tracking_valid=None,supervised_input_accepted=True)
    return out


class RuntimeTests(unittest.TestCase):
    def setup_runtime(self, *, motion=True, name='', supervised=False, seconds=30, confirmed=True,
                      snapshot_advance=0., tracker=False, session_limit=None):
        self.clock=Clock();self.source=Source(self.clock);self.events=[];self.pub=Publisher();self.creations=[]
        if snapshot_advance:
            original=self.source.snapshot
            def updated_snapshot():
                self.clock.advance(snapshot_advance)
                return original()
            self.source.snapshot=updated_snapshot
        def transport(interface):
            self.creations.append(interface);return self.pub,factory,CRC()
        cfg=config(supervised,tracker)
        if session_limit is not None:cfg=replace(cfg,max_session_s=session_limit)
        self.session=g1.LowCmdSession(cfg,enable_motion=motion,interface='fake0',
                                      transport_factory=transport,clock=self.clock)
        readiness=dict(received_t=self.clock(),check_mode_code=0,motion_name=name,raw={'name':name},
                       suspended=confirmed,operator_ready=confirmed,exclusive_control_confirmed=confirmed)
        self.runtime=runner.Runtime(self.session,self.source,readiness,self.events.append,
                                    reference_id='fixed-ref',seconds=seconds,clock=self.clock)
        self.runtime.start()
        return self.runtime

    def challenge(self): return next(x for x in reversed(self.events) if x['type']=='challenge')

    def reply(self, *, held=True, supervised=False, auto_prepare=True, **changes):
        packet=response(self.challenge(),held=held,supervised=supervised,
                        tracker=self.session.config.input_policy_id=='supervised_tracker_window_v1');packet.update(changes)
        self.clock.advance();self.runtime.tick([json.dumps(packet)])
        if (auto_prepare and not self.runtime.done and self.session.prepared_t is not None and
                self.runtime.target is None and self.session.state=='DISABLED'):
            return self.reply(held=held,supervised=supervised,auto_prepare=False,**changes)
        return packet

    def test_wait_rotates_expired_unanswered_nonce_and_remains_bounded(self):
        r=self.setup_runtime(seconds=.2);old=self.challenge()
        for _ in range(20):self.clock.advance();r.tick()
        self.assertFalse(r.done);self.assertNotEqual(old['nonce'],self.challenge()['nonce'])
        self.assertEqual(self.pub.messages,[])
        for _ in range(21):self.clock.advance();r.tick()
        self.assertTrue(r.done);self.assertEqual(r.final['reason'],'runtime_deadline')

    def test_held_false_waits_then_rise_starts_and_release_damps_once(self):
        r=self.setup_runtime();self.reply(held=False)
        self.assertEqual(self.session.state,'DISABLED');self.assertEqual(self.creations,[])
        self.reply();self.assertEqual(self.session.state,'ACTIVE')
        first=self.pub.messages[0]
        self.assertEqual(first.motor_cmd[25].q,.1);self.assertEqual(first.motor_cmd[25].kp,0.)
        self.reply(held=False)
        self.assertEqual(r.final['reason'],'trigger_released')
        self.assertTrue(r.final['damping']['published']);count=len(self.pub.messages)
        r.finish('another_close');self.assertEqual(len(self.pub.messages),count)
        self.assertTrue(all(m.kp==0 and m.kd>0 for m in self.pub.messages[-1].motor_cmd[:29]))

    def test_target_age_is_challenge_issue_not_arrival(self):
        self.setup_runtime();self.reply();ch=self.challenge()
        issued=self.runtime.protocol.pending['issued_t'];self.clock.advance(.04)
        packet=response(ch);self.runtime.tick([json.dumps(packet)])
        self.assertEqual(self.runtime.target['received_t'],issued)
        self.assertAlmostEqual(issued,ch['issued_monotonic_ns']/1e9,places=8)
        self.assertGreaterEqual(self.clock()-self.runtime.target['received_t'],.0399)

    def test_official_200ms_preparation_never_writes_and_requires_fresh_challenge(self):
        r=self.setup_runtime()
        original=self.session.transport_factory
        def delayed(interface):
            self.clock.advance(.2)
            return original(interface)
        self.session.transport_factory=delayed
        old=self.reply(auto_prepare=False)
        self.assertFalse(r.done);self.assertEqual(self.pub.messages,[])
        self.assertEqual(self.session.state,'DISABLED');self.assertIsNone(self.session.reference)
        self.assertIsNone(r.target);self.assertGreaterEqual(self.challenge()['issued_monotonic_ns']/1e9,
                                                          self.session.prepared_t)
        self.reply();self.assertEqual(self.session.state,'ACTIVE')
        self.assertEqual(len(self.creations),1);self.assertEqual(len(self.pub.messages),1)
        self.assertEqual(self.pub.messages[0].motor_cmd[22].q,.1)

    def test_eof_after_preparation_closes_without_damping_or_motion(self):
        r=self.setup_runtime();self.reply(auto_prepare=False)
        self.clock.advance();r.tick(eof=True)
        self.assertEqual(r.final['actual_write_count'],0);self.assertIsNone(r.final['damping'])
        self.assertTrue(self.pub.closed)

    def test_tracker_window_has_no_human_held_field_and_expires_once(self):
        r=self.setup_runtime(tracker=True,session_limit=.05)
        first=self.reply()
        self.assertEqual(first['type'],'tracker_target')
        self.assertNotIn('held',first);self.assertNotIn('enable_sequence',first)
        self.assertNotIn('held',r.enable);self.assertTrue(r.enable['window_active'])
        start=r.tracker_window_started
        for _ in range(12):
            if not r.done:self.reply()
        self.assertEqual(r.final['reason'],'authorized_window_deadline')
        self.assertEqual(r.tracker_window_started,start)
        self.assertTrue(r.final['damping']['published'])
        self.assertEqual(r.final['right_anchor_q'],[.1]*7)
        self.assertEqual(r.final['right_measured_q'],[.1]*7)
        self.assertIsNotNone(r.final['feedback_tick'])
        self.assertFalse(r.final['physical_stop_verified'])

    def test_tracker_window_counts_preparation_time_and_cannot_rearm(self):
        r=self.setup_runtime(tracker=True,session_limit=.1)
        original=self.session.transport_factory
        def delayed(interface):
            self.clock.advance(.2)
            return original(interface)
        self.session.transport_factory=delayed
        self.reply(auto_prepare=False)
        self.assertEqual(self.pub.messages,[])
        self.reply(auto_prepare=False)
        self.assertEqual(r.final['reason'],'authorized_window_deadline')
        self.assertEqual(self.pub.messages,[])

    def test_tracker_target_strict_schema_and_explicit_acceptance(self):
        for changes in (dict(held=True),dict(enable_sequence=1),dict(enable_source='trigger'),
                        dict(tracking_valid=False),dict(tracking_valid=True),
                        dict(supervised_input_accepted=False),dict(type='target')):
            r=self.setup_runtime(tracker=True)
            packet=response(self.challenge(),tracker=True);packet.update(changes)
            self.clock.advance();r.tick([json.dumps(packet)])
            self.assertTrue(r.done);self.assertEqual(self.creations,[])
        r=self.setup_runtime();self.reply(type='tracker_target')
        self.assertTrue(r.done);self.assertEqual(self.creations,[])

    def test_tracker_configuration_requires_named_local_window_and_ten_second_limit(self):
        for changes in (dict(max_session_s=10.01),dict(enable_source='trigger')):
            with self.assertRaises(ValueError):replace(config(tracker=True),**changes)

    def test_tracker_eof_after_motion_has_exactly_one_explicit_damping(self):
        r=self.setup_runtime(tracker=True);self.reply();before=len(self.pub.messages)
        self.clock.advance();r.tick(eof=True);r.finish('again')
        self.assertEqual(len(self.pub.messages),before+1)
        self.assertTrue(r.final['damping']['published'])

    def test_callback_advancing_clock_during_snapshot_is_not_future_state(self):
        # Exercise startup with callback racing every snapshot; the new clock
        # sample must follow the returned feedback, never rewrite its timestamp.
        r=self.setup_runtime(snapshot_advance=.0002)
        self.reply();self.assertEqual(self.session.state,'ACTIVE')
        self.assertFalse(r.done)
        self.clock.advance();r.tick(eof=True)
        self.assertTrue(r.final['damping']['published'])
        self.assertFalse(r.final['last_action_unknown'])

    def test_snapshot_delay_still_counts_against_target_age(self):
        r=self.setup_runtime();original=self.source.snapshot
        def delayed_snapshot():
            self.clock.advance(.11)
            return original()
        self.source.snapshot=delayed_snapshot
        self.reply()
        self.assertTrue(r.done);self.assertEqual(self.creations,[])

    def test_replay_and_remote_clock_field_fail_without_new_targets(self):
        r=self.setup_runtime();packet=self.reply();count=len(self.pub.messages)
        self.clock.advance();r.tick([json.dumps(packet)])
        self.assertTrue(r.done);self.assertEqual(len(self.pub.messages),count+1)  # damping only
        r=self.setup_runtime();self.reply(received_t=999999)
        self.assertTrue(r.done);self.assertEqual(self.pub.messages,[])

    def test_wait_old_nonce_response_rejected(self):
        r=self.setup_runtime();old=response(self.challenge())
        for _ in range(18):self.clock.advance();r.tick()
        self.clock.advance();r.tick([json.dumps(old)])
        self.assertTrue(r.done);self.assertEqual(self.pub.messages,[])

    def test_no_response_in_active_ends_and_damps(self):
        r=self.setup_runtime();self.reply()
        for _ in range(18):self.clock.advance();r.tick()
        self.assertTrue(r.done);self.assertIn('nonce_response_deadline',r.final['reason'])
        self.assertTrue(r.final['damping']['published'])

    def test_late_loop_cannot_be_rescued_by_fresh_packet(self):
        r=self.setup_runtime();self.reply();p=response(self.challenge())
        self.clock.advance(.1);r.tick([json.dumps(p)])
        self.assertIn('runtime_loop_deadline',r.final['reason'])
        self.assertTrue(r.final['damping']['published'])

    def test_eof_signal_deadline_client_stop_share_explicit_shutdown(self):
        for kind in ('eof','signal','deadline','client_stop'):
            with self.subTest(kind=kind):
                r=self.setup_runtime(seconds=.03);self.reply();self.clock.advance()
                if kind=='eof':r.tick(eof=True)
                elif kind=='signal':r.tick(stop_reason='signal:15')
                elif kind=='client_stop':r.tick([json.dumps({'type':'stop','session_id':r.protocol.session_id})])
                else:
                    for _ in range(7):self.clock.advance();r.tick()
                self.assertTrue(r.done);self.assertTrue(r.final['damping']['published'])
                self.assertFalse(r.final['physical_stop_verified']);self.assertTrue(self.pub.closed)

    def test_stale_feedback_suppresses_damping_and_marks_unknown(self):
        r=self.setup_runtime();self.reply();self.source.frozen=True
        self.clock.advance(.11);r.tick()
        self.assertTrue(r.done);self.assertFalse(r.final['damping']['published'])
        self.assertTrue(r.final['last_action_unknown'])

    def test_callback_error_not_hidden_by_output_loop(self):
        r=self.setup_runtime();self.reply();self.source.error=True
        self.clock.advance();r.tick()
        self.assertTrue(r.done);self.assertTrue(r.final['last_action_unknown'])
        self.assertEqual(len(self.pub.messages),1)

    def test_preview_with_ai_validates_protocol_but_never_fakes_readiness(self):
        r=self.setup_runtime(motion=False,name='ai',confirmed=False)
        self.reply();self.assertFalse(r.done);self.assertEqual(self.creations,[])
        self.assertTrue(any(e.get('reason')=='motion_service_not_inactive' for e in self.events))
        self.assertEqual(r.readiness['motion_name'],'ai')

    def test_motion_with_ai_does_not_construct_publisher(self):
        r=self.setup_runtime(name='ai');self.reply()
        self.assertTrue(r.done);self.assertEqual(self.creations,[])

    def test_stale_readiness_refresh_discards_target_and_demands_new_nonce(self):
        r=self.setup_runtime();r.readiness['received_t']=99.
        calls=[]
        def query():
            calls.append(True);self.clock.advance(.2)
            return dict(received_t=self.clock(),check_mode_code=0,motion_name='',raw={'name':''})
        r.mode_query=query
        self.reply();self.assertEqual(calls,[True]);self.assertEqual(self.pub.messages,[])
        self.assertIsNone(r.target)
        self.reply();self.assertTrue(len(self.pub.messages)>0)

    def test_source_sequences_must_advance_after_each_nonce(self):
        r=self.setup_runtime();first=self.reply(held=False)
        self.reply(input_sequence=first['input_sequence'])
        self.assertTrue(r.done);self.assertEqual(self.pub.messages,[])

    def test_supervised_unknown_is_explicit_and_invalid_still_rejected(self):
        r=self.setup_runtime(supervised=True);self.reply(supervised=True)
        self.assertEqual(self.session.state,'ACTIVE')
        self.assertIsNone(r.target['tracking_valid'])
        r=self.setup_runtime(supervised=True);self.reply(supervised=True,tracking_valid=False)
        self.assertTrue(r.done);self.assertEqual(self.creations,[])
        r=self.setup_runtime();self.reply(tracking_valid=None)
        self.assertTrue(r.done);self.assertEqual(self.creations,[])

    def test_supervised_requires_matching_policy_acceptance_and_ten_second_cap(self):
        for changes in [dict(supervised_input_accepted=False),dict(input_policy_id='vendor_tracking')]:
            r=self.setup_runtime(supervised=True);self.reply(supervised=True,**changes)
            self.assertTrue(r.done);self.assertEqual(self.creations,[])
        with self.assertRaises(ValueError):
            g1.Config(right_kp=(10.,)*7,right_kd=(1.,)*7,nonright_kd=(3.,)*22,
                      calibration_id='c',enable_source='e',gain_review_id='g',
                      input_policy_id='supervised_position_only_v1',max_session_s=11.)

    def test_duplicate_json_keys_nonfinite_and_oversize_rejected(self):
        for line in ['{"type":"target","type":"target"}', '{"x":NaN}', 'x'*4097]:
            r=self.setup_runtime();self.clock.advance();r.tick([line])
            self.assertTrue(r.done);self.assertEqual(self.pub.messages,[])

    def test_error_output_or_write_failure_does_not_retry(self):
        r=self.setup_runtime();self.pub.success=False;self.reply()
        self.assertTrue(r.done);self.assertTrue(r.final['last_action_unknown'])
        self.assertEqual(len(self.pub.messages),1)

    def test_plan_cli_does_not_need_sdk(self):
        result=subprocess.run([sys.executable,str(Path(runner.__file__)),'--plan'],capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout)['default'],'preview')

    def test_configuration_separates_wait_and_enabled_limit(self):
        payload={k:getattr(config(True),k) for k in ('right_kp','right_kd','nonright_kd','calibration_id',
                                                  'enable_source','gain_review_id','input_policy_id','max_session_s')}
        value=runner.configuration(json.dumps(payload),30.)
        self.assertEqual(value.max_session_s,10);self.assertEqual(value.max_command_hz,200)

    def test_pipe_io_partial_json_eof_and_backpressure_bounds(self):
        in_read,in_write=os.pipe();out_read,out_write=os.pipe()
        try:
            io=runner.JsonLinesIO(in_read,out_write)
            os.write(in_write,b'{"a":');self.assertEqual(io.read(),([],False))
            os.write(in_write,b'1}\n');self.assertEqual(io.read(),(['{"a":1}'],False))
            io.emit({'type':'test'});self.assertEqual(json.loads(os.read(out_read,1024)),{'type':'test'})
            io.outbound=b'x'*65536
            with self.assertRaises(BufferError):io.emit({'type':'test'})
            os.close(in_write);in_write=None
            self.assertEqual(io.read(),([],True))
        finally:
            for fd in (in_read,in_write,out_read,out_write):
                if fd is not None:os.close(fd)


if __name__=='__main__': unittest.main()
