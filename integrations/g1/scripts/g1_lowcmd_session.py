"""Explicit G1 hanging right-arm lowcmd session; no network at import/construction.

The owner must call step on a bounded local loop and decode fresh lowstate with
CRC validation. A latch stops new targets, not the physical robot. close never
sends an implicit command. damp_and_latch is an explicit motor command.
"""
from dataclasses import dataclass
import hashlib
import importlib.util
import math
from pathlib import Path
import platform
import time


RIGHT = tuple(range(22, 29))
LOWER = (-3.0892, -2.2515, -2.618, -1.0472, -1.972222054, -1.614429558, -1.614429558)
UPPER = (2.6704, 1.5882, 2.618, 2.0944, 1.972222054, 1.614429558, 1.614429558)
# Project commissioning ceilings based on examples, NOT hardware maximum gains.
KP_CEILING, KD_CEILING, DAMPING_CEILING = 40., 1., 8.
SDK_COMMIT = '814556d15970dd2ecf1c9984e845ca02ab07e206'
SDK_FILES = {
    'core/channel.py': '8a58eea2bc6bb8792e5b5fa76949407bd8c2ef8af07172d1a3a465bcc544e247',
    'idl/default.py': 'b3101722e07534c667d4c673677a8a5d57643f307cfdeb11f4761ebc456b18ea',
    'idl/unitree_hg/msg/dds_/_LowCmd_.py': 'a23cec88e4bf782263ac2bea29d3908c9d21a44a14361ffc0bf3424cd36597ae',
    'idl/unitree_hg/msg/dds_/_MotorCmd_.py': '710a8321be4d6731093b6f8328c70ab11606372561ed01a7af9a187143f54c4d',
    'utils/crc.py': 'b95a530423f72c5acc96811f75677699cb3a185b6327353e7c44b355a425a20e',
}
CRC_BINARIES = {
    'x86_64': ('crc_amd64.so', 'b136a91a7e99c5cf914f465e131f63c897a17107939184a6f85d1a57cf315ade'),
    'aarch64': ('crc_aarch64.so', 'a4db103653db78540d141ff4dc55216b83e5298280e639b2ad54a84ac2fae9a5'),
}


class PreWriteExpired(ValueError):
    """Validation rejected before Write; the existing transport did not fail."""


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('nonfinite_or_invalid_' + name)
    return float(value)


def vector(value, count, name):
    if not isinstance(value, (tuple, list)) or len(value) != count:
        raise ValueError('wrong_length_' + name)
    return tuple(number(x, name) for x in value)


def uint(value, maximum, name):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('invalid_' + name)
    return value


