import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


spec = importlib.util.spec_from_file_location("arm_command_gate", Path(__file__).resolve().parents[1] / "scripts/arm_command_gate.py")
arm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = arm
spec.loader.exec_module(arm)


def config(**changes):
    values = {"calibration_id": "test-calibration", "enable_source": "test-deadman", "regular_mode_confirmed": True}
    return arm.Config(**dict(values, **changes))


def frame(seq, q=None, measured=None, held=True, action=None):
    now = 100. + seq * .01
    return {"now": now, "action": action,
            "feedback": {"sequence": seq, "tick": seq, "received_t": now, "q": measured or [.1] * 14,
                         "dq": [0.] * 14, "mode_machine": 5, "mode_pr": 0},
            "target": {"sequence": seq, "received_t": now, "q": q or [.1] * 14,
                       "tracking_valid": True, "calibration_id": "test-calibration", "reference_id": "reference-1"},
            "enable": {"sequence": seq, "received_t": now, "held": held, "source_id": "test-deadman"}}


class Publisher:
    def __init__(self):
        self.messages = []
        self.closed = False
        self.succeed = True

    def Write(self, msg):
        self.messages.append(copy.deepcopy(msg))
        return self.succeed

    def Close(self):
        self.closed = True


class CRC:
    def __init__(self):
        self.messages = []

    def Crc(self, msg):
        self.messages.append(copy.deepcopy(msg))
        return 0x12345678  # fake CRC verifies placement/order, not wire checksum


