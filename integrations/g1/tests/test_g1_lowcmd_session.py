import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location('g1_lowcmd_session', Path(__file__).resolve().parents[1] / 'scripts/g1_lowcmd_session.py')
g1 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = g1
spec.loader.exec_module(g1)


def config(**changes):
    values = dict(right_kp=(10.,)*7, right_kd=(1.,)*7, nonright_kd=(3.,)*22,
                  calibration_id='calibration-test', enable_source='deadman-test', gain_review_id='offline-test-only')
    values.update(changes)
    return g1.Config(**values)


def record(seq=0):
    now = 100. + seq*.01
    return dict(now=now, action='enable' if seq == 0 else None,
                feedback=dict(received_t=now, sequence=seq, tick=seq, crc_valid=True,
                              q=[0.]*22+[.1]*7, dq=[0.]*29, motorstate=[0]*29,
                              mode_machine=5, mode_pr=0),
                target=dict(received_t=now, sequence=seq, q=[.1]*7,
                            tracking_valid=True, calibration_id='calibration-test', reference_id='ref-1'),
                enable=dict(received_t=now, sequence=seq, held=True, source_id='deadman-test'),
                readiness=dict(received_t=now, check_mode_code=0, motion_name='',
                               suspended=True, operator_ready=True, exclusive_control_confirmed=True))


class CRC:
    def __init__(self):
        self.snapshots = []

    def Crc(self, msg):
        self.snapshots.append(copy.deepcopy(msg))
        return 0x12345678


def factory():
    # Deliberately dirty defaults: encoder must completely overwrite every slot.
    return SimpleNamespace(motor_cmd=[SimpleNamespace(mode=99, q=99., dq=99., tau=99., kp=99., kd=99., reserve=99) for _ in range(35)])