@dataclass(frozen=True)
class Config:
    # Required: no gain configuration is silently selected for hardware.
    right_kp: tuple
    right_kd: tuple
    nonright_kd: tuple
    calibration_id: str
    enable_source: str
    gain_review_id: str
    input_policy_id: str = 'vendor_tracking'
    max_excursion_rad: float = .03
    max_speed_rad_s: float = .05
    max_measured_speed_rad_s: float = .5
    max_tracking_error_rad: float = .05
    max_state_age_s: float = .1
    max_target_age_s: float = .1
    max_enable_age_s: float = .1
    max_loop_gap_s: float = .05
    max_command_hz: float = 500.
    gain_ramp_s: float = 1.
    max_session_s: float = 60.

    def __post_init__(self):
        if self.input_policy_id not in ('vendor_tracking', 'supervised_position_only_v1', 'supervised_tracker_window_v1'):
            raise ValueError('unknown_input_policy')
        if self.input_policy_id != 'vendor_tracking' and number(self.max_session_s, 'max_session_s') > 10:
            raise ValueError('supervised_position_only_requires_at_most_10_seconds')
        if self.input_policy_id == 'supervised_tracker_window_v1' and self.enable_source != 'authorized_ten_second_window':
            raise ValueError('tracker_policy_requires_explicit_authorized_window_source')
        for field, count, cap in [('right_kp', 7, KP_CEILING), ('right_kd', 7, KD_CEILING),
                                  ('nonright_kd', 22, DAMPING_CEILING)]:
            values = vector(getattr(self, field), count, field)
            if any(not 0 < x <= cap for x in values):
                raise ValueError(field + '_outside_project_commissioning_ceiling')
            object.__setattr__(self, field, values)
        for field in ('calibration_id', 'enable_source', 'gain_review_id'):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValueError('missing_' + field)
        for field, cap in [('max_excursion_rad', .03), ('max_speed_rad_s', .1),
                           ('max_measured_speed_rad_s', .5), ('max_tracking_error_rad', .05),
                           ('max_state_age_s', .1), ('max_target_age_s', .1),
                           ('max_enable_age_s', .1), ('max_loop_gap_s', .05),
                           ('max_command_hz', 500.), ('max_session_s', 60.)]:
            if not 0 < number(getattr(self, field), field) <= cap:
                raise ValueError('outside_project_envelope_' + field)
        if not 1 <= number(self.gain_ramp_s, 'gain_ramp_s') <= 10:
            raise ValueError('gain_ramp_s_must_be_1_to_10')
        if self.max_loop_gap_s < 1 / self.max_command_hz:
            raise ValueError('loop_gap_shorter_than_command_period')


def decode_lowstate(message, *, received_t, sequence, crc):
    """Call in the receive callback; received_t must be this host's monotonic time.

    Never replace receive time when polling a cached message. CRC is the reviewed
    SDK CRC() instance; it supports HG LowState as well as HG LowCmd.
    """
    if len(message.motor_state) != 35 or crc.Crc(message) != message.crc:
        raise ValueError('invalid_lowstate_shape_or_crc')
    motors = message.motor_state[:29]
    return dict(received_t=number(received_t, 'received_t'), sequence=uint(sequence, 2**63-1, 'sequence'),
                tick=uint(message.tick, 2**32-1, 'tick'), mode_machine=uint(message.mode_machine, 255, 'mode_machine'),
                mode_pr=uint(message.mode_pr, 255, 'mode_pr'), crc_valid=True,
                q=vector([m.q for m in motors], 29, 'q'), dq=vector([m.dq for m in motors], 29, 'dq'),
                motorstate=[uint(m.motorstate, 2**32-1, 'motorstate') for m in motors])


def encode_idl(candidate, factory, crc):
    """Create a fresh complete HG frame. No arm_sdk weight slot is used."""
    if candidate['topic'] != 'rt/lowcmd' or candidate['mode_pr'] != 0:
        raise ValueError('wrong_lowcmd_topic_or_mode')
    msg = factory()
    if len(msg.motor_cmd) != 35:
        raise ValueError('expected_35_motor_slots')
    msg.mode_pr = 0
    msg.mode_machine = uint(candidate['mode_machine'], 255, 'mode_machine')
    msg.reserve, msg.crc = [0] * 4, 0
    fields = {key: vector(candidate[key], 29, key) for key in ('q', 'dq', 'tau', 'kp', 'kd')}
    if any(x != 0 for x in fields['dq'] + fields['tau']):
        raise ValueError('nonzero_velocity_or_feedforward_not_reviewed')
    if any(x != 0 for x in fields['q'][:22] + fields['kp'][:22]):
        raise ValueError('nonright_position_control_not_allowed')
    if any(not 0 < x <= DAMPING_CEILING for x in fields['kd'][:22]):
        raise ValueError('nonright_damping_required')
    for i in range(22, 29):
        if not 0 <= fields['kp'][i] <= KP_CEILING or not 0 < fields['kd'][i] <= KD_CEILING:
            raise ValueError('right_gain_ceiling')
        if not LOWER[i-22] <= fields['q'][i] <= UPPER[i-22]:
            raise ValueError('right_joint_limit')
    for i, motor in enumerate(msg.motor_cmd):
        motor.mode, motor.q, motor.dq, motor.tau = 0, 0., 0., 0.
        motor.kp, motor.kd, motor.reserve = 0., 0., 0
        if i < 29:
            motor.mode = 1
            for key, values in fields.items():
                setattr(motor, key, values[i])
    msg.crc = crc.Crc(msg)
    return msg


