"""Synthetic source selection; no SDK or hardware."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from arm_pose_source import CONTROLLER_IDS, effective_config, select_frame, source_ids, validate_source

class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.pose=[.1,1.,-.2,0.,0.,0.,1.]
        self.config={'left_sn':'WRIST-L','right_sn':'WRIST-R',
            'offset_tracker_m':[.1,.2,.3],'right_offset_tracker_m':[.4,.5,.6]}
        self.frame={'session':'session-1','seq':1,'sample_mono':100.,'head_pose':list(self.pose),
            'trackers':{'WRIST-L':list(self.pose),'WRIST-R':list(self.pose)},
            'controllers':{'left':list(self.pose),'right':list(self.pose)},
            'controller_sample_mono_by_side':{'left':100.,'right':100.},
            'controller_sample_mono':100.,'tracker_sample_mono':100.,'head_sample_mono':100.}

    def test_legacy_tracker_schema_and_default_config_are_unchanged(self):
        frame={'session':'old','trackers':{'WRIST-L':self.pose}}
        self.assertEqual(validate_source(),'trackers')
        self.assertIs(select_frame(frame,now=100.),frame)
        result=effective_config(self.config)
        self.assertEqual(result,self.config)
        result['offset_tracker_m'][0]=9.
        self.assertEqual(self.config['offset_tracker_m'][0],.1)

    def test_controller_ids_zero_offsets_and_wrist_calibration_isolation(self):
        self.config['arm_calibration']={'schema':'g1-arm-calibration-v1','sides':{
            'left':{'serial':'WRIST-L'},'right':{'serial':'WRIST-R'}}}
        before=copy.deepcopy(self.config)
        result=effective_config(self.config,'controllers')
        self.assertEqual(result['left_sn'],CONTROLLER_IDS['left'])
        self.assertEqual(result['right_sn'],CONTROLLER_IDS['right'])
        self.assertEqual(result['offset_tracker_m'],[0.,0.,0.])
        self.assertEqual(result['right_offset_tracker_m'],[0.,0.,0.])
        self.assertNotIn('arm_calibration',result)
        self.assertEqual(self.config,before)

    def test_controller_calibration_is_preserved_and_idempotent(self):
        self.config['arm_calibration']={'schema':'g1-arm-calibration-v1','sides':{
            'right':{'serial':CONTROLLER_IDS['right'],'translation_scale':2.}}}
        result=effective_config(self.config,'controllers')
        self.assertEqual(result['arm_calibration'],self.config['arm_calibration'])
        self.assertEqual(effective_config(result,'controllers'),result)
        result['arm_calibration']['sides']['right']['translation_scale']=8.
        self.assertEqual(self.config['arm_calibration']['sides']['right']['translation_scale'],2.)

    def test_controller_projection_preserves_session_head_and_does_not_mutate_input(self):
        before=copy.deepcopy(self.frame)
        result=select_frame(self.frame,'controllers',now=100.2)
        self.assertEqual(result['trackers'],{sn:self.pose for sn in CONTROLLER_IDS.values()})
        self.assertEqual(result['session'],'session-1')
        self.assertEqual(result['head_pose'],self.pose)
        result['trackers'][CONTROLLER_IDS['left']][0]=77.
        self.assertEqual(self.frame,before)

    def test_selected_source_projects_only_its_matching_packet_heads(self):
        left_head=[.3,1.5,.1,0.,0.,0.,1.]
        right_head=[.4,1.5,.2,0.,0.,0.,1.]
        tracker_head=[.5,1.5,.3,0.,0.,0.,1.]
        self.frame['controller_head_poses']={'left':left_head,'right':right_head}
        self.frame['tracker_head_poses']={'WRIST-L':tracker_head,'WRIST-R':tracker_head}
        before=copy.deepcopy(self.frame)
        controllers=select_frame(self.frame,'controllers',now=100.2)
        self.assertEqual(controllers['head_pose_by_tracker'],{
            CONTROLLER_IDS['left']:left_head,CONTROLLER_IDS['right']:right_head})
        trackers=select_frame(self.frame,'trackers',now=100.2)
        self.assertEqual(trackers['head_pose_by_tracker'],self.frame['tracker_head_poses'])
        self.assertEqual(controllers['head_pose'],self.pose)
        self.assertEqual(trackers['head_pose'],self.pose)
        controllers['head_pose_by_tracker'][CONTROLLER_IDS['left']][0]=77.
        trackers['head_pose_by_tracker']['WRIST-L'][0]=88.
        self.assertEqual(self.frame,before)

    def test_present_empty_or_invalid_pair_map_never_falls_back_to_global_head(self):
        for source,key in (('controllers','controller_head_poses'),('trackers','tracker_head_poses')):
            for pairs in ({},None,[]):
                with self.subTest(source=source,pairs=pairs):
                    frame=copy.deepcopy(self.frame)
                    frame[key]=pairs
                    result=select_frame(frame,source,now=100.)
                    self.assertEqual(result['head_pose_by_tracker'],{})
                    self.assertEqual(result['head_pose'],self.pose)
                    self.assertTrue(result['trackers'])

    def test_controller_pair_map_excludes_stale_or_missing_sides_without_new_threshold(self):
        self.frame['controller_head_poses']={'left':self.pose,'right':self.pose}
        self.frame['controller_sample_mono_by_side']['left']=99.49
        result=select_frame(self.frame,'controllers',now=100.)
        self.assertEqual(set(result['head_pose_by_tracker']),{CONTROLLER_IDS['right']})
        self.frame['controller_sample_mono_by_side']['left']=99.5
        result=select_frame(self.frame,'controllers',now=100.)
        self.assertEqual(len(result['head_pose_by_tracker']),2)
        self.frame['controller_head_poses'].pop('left')
        result=select_frame(self.frame,'controllers',now=100.)
        self.assertIn(CONTROLLER_IDS['left'],result['trackers'])
        self.assertNotIn(CONTROLLER_IDS['left'],result['head_pose_by_tracker'])

    def test_legacy_pair_fallback_and_invalid_global_head_are_preserved(self):
        for source,key in (('controllers','controller_head_poses'),('trackers','tracker_head_poses')):
            legacy=select_frame(self.frame,source,now=100.)
            self.assertNotIn('head_pose_by_tracker',legacy)
            frame=copy.deepcopy(self.frame)
            frame[key]={'left':self.pose} if source=='controllers' else {'WRIST-L':self.pose}
            frame['head_sample_mono']=99.
            result=select_frame(frame,source,now=100.)
            self.assertIsNone(result['head_pose'])
            self.assertTrue(result['head_pose_by_tracker'])
            self.assertTrue(result['trackers'])

    def test_fresh_other_source_cannot_refresh_stale_tracker_sample(self):
        self.frame['tracker_sample_mono']=99.49
        self.assertIsNone(select_frame(self.frame,'trackers',now=100.))
        self.assertTrue(select_frame(self.frame,'controllers',now=100.)['trackers'])
        self.frame['tracker_sample_mono']=99.5
        self.assertIs(select_frame(self.frame,'trackers',now=100.),self.frame)

    def test_controller_per_side_timestamps_cannot_borrow_group_freshness(self):
        self.frame['controller_sample_mono_by_side']['left']=99.49
        result=select_frame(self.frame,'controllers',now=100.)
        self.assertEqual(set(result['trackers']),{CONTROLLER_IDS['right']})
        self.frame['controller_sample_mono_by_side']['left']=99.5
        self.assertEqual(len(select_frame(self.frame,'controllers',now=100.)['trackers']),2)
        self.frame['controller_sample_mono_by_side'].pop('left')
        self.assertNotIn(CONTROLLER_IDS['left'],select_frame(self.frame,'controllers',now=100.)['trackers'])

    def test_group_timestamp_compatibility_rejects_stale_future_or_unknown_age(self):
        self.frame.pop('controller_sample_mono_by_side')
        self.assertEqual(len(select_frame(self.frame,'controllers',now=100.)['trackers']),2)
        for stamp in (None,True,float('nan'),float('inf'),99.49,100.1):
            with self.subTest(stamp=stamp):
                self.frame['controller_sample_mono']=stamp
                self.assertEqual(select_frame(self.frame,'controllers',now=100.)['trackers'],{})

    def test_absent_source_stays_absent_despite_fresh_historical_timestamp(self):
        self.frame['controllers']={}
        self.assertEqual(select_frame(self.frame,'controllers',now=100.)['trackers'],{})
        self.frame.pop('controllers')
        self.assertIsNone(select_frame(self.frame,'controllers',now=100.))
        self.assertIsNone(select_frame(None,'controllers',now=100.))

    def test_stationary_poses_accept_advancing_source_timestamp(self):
        self.frame['pose_age_ms']={'WRIST-L':999999999.}
        self.frame['controller_sample_mono_by_side']={'left':101.,'right':101.}
        self.assertEqual(len(select_frame(self.frame,'controllers',now=101.1)['trackers']),2)

    def test_stale_head_does_not_remove_fresh_source_after_neutral(self):
        self.frame['head_sample_mono']=99.
        for source in ('trackers','controllers'):
            result=select_frame(self.frame,source,now=100.)
            self.assertIsNone(result['head_pose'])
            self.assertTrue(result['trackers'])
        self.assertEqual(self.frame['head_pose'],self.pose)

    def test_invalid_sources_do_not_silently_fall_back(self):
        for source in (None,True,'controller','debug',{},[]):
            with self.subTest(source=source),self.assertRaises(ValueError):validate_source(source)
        self.assertEqual(source_ids('controllers'),CONTROLLER_IDS)
        self.assertEqual(source_ids('trackers'),{})

if __name__=='__main__':unittest.main()
