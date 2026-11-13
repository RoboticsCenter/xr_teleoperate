"""Pure policy contracts: fabricated IK is never a robot/solver validation."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy_module = load('pico_supervised_input')
decoder_module = load('pico_raw_probe')
START = 10_000_000_000
ORIGIN = [.2, .3, .4, 0., 0., 0., 1.]


def reference(ident='ref1'):
    return {'type':'reference', 'reference_id':ident, 'right_sn':'RIGHT',
            'state_valid':True, 'clock_domain':'local_monotonic', 'reference_kind':'live',
            'received_monotonic_ns':START, 'state_sequence':1, 'q_rad':[0.]*14,
            'axis_config':{'id':'axes1', 'pose_unit':'m', 'verified':False,
                           'T_robot_axes_xr_axes':[[0.,0.,-1.],[-1.,0.,0.],[0.,1.,0.]]}}


def sample(seq=1, receipt=None, pose=None, trigger=1., input_mode=1):
    receipt = START+seq*10_000_000 if receipt is None else receipt
    pose = ORIGIN if pose is None else pose
    raw = {'value':{'timeStampNs':1_000_000_000+seq, 'Input':input_mode, 'appState':{'focus':True},
                    'Controller':{'right':{'trigger':trigger, 'grip':0.}},
                    'Motion':{'len':1, 'joints':[{'sn':'RIGHT', 'p':','.join(map(str,pose))}]}}}
    decoder = decoder_module.PacketDecoder()
    decoder.sequence = seq-1
    return decoder.decode(json.dumps(raw), 'HEADSET', receipt)


def candidate(packet, ref=None, first=True, origin=None):
    ref = reference() if ref is None else ref
    p = packet['trackers'][0]['xyzw_pose']
    origin = (p if first else ORIGIN) if origin is None else origin
    raw_delta = [p[i]-origin[i] for i in range(3)]
    matrix = ref['axis_config']['T_robot_axes_xr_axes']
    scaled = [.02*sum(row[i]*raw_delta[i] for i in range(3)) for row in matrix]
    magnitude = math.sqrt(sum(x*x for x in scaled))
    factor = min(1., .01/magnitude) if magnitude else 1.
    return {'accepted':True, 'mode':'live_input_preview', 'tracking_valid':None,
            'sequence':packet['packet_sequence'], 'source_packet_sequence':packet['packet_sequence'],
            'source_received_monotonic_ns':packet['local_monotonic_ns'],
            'source_app_timestamp_ns':packet['app_timestamp_ns'],
            'reference_received_monotonic_ns':ref['received_monotonic_ns'],
            'reference_id':ref['reference_id'], 'right_sn':'RIGHT', 'calibration_id':ref['axis_config']['id'],
            'left_arm':'fixed', 'orientation':'held_at_robot_reference', 'position_scale':.02,
            'q_candidate_rad':list(ref['q_rad']), 'status':'origin_captured' if first else 'candidate',
            'raw_tracker_delta_m':raw_delta, 'bounded_delta_m':[x*factor for x in scaled],
            'residuals':{s:{'position_m':0., 'rotation_rad':0.} for s in ('left','right')}}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ref = reference()
        self.policy = policy_module.SupervisedInputPolicy(sn='RIGHT')
        self.policy.set_reference(self.ref)

    def evaluate(self, packet, ik=None, now_ns=None, policy=None, first=True):
        return (policy or self.policy).evaluate(packet, candidate(packet,self.ref,first) if ik is None else ik,
                      now_ns=packet['local_monotonic_ns'] if now_ns is None else now_ns)

    def test_requires_explicit_reference(self):
        decision = self.evaluate(sample(), policy=policy_module.SupervisedInputPolicy('RIGHT'))
        self.assertFalse(decision['supervised_input_accepted'])
        self.assertEqual(decision['reason'], 'set_reference_required')

    def test_unknown_vendor_quality_never_rewritten_and_inputs_not_mutated(self):
        p = sample()
        ik = candidate(p)
        before = copy.deepcopy((p,ik,self.ref))
        decision = self.evaluate(p,ik)
        self.assertTrue(decision['supervised_input_accepted'])
        self.assertIsNone(decision['tracking_valid'])
        self.assertFalse(decision['tracker_sample_freshness_verified'])
        self.assertFalse(decision['controller_connected_verified'])
        self.assertFalse(decision['motion_ready'])
        self.assertFalse(decision['axes_physically_verified'])
        self.assertEqual((p,ik,self.ref), before)

    def test_explicit_invalid_always_rejected(self):
        for where,key,value in (('top','tracking_valid',False), ('tracker','tracking_valid',False),
                                ('tracker','confidence',2), ('tracker','confidence',3)):
            self.setUp()
            p = sample()
            (p if where=='top' else p['trackers'][0])[key] = value
            self.assertEqual(self.evaluate(p)['reason'], 'tracking_explicitly_invalid')

    def test_present_tracking_metadata_requires_vendor_policy(self):
        p = sample()
        p['trackers'][0].update(confidence=1, tracking_valid=True)
        self.assertEqual(self.evaluate(p)['reason'], 'use_vendor_policy_when_tracking_metadata_is_present')

    def test_controller_and_ik_must_have_same_original_packet_identity(self):
        for key in ('sequence','local_monotonic_ns','app_timestamp_ns'):
            self.setUp()
            p = sample()
            p['controller_enable'][key] += 1
            self.assertEqual(self.evaluate(p)['reason'], 'controller_enable_packet_mismatch')
        for key in ('sequence','source_packet_sequence','source_received_monotonic_ns','source_app_timestamp_ns'):
            self.setUp()
            p = sample()
            ik = candidate(p)
            ik[key] += 1
            self.assertEqual(self.evaluate(p,ik)['reason'], 'ik_source_packet_mismatch')

    def test_focus_loss_missing_or_string_trigger_fault_even_before_active(self):
        for kind in ('focus','missing','string','held_mismatch'):
            self.setUp()
            p = sample()
            if kind=='focus':
                p['app_focused'] = False
            elif kind=='missing':
                p['controller_enable']['held'] = None
            elif kind=='string':
                p['controller_enable']['trigger'] = '1'
            else:
                p['controller_enable']['held'] = False
            r = self.evaluate(p)
            self.assertFalse(r['supervised_input_accepted'])
            self.assertEqual(r['state'],'LATCHED')

    def test_wait_records_origin_and_sequence_without_starting_clock(self):
        p = sample(trigger=0.)
        r = self.evaluate(p)
        self.assertEqual(r['reason'],'waiting_for_trigger')
        self.assertFalse(r['supervised_input_accepted'])
        self.assertFalse(self.policy.latched)
        self.assertEqual(self.policy.origin,ORIGIN)
        self.assertIsNone(self.policy.started_ns)
        # Waiting longer than the active window does not consume it.
        for seq in range(2,112):
            p = sample(seq,receipt=START+10_000_000+(seq-1)*100_000_000,trigger=0.)
            r = self.evaluate(p,first=False)
            self.assertEqual(r['reason'],'waiting_for_trigger')
        p = sample(112,receipt=p['local_monotonic_ns']+100_000_000,trigger=.8)
        r = self.evaluate(p,first=False)
        self.assertTrue(r['supervised_input_accepted'])
        self.assertEqual(r['elapsed_s'],0.)
        self.assertEqual(self.policy.started_ns,p['local_monotonic_ns'])

    def test_release_after_activation_latches_and_cannot_repress(self):
        self.assertTrue(self.evaluate(sample())['supervised_input_accepted'])
        r = self.evaluate(sample(2,trigger=0.),first=False)
        self.assertEqual(r['reason'],'right_trigger_released_after_activation')
        self.assertEqual(r['state'],'LATCHED')
        self.assertEqual(self.evaluate(sample(3),first=False)['reason'],'policy_latched_new_reference_required')

    def test_explicit_hand_mode_waits_without_enabling_or_starting_window(self):
        p = sample(input_mode=2)
        self.assertIs(p['controller_enable']['held'],False)
        r = self.evaluate(p)
        self.assertEqual(r['reason'],'waiting_for_controller')
        self.assertFalse(r['supervised_input_accepted'])
        self.assertFalse(self.policy.latched)
        self.assertEqual(self.policy.origin,ORIGIN)
        for seq in range(2,112):
            p = sample(seq,receipt=START+10_000_000+(seq-1)*100_000_000,input_mode=2)
            r = self.evaluate(p,first=False)
            self.assertEqual(r['state'],'WAIT')
            self.assertEqual(r['reason'],'waiting_for_controller')
            self.assertFalse(r['supervised_input_accepted'])
            self.assertIsNone(self.policy.started_ns)
        p = sample(112,receipt=p['local_monotonic_ns']+100_000_000,trigger=0.)
        self.assertEqual(self.evaluate(p,first=False)['reason'],'waiting_for_trigger')
        p = sample(113,receipt=p['local_monotonic_ns']+100_000_000,trigger=.8)
        r = self.evaluate(p,first=False)
        self.assertTrue(r['supervised_input_accepted'])
        self.assertEqual(r['elapsed_s'],0.)

    def test_hand_mode_after_activation_immediately_latches(self):
        self.evaluate(sample())
        r = self.evaluate(sample(2,input_mode=2),first=False)
        self.assertFalse(r['supervised_input_accepted'])
        self.assertEqual(r['reason'],'controller_mode_lost_after_activation')
        self.assertEqual(r['state'],'LATCHED')
        self.assertEqual(self.evaluate(sample(3),first=False)['reason'],'policy_latched_new_reference_required')

    def test_waiting_for_controller_still_checks_identity_age_and_ik(self):
        for kind in ('sn','sequence','age','ik','focus'):
            self.setUp()
            p = sample(input_mode=2)
            ik = candidate(p)
            now = p['local_monotonic_ns']
            if kind=='sn':
                p['trackers'][0]['serial_number'] = 'WRONG'
            elif kind=='sequence':
                p['controller_enable']['sequence'] += 1
            elif kind=='age':
                now += 100_000_001
            elif kind=='ik':
                ik['q_candidate_rad'][7] = .1
            else:
                p['app_focused'] = False
            r = self.evaluate(p,ik,now_ns=now)
            self.assertFalse(r['supervised_input_accepted'])
            self.assertEqual(r['state'],'LATCHED')

    def test_hand_wait_does_not_accept_unknown_modes_or_malformed_controller(self):
        for mode in (None,True,'2',0,3):
            self.setUp()
            self.assertEqual(self.evaluate(sample(input_mode=mode))['state'],'LATCHED')
        for field,value in (('held',None),('held',True),('controller_input_active',None),
                            ('input_mode',None),('input_mode',1),('trigger',None),('trigger','1')):
            self.setUp()
            p = sample(input_mode=2)
            p['controller_enable'][field] = value
            self.assertEqual(self.evaluate(p)['state'],'LATCHED')

    def test_stale_and_future_original_receipt_rejected(self):
        for offset in (-1,100_000_001):
            self.setUp()
            p = sample()
            self.assertEqual(self.evaluate(p,now_ns=p['local_monotonic_ns']+offset)['reason'],
                             'stale_or_future_pico_packet')

    def test_replay_latches_even_while_waiting(self):
        p = sample(trigger=0.)
        self.evaluate(p)
        self.assertEqual(self.evaluate(p,first=False)['reason'],'replayed_or_regressed_packet')
        self.assertEqual(self.evaluate(sample(2),first=False)['reason'],'policy_latched_new_reference_required')

    def test_stationary_pose_in_new_packets_is_allowed(self):
        self.evaluate(sample())
        self.assertTrue(self.evaluate(sample(2),first=False)['supervised_input_accepted'])

    def test_ten_second_window_uses_first_held_original_receipt(self):
        p = sample()
        self.evaluate(p)
        first_receipt = p['local_monotonic_ns']
        for seq in range(2,101):
            p = sample(seq,receipt=first_receipt+(seq-1)*100_000_000)
            self.assertTrue(self.evaluate(p,first=False)['supervised_input_accepted'])
        p = sample(101,receipt=first_receipt+10_000_000_000)
        self.assertEqual(self.evaluate(p,first=False)['reason'],'supervised_ten_second_window_expired')

    def test_translation_rotation_jump_and_packet_gap_rejected(self):
        for kind in ('translation','rotation','gap'):
            self.setUp()
            self.evaluate(sample())
            pose = list(ORIGIN)
            if kind=='translation':
                pose[0] += .11
            elif kind=='rotation':
                pose[3:] = [0.,math.sin(.3),0.,math.cos(.3)]
            p = sample(2,pose=pose,receipt=START+220_000_000 if kind=='gap' else None)
            reason = self.evaluate(p,first=False)['reason']
            self.assertEqual(reason,'packet_stream_gap' if kind=='gap' else 'tracker_pose_jump')

    def test_joint_fixed_left_orientation_scale_and_residual_envelopes(self):
        for kind in ('joint','left','orientation','scale','residual'):
            self.setUp()
            self.evaluate(sample())
            p = sample(2)
            ik = candidate(p,first=False)
            if kind=='joint':
                ik['q_candidate_rad'][7] = .03001
            elif kind=='left':
                ik['q_candidate_rad'][0] = .001
            elif kind=='orientation':
                ik['orientation'] = 'follow_tracker'
            elif kind=='scale':
                ik['position_scale'] = .1
            else:
                ik['residuals']['right']['position_m'] = .002001
            self.assertFalse(self.evaluate(p,ik)['supervised_input_accepted'])

    def test_axis_mapping_and_translation_limit_are_recomputed(self):
        for kind in ('raw','mapped','limit'):
            self.setUp()
            self.evaluate(sample())
            p = sample(2,pose=[.21,.3,.4,0.,0.,0.,1.])
            ik = candidate(p,first=False)
            if kind=='raw':
                ik['raw_tracker_delta_m'][0] += .001
            elif kind=='mapped':
                ik['bounded_delta_m'][1] *= -1
            else:
                ik['bounded_delta_m'] = [0.,.010001,0.]
            self.assertFalse(self.evaluate(p,ik)['supervised_input_accepted'])

    def test_reference_sn_and_axis_id_must_match_ik(self):
        for key in ('reference_id','right_sn','calibration_id'):
            self.setUp()
            p = sample()
            ik = candidate(p)
            ik[key] = 'different'
            self.assertEqual(self.evaluate(p,ik)['reason'],'ik_reference_or_axis_id_mismatch')

    def test_rearm_requires_unique_reference_and_first_candidate_equals_reference(self):
        with self.assertRaises(ValueError):
            self.policy.set_reference(self.ref)
        self.ref = reference('ref2')
        self.policy.set_reference(self.ref)
        p = sample()
        ik = candidate(p,self.ref)
        ik['q_candidate_rad'][7] = .001
        self.assertEqual(self.evaluate(p,ik)['reason'],'initial_candidate_must_equal_reference')

    def test_offline_ik_and_zero_position_are_not_accepted(self):
        p = sample()
        ik = candidate(p)
        ik['mode'] = 'offline_preview'
        self.assertEqual(self.evaluate(p,ik)['reason'],'accepted_live_ik_required')
        self.setUp()
        p = sample(pose=[0.,0.,0.,0.,0.,0.,1.])
        self.assertEqual(self.evaluate(p)['reason'],'zero_position_excluded_by_supervised_policy')


class TrackerWindowTests(unittest.TestCase):
    def setUp(self):
        self.ref = reference()
        self.policy = policy_module.TrackerWindowPolicy(sn='RIGHT')
        self.policy.set_reference(self.ref)

    def packet(self,seq=1,**kwargs):
        p = sample(seq,input_mode=kwargs.pop('input_mode',2),**kwargs)
        p.pop('controller_enable')
        return p

    def evaluate(self,p,ik=None,*,first=True,now_ns=None):
        return self.policy.evaluate(p,candidate(p,self.ref,first) if ik is None else ik,
                                    now_ns=p['local_monotonic_ns'] if now_ns is None else now_ns)

    def test_modes_one_and_two_need_no_controller_and_fabricate_no_button_flags(self):
        for mode in (1,2):
            self.setUp()
            p = self.packet(input_mode=mode,trigger=0.)
            r = self.evaluate(p)
            self.assertTrue(r['supervised_input_accepted'])
            self.assertEqual(r['input_policy_id'],'supervised_tracker_window_v1')
            self.assertEqual(r['source_id'],'pico_right_tracker')
            self.assertEqual(r['state'],'ACTIVE')
            self.assertIsNone(r['tracking_valid'])
            self.assertFalse(r['tracker_sample_freshness_verified'])
            self.assertFalse(r['motion_ready'])
            self.assertEqual(r['commands_published'],0)
            for key in ('held','enable_sequence','controller_connected_verified','vendor_controller_valid'):
                self.assertNotIn(key,r)
            self.assertEqual(r['sequence'],p['packet_sequence'])
            self.assertEqual(r['local_monotonic_ns'],p['local_monotonic_ns'])
            self.assertEqual(r['app_timestamp_ns'],p['app_timestamp_ns'])

    def test_raw_packet_without_controller_can_normalize_and_enter_window(self):
        raw = {'value':{'timeStampNs':1_000_000_001,'Input':2,'appState':{'focus':True},
                        'Motion':{'len':1,'joints':[{'sn':'RIGHT','p':','.join(map(str,ORIGIN))}]}}}
        p = decoder_module.PacketDecoder().decode(json.dumps(raw),'HEADSET',START+10_000_000)
        self.assertIsNone(p['controller_enable']['trigger'])
        self.assertTrue(self.evaluate(p)['supervised_input_accepted'])

    def test_controller_garbage_is_not_used_as_tracker_authority(self):
        for unused in (None,{'held':False},{'held':'true','trigger':'bad'},'invalid'):
            self.setUp()
            p = self.packet()
            p['controller_enable'] = unused
            self.assertTrue(self.evaluate(p)['supervised_input_accepted'])

    def test_first_origin_original_receipt_starts_ten_second_window(self):
        p = self.packet()
        origin_receipt = p['local_monotonic_ns']
        r = self.evaluate(p,now_ns=origin_receipt+50_000_000)
        self.assertEqual(self.policy.started_ns,origin_receipt)
        self.assertAlmostEqual(r['elapsed_s'],.05)
        for seq in range(2,101):
            p = self.packet(seq,receipt=origin_receipt+(seq-1)*100_000_000)
            self.assertTrue(self.evaluate(p,first=False)['supervised_input_accepted'])
        p = self.packet(101,receipt=origin_receipt+10_000_000_000)
        r = self.evaluate(p,first=False)
        self.assertEqual(r['reason'],'supervised_ten_second_window_expired')
        self.assertEqual(r['state'],'LATCHED')
        self.assertFalse(r['supervised_input_accepted'])

    def test_active_input_mode_switch_and_stationary_tracker_do_not_require_controller(self):
        self.evaluate(self.packet(input_mode=2))
        self.assertTrue(self.evaluate(self.packet(2,input_mode=1),first=False)['supervised_input_accepted'])
        self.assertTrue(self.evaluate(self.packet(3,input_mode=2),first=False)['supervised_input_accepted'])

    def test_focus_loss_and_unknown_modes_latch_without_a_wait_state(self):
        for mode in (None,0,3,True,'2'):
            self.setUp()
            p = self.packet()
            p['input_mode'] = mode
            self.assertEqual(self.evaluate(p)['state'],'LATCHED')
        self.setUp()
        self.evaluate(self.packet())
        p = self.packet(2)
        p['app_focused'] = False
        self.assertEqual(self.evaluate(p,first=False)['state'],'LATCHED')
        self.assertEqual(self.evaluate(self.packet(3),first=False)['reason'],
                         'policy_latched_new_reference_required')

    def test_explicit_invalid_tracker_or_bad_structure_is_never_accepted(self):
        for kind in ('invalid','confidence','count','sn','issues'):
            self.setUp()
            p = self.packet()
            if kind=='invalid':
                p['trackers'][0]['tracking_valid'] = False
            elif kind=='confidence':
                p['trackers'][0]['confidence'] = 2
            elif kind=='count':
                p['motion_declared_count'] = 2
            elif kind=='sn':
                p['trackers'][0]['serial_number'] = 'WRONG'
            else:
                p['issues'] = ['packet_invalid']
            self.assertFalse(self.evaluate(p)['supervised_input_accepted'])

    def test_original_packet_age_sequence_and_ik_identity_still_checked(self):
        for offset in (-1,100_000_001):
            self.setUp()
            p = self.packet()
            self.assertEqual(self.evaluate(p,now_ns=p['local_monotonic_ns']+offset)['reason'],
                             'stale_or_future_pico_packet')
        for key in ('source_packet_sequence','source_received_monotonic_ns','source_app_timestamp_ns',
                    'reference_id','calibration_id','right_sn'):
            self.setUp()
            p = self.packet()
            ik = candidate(p)
            ik[key] = ik[key]+1 if isinstance(ik[key],int) else 'wrong'
            self.assertFalse(self.evaluate(p,ik)['supervised_input_accepted'])
        self.setUp()
        p = self.packet()
        self.evaluate(p)
        self.assertEqual(self.evaluate(p,first=False)['reason'],'replayed_or_regressed_packet')

    def test_ik_bounds_and_fixed_left_orientation_are_shared_and_enforced(self):
        for kind in ('joint','left','orientation','scale','translation','residual'):
            self.setUp()
            self.evaluate(self.packet())
            p = self.packet(2)
            ik = candidate(p,first=False)
            if kind=='joint':
                ik['q_candidate_rad'][7] = .030001
            elif kind=='left':
                ik['q_candidate_rad'][0] = .001
            elif kind=='orientation':
                ik['orientation'] = 'follow'
            elif kind=='scale':
                ik['position_scale'] = .021
            elif kind=='translation':
                ik['bounded_delta_m'] = [.010001,0.,0.]
            else:
                ik['residuals']['right']['rotation_rad'] = .030001
            self.assertFalse(self.evaluate(p,ik,first=False)['supervised_input_accepted'])

    def test_jump_and_stream_gap_latch_without_controller(self):
        for kind in ('translation','rotation','gap'):
            self.setUp()
            self.evaluate(self.packet())
            p = list(ORIGIN)
            if kind=='translation':
                p[0] += .11
            elif kind=='rotation':
                p[3:] = [0.,math.sin(.3),0.,math.cos(.3)]
            packet = self.packet(2,pose=p,receipt=START+220_000_000 if kind=='gap' else None)
            self.assertEqual(self.evaluate(packet,first=False)['state'],'LATCHED')

    def test_new_unique_reference_is_required_after_fault(self):
        p = self.packet()
        p['app_focused'] = False
        self.evaluate(p)
        with self.assertRaises(ValueError):
            self.policy.set_reference(self.ref)
        self.ref = reference('second-window')
        self.policy.set_reference(self.ref)
        self.assertTrue(self.evaluate(self.packet())['supervised_input_accepted'])


if __name__ == '__main__':
    unittest.main()
