"""Pure synthetic Tracker frames; no sockets, SDK or filesystem operations."""
import copy
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from pico_distance import DistanceMeter


class DistanceTests(unittest.TestCase):
    def setUp(self):
        self.meter=DistanceMeter()
        self.settings={'arms_mode':'both','tracker_config':{
            'left_sn':'LEFT','right_sn':'RIGHT','offset_tracker_m':[0.,0.,0.],
            'right_offset_tracker_m':[0.,0.,0.]}}
        self.calibration={'distance_cm':20,'captured_stages':['origin']}
        self.frame={'session':'stream-1','seq':10,'head_pose':[0.,1.6,0.,0.,0.,0.,1.],
            'trackers':{'LEFT':[-.2,1.,-.3,0.,0.,0.,1.],
                        'RIGHT':[.2,1.,-.3,0.,0.,0.,1.]}}

    def sample(self,frame=None):
        return self.meter.sample(self.frame if frame is None else frame,self.settings,self.calibration)

    def zero(self):return self.meter.zero(self.frame,self.settings,self.calibration)

    def moved(self,side,delta):
        frame=copy.deepcopy(self.frame)
        frame['trackers'][side][:3]=[v+d for v,d in zip(frame['trackers'][side][:3],delta)]
        return frame

    def test_no_automatic_origin_and_explicit_zero_is_separate_from_calibration(self):
        settings,calibration,frame=map(copy.deepcopy,(self.settings,self.calibration,self.frame))
        state=self.sample()
        self.assertTrue(state['zero_ready']);self.assertFalse(state['has_origin'])
        self.assertTrue(state['needs_zero']);self.assertFalse(state['ready'])
        self.assertIsNone(state['sides']['left']['displacement_cm'])
        state=self.zero()
        self.assertTrue(state['ready']);self.assertEqual(state['origin_label'],'本页测量起点')
        self.assertEqual(state['sides']['left']['displacement_cm'],0.)
        self.assertEqual((self.settings,self.calibration,self.frame),(settings,calibration,frame))

    def test_headset_distance_cancels_body_translation_and_requires_current_head(self):
        self.settings['arm_reference_frame']='headset'
        self.zero()
        value=copy.deepcopy(self.frame)
        for pose in [value['head_pose'],*value['trackers'].values()]:
            pose[:3]=[p+d for p,d in zip(pose[:3],[2.,.5,-3.])]
        state=self.sample(value)
        self.assertTrue(state['ready'])
        self.assertAlmostEqual(state['sides']['left']['displacement_cm'],0.)
        self.assertIn('头显',state['distance_definition'])
        value['head_pose']=None
        self.assertFalse(self.sample(value)['ready'])
        self.settings['arm_reference_frame']='world'
        self.assertTrue(self.sample()['needs_zero'])

    def test_signed_axes_centimeters_and_straight_line_norm(self):
        self.zero();state=self.sample(self.moved('LEFT',[-.3,.1,-.2]))['sides']['left']
        self.assertAlmostEqual(state['forward_cm'],20.)
        self.assertAlmostEqual(state['left_cm'],30.)
        self.assertAlmostEqual(state['up_cm'],10.)
        self.assertAlmostEqual(state['displacement_cm'],math.sqrt(1400))
        reverse=self.sample(self.moved('RIGHT',[.15,-.08,.12]))['sides']['right']
        self.assertAlmostEqual(reverse['forward_cm'],-12.)
        self.assertAlmostEqual(reverse['left_cm'],-15.)
        self.assertAlmostEqual(reverse['up_cm'],-8.)

    def test_headset_meter_ignores_head_tilt_but_origin_continuously_follows_translation(self):
        self.settings['arm_reference_frame']='headset'
        self.zero()
        value=self.moved('LEFT',[0.,0.,-.2])
        for pose in [value['head_pose'],*value['trackers'].values()]:
            pose[:3]=[p+d for p,d in zip(pose[:3],[2.,.1,-1.])]
        for turn in ([math.sin(.3),0.,0.,math.cos(.3)], [0.,0.,math.sin(.4),math.cos(.4)]):
            value['head_pose'][3:]=turn
            state=self.sample(value)
            self.assertTrue(state['ready'])
            self.assertAlmostEqual(state['sides']['left']['forward_cm'],20.)
            self.assertAlmostEqual(state['sides']['left']['up_cm'],0.)
            self.assertAlmostEqual(state['sides']['right']['displacement_cm'],0.)

    def test_distance_is_from_origin_not_accumulated_path(self):
        self.zero()
        self.assertAlmostEqual(self.sample(self.moved('LEFT',[0.,0.,-.2]))['sides']['left']['displacement_cm'],20.)
        self.assertAlmostEqual(self.sample(self.moved('LEFT',[0.,0.,-.1]))['sides']['left']['displacement_cm'],10.)
        self.assertEqual(self.sample()['sides']['left']['displacement_cm'],0.)

    def test_frozen_heading_requires_no_continuous_head_input(self):
        self.zero();frame=self.moved('LEFT',[0.,0.,-.2]);frame['head_pose']=None
        state=self.sample(frame)
        self.assertTrue(state['ready']);self.assertFalse(state['zero_ready'])
        self.assertAlmostEqual(state['sides']['left']['forward_cm'],20.)
        frame['head_pose']=[0.,0.,0.,0.,math.sqrt(.5),0.,math.sqrt(.5)]
        self.assertAlmostEqual(self.sample(frame)['sides']['left']['forward_cm'],20.)

    def test_offset_matches_original_rotated_tracker_lever(self):
        self.settings['tracker_config']['offset_tracker_m']=[.1,0.,0.]
        self.zero();frame=copy.deepcopy(self.frame)
        frame['trackers']['LEFT'][3:]=[0.,0.,math.sqrt(.5),math.sqrt(.5)]
        values=self.sample(frame)['sides']['left']
        self.assertAlmostEqual(values['forward_cm'],0.)
        self.assertAlmostEqual(values['left_cm'],10.)
        self.assertAlmostEqual(values['up_cm'],10.)
        self.assertAlmostEqual(values['displacement_cm'],math.sqrt(200))

    def test_saved_gain_and_calibration_progress_do_not_change_measurements(self):
        self.zero();frame=self.moved('LEFT',[0.,0.,-.2])
        self.settings['tracker_config']['arm_calibration']={'sides':{'left':{'translation_scale':8}}}
        self.calibration.update(distance_cm=30,captured_stages=['origin','forward','left'])
        state=self.sample(frame)
        self.assertEqual(state['target_cm'],30.)
        self.assertEqual(state['current_stage'],'up');self.assertFalse(state['needs_zero'])
        self.assertAlmostEqual(state['sides']['left']['displacement_cm'],20.)
        self.calibration['captured_stages'].append('up')
        self.assertEqual(self.sample(frame)['current_stage'],'complete')

    def test_missing_one_wrist_retains_other_side_and_no_fresh_frame_hides_both(self):
        self.zero();frame=self.moved('LEFT',[0.,0.,-.2]);del frame['trackers']['RIGHT']
        state=self.sample(frame)
        self.assertFalse(state['ready']);self.assertTrue(state['sides']['left']['live'])
        self.assertAlmostEqual(state['sides']['left']['displacement_cm'],20.)
        self.assertFalse(state['sides']['right']['live']);self.assertIsNone(state['sides']['right']['displacement_cm'])
        state=self.meter.sample(None,self.settings,self.calibration)
        self.assertFalse(state['ready']);self.assertFalse(state['needs_zero'])
        self.assertTrue(all(not side['live'] for side in state['sides'].values()))
        self.assertTrue(self.sample()['ready'])

    def test_stationary_frames_are_valid_and_single_selected_side_is_enough(self):
        self.settings['arms_mode']='left';del self.frame['trackers']['RIGHT']
        self.frame['pose_age_ms']={'LEFT':9999999}
        self.zero()
        for _ in range(3):self.assertTrue(self.sample()['ready'])
        self.assertEqual(self.sample()['selected_sides'],['left'])
        self.assertEqual(list(self.sample()['sides']),['left'])

    def test_session_change_stays_invalid_until_explicit_rezero(self):
        self.zero();frame=copy.deepcopy(self.frame);frame['session']='new-stream'
        state=self.sample(frame)
        self.assertTrue(state['needs_zero']);self.assertTrue(state['has_origin'])
        self.assertIsNone(state['sides']['left']['displacement_cm'])
        self.assertTrue(self.sample()['needs_zero'])
        self.assertFalse(self.zero()['needs_zero'])

    def test_settings_changes_invalidate_even_if_old_settings_are_restored(self):
        for key,value in [('left_sn','OTHER'),('offset_tracker_m',[.1,0.,0.])]:
            with self.subTest(key=key):
                self.zero();old=self.settings['tracker_config'][key]
                self.settings['tracker_config'][key]=value
                self.assertTrue(self.sample()['needs_zero'])
                self.settings['tracker_config'][key]=old
                self.assertTrue(self.sample()['needs_zero'])
                self.zero()
        self.settings['arms_mode']='left';self.assertTrue(self.sample()['needs_zero'])

    def test_failed_zero_preserves_existing_measurement_origin(self):
        self.zero();bad=copy.deepcopy(self.frame);bad['head_pose']=[0.]*7
        with self.assertRaises(ValueError):self.meter.zero(bad,self.settings,self.calibration)
        self.assertTrue(self.sample()['ready'])
        bad['head_pose']=self.frame['head_pose'];bad['trackers']['LEFT'][0]=float('nan')
        with self.assertRaises(ValueError):self.meter.zero(bad,self.settings,self.calibration)
        self.assertFalse(self.sample(bad)['sides']['left']['live'])
        self.assertTrue(self.sample()['ready'])

    def test_duplicate_serial_missing_head_invalid_offsets_and_overflow_are_sanitized(self):
        self.settings['tracker_config']['right_sn']='LEFT'
        self.assertFalse(self.sample()['zero_ready'])
        with self.assertRaises(ValueError):self.zero()
        self.settings['tracker_config']['right_sn']='RIGHT'
        self.frame['head_pose']=[0.]*7;self.assertFalse(self.sample()['zero_ready'])
        self.frame['head_pose']=[0.,0.,0.,0.,0.,0.,1.]
        self.settings['tracker_config']['offset_tracker_m']=[float('nan'),0.,0.]
        self.assertFalse(self.sample()['zero_ready'])
        self.settings['tracker_config']['offset_tracker_m']=[0.,0.,0.];self.zero()
        frame=self.moved('LEFT',[1e308,0.,0.]);state=self.sample(frame)
        self.assertFalse(state['sides']['left']['live'])
        json.dumps(state,allow_nan=False)


if __name__=='__main__':unittest.main()