class Publisher:
    def __init__(self, clock):
        self.clock, self.messages, self.closed = clock, [], False
        self.succeed, self.delay = True, 0.

    def Write(self, msg):
        self.messages.append(copy.deepcopy(msg))
        self.clock[0] += self.delay
        return self.succeed

    def Close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setup_live(self, **cfg):
        self.clock = [100.]
        self.pub, self.crc, self.creations = Publisher(self.clock), CRC(), []
        def transport(interface):
            self.creations.append(interface)
            return self.pub, factory, self.crc
        return g1.LowCmdSession(config(**cfg), enable_motion=True, interface='fake0',
                                transport_factory=transport, clock=lambda: self.clock[0])

    def send(self, session, seq=0, **changes):
        r = record(seq)
        r.update(changes)
        self.clock[0] = r['now']
        return session.step(**r)

    def test_default_never_imports_or_constructs_transport(self):
        calls = []
        session = g1.LowCmdSession(transport_factory=lambda _: calls.append(True))
        self.assertEqual(session.state, 'DISABLED')
        self.assertFalse(session.step(**record())['published'])
        session.close()
        self.assertEqual(calls, [])

    def test_explicit_gain_configuration_and_ceilings(self):
        for changes in [dict(nonright_kd=(0.,)*22), dict(right_kp=(41.,)*7),
                        dict(right_kd=(1.1,)*7), dict(nonright_kd=(9.,)*22),
                        dict(max_excursion_rad=.031), dict(gain_review_id='')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_readiness_and_action_required_before_transport(self):
        for changes in [dict(motion_name='ai'), dict(check_mode_code=3102),
                        dict(exclusive_control_confirmed=False), dict(operator_ready=False),
                        dict(received_t=95.)]:
            s = self.setup_live()
            r = record(); r['readiness'].update(changes)
            self.assertEqual(s.step(**r)['state'], 'LATCHED')
            self.assertEqual(self.creations, [])
        s = self.setup_live(); r = record(); r['action'] = None
        self.assertEqual(s.step(**r)['state'], 'DISABLED')
        self.assertEqual(self.creations, [])

    def test_first_frame_measured_and_all_slots_explicit_crc_last(self):
        s = self.setup_live(); r = record(); r['target']['q'] = [.12]*7
        out = s.step(**r)
        self.assertTrue(out['published'])
        self.assertEqual(self.creations, ['fake0'])
        msg = self.pub.messages[-1]
        self.assertEqual((msg.mode_machine, msg.mode_pr, msg.crc), (5, 0, 0x12345678))
        for i,m in enumerate(msg.motor_cmd):
            self.assertEqual((m.dq, m.tau, m.reserve), (0., 0., 0))
            if i < 22:
                self.assertEqual((m.mode,m.q,m.kp,m.kd), (1,0.,0.,3.))
            elif i < 29:
                self.assertEqual((m.mode,m.q,m.kp,m.kd), (1,.1,0.,1.))
            else:
                self.assertEqual((m.mode,m.q,m.kp,m.kd), (0,0.,0.,0.))
        self.assertEqual(self.crc.snapshots[-1].crc, 0)
        self.assertEqual(self.crc.snapshots[-1].motor_cmd[29].q, 0.)

    def test_right_only_velocity_limit_and_gain_ramp(self):
        s = self.setup_live(); self.send(s)
        r = record(1); r['target']['q'] = [.13]*7; self.clock[0]=r['now']
        result = s.step(**r); msg=self.pub.messages[-1]
        self.assertTrue(result['published'])
        self.assertAlmostEqual(msg.motor_cmd[25].q, .1005)
        self.assertAlmostEqual(msg.motor_cmd[25].kp, .1)
        self.assertTrue(all(m.kp==0 and m.kd==3 for m in msg.motor_cmd[:22]))

    def test_out_of_window_latches_and_fresh_target_does_not_resume(self):
        s=self.setup_live();self.send(s)
        r=record(1);r['target']['q'][0]=.131;self.clock[0]=r['now']
        self.assertEqual(s.step(**r)['state'],'LATCHED')
        self.assertEqual(self.send(s,2)['state'],'LATCHED')
        self.assertEqual(len(self.pub.messages),1)

    def test_stale_feedback_target_deadman_and_unknown_tracking_rejected(self):
        for key in ('feedback','target','enable'):
            s=self.setup_live(); r=record();r[key]['received_t']=99.
            self.assertEqual(s.step(**r)['state'],'LATCHED');self.assertEqual(self.creations,[])
        s=self.setup_live();r=record();r['target']['tracking_valid']=None
        self.assertEqual(s.step(**r)['state'],'LATCHED')

    def test_duplicate_source_sequence_cannot_refresh_cache_timestamp(self):
        s=self.setup_live();self.send(s)
        r=record(1);r['target']['sequence']=0;self.clock[0]=r['now']
        out=s.step(**r)
        self.assertEqual(out['state'],'LATCHED')
        self.assertIn('retimestamps_cached_target',out['reason'])

    def test_machine_change_any_motor_fault_bad_crc_and_excess_speed(self):
        for change in ('mode','fault','crc','speed'):
            s=self.setup_live();self.send(s);r=record(1);self.clock[0]=r['now']
            if change=='mode':r['feedback']['mode_machine']=6
            if change=='fault':r['feedback']['motorstate'][0]=1
            if change=='crc':r['feedback']['crc_valid']=False
            if change=='speed':r['feedback']['dq'][0]=.6
            self.assertEqual(s.step(**r)['state'],'LATCHED')
            self.assertEqual(len(self.pub.messages),1)

    def test_loop_gap_cannot_be_hidden_by_new_inputs(self):
        s=self.setup_live();self.send(s)
        out=self.send(s,20)
        self.assertEqual(out['state'],'LATCHED')
        self.assertIn('loop_deadline',out['reason'])
        self.assertEqual(len(self.pub.messages),1)

    def test_clock_reversal_latches(self):
        s=self.setup_live();self.send(s);r=record(1);r['now']=99.9;self.clock[0]=99.9
        self.assertEqual(s.step(**r)['state'],'LATCHED')

    def test_tick_must_advance_with_new_callback_and_can_wrap(self):
        s=self.setup_live();r=record();r['feedback']['tick']=0xffffffff;s.step(**r)
        r=record(1);r['feedback']['tick']=0;self.clock[0]=r['now']
        self.assertTrue(s.step(**r)['published'])
        r=record(2);r['feedback']['tick']=0;self.clock[0]=r['now']
        self.assertEqual(s.step(**r)['state'],'LATCHED')

    def test_nan_and_wrong_target_shape_rejected(self):
        for values in ([float('nan')]*7,[.1]*14):
            s=self.setup_live();r=record();r['target']['q']=values
            self.assertEqual(s.step(**r)['state'],'LATCHED');self.assertEqual(self.creations,[])

    def test_reference_change_tracking_error_and_session_deadline(self):
        for failure in ('reference','tracking','deadline'):
            s=self.setup_live(max_session_s=.02);self.send(s)
            r=record(2 if failure=='deadline' else 1);self.clock[0]=r['now']
            if failure=='reference':r['target']['reference_id']='different-ref'
            if failure=='tracking':r['feedback']['q'][22]=.16
            self.assertEqual(s.step(**r)['state'],'LATCHED')
            self.assertEqual(len(self.pub.messages),1)

    def test_internal_output_helper_cannot_open_before_enable(self):
        s=self.setup_live()
        out=s._emit({},100.)
        self.assertEqual(out['state'],'LATCHED');self.assertEqual(self.creations,[])

    def test_transport_setup_delay_rechecked_before_first_write(self):
        s=self.setup_live()
        def delayed(_):
            self.clock[0]+=.2
            return self.pub,factory,self.crc
        s.transport_factory=delayed
        self.assertEqual(s.step(**record())['state'],'LATCHED')
        self.assertEqual(self.pub.messages,[])

    def test_explicit_preparation_discards_inputs_before_later_enable(self):
        s=self.setup_live()
        def delayed(_):
            self.clock[0]+=.2
            return self.pub,factory,self.crc
        s.transport_factory=delayed
        r=record();r['action']='prepare'
        prepared=s.step(**r)
        self.assertTrue(prepared['transport_prepared']);self.assertEqual(s.state,'DISABLED')
        self.assertIsNone(s.reference);self.assertEqual(self.pub.messages,[])
        r=record(21);r['action']='enable';r['feedback']['q'][22]=.102
        self.clock[0]=r['now'];self.assertTrue(s.step(**r)['published'])
        self.assertEqual(self.pub.messages[0].motor_cmd[22].q,.102)

    def test_preparation_rejects_cached_inputs_and_has_no_default_output(self):
        s=self.setup_live();r=record();r['action']='prepare';s.step(**r)
        r['action']='enable';r['now']+=.005;self.clock[0]=r['now']
        out=s.step(**r)
        self.assertEqual(out['state'],'LATCHED');self.assertIn('fresh_post_preparation',out['reason'])
        self.assertEqual(self.pub.messages,[])
        default=g1.LowCmdSession(config())
        r=record();r['action']='prepare'
        self.assertEqual(default.step(**r)['state'],'LATCHED')
        self.assertIsNone(default.publisher)

    def test_blocking_write_latches_after_return_without_claiming_stop(self):
        s=self.setup_live();self.pub.delay=.1
        out=s.step(**record())
        self.assertEqual(out['state'],'LATCHED');self.assertTrue(out['published'])
        self.assertFalse(out['physical_stop_verified'])

    def test_explicit_damping_once_and_close_never_writes(self):
        s=self.setup_live();self.send(s)
        r=record(1);r['enable']['held']=False;self.clock[0]=r['now'];s.step(**r)
        r=record(2);self.clock[0]=r['now']
        out=s.damp_and_latch(now=r['now'],feedback=r['feedback'])
        self.assertTrue(out['published']);self.assertEqual(out['state'],'LATCHED')
        self.assertTrue(all(m.kp==0 and m.q==0 for m in self.pub.messages[-1].motor_cmd))
        self.assertTrue(all(m.kd>0 for m in self.pub.messages[-1].motor_cmd[:29]))
        s.damp_and_latch(now=r['now'],feedback=r['feedback']);s.close()
        self.assertEqual(len(self.pub.messages),2);self.assertTrue(self.pub.closed)

    def test_write_failure_not_retried_even_for_damping(self):
        s=self.setup_live();self.pub.succeed=False
        out=s.step(**record());self.assertTrue(out['write_delivery_unknown'])
        r=record(1);self.clock[0]=r['now'];s.damp_and_latch(now=r['now'],feedback=r['feedback'])
        self.assertEqual(len(self.pub.messages),1)

    def test_expiry_before_write_keeps_transport_available_for_explicit_damping(self):
        s=self.setup_live();first=record();first['target']['received_t']=99.93
        self.assertTrue(s.step(**first)['published'])
        original_crc=self.crc.Crc
        def delayed_crc(msg):
            self.clock[0]+=.02
            return original_crc(msg)
        self.crc.Crc=delayed_crc
        r=record(2);r['target']=first['target'];self.clock[0]=r['now']
        out=s.step(**r)
        self.assertEqual(out['state'],'LATCHED')
        self.assertIn('expired_before_write_target',out['reason'])
        self.assertFalse(s.transport_failed);self.assertEqual(len(self.pub.messages),1)
        feedback=record(5)['feedback'];self.clock[0]=feedback['received_t']
        self.assertTrue(s.damp_and_latch(now=self.clock[0],feedback=feedback)['published'])
        self.assertEqual(len(self.pub.messages),2)

    def test_decode_lowstate_validates_wire_crc_and_29_states(self):
        msg=SimpleNamespace(motor_state=[SimpleNamespace(q=.1,dq=0.,motorstate=0) for _ in range(35)],
                            tick=10,mode_machine=5,mode_pr=0,crc=0x12345678)
        out=g1.decode_lowstate(msg,received_t=100.,sequence=1,crc=CRC())
        self.assertEqual(len(out['q']),29);self.assertTrue(out['crc_valid'])
        msg.crc=0
        with self.assertRaises(ValueError):g1.decode_lowstate(msg,received_t=100.,sequence=1,crc=CRC())


if __name__=='__main__':
    unittest.main()
