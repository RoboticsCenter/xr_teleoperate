"""Synthetic geometry and temporary files only; no SDK or hardware access."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from dashboard_arm_calibration import ArmCalibration, AXES
from pico_tracker.geometry import heading_basis


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ArmCalibration(self.root)
        self.config = {'left_sn':'LEFT', 'right_sn':'RIGHT',
            'offset_tracker_m':[0.,0.,0.], 'right_offset_tracker_m':[0.,0.,0.]}
        self.origin = {'session':'session-1', 'seq':1,
            'head_pose':[0.,1.6,0.,0.,0.,0.,1.],
            'trackers':{'LEFT':[-.2,1.,-.3,0.,0.,0.,1.],
                        'RIGHT':[.2,1.,-.3,0.,0.,0.,1.]}}
        heading=np.asarray(heading_basis(self.origin['head_pose']))
        angle=.12
        yaw=np.array([[np.cos(angle),-np.sin(angle),0.],
                      [np.sin(angle),np.cos(angle),0.],[0.,0.,1.]])
        self.rotations={'left':heading,'right':yaw@heading}
        self.gains={'left':1.25,'right':.8}

    def frame(self, stage, *, multiplier=1.):
        value=copy.deepcopy(self.origin)
        value['seq']+=1+AXES.index(stage)
        axis=np.eye(3)[:,AXES.index(stage)]*.2*multiplier
        for side,sn in [('left','LEFT'),('right','RIGHT')]:
            shift=self.rotations[side].T@axis/self.gains[side]
            value['trackers'][sn][:3]=(np.asarray(value['trackers'][sn][:3])+shift).tolist()
        return value

    def complete(self, manager=None, mode='both'):
        manager=manager or self.manager
        manager.capture('origin',self.origin,self.config,mode)
        for axis in AXES:
            state=manager.capture(axis,self.frame(axis),self.config,mode)
        return state

    def test_inert_construction_and_capture_do_not_create_files(self):
        self.assertFalse(self.manager.status(self.config)['can_save'])
        self.complete()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_headset_calibration_cancels_different_body_transforms_at_each_axis(self):
        manager=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        config=dict(self.config,arm_reference_frame='headset')
        manager.capture('origin',self.origin,config)
        for index,axis in enumerate(AXES):
            value=self.frame(axis)
            angle=.4*(index+1)
            rotation=np.array([[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]])
            shift=np.array([index+2.,.3,-1.])
            for pose in [value['head_pose'],*value['trackers'].values()]:
                pose[:3]=(rotation@np.asarray(pose[:3])+shift).tolist()
                pose[3:]=[0.,float(np.sin(angle/2)),0.,float(np.cos(angle/2))]
            state=manager.capture(axis,value,config)
        self.assertTrue(state['can_save'])
        for side in ('left','right'):
            record=state['candidate']['sides'][side]
            self.assertEqual(record['capture_binding']['arm_reference_frame'],'headset')
            self.assertAlmostEqual(record['translation_scale'],self.gains[side])
            self.assertLess(record['diagnostics']['max_relative_residual'],1e-12)
        manager.save(config)
        restored=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        self.assertTrue(restored.status(config)['saved_compatible'])
        self.assertFalse(restored.status(self.config)['saved_compatible'])
        self.assertFalse((self.root/'config/arm-calibration.json').exists())

    def test_headset_calibration_requires_head_on_every_axis(self):
        manager=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        config=dict(self.config,arm_reference_frame='headset')
        manager.capture('origin',self.origin,config)
        value=self.frame('forward');value['head_pose']=None
        with self.assertRaises(ValueError):manager.capture('forward',value,config)
        self.assertEqual(manager.status(config)['captured_stages'],['origin'])

    def test_head_pitch_during_axis_capture_does_not_mix_vertical_and_forward(self):
        manager=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        config=dict(self.config,arm_reference_frame='headset')
        manager.capture('origin',self.origin,config)
        for axis,angle in zip(AXES,(.35,-.4,.55)):
            value=self.frame(axis)
            value['head_pose'][3:]=[float(np.sin(angle/2)),0.,0.,float(np.cos(angle/2))]
            state=manager.capture(axis,value,config)
        self.assertTrue(state['can_save'])
        for side in ('left','right'):
            self.assertAlmostEqual(state['candidate']['sides'][side]['translation_scale'],self.gains[side])
        self.assertEqual(state['candidate']['arm_reference_mapping'],'headset_heading_v1')

    def test_world_calibration_cannot_be_loaded_as_headset_calibration(self):
        self.complete();self.manager.save(self.config)
        (self.root/'config/arm-calibration-headset.json').write_bytes((self.root/'config/arm-calibration.json').read_bytes())
        manager=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        self.assertIsNone(manager.saved)
        self.assertIn('reference frame',manager.status(dict(self.config,arm_reference_frame='headset'))['saved_error'])

    def test_old_full_rotation_headset_calibration_preserved_but_not_applied(self):
        manager=ArmCalibration(self.root,filename='arm-calibration-headset.json',reference='headset')
        config=dict(self.config,arm_reference_frame='headset')
        manager.capture('origin',self.origin,config)
        for axis in AXES:manager.capture(axis,self.frame(axis),config)
        manager.save(config)
        value=json.loads(manager.path.read_text())
        self.assertEqual(value.pop('arm_reference_mapping'),'headset_heading_v1')
        for record in value['sides'].values():record['capture_binding'].pop('arm_reference_mapping')
        manager.path.write_text(json.dumps(value))
        before=manager.path.read_bytes()
        reloaded=ArmCalibration(self.root,filename=manager.path.name,reference='headset')
        self.assertIsNone(reloaded.saved)
        self.assertIn('reference mapping changed',reloaded.status(config)['saved_error'])
        self.assertEqual(manager.path.read_bytes(),before)

    def test_independent_rotation_and_gain_recovered_from_three_axes(self):
        state=self.complete()
        self.assertTrue(state['can_save'])
        self.assertEqual(state['captured_stages'],['origin','forward','left','up'])
        self.assertFalse(state['candidate_saved'])
        heading=np.asarray(heading_basis(self.origin['head_pose']))
        for side in ('left','right'):
            record=state['candidate']['sides'][side]
            np.testing.assert_allclose(np.asarray(record['rotation_from_heading'])@heading,
                                       self.rotations[side],atol=1e-10)
            self.assertAlmostEqual(record['translation_scale'],self.gains[side])
            self.assertLess(record['diagnostics']['max_relative_residual'],1e-12)

    def test_reflected_axis_accepted_with_proper_orientation_and_position_correction(self):
        self.manager.capture('origin',self.origin,self.config)
        for stage in ('forward','left'):
            self.manager.capture(stage,self.frame(stage),self.config)
        state=self.manager.capture('up',self.frame('up',multiplier=-1),self.config)
        self.assertTrue(state['can_save'])
        self.assertEqual(state['reference'],'operator')
        heading=np.asarray(heading_basis(self.origin['head_pose']))
        for side in ('left','right'):
            record=state['candidate']['sides'][side]
            transform=np.asarray(record['rotation_from_heading'])@heading
            self.assertAlmostEqual(np.linalg.det(transform),1.)
            self.assertLess(np.linalg.det(record['translation_correction']),0.)
            for axis,stage in enumerate(AXES):
                frame=self.frame(stage,multiplier=-1 if stage=='up' else 1)
                delta=np.asarray(frame['trackers'][side.upper()][:3])-self.origin['trackers'][side.upper()][:3]
                np.testing.assert_allclose(np.asarray(record['translation_correction'])@transform@delta,
                                           np.eye(3)[:,axis]*.2,atol=1e-12)

    def test_degenerate_axis_rejected_but_unequal_distances_accepted(self):
        self.manager.capture('origin',self.origin,self.config)
        self.manager.capture('forward',self.frame('forward'),self.config)
        self.manager.capture('left',self.frame('forward'),self.config)
        with self.assertRaisesRegex(ValueError,'线性相关'):
            self.manager.capture('up',self.frame('up'),self.config)
        self.manager.capture('left',self.frame('left'),self.config)
        self.assertTrue(self.manager.capture('up',self.frame('up',multiplier=2),self.config)['can_save'])

    def test_zero_motion_absent_input_and_nonfinite_pose_are_rejected(self):
        self.manager.capture('origin',self.origin,self.config)
        for value in (None,self.frame('forward',multiplier=0)):
            with self.assertRaises(ValueError):
                self.manager.capture('forward',value,self.config)
        broken=self.frame('forward');broken['trackers']['LEFT'][0]=float('nan')
        with self.assertRaises(ValueError): self.manager.capture('forward',broken,self.config)
        self.assertEqual(self.manager.status(self.config)['captured_stages'],['origin'])

    def test_human_distances_override_small_large_sheared_and_anisotropic_readings(self):
        matrices = [np.array([[.01,.02,.03],[0.,.1,.01],[0.,0.,.4]]),
                    np.eye(3)*1e-8, np.diag([4.,5.,6.]),
                    np.array([[.01,.09,.04],[0.,.001,.002],[0.,0.,.003]])]
        for measured in matrices:
            with self.subTest(measured=measured.tolist()):
                self.manager.reset()
                self.manager.capture('origin',self.origin,self.config)
                actual=[20.,30.,40.]
                for axis,stage in enumerate(AXES):
                    frame=copy.deepcopy(self.origin)
                    for sn in ('LEFT','RIGHT'):
                        frame['trackers'][sn][:3]=(np.asarray(self.origin['trackers'][sn][:3])+measured[:,axis]).tolist()
                    state=self.manager.capture(stage,frame,self.config,distance_cm=actual[axis])
                self.assertTrue(state['can_save'])
                for side in ('left','right'):
                    record=state['candidate']['sides'][side]
                    transform=np.asarray(record['rotation_from_heading'])@heading_basis(self.origin['head_pose'])
                    # Use captured finite differences, not ideal pre-rounding fixture values.
                    samples=state['candidate']['capture']['samples']
                    observed=np.column_stack([np.asarray(samples[a]['points_m'][side])-
                        samples['origin']['points_m'][side] for a in AXES])
                    np.testing.assert_allclose(np.asarray(record['translation_correction'])@transform@observed,
                                               np.diag(np.asarray(actual)/100),atol=1e-11)
                    self.assertEqual(record['diagnostics']['desired_distance_cm'],dict(zip(AXES,actual)))
                self.assertTrue(self.manager.save(self.config)['candidate_saved'])

    def test_binding_and_stream_session_cannot_change_inside_draft(self):
        self.manager.capture('origin',self.origin,self.config)
        changed=self.frame('forward');changed['session']='new-session'
        with self.assertRaisesRegex(ValueError,'会话'):
            self.manager.capture('forward',changed,self.config)
        for key,value in [('left_sn','OTHER'),('offset_tracker_m',[.1,0.,0.])]:
            config=dict(self.config,**{key:value})
            with self.assertRaises(ValueError): self.manager.capture('forward',self.frame('forward'),config)
        with self.assertRaises(ValueError):
            self.manager.capture('forward',self.frame('forward'),self.config,'left')
        self.assertFalse(self.manager.status(dict(self.config,left_sn='OTHER'))['can_save'])

    def test_origin_recapture_discards_later_draft_stages(self):
        self.complete()
        state=self.manager.capture('origin',self.origin,self.config)
        self.assertEqual(state['captured_stages'],['origin'])
        self.assertIsNone(state['candidate'])
        self.assertFalse(state['can_save'])
        self.assertEqual(self.manager.capture('origin',self.origin,self.config,distance_cm=30)['distance_cm'],30.)

    def test_atomic_save_backup_and_reset_keep_saved_asset(self):
        self.complete();saved=self.manager.save(self.config)
        path=self.root/'config/arm-calibration.json';first=path.read_bytes()
        self.assertTrue(saved['candidate_saved'])
        self.assertFalse(saved['can_save'])
        reset=self.manager.reset()
        self.assertEqual(path.read_bytes(),first)
        self.assertIsNotNone(reset['saved']);self.assertIsNone(reset['candidate'])
        self.complete();second=self.manager.save(self.config)['saved']
        self.assertEqual(Path(second['backup_path']).read_bytes(),first)
        self.assertEqual(ArmCalibration(self.root).saved,second)
        detached=self.manager.saved;detached['sides']['left']['translation_scale']=9.
        self.assertNotEqual(self.manager.saved,detached)

    def test_save_failure_preserves_existing_file(self):
        self.complete();self.manager.save(self.config)
        path=self.root/'config/arm-calibration.json';before=path.read_bytes()
        self.complete()
        with mock.patch('dashboard_arm_calibration.os.replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError):self.manager.save(self.config)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(path.parent.glob('*.tmp')),[])

    def test_changed_binding_cannot_save_or_claim_saved_compatible(self):
        self.complete();self.manager.save(self.config);self.complete()
        changed=dict(self.config,left_sn='OTHER')
        with self.assertRaises(ValueError):self.manager.save(changed)
        state=self.manager.status(changed)
        self.assertFalse(state['can_save']);self.assertFalse(state['saved_compatible'])
        self.assertIsNotNone(state['saved_error'])

    def test_simulation_save_never_reads_or_overwrites_production(self):
        self.complete();self.manager.save(self.config)
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        simulation=ArmCalibration(self.root,simulation=True)
        self.assertIsNone(simulation.saved)
        self.complete(simulation);state=simulation.save(self.config)
        self.assertTrue(state['simulation']);self.assertTrue(state['saved']['simulation'])
        after={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_single_arm_save_preserves_compatible_other_arm(self):
        self.complete();self.manager.save(self.config)
        right=self.manager.saved['sides']['right']
        self.complete(mode='left');saved=self.manager.save(self.config,'left')['saved']
        self.assertEqual(saved['sides']['right'],right)
        self.assertEqual(saved['preserved_sides'],['right'])
        self.assertEqual(saved['sides']['right']['capture_binding'],right['capture_binding'])

    def test_missing_side_uses_original_mapping_but_changed_offset_is_incompatible(self):
        self.complete(mode='left');self.manager.save(self.config,'left')
        state=self.manager.status(self.config,'both')
        self.assertTrue(state['saved_compatible']);self.assertFalse(state['saved_complete'])
        self.assertIsNone(state['saved_error'])
        state=self.manager.status(dict(self.config,offset_tracker_m=[.01,0.,0.]),'both')
        self.assertFalse(state['saved_compatible'])
        self.assertIn('安装偏移',state['saved_error'])

    def test_production_rejects_persisted_simulation_artifact(self):
        state=self.complete();value=state['candidate'];value['simulation']=True
        path=self.root/'config/arm-calibration.json';path.parent.mkdir()
        path.write_text(json.dumps(value))
        manager=ArmCalibration(self.root)
        self.assertIsNone(manager.saved)
        self.assertIn('simulation',manager.status(self.config)['saved_error'])

    def test_malformed_saved_binding_is_reported_without_crashing_status(self):
        state=self.complete();value=state['candidate']
        value['sides']['left']['capture_binding']=['bad']
        path=self.root/'config/arm-calibration.json';path.parent.mkdir()
        path.write_text(json.dumps(value))
        manager=ArmCalibration(self.root)
        self.assertIsNone(manager.saved)
        self.assertIn('binding',manager.status(self.config)['saved_error'])

    def test_head_and_distance_must_be_valid(self):
        bad=copy.deepcopy(self.origin);bad['head_pose']=None
        with self.assertRaises(ValueError):self.manager.capture('origin',bad,self.config)
        for distance in (True,float('nan'),0.,101.):
            with self.assertRaises(ValueError):
                self.manager.capture('origin',self.origin,self.config,distance_cm=distance)
        with self.assertRaises(ValueError):self.manager.save(self.config)


if __name__=='__main__':unittest.main()