def factory():
    return SimpleNamespace(motor_cmd=[SimpleNamespace() for _ in range(35)])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.gate = arm.ArmCommandGate(config())

    def activate(self):
        self.gate.step(**frame(0, action="arm"))
        for seq in range(1, 202):
            result = self.gate.step(**frame(seq))
        self.assertEqual(result["state"], "ACTIVE")
        return 202

    def test_first_candidate_is_measured_not_zero(self):
        result = self.gate.step(**frame(0, action="arm"))
        self.assertEqual(result["command_candidate"]["q"], [.1] * 14)
        self.assertEqual(result["weight"], 0)
        self.assertFalse(result["published"])

    def test_no_regular_mode_confirmation_cannot_arm(self):
        result = arm.ArmCommandGate(config(regular_mode_confirmed=False)).step(**frame(0, action="arm"))
        self.assertEqual(result["state"], "LATCHED")
        self.assertNotIn("command_candidate", result)

    def test_nonmatching_initial_target_is_rejected(self):
        q = [.1] * 14
        q[10] = .11
        result = self.gate.step(**frame(0, q=q, action="arm"))
        self.assertEqual(result["reason"], "first_target_must_equal_measured_reference")

    def test_ramp_preserves_reference_then_active_limits_speed(self):
        seq = self.activate()
        q = [.1] * 14
        q[10] = .12
        result = self.gate.step(**frame(seq, q=q))
        self.assertAlmostEqual(result["command_candidate"]["q"][10], .101, places=8)
        self.assertEqual(result["command_candidate"]["q"][:3], [.1] * 3)

    def test_unselected_joint_and_excursion_and_limit_are_rejected(self):
        for index, value, reason in ((7, .11, "target_changed_unselected_joint"),
                                     (10, .15, "reference_excursion_exceeded"),
                                     (10, 3., "joint_limit_or_margin")):
            with self.subTest(reason=reason):
                gate = arm.ArmCommandGate(config())
                gate.step(**frame(0, action="arm"))
                q = [.1] * 14
                q[index] = value
                result = gate.step(**frame(1, q=q))
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("command_candidate", result)

    def test_stale_target_latches_and_fresh_data_does_not_rearm(self):
        self.gate.step(**frame(0, action="arm"))
        value = frame(1)
        value["target"]["received_t"] -= .2
        result = self.gate.step(**value)
        self.assertEqual(result["reason"], "stale_or_future_target")
        result = self.gate.step(**frame(2))
        self.assertEqual(result["state"], "LATCHED")
        self.assertNotIn("command_candidate", result)

    def test_enable_release_and_stale_feedback_do_not_emit(self):
        for which in ("release", "stale-enable", "stale-feedback"):
            gate = arm.ArmCommandGate(config())
            gate.step(**frame(0, action="arm"))
            value = frame(1, held=which != "release")
            if which.startswith("stale"):
                value[which.split("-")[1]]["received_t"] -= .2
            result = gate.step(**value)
            self.assertEqual(result["state"], "LATCHED")
            self.assertNotIn("command_candidate", result)

    def test_deadline_overrun_cannot_create_large_motion_step(self):
        self.gate.step(**frame(0, action="arm"))
        result = self.gate.step(**frame(10))
        self.assertEqual(result["reason"], "control_loop_deadline_missed")

    def test_lowstate_tick_replay_is_rejected_and_uint32_wrap_is_allowed(self):
        first = frame(0, action="arm")
        first["feedback"]["tick"] = 2**32 - 1
        self.gate.step(**first)
        second = frame(1)
        second["feedback"]["tick"] = 0
        self.assertIn("command_candidate", self.gate.step(**second))
        third = frame(2)
        third["feedback"]["tick"] = 0
        self.assertEqual(self.gate.step(**third)["reason"], "replayed_or_regressed_lowstate_tick")

    def test_mode_change_latches_and_is_never_replaced_by_hardcoded_mode(self):
        self.gate.step(**frame(0, action="arm"))
        value = frame(1)
        value["feedback"]["mode_machine"] = 6
        result = self.gate.step(**value)
        self.assertEqual(result["reason"], "mode_machine_changed")
        self.assertNotIn("command_candidate", result)

    def test_same_sequence_cannot_refresh_receipt_or_change_data(self):
        self.gate.step(**frame(0, action="arm"))
        value = frame(1)
        value["target"]["sequence"] = 0
        self.assertEqual(self.gate.step(**value)["reason"], "replayed_or_rewritten_target")

    def test_manual_handover_ramps_weight_only_no_go_home(self):
        seq = self.activate()
        self.gate.step(**frame(seq, held=False))
        result = self.gate.step(**frame(seq + 1, action="release"))
        self.assertEqual(result["state"], "RAMP_OUT")
        previous = result["weight"]
        for current in range(seq + 2, seq + 210):
            result = self.gate.step(**frame(current))
            self.assertLessEqual(result["weight"], previous)
            if "command_candidate" in result:
                self.assertEqual(result["command_candidate"]["q"], [.1] * 14)
            previous = result["weight"]
        self.assertEqual(result["state"], "RELEASED")
        self.assertEqual(result["weight"], 0)

    def test_releasing_after_large_measured_drift_requires_external_recovery(self):
        self.gate.step(**frame(0, action="arm"))
        q = [.2] * 14
        result = self.gate.step(**frame(1, measured=q, action="release"))
        self.assertEqual(result["reason"], "release_tracking_error_requires_external_recovery")
        self.assertNotIn("command_candidate", result)

    def test_release_event_does_not_bypass_maximum_candidate_rate(self):
        self.gate.step(**frame(0, action="arm"))
        value = frame(1, action="release")
        value["now"] = 100.001
        value["feedback"]["received_t"] = 100.001
        result = self.gate.step(**value)
        self.assertEqual(result["state"], "RAMP_OUT")
        self.assertNotIn("command_candidate", result)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.publisher, self.crc = Publisher(), CRC()
        self.transport = Mock(return_value=(self.publisher, factory, self.crc))

    def actual_session(self):
        return arm.ArmOutputSession(config(), enable_motion=True, interface="test-interface",
                                    exclusive_control_confirmed=True, motion_review_id="TEST_ONLY",
                                    transport_factory=self.transport)

    def test_preview_never_opens_transport_even_when_armed(self):
        session = arm.ArmOutputSession(config(), transport_factory=self.transport)
        session.step(**frame(0, action="arm"))
        session.close()
        self.transport.assert_not_called()

    def test_actual_path_requires_explicit_reviews_then_opens_lazily(self):
        with self.assertRaises(ValueError):
            arm.ArmOutputSession(config(), enable_motion=True, transport_factory=self.transport)
        session = self.actual_session()
        self.transport.assert_not_called()
        result = session.step(**frame(0, action="arm"))
        self.transport.assert_called_once_with("test-interface")
        self.assertTrue(result["published"])
        self.assertFalse(result["acknowledged"])

    def test_crc_is_last_and_fields_are_only_arms_plus_weight(self):
        session = self.actual_session()
        session.step(**frame(0, action="arm"))
        msg = self.publisher.messages[0]
        self.assertEqual(msg.mode_machine, 5)
        self.assertEqual(msg.mode_pr, 0)
        self.assertEqual(msg.motor_cmd[29].q, 0)
        self.assertEqual([msg.motor_cmd[i].q for i in range(15, 29)], [.1] * 14)
        self.assertEqual(msg.crc, 0x12345678)
        self.assertEqual(self.crc.messages[0].crc, 0)
        for i in list(range(15)) + list(range(30, 35)):
            self.assertEqual((msg.motor_cmd[i].mode, msg.motor_cmd[i].kp, msg.motor_cmd[i].tau), (0, 0, 0))

    def test_write_failure_latches_and_does_not_retry_even_release(self):
        self.publisher.succeed = False
        session = self.actual_session()
        result = session.step(**frame(0, action="arm"))
        self.assertEqual(result["state"], "LATCHED")
        self.assertTrue(result["write_delivery_unknown"])
        session.step(**frame(1, action="release"))
        self.assertEqual(len(self.publisher.messages), 1)
        self.assertTrue(session.close()["handover_incomplete"])

    def test_actual_release_writes_final_zero_and_close_does_not_add_commands(self):
        session = self.actual_session()
        session.step(**frame(0, action="arm"))
        for seq in range(1, 202):
            session.step(**frame(seq))
        session.step(**frame(202, action="release"))
        for seq in range(203, 410):
            session.step(**frame(seq))
        self.assertEqual(self.publisher.messages[-1].motor_cmd[29].q, 0)
        count = len(self.publisher.messages)
        result = session.close()
        self.assertFalse(result["handover_incomplete"])
        self.assertFalse(result["physical_handover_verified"])
        self.assertEqual(len(self.publisher.messages), count)
        self.assertTrue(self.publisher.closed)

    def test_close_without_handover_reports_incomplete_and_never_homes(self):
        session = self.actual_session()
        session.step(**frame(0, action="arm"))
        session.step(**frame(1))
        count = len(self.publisher.messages)
        result = session.close()
        self.assertTrue(result["handover_incomplete"])
        self.assertEqual(len(self.publisher.messages), count)


if __name__ == "__main__":
    unittest.main()
