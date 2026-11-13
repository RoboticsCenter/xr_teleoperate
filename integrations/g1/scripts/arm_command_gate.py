#!/usr/bin/env python3
"""G1 arm_sdk 门控：CLI仅离线；实际输出仅通过显式授权的 ArmOutputSession。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import platform


XR_COMMIT = "817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b"
SDK_COMMIT = "814556d15970dd2ecf1c9984e845ca02ab07e206"
ARM_INDICES = tuple(range(15, 29))
WEIGHT_INDEX = 29
# Fixed official g1_body29_hand14.urdf; left7 + right7, radians.
LOWER = (-3.0892, -1.5882, -2.618, -1.0472, -1.972222054, -1.614429558, -1.614429558,
         -3.0892, -2.2515, -2.618, -1.0472, -1.972222054, -1.614429558, -1.614429558)
UPPER = (2.6704, 2.2515, 2.618, 2.0944, 1.972222054, 1.614429558, 1.614429558,
         2.6704, 1.5882, 2.618, 2.0944, 1.972222054, 1.614429558, 1.614429558)
KP = [80., 80., 80., 80., 40., 40., 40.] * 2
KD = [3., 3., 3., 3., 1.5, 1.5, 1.5] * 2
SDK_FILES = {
    "core/channel.py": "8a58eea2bc6bb8792e5b5fa76949407bd8c2ef8af07172d1a3a465bcc544e247",
    "idl/default.py": "b3101722e07534c667d4c673677a8a5d57643f307cfdeb11f4761ebc456b18ea",
    "idl/unitree_hg/msg/dds_/_LowCmd_.py": "a23cec88e4bf782263ac2bea29d3908c9d21a44a14361ffc0bf3424cd36597ae",
    "idl/unitree_hg/msg/dds_/_MotorCmd_.py": "710a8321be4d6731093b6f8328c70ab11606372561ed01a7af9a187143f54c4d",
    "utils/crc.py": "b95a530423f72c5acc96811f75677699cb3a185b6327353e7c44b355a425a20e",
}
CRC_BINARIES = {
    "x86_64": ("crc_amd64.so", "b136a91a7e99c5cf914f465e131f63c897a17107939184a6f85d1a57cf315ade"),
    "aarch64": ("crc_aarch64.so", "a4db103653db78540d141ff4dc55216b83e5298280e639b2ad54a84ac2fae9a5"),
}


def finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 14:
        raise ValueError(f"{label} requires 14 joints, left7 then right7")
    return [finite(v, label) for v in value]


@dataclass(frozen=True)
class Config:
    # Conservative commissioning proposal, not a hardware-validated safety bound.
    active_indices: tuple = (10,)  # right elbow, motor index25
    calibration_id: str = ""
    enable_source: str = ""
    regular_mode_confirmed: bool = False
    target_age_s: float = .1
    feedback_age_s: float = .1
    enable_age_s: float = .1
    max_dt_s: float = .05
    max_command_hz: float = 250.
    max_joint_speed_rad_s: float = .1
    max_measured_speed_rad_s: float = .3
    max_tracking_error_rad: float = .05
    max_excursion_rad: float = .03
    limit_margin_rad: float = .02
    weight_rate_per_s: float = .5

    def __post_init__(self):
        object.__setattr__(self, "active_indices", tuple(self.active_indices))

    def validate(self):
        if type(self.regular_mode_confirmed) is not bool or any(not isinstance(getattr(self, field), str)
                                                               for field in ("calibration_id", "enable_source")):
            raise ValueError("confirmation must be bool; calibration/enable IDs must be strings")
        if not self.active_indices or len(set(self.active_indices)) != len(self.active_indices):
            raise ValueError("active_indices must be nonempty and unique")
        if any(type(i) is not int or not 0 <= i < 14 for i in self.active_indices):
            raise ValueError("active_indices must index left7+right7")
        for field in ("target_age_s", "feedback_age_s", "enable_age_s", "max_dt_s", "max_command_hz",
                      "max_joint_speed_rad_s", "max_measured_speed_rad_s", "max_tracking_error_rad",
                      "max_excursion_rad", "weight_rate_per_s"):
            if finite(getattr(self, field), field) <= 0:
                raise ValueError(field + " must be positive")
        if not 0 <= finite(self.limit_margin_rad, "limit_margin_rad") <= .1:
            raise ValueError("limit margin must be 0–0.1rad")
        if self.max_command_hz > 250 or self.max_joint_speed_rad_s > .5 or self.max_excursion_rad > .1:
            raise ValueError("commissioning envelope: <=250Hz, <=0.5rad/s, <=0.1rad excursion")
        if self.max_dt_s < 1 / self.max_command_hz:
            raise ValueError("max_dt_s below command period")
        if max(self.target_age_s, self.feedback_age_s, self.enable_age_s) > .5 or self.max_dt_s > .1:
            raise ValueError("freshness <=0.5s and loop deadline <=0.1s required")


class ArmCommandGate:
    """Pure state machine; no SDK imports and no publisher.

    Latching stops new candidates. Firmware behavior after stream loss is not
    known, so a latch is NOT a claim that the physical robot is held/stopped.
    """
    def __init__(self, config=Config()):
        config.validate()
        self.config = config
        self.state, self.reason, self.weight = "DISARMED", None, 0.
        self.last_q = self.reference_q = self.mode_machine = self.reference_id = None
        self.last_now = self.last_command_t = None
        self.seen, self.used_reference_ids = {}, set()
        self.last_feedback_tick = self.last_feedback_sequence = None

    def output(self, candidate=None):
        result = {"state": self.state, "reason": self.reason, "weight": self.weight,
                  "published": False, "acknowledged": False, "motion_ready": False,
                  "handover_required": self.weight > 0}
        if candidate is not None:
            result["command_candidate"] = candidate
        return result

    def latch(self, reason):
        self.state, self.reason = "LATCHED", reason
        return self.output()

    def sample(self, record, kind, now, age):
        if not isinstance(record, dict):
            raise ValueError(f"missing {kind}")
        stamp = finite(record.get("received_t"), kind + ".received_t")
        if not 0 <= stamp <= now or now - stamp > age:
            raise ValueError(f"stale_or_future_{kind}")
        seq = record.get("sequence")
        if type(seq) is not int or seq < 0:
            raise ValueError(f"invalid_{kind}_sequence")
        fingerprint = json.dumps(record, sort_keys=True, allow_nan=False)
        previous = self.seen.get(kind)
        if previous is not None:
            old_seq, old_stamp, old_fingerprint = previous
            if seq < old_seq or (seq == old_seq and fingerprint != old_fingerprint):
                raise ValueError(f"replayed_or_rewritten_{kind}")
            if seq > old_seq and stamp <= old_stamp:
                raise ValueError(f"regressed_{kind}_receipt")
        self.seen[kind] = (seq, stamp, fingerprint)
        return record

    def check_q(self, q):
        margin = self.config.limit_margin_rad
        if any(not lo + margin <= value <= hi - margin for value, lo, hi in zip(q, LOWER, UPPER)):
            raise ValueError("joint_limit_or_margin")

    def feedback(self, record, now):
        record = self.sample(record, "feedback", now, self.config.feedback_age_s)
        tick = record.get("tick")
        if type(tick) is not int or not 0 <= tick < 2**32:
            raise ValueError("invalid_lowstate_tick")
        if self.last_feedback_sequence is not None and record["sequence"] > self.last_feedback_sequence:
            delta = (tick - self.last_feedback_tick) % (2**32)
            if not 0 < delta < 2**31:
                raise ValueError("replayed_or_regressed_lowstate_tick")
        self.last_feedback_tick, self.last_feedback_sequence = tick, record["sequence"]
        q, dq = vector(record.get("q"), "feedback.q"), vector(record.get("dq"), "feedback.dq")
        self.check_q(q)
        mode = record.get("mode_machine")
        if type(mode) is not int or not 0 <= mode <= 255 or record.get("mode_pr") != 0:
            raise ValueError("unsupported_feedback_mode_fields")
        if self.mode_machine is not None and mode != self.mode_machine:
            raise ValueError("mode_machine_changed")
        if max(map(abs, dq)) > self.config.max_measured_speed_rad_s:
            raise ValueError("measured_speed_exceeded")
        return q, mode

    def target(self, record, now):
        record = self.sample(record, "target", now, self.config.target_age_s)
        if record.get("tracking_valid") is not True or record.get("calibration_id") != self.config.calibration_id:
            raise ValueError("target_validity_or_calibration")
        reference = record.get("reference_id")
        if not isinstance(reference, str) or not reference:
            raise ValueError("missing_target_reference")
        if self.reference_id is not None and reference != self.reference_id:
            raise ValueError("target_reference_changed")
        q = vector(record.get("q"), "target.q")
        self.check_q(q)
        return q, reference

    def enabled(self, record, now):
        record = self.sample(record, "enable", now, self.config.enable_age_s)
        if record.get("source_id") != self.config.enable_source or type(record.get("held")) is not bool:
            raise ValueError("invalid_manual_enable_source")
        return record["held"]

    def candidate(self, mode):
        return {"topic": "rt/arm_sdk", "mode_pr": 0, "mode_machine": mode,
                "arm_motor_indices": list(ARM_INDICES), "q": list(self.last_q),
                "dq": [0.] * 14, "tau": [0.] * 14, "kp": list(KP), "kd": list(KD),
                "weight_motor_index": WEIGHT_INDEX, "weight": self.weight}

    def step(self, now, feedback=None, target=None, enable=None, action=None):
        try:
            return self._step(now, feedback, target, enable, action)
        except (ValueError, TypeError, KeyError) as exc:
            return self.latch(str(exc))

    def _step(self, now, feedback, target, enable, action):
        now = finite(now, "now")
        if now < 0 or (self.last_now is not None and now <= self.last_now):
            raise ValueError("nonmonotonic_gate_clock")
        dt = 0. if self.last_now is None else now - self.last_now
        self.last_now = now
        if action not in (None, "arm", "release", "reset"):
            raise ValueError("unknown_manual_action")
        if self.state in ("RAMP_IN", "ACTIVE", "RAMP_OUT") and dt > self.config.max_dt_s:
            raise ValueError("control_loop_deadline_missed")
        if self.state in ("DISARMED", "RELEASED") and action is None:
            return self.output()
        q_measured, mode = self.feedback(feedback, now)
        if action == "reset":
            if self.state not in ("LATCHED", "RELEASED") or self.weight != 0 or self.enabled(enable, now):
                raise ValueError("reset_requires_zero_weight_and_released_enable")
            self.state, self.reason, self.reference_id = "DISARMED", None, None
            self.last_command_t = None
            return self.output()
        if action == "release":
            if self.last_q is None:
                self.state = "RELEASED"
                return self.output()
            if max(abs(a - b) for a, b in zip(self.last_q, q_measured)) > self.config.max_tracking_error_rad:
                raise ValueError("release_tracking_error_requires_external_recovery")
            self.state, self.reason = "RAMP_OUT", "manual_handover_requested"
            if now - self.last_command_t + 1e-12 < 1 / self.config.max_command_hz:
                return self.output()
            # Resuming after a latched gap must not collapse the weight ramp.
            self.last_command_t = now
            return self.output(self.candidate(mode))
        if self.state == "LATCHED":
            return self.output()  # restored input never auto-rearms
        if action == "arm":
            if self.state != "DISARMED" or self.weight != 0:
                raise ValueError("arm_requires_disarmed_zero_weight")
            if self.config.regular_mode_confirmed is not True or not self.config.calibration_id or not self.config.enable_source:
                raise ValueError("missing_reviewed_regular_mode_calibration_or_enable")
            q_target, reference = self.target(target, now)
            if reference in self.used_reference_ids:
                raise ValueError("new_clutch_reference_required")
            if not self.enabled(enable, now):
                raise ValueError("manual_enable_not_held")
            if max(abs(a - b) for a, b in zip(q_target, q_measured)) > 1e-6:
                raise ValueError("first_target_must_equal_measured_reference")
            self.reference_id, self.reference_q = reference, list(q_measured)
            self.used_reference_ids.add(reference)
            self.mode_machine, self.last_q = mode, list(q_measured)
            self.weight, self.state, self.reason = 0., "RAMP_IN", None
            self.last_command_t = now
            return self.output(self.candidate(mode))
        if self.last_q is None:
            return self.output()
        if max(abs(a - b) for a, b in zip(self.last_q, q_measured)) > self.config.max_tracking_error_rad:
            raise ValueError("command_measured_tracking_error")
        if self.state != "RAMP_OUT":
            if not self.enabled(enable, now):
                raise ValueError("manual_enable_released")
            q_target, _ = self.target(target, now)
            if max(abs(a - b) for a, b in zip(q_target, self.reference_q)) > self.config.max_excursion_rad:
                raise ValueError("reference_excursion_exceeded")
            if any(abs(q_target[i] - self.reference_q[i]) > 1e-6 for i in range(14)
                   if i not in self.config.active_indices):
                raise ValueError("target_changed_unselected_joint")
        command_dt = now - self.last_command_t
        if command_dt + 1e-12 < 1 / self.config.max_command_hz:
            return self.output()
        if command_dt > self.config.max_dt_s:
            raise ValueError("command_deadline_missed")
        if self.state == "RAMP_OUT":
            self.weight = max(0., self.weight - self.config.weight_rate_per_s * command_dt)
            if self.weight == 0:
                self.state = "RELEASED"
        elif self.state == "RAMP_IN":
            self.weight = min(1., self.weight + self.config.weight_rate_per_s * command_dt)
            if self.weight == 1:
                self.state = "ACTIVE"
        elif self.state == "ACTIVE":
            step = self.config.max_joint_speed_rad_s * command_dt
            self.last_q = [max(measured - self.config.max_tracking_error_rad,
                               min(measured + self.config.max_tracking_error_rad,
                                   previous + max(-step, min(step, goal - previous))))
                           for previous, goal, measured in zip(self.last_q, q_target, q_measured)]
        self.last_command_t = now
        return self.output(self.candidate(mode))


def encode_idl(candidate, factory, crc):
    """In-memory official HG LowCmd shape; no transport or mode change."""
    if candidate.get("topic") != "rt/arm_sdk":
        raise ValueError("only rt/arm_sdk is supported")
    mode, weight = candidate["mode_machine"], finite(candidate["weight"], "weight")
    if type(mode) is not int or not 0 <= mode <= 255 or not 0 <= weight <= 1:
        raise ValueError("invalid mode_machine or SDK weight")
    q = vector(candidate["q"], "q")
    if any(not lo <= value <= hi for value, lo, hi in zip(q, LOWER, UPPER)):
        raise ValueError("joint limits")
    dq, tau, kp, kd = [vector(candidate[key], key) for key in ("dq", "tau", "kp", "kd")]
    if any(value != 0 for value in dq + tau) or kp != KP or kd != KD:
        raise ValueError("unreviewed dq/tau/gains")
    msg = factory()
    if len(msg.motor_cmd) != 35:
        raise ValueError("G1 requires 35 HG motor slots")
    msg.mode_pr, msg.mode_machine, msg.reserve, msg.crc = 0, mode, [0] * 4, 0
    for motor in msg.motor_cmd:
        motor.mode, motor.q, motor.dq, motor.tau = 0, 0., 0., 0.
        motor.kp, motor.kd, motor.reserve = 0., 0., 0
    for i, index in enumerate(ARM_INDICES):
        motor = msg.motor_cmd[index]
        motor.mode, motor.q, motor.dq, motor.tau = 1, q[i], dq[i], tau[i]
        motor.kp, motor.kd = kp[i], kd[i]
    msg.motor_cmd[WEIGHT_INDEX].q = weight
    msg.crc = crc.Crc(msg)  # final operation before write
    return msg


def official_transport(interface):
    """Lazy import: reachable only after explicit motion config + first arm gate."""
    spec = importlib.util.find_spec("unitree_sdk2py")
    if spec is None or not spec.submodule_search_locations or platform.system() != "Linux":
        raise RuntimeError("reviewed Linux Unitree SDK package is required")
    root = Path(next(iter(spec.submodule_search_locations)))
    if platform.machine() not in CRC_BINARIES:
        raise RuntimeError("unreviewed CRC architecture")
    binary, checksum = CRC_BINARIES[platform.machine()]
    for relative, expected in dict(SDK_FILES, **{f"utils/lib/{binary}": checksum}).items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"SDK file differs from reviewed {SDK_COMMIT}: {relative}")
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
    from unitree_sdk2py.utils.crc import CRC
    crc = CRC()
    ChannelFactoryInitialize(0, interface)
    publisher = ChannelPublisher("rt/arm_sdk", LowCmd_)
    try:
        publisher.Init()
    except Exception:
        publisher.Close()
        raise
    return publisher, unitree_hg_msg_dds__LowCmd_, crc


class ArmOutputSession:
    """Explicit actual write path. Constructing the default never loads DDS.

    step(action='release') performs actual ramp-out writes if enable_motion=True.
    close() only closes transport: it never silently writes, homes or changes mode.
    """
    def __init__(self, config=Config(), *, enable_motion=False, interface=None,
                 exclusive_control_confirmed=False, motion_review_id=None, transport_factory=official_transport):
        self.gate = ArmCommandGate(config)
        self.enable_motion = enable_motion is True
        if type(enable_motion) is not bool:
            raise ValueError("enable_motion must be an explicit bool")
        if self.enable_motion and (config.regular_mode_confirmed is not True or exclusive_control_confirmed is not True
                                   or not isinstance(interface, str) or not interface.strip()
                                   or not isinstance(motion_review_id, str) or not motion_review_id.strip()):
            raise ValueError("actual output requires Regular mode, exclusive control, interface, motion_review_id")
        self.interface, self.transport_factory = interface, transport_factory
        self.publisher = self.message_factory = self.crc = None
        self.closed = self.transport_failed = self.zero_weight_written = False
        self.write_count = 0

    def step(self, **record):
        if self.closed or self.transport_failed:
            return self.gate.latch("output_closed_or_failed_requires_new_session")
        result = self.gate.step(**record)
        candidate = result.get("command_candidate")
        if not self.enable_motion or candidate is None:
            return result
        try:
            if self.publisher is None:
                if candidate["weight"] != 0 or result["state"] != "RAMP_IN":
                    raise ValueError("first actual write must be measured reference with weight0")
                self.publisher, self.message_factory, self.crc = self.transport_factory(self.interface)
            msg = encode_idl(candidate, self.message_factory, self.crc)
            if self.publisher.Write(msg) is not True:
                raise RuntimeError("DDS Write did not report success")
            self.write_count += 1
            self.zero_weight_written = candidate["weight"] == 0
            result.update(published=True, acknowledged=False, actual_write_count=self.write_count)
        except Exception as exc:
            self.transport_failed = True
            result = self.gate.latch(f"transport_failure:{type(exc).__name__}:{exc}")
            result["write_delivery_unknown"] = True
            result["handover_required"] = True
        return result

    def close(self):
        self.closed = True
        error = None
        if self.publisher is not None:
            try:
                self.publisher.Close()
            except Exception as exc:
                error = str(exc)
        return {"closed": error is None, "actual_write_count": self.write_count, "published": False,
                "zero_weight_write_reported": self.zero_weight_written,
                "handover_incomplete": self.enable_motion and (self.write_count > 0 or self.transport_failed)
                    and (self.gate.state != "RELEASED" or not self.zero_weight_written or self.transport_failed),
                "physical_handover_verified": False, "close_error": error}


def demo_records():
    config = Config(calibration_id="SYNTHETIC_ONLY", enable_source="SYNTHETIC_BUTTON", regular_mode_confirmed=True)
    session = ArmOutputSession(config)
    for seq in range(440):
        now = 100. + seq * .01
        reference = [.1] * 14
        q = list(session.gate.last_q or reference)
        target = list(reference)
        if seq > 210:
            target[10] += .01
        record = {"now": now,
                  "feedback": {"sequence": seq, "tick": seq, "received_t": now, "q": q, "dq": [0.] * 14, "mode_machine": 5, "mode_pr": 0},
                  "target": {"sequence": seq, "received_t": now, "q": target, "tracking_valid": True, "calibration_id": "SYNTHETIC_ONLY", "reference_id": "synthetic-reference"},
                  "enable": {"sequence": seq, "received_t": now, "held": seq != 230, "source_id": "SYNTHETIC_BUTTON"}}
        if seq in (0, 231):
            record["action"] = "arm" if seq == 0 else "release"
        yield session.step(**record)
    yield session.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo", action="store_true")
    p.add_argument("--config", type=Path)
    p.add_argument("--input", type=Path, help="离线JSONL：now/feedback/target/enable/action")
    args = p.parse_args(argv)
    if args.demo:
        if args.config or args.input:
            p.error("--demo 不能混用配置/输入")
        records = demo_records()
    else:
        if not args.config or not args.input:
            p.error("CLI仅离线：提供 --config 与 --input，或 --demo")
        session = ArmOutputSession(Config(**json.loads(args.config.read_text())))
        records = (session.step(**json.loads(line)) for line in args.input.read_text().splitlines() if line.strip())
    for result in records:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
