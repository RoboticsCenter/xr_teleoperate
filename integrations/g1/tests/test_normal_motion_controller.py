"""Exercise the pinned controller lifecycle using only fake DDS endpoints."""
import copy
import importlib.util
import logging
from pathlib import Path
import sys
import time
from types import ModuleType,SimpleNamespace as NS
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from pc2_branch_arm import motion_controller
ROOT=Path(__file__).resolve().parents[1]

def module(name,**values):
    result=ModuleType(name);result.__dict__.update(values);return result

def message():
    return NS(mode_pr=0,mode_machine=0,crc=0,motor_cmd=[NS(mode=0,q=0.,dq=0.,tau=0.,kp=0.,kd=0.) for _ in range(35)])

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.writes=[];self.topics=[]
        state=NS(mode_machine=5,motor_state=[NS(q=.3+i*.001,dq=0.) for i in range(35)])
        self.state=state
        writes,topics=self.writes,self.topics
        class Publisher:
            def __init__(self,topic,*_):topics.append(topic)
            def Init(self):pass
            def Write(self,msg):writes.append(copy.deepcopy(msg));return True
        class Subscriber:
            def __init__(self,*_):pass
            def Init(self):pass
            def Read(self):return state
        mods={
          'unitree_sdk2py.core.channel':module('channel',ChannelPublisher=Publisher,ChannelSubscriber=Subscriber,ChannelFactoryInitialize=lambda *_:None),
          'unitree_sdk2py.idl.unitree_hg.msg.dds_':module('hg',LowCmd_=object,LowState_=object),
          'unitree_sdk2py.idl.unitree_go.msg.dds_':module('go',LowCmd_=object,LowState_=object),
          'unitree_sdk2py.idl.default':module('default',unitree_hg_msg_dds__LowCmd_=message,unitree_go_msg_dds__LowCmd_=message),
          'unitree_sdk2py.utils.crc':module('crc',CRC=lambda:NS(Crc=lambda msg:123)),
          'logging_mp':module('logging_mp',getLogger=logging.getLogger),
          'teleop.robot_control.dds_utils':module('dds_utils',wait_for_dds=self.wait_ready)}
        with patch.dict(sys.modules,mods):
            spec=importlib.util.spec_from_file_location('source_arm_fixture',ROOT/'vendor/xr_h2/teleop/robot_control/robot_arm.py')
            source=importlib.util.module_from_spec(spec);spec.loader.exec_module(source)
        self.controller=motion_controller(source)()
        self.addCleanup(lambda: self.controller.release() if self.controller.publish_thread.is_alive() else None)

    @staticmethod
    def wait_ready(predicate,*_):
        deadline=time.monotonic()+1
        while not predicate() and time.monotonic()<deadline:time.sleep(.001)
        assert predicate()

    def test_launch_has_zero_authority_and_source_waist_support(self):
        self.assertEqual(self.topics,['rt/arm_sdk'])
        self.assertTrue(self.writes)
        for msg in self.writes:
            self.assertEqual(msg.motor_cmd[29].q,0.)
            for i,motor in enumerate(msg.motor_cmd):
                if 12<=i<=28: self.assertAlmostEqual(motor.q,.3+i*.001)
                elif i!=29:self.assertEqual(vars(motor),vars(message().motor_cmd[i]))
            for i in (12,13,14):
                self.assertEqual(msg.motor_cmd[i].mode,1)
                self.assertEqual(msg.motor_cmd[i].kp,self.controller.kp_high)
                self.assertEqual(msg.motor_cmd[i].kd,self.controller.kd_high)
        self.assertEqual(self.controller.arm_weight,0.)

    def test_start_blends_and_stop_releases_without_zero_posture(self):
        self.controller.activate()
        deadline=time.monotonic()+1
        while self.controller.arm_weight<.05 and time.monotonic()<deadline:time.sleep(.005)
        self.assertGreater(self.controller.arm_weight,0.)
        self.assertLess(self.controller.arm_weight,.5)
        self.controller.ctrl_dual_arm(np.full(14,.4),np.full(14,.1))
        time.sleep(.02)
        self.assertTrue(self.controller.release())
        self.assertEqual(self.writes[-1].motor_cmd[29].q,0.)
        self.assertFalse(self.controller.publish_thread.is_alive())
        self.assertFalse(self.controller.subscribe_thread.is_alive())
        for msg in self.writes:
            for i in range(15,29):self.assertGreater(msg.motor_cmd[i].q,.25)
            for i in list(range(12))+list(range(30,35)):
                self.assertEqual(vars(msg.motor_cmd[i]),vars(message().motor_cmd[i]))
            for i in (12,13,14):
                self.assertEqual(msg.motor_cmd[i].kp,300.)
                self.assertEqual(msg.motor_cmd[i].kd,3.)

    def test_start_recaptures_waist_once_and_holds_during_arm_motion(self):
        expected=[.01,-.02,.03]
        for i,q in zip((12,13,14),expected):self.state.motor_state[i].q=q
        deadline=time.monotonic()+1
        while self.controller.get_current_motor_q()[12]!=expected[0] and time.monotonic()<deadline:time.sleep(.005)
        self.controller.activate()
        active_writes_start=len(self.writes)
        self.controller.ctrl_dual_arm(np.full(14,.4),np.full(14,.1))
        for i in (12,13,14):self.state.motor_state[i].q=.2
        time.sleep(.02)
        msg=self.writes[-1]
        self.assertGreater(msg.motor_cmd[29].q,0.)
        self.assertEqual([msg.motor_cmd[i].q for i in (12,13,14)],expected)
        self.assertEqual([msg.motor_cmd[i].kp for i in (12,13,14)],[300.]*3)
        self.assertTrue(self.controller.release())
        self.assertEqual(self.writes[-1].motor_cmd[29].q,0.)
        for frame in self.writes[active_writes_start:]:
            self.assertEqual([frame.motor_cmd[i].q for i in (12,13,14)],expected)
            self.assertEqual([frame.motor_cmd[i].kp for i in (12,13,14)],[300.]*3)
            self.assertEqual([frame.motor_cmd[i].kd for i in (12,13,14)],[3.]*3)

    def test_invalid_waist_feedback_cannot_activate_authority(self):
        with patch.object(self.controller,'get_current_motor_q',return_value=np.full(35,float('nan'))):
            with self.assertRaisesRegex(RuntimeError,'finite measured waist'):
                self.controller.activate()
        self.assertEqual(self.controller._weight_target,0.)

    def test_release_does_not_wait_for_arm_to_reach_home(self):
        self.controller.activate()
        self.controller.ctrl_dual_arm(np.full(14,1.),np.zeros(14))
        self.assertTrue(self.controller.release())
        self.assertEqual(self.writes[-1].motor_cmd[29].q,0.)
        self.assertGreater(self.writes[-1].motor_cmd[22].q,0.)

if __name__=='__main__':unittest.main()
