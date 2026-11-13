"""Headset-relative pose math only; no SDK or robot connections."""
import copy
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from arm_reference_frame import reference_frame, reference_mapping, validate_reference_frame
from branch_arm_input import capture
from pico_tracker.geometry import rotation


IDENTITY = [0., 0., 0., 0., 0., 0., 1.]


def multiply(a, b):
    x,y,z,w=a; X,Y,Z,W=b
    return [w*X+x*W+y*Z-z*Y, w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W, w*W-x*X-y*Y-z*Z]


def rigid_pose(pose, turn, translation):
    point=np.asarray(rotation([0.,0.,0.]+turn))@np.asarray(pose[:3])+translation
    return point.tolist()+multiply(turn,pose[3:])


class ReferenceFrameTests(unittest.TestCase):
    def setUp(self):
        self.frame={'session':'one','seq':2,'sample_mono':100.,'head_sample_mono':100.,
            'head_pose':[0.,1.6,0.,0.,0.,0.,1.],
            'trackers':{'L':[-.2,1.1,-.3,0.,0.,0.,1.],
                        'R':[.2,1.1,-.3,.1,0.,0.,math.sqrt(.99)]}}

    def test_world_is_exact_legacy_passthrough(self):
        self.assertIs(reference_frame(self.frame),self.frame)
        self.assertIsNone(reference_frame(None,'world'))
        self.frame['head_pose']=None
        self.assertIs(reference_frame(self.frame,'world'),self.frame)
        for invalid in ('head','',None,False):
            with self.assertRaises(ValueError):validate_reference_frame(invalid)
        self.assertEqual(reference_mapping('world'),'world_v1')
        self.assertEqual(reference_mapping('headset'),'headset_heading_v1')

    def test_relative_pose_and_metadata_are_independent_copies(self):
        before=copy.deepcopy(self.frame)
        result=reference_frame(self.frame,'headset')
        self.assertEqual(self.frame,before)
        self.assertEqual(result['head_pose'],IDENTITY)
        self.assertEqual(result['arm_reference_mapping'],'headset_heading_v1')
        np.testing.assert_allclose(result['trackers']['L'][:3],[-.2,-.5,-.3])
        self.assertEqual(result['trackers']['R'][3:],self.frame['trackers']['R'][3:])
        for key in ('session','seq','sample_mono','head_sample_mono'):
            self.assertEqual(result[key],self.frame[key])
        result['trackers']['L'][0]=9.
        self.assertEqual(self.frame,before)

    def test_common_translation_and_yaw_preserve_relative_pose_and_targets(self):
        original=reference_frame(self.frame,'headset')
        robot={side:np.eye(4).tolist() for side in ('left','right')}
        config={'left_sn':'L','right_sn':'R','arm_reference_frame':'headset',
                'offset_tracker_m':[.02,0.,0.],'right_offset_tracker_m':[0.,.03,0.]}
        captured=capture(original,[0.]*14,robot,config,'both')
        expected=captured.targets(original,robot)
        turn=[0.,math.sin(.7),0.,math.cos(.7)]
        moved=copy.deepcopy(self.frame)
        moved['head_pose']=rigid_pose(moved['head_pose'],turn,np.array([3.,-2.,.7]))
        moved['trackers']={sn:rigid_pose(pose,turn,np.array([3.,-2.,.7]))
                            for sn,pose in moved['trackers'].items()}
        result=reference_frame(moved,'headset')
        for sn in ('L','R'):
            np.testing.assert_allclose(result['trackers'][sn],original['trackers'][sn],atol=1e-14)
        actual=captured.targets(result,robot)
        for side in ('left','right'):
            np.testing.assert_allclose(actual[side],expected[side],atol=1e-14)
        self.assertEqual(captured.metadata()['arm_reference_frame'],'headset')

    def test_forward_left_up_are_robot_axes_at_multiple_head_yaws(self):
        robot={side:np.eye(4).tolist() for side in ('left','right')}
        config={'left_sn':'L','arm_reference_frame':'headset'}
        for degrees in (-170.,-90.,-30.,0.,45.,90.,170.):
            yaw=math.radians(degrees)
            turn=[0.,math.sin(yaw/2),0.,math.cos(yaw/2)]
            heading=np.asarray(rotation([0.,0.,0.]+turn))
            base=copy.deepcopy(self.frame)
            base['head_pose']=rigid_pose(base['head_pose'],turn,np.array([1.,2.,3.]))
            base['trackers']={sn:rigid_pose(pose,turn,np.array([1.,2.,3.]))
                              for sn,pose in base['trackers'].items()}
            captured=capture(reference_frame(base,'headset'),[0.]*14,robot,config,'left')
            for axis,local in enumerate(([0.,0.,-.2],[-.2,0.,0.],[0.,.2,0.])):
                with self.subTest(yaw=degrees,axis=axis):
                    moved=copy.deepcopy(base)
                    moved['trackers']['L'][:3]=(np.array(moved['trackers']['L'][:3])+heading@local).tolist()
                    target=captured.targets(reference_frame(moved,'headset'),robot)['left']
                    np.testing.assert_allclose(np.asarray(target)[:3,3],np.eye(3)[axis]*.2,atol=1e-14)

    def test_head_pitch_and_roll_alone_do_not_move_or_rotate_arm_targets(self):
        robot={side:np.eye(4).tolist() for side in ('left','right')}
        config={'left_sn':'L','right_sn':'R','arm_reference_frame':'headset'}
        for yaw in (-1.4,0.,1.8):
            q_yaw=[0.,math.sin(yaw/2),0.,math.cos(yaw/2)]
            base=copy.deepcopy(self.frame)
            base['head_pose'][3:]=q_yaw
            initial=reference_frame(base,'headset')
            captured=capture(initial,[0.]*14,robot,config,'both')
            expected=captured.targets(initial,robot)
            for pitch,roll in ((.5,0.),(-.8,0.),(0.,.9),(.6,-.7)):
                with self.subTest(yaw=yaw,pitch=pitch,roll=roll):
                    moved=copy.deepcopy(base)
                    q_pitch=[math.sin(pitch/2),0.,0.,math.cos(pitch/2)]
                    q_roll=[0.,0.,math.sin(roll/2),math.cos(roll/2)]
                    moved['head_pose'][3:]=multiply(multiply(q_yaw,q_pitch),q_roll)
                    transformed=reference_frame(moved,'headset')
                    targets=captured.targets(transformed,robot)
                    for side in ('left','right'):
                        np.testing.assert_allclose(targets[side],expected[side],atol=1e-14)

    def test_near_vertical_heading_is_unavailable_then_recovers(self):
        from pico_tracker.geometry import heading_basis
        for angle in (math.pi/2,-math.pi/2):
            frame=copy.deepcopy(self.frame)
            frame['head_pose'][3:]=[math.sin(angle/2),0.,0.,math.cos(angle/2)]
            with self.assertRaises(ValueError):heading_basis(frame['head_pose'])
            self.assertIsNone(reference_frame(frame,'headset'))
        self.assertEqual(set(reference_frame(self.frame,'headset')['trackers']),{'L','R'})
        paired=copy.deepcopy(self.frame)
        paired['head_pose_by_tracker']={'L':[0.,1.6,0.,math.sqrt(.5),0.,0.,math.sqrt(.5)],
                                       'R':self.frame['head_pose']}
        self.assertEqual(set(reference_frame(paired,'headset')['trackers']),{'R'})

    def test_relative_hand_motion_changes_position_and_wrist_orientation(self):
        origin=reference_frame(self.frame,'headset')
        robot={side:np.eye(4).tolist() for side in ('left','right')}
        config={'left_sn':'L','arm_reference_frame':'headset'}
        captured=capture(origin,[0.]*14,robot,config,'left')
        moved=copy.deepcopy(self.frame)
        moved['trackers']['L'][2]-=.2
        moved['trackers']['L'][3:]=[math.sin(.2),0.,0.,math.cos(.2)]
        target=captured.targets(reference_frame(moved,'headset'),robot)['left']
        self.assertAlmostEqual(target[0][3],.2)
        self.assertFalse(np.allclose(np.asarray(target)[:3,:3],np.eye(3)))

    def test_head_only_yaw_changes_relative_hand_position_and_orientation(self):
        origin=reference_frame(self.frame,'headset')
        moved=copy.deepcopy(self.frame)
        moved['head_pose'][3:]=[0.,math.sqrt(.5),0.,math.sqrt(.5)]
        result=reference_frame(moved,'headset')
        self.assertFalse(np.allclose(result['trackers']['L'][:3],origin['trackers']['L'][:3]))
        self.assertFalse(np.allclose(rotation(result['trackers']['L']),rotation(origin['trackers']['L'])))

    def test_matching_head_is_used_when_current_head_has_advanced(self):
        original=reference_frame(self.frame,'headset')
        self.frame['head_pose_by_tracker']={sn:list(self.frame['head_pose']) for sn in ('L','R')}
        self.frame['head_pose']=[5.,6.,7.,0.,math.sqrt(.5),0.,math.sqrt(.5)]
        result=reference_frame(self.frame,'headset')
        for sn in ('L','R'):
            np.testing.assert_allclose(result['trackers'][sn],original['trackers'][sn])

    def test_present_pair_map_never_falls_back_to_global_head(self):
        for pairs in ({},None,{'L':IDENTITY,'R':[0.]*7}):
            self.frame['head_pose_by_tracker']=pairs
            result=reference_frame(self.frame,'headset')
            self.assertEqual(set(result['trackers']),{'L'} if isinstance(pairs,dict) and 'L' in pairs else set())

    def test_missing_invalid_current_head_holds_then_same_session_recovers(self):
        original=copy.deepcopy(self.frame)
        for invalid in (None,[0.]*7,[float('nan')]+IDENTITY[1:]):
            self.frame['head_pose']=invalid
            self.frame['head_pose_by_tracker']={'L':original['head_pose']}
            self.assertIsNone(reference_frame(self.frame,'headset'))
        recovered=reference_frame(original,'headset')
        self.assertEqual(recovered['session'],'one')
        self.assertEqual(set(recovered['trackers']),{'L','R'})

    def test_double_transformation_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError,'already been applied'):
            reference_frame(reference_frame(self.frame,'headset'),'headset')


if __name__ == '__main__':unittest.main()