def official_transport(interface):
    """Only called after explicit enable passes the local gate."""
    spec = importlib.util.find_spec('unitree_sdk2py')
    if spec is None or not spec.submodule_search_locations or platform.system() != 'Linux':
        raise RuntimeError('reviewed_Linux_SDK_required')
    root = Path(next(iter(spec.submodule_search_locations)))
    if platform.machine() not in CRC_BINARIES:
        raise RuntimeError('unreviewed_architecture')
    binary, checksum = CRC_BINARIES[platform.machine()]
    for path, expected in dict(SDK_FILES, **{'utils/lib/' + binary: checksum}).items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != expected:
            raise RuntimeError('SDK_hash_mismatch:' + path)
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
    from unitree_sdk2py.utils.crc import CRC
    ChannelFactoryInitialize(0, interface)
    publisher = ChannelPublisher('rt/lowcmd', LowCmd_)
    try:
        publisher.Init()
    except Exception:
        publisher.Close()
        raise
    return publisher, unitree_hg_msg_dds__LowCmd_, CRC()


class LowCmdSession:
    """Single-owner loop; call step continuously, never from competing threads.

    No background watchdog is hidden here. Blocking DDS writes / a stopped owner
    loop cannot be converted into a timely physical stop by this class.
    """
    def __init__(self, config=None, *, enable_motion=False, interface=None,
                 transport_factory=official_transport, clock=time.monotonic):
        if config is not None and not isinstance(config, Config):
            raise ValueError('Config_required')
        if type(enable_motion) is not bool:
            raise ValueError('enable_motion_must_be_bool')
        if enable_motion and (config is None or not isinstance(interface, str) or not interface.strip()):
            raise ValueError('explicit_config_and_interface_required')
        self.config, self.enable_motion, self.interface = config, enable_motion, interface
        self.transport_factory = transport_factory
        self.clock = clock
        self.publisher = self.factory = self.crc = None
        self.state, self.reason = 'DISABLED', None
        self.closed, self.write_count = False, 0
        self.now = self.last_write_t = self.started_t = None
        self.reference = self.last_q = self.mode_machine = self.reference_id = None
        self.seen = {}
        self.feedback_sequence = self.feedback_tick = None
        self.transport_failed = self.damping_attempted = False
        self.prepared_t = self.prepared_target_sequence = self.prepared_enable_sequence = None
        self.authorized_window_started = self.authorized_window_deadline = None

    def result(self, candidate=None, **extra):
        out = dict(state=self.state, reason=self.reason, published=False, acknowledged=False,
                   write_count=self.write_count, physical_stop_verified=False)
        if candidate is not None:
            out['command_candidate'] = candidate
        out.update(extra)
        return out

    def latch(self, reason):
        self.state, self.reason = 'LATCHED', str(reason)
        return self.result()

    def stamp(self, record, kind, now, max_age):
        if not isinstance(record, dict):
            raise ValueError('missing_' + kind)
        stamp = number(record.get('received_t'), kind + '_time')
        seq = uint(record.get('sequence'), 2**63-1, kind + '_sequence')
        if not 0 <= stamp <= now or now - stamp > max_age:
            raise ValueError('stale_or_future_' + kind)
        prior = self.seen.get(kind)
        if prior and (seq < prior[0] or stamp < prior[1] or (seq == prior[0] and stamp != prior[1])):
            raise ValueError('reordered_or_retimestamps_cached_' + kind)
        self.seen[kind] = (seq, stamp)

    def feedback(self, record, now):
        self.stamp(record, 'feedback', now, self.config.max_state_age_s)
        q, dq = vector(record.get('q'), 29, 'state_q'), vector(record.get('dq'), 29, 'state_dq')
        faults = record.get('motorstate')
        if record.get('crc_valid') is not True or not isinstance(faults, (tuple, list)) or len(faults) != 29:
            raise ValueError('unverified_crc_or_fault_fields')
        if any(uint(x, 2**32-1, 'motorstate') != 0 for x in faults):
            raise ValueError('motor_fault')
        if any(abs(x) > self.config.max_measured_speed_rad_s for x in dq):
            raise ValueError('measured_speed_limit')
        mode = uint(record.get('mode_machine'), 255, 'mode_machine')
        if record.get('mode_pr') != 0 or type(record.get('mode_pr')) is not int:
            raise ValueError('state_not_PR')
        if self.mode_machine is not None and mode != self.mode_machine:
            raise ValueError('machine_mode_changed')
        tick = uint(record.get('tick'), 2**32-1, 'tick')
        seq = record['sequence']
        if self.feedback_sequence is not None and seq > self.feedback_sequence:
            delta = (tick - self.feedback_tick) & 0xffffffff
            if not 0 < delta < 0x80000000:
                raise ValueError('state_tick_not_forward')
        self.feedback_sequence, self.feedback_tick = seq, tick
        right = q[22:29]
        if any(not lo + .01 <= x <= hi - .01 for x, lo, hi in zip(right, LOWER, UPPER)):
            raise ValueError('measured_right_joint_limit')
        return right, mode

    def candidate(self, q, kp):
        return dict(topic='rt/lowcmd', mode_machine=self.mode_machine, mode_pr=0,
                    q=[0.] * 22 + list(q), dq=[0.] * 29, tau=[0.] * 29,
                    kp=[0.] * 22 + list(kp), kd=list(self.config.nonright_kd + self.config.right_kd))

    def _emit(self, candidate, now, *, damping=False):
        if self.closed or self.started_t is None or self.mode_machine is None or self.transport_failed:
            return self.latch('output_requires_enabled_healthy_session')
        if not self.enable_motion:
            self.last_write_t = now
            return self.result(candidate)
        try:
            if self.publisher is None:
                self.publisher, self.factory, self.crc = self.transport_factory(self.interface)
            msg = encode_idl(candidate, self.factory, self.crc)
            write_time = number(self.clock(), 'write_time')
            if not 0 <= write_time - now <= self.config.max_loop_gap_s:
                raise PreWriteExpired('transport_setup_or_encode_deadline')
            checks = [('feedback', self.config.max_state_age_s)]
            if not damping:
                checks += [('target', self.config.max_target_age_s), ('enable', self.config.max_enable_age_s)]
            for kind, limit in checks:
                if write_time - self.seen[kind][1] > limit:
                    raise PreWriteExpired('expired_before_write_' + kind)
            if not damping and self.authorized_window_deadline is not None and write_time >= self.authorized_window_deadline:
                raise PreWriteExpired('authorized_window_expired_before_write')
            if self.publisher.Write(msg) is not True:
                raise RuntimeError('DDS_Write_not_successful')
            self.write_count += 1
            completed = number(self.clock(), 'write_completed')
            self.last_write_t = completed
            if not 0 <= completed - write_time <= self.config.max_loop_gap_s:
                self.latch('DDS_write_blocked_past_deadline')
                return self.result(published=True, handover_required=True)
            return self.result(candidate, published=True)
        except PreWriteExpired as exc:
            # No Write was attempted. Latch targets, but permit the owner's one
            # explicit damping attempt with a NEW valid feedback snapshot.
            return self.latch('prewrite_revalidation:'+str(exc))
        except Exception as exc:
            self.transport_failed = True
            self.latch('transport_failure:' + str(exc))
            return self.result(write_delivery_unknown=True)

    def step(self, *, now, feedback, target, enable, action=None, readiness=None):
        if self.closed or self.state == 'LATCHED':
            return self.result()
        if self.config is None:
            return self.latch('explicit_gain_config_required')
        try:
            now = number(now, 'now')
            if self.now is not None and (now < self.now or
                    (self.state == 'ACTIVE' and now - self.now > self.config.max_loop_gap_s)):
                raise ValueError('clock_reversal_or_loop_deadline')
            self.now = now
            measured, mode = self.feedback(feedback, now)
            self.stamp(target, 'target', now, self.config.max_target_age_s)
            self.stamp(enable, 'enable', now, self.config.max_enable_age_s)
            goal = vector(target.get('q'), 7, 'right_target')
            if self.config.input_policy_id == 'vendor_tracking':
                input_ok = (target.get('tracking_valid') is True and
                            target.get('input_policy_id', 'vendor_tracking') == 'vendor_tracking' and
                            (target.get('supervised_input_accepted') is False or
                             target.get('supervised_input_accepted') is None))
            else:
                input_ok = (target.get('tracking_valid') is None and
                            target.get('input_policy_id') == self.config.input_policy_id and
                            target.get('supervised_input_accepted') is True)
            if not input_ok or target.get('calibration_id') != self.config.calibration_id:
                raise ValueError('invalid_tracking_or_calibration')
            reference_id = target.get('reference_id')
            if not isinstance(reference_id, str) or not reference_id:
                raise ValueError('missing_target_reference')
            enabled = (enable.get('window_active') is True if self.config.input_policy_id == 'supervised_tracker_window_v1'
                       else enable.get('held') is True)
            if not enabled or enable.get('source_id') != self.config.enable_source:
                raise ValueError('enable_inactive_or_wrong_source')
            if self.config.input_policy_id == 'supervised_tracker_window_v1':
                window_start = number(enable.get('window_started_t'), 'window_start')
                deadline = number(enable.get('window_deadline_t'), 'window_deadline')
                if (not 0 <= window_start <= now < deadline or
                        not 0 < deadline-window_start <= self.config.max_session_s+1e-12):
                    raise ValueError('authorized_window_expired_or_invalid')
                if self.authorized_window_started is not None and (window_start != self.authorized_window_started or
                        deadline != self.authorized_window_deadline):
                    raise ValueError('authorized_window_cannot_restart')
                self.authorized_window_started, self.authorized_window_deadline = window_start, deadline
            if action not in (None, 'enable', 'prepare'):
                raise ValueError('unknown_action_use_explicit_damp_and_latch')
            if self.state == 'DISABLED':
                if action not in ('enable', 'prepare'):
                    return self.result()
                if not isinstance(readiness, dict):
                    raise ValueError('missing_readiness')
                stamp = number(readiness.get('received_t'), 'readiness_time')
                if not 0 <= now - stamp <= 1.:
                    raise ValueError('readiness_stale')
                if type(readiness.get('check_mode_code')) is not int or readiness['check_mode_code'] != 0 or readiness.get('motion_name') != '':
                    raise ValueError('motion_service_not_confirmed_inactive')
                if any(readiness.get(key) is not True for key in ('suspended', 'operator_ready', 'exclusive_control_confirmed')):
                    raise ValueError('physical_or_control_readiness_missing')
                if any(abs(x-y) > self.config.max_excursion_rad + 1e-12 for x,y in zip(goal, measured)):
                    raise ValueError('initial_target_outside_window')
                if action == 'prepare':
                    if not self.enable_motion or self.publisher is not None or self.prepared_t is not None:
                        raise ValueError('preparation_requires_motion_permission_and_unopened_transport')
                    # Official Writer.Init sleeps 200ms. Advertise only after all
                    # enable checks, discard these inputs, and require fresh ones
                    # before setting the anchor or sending ANY frame.
                    try:
                        self.publisher, self.factory, self.crc = self.transport_factory(self.interface)
                        completed = number(self.clock(), 'transport_prepared_time')
                        if not 0 <= completed-now <= .75:
                            raise ValueError('transport_preparation_deadline')
                    except Exception as exc:
                        self.transport_failed = True
                        return self.latch('transport_preparation_failure:'+str(exc))
                    self.prepared_t = completed
                    self.prepared_target_sequence = target['sequence']
                    self.prepared_enable_sequence = enable['sequence']
                    return self.result(transport_prepared=True, fresh_post_preparation_inputs_required=True)
                if self.prepared_t is not None:
                    if (target['received_t'] < self.prepared_t or enable['received_t'] < self.prepared_t or
                            target['sequence'] <= self.prepared_target_sequence or
                            enable['sequence'] <= self.prepared_enable_sequence):
                        raise ValueError('fresh_post_preparation_inputs_required')
                self.reference = self.last_q = measured
                self.mode_machine, self.reference_id = mode, reference_id
                self.started_t, self.state = now, 'ACTIVE'
                return self._emit(self.candidate(measured, [0.] * 7), now)
            if action in ('enable', 'prepare'):
                raise ValueError('already_enabled_no_rearm')
            if now >= self.started_t + self.config.max_session_s:
                raise ValueError('session_deadline')
            if reference_id != self.reference_id:
                raise ValueError('reference_changed')
            if any(abs(x-y) > self.config.max_excursion_rad + 1e-12 for x,y in zip(goal, self.reference)):
                raise ValueError('target_outside_reference_window')
            if any(not lo + .01 <= x <= hi - .01 for x,lo,hi in zip(goal,LOWER,UPPER)):
                raise ValueError('target_joint_limit')
            if any(abs(x-y) > self.config.max_tracking_error_rad for x,y in zip(self.last_q, measured)):
                raise ValueError('tracking_error')
            dt = now - self.last_write_t
            if dt < 1 / self.config.max_command_hz:
                return self.result()
            # A missed write deadline cannot be hidden by frequent no-write calls.
            if dt > self.config.max_loop_gap_s:
                raise ValueError('write_deadline')
            limit = self.config.max_speed_rad_s * dt
            q = tuple(old + max(-limit, min(limit, dest-old)) for old,dest in zip(self.last_q,goal))
            gain = min(1., (now-self.started_t) / self.config.gain_ramp_s)
            self.last_q = q
            return self._emit(self.candidate(q, [gain*x for x in self.config.right_kp]), now)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return self.latch(exc)

    def damp_and_latch(self, *, now, feedback):
        """Explicit active output: one all-29 damping frame, then latch.

        Only an already-open session may write. It does not promise firmware
        persistence or physical stopping; no retry and no automatic recovery.
        """
        if self.closed or self.config is None or self.started_t is None:
            return self.latch('damping_requires_previously_enabled_session')
        if self.damping_attempted or self.transport_failed:
            return self.latch('no_damping_retry_after_attempt_or_transport_failure')
        try:
            now = number(now, 'now')
            if self.now is not None and now < self.now:
                raise ValueError('clock_reversal')
            self.feedback(feedback, now)
            if self.enable_motion and self.publisher is None:
                raise ValueError('damping_will_not_create_transport')
            self.damping_attempted = True
            self.latch('explicit_damping_and_latch')
            return self._emit(self.candidate([0.] * 7, [0.] * 7), now, damping=True)
        except (ValueError, TypeError, KeyError) as exc:
            return self.latch(exc)

    def close(self):
        self.closed = True
        error = None
        if self.publisher is not None:
            try:
                self.publisher.Close()
            except Exception as exc:
                error = str(exc)
        return self.result(closed=True, close_error=error,
                           handover_required=self.write_count > 0,
                           message='No implicit final frame or mode restoration; stopping packets is not a physical stop.')
