"""BrainCo DDS output session for an existing HandDDSPreview mapper.

Construction is input-only by default. No service, reader, network discovery, or
publisher starts here. The caller supplies original-reader frames and fresh state
in the mapper's existing schema, then calls check_timeout() while idle.
"""
import math
import time


def encode_command(candidate, message_factory, motor_factory):
    """Use the official GO MotorCmds_ shape; only q/dq are consumed by the service."""
    commands = candidate.get('cmds')
    if not isinstance(commands, list) or len(commands) != 6:
        raise ValueError('Exactly six commands required')
    message = message_factory()
    message.cmds = []
    for value in commands:
        q, dq = value['q'], value['dq']
        if (type(q) not in (int, float) or type(dq) not in (int, float)
                or not math.isfinite(q) or not math.isfinite(dq)
                or not 0 <= q <= 1 or not .001 <= dq <= 1):
            raise ValueError('Invalid normalized q/dq')
        motor = motor_factory()
        motor.q, motor.dq = q, dq
        message.cmds.append(motor)
    return message


def official_transport(interface, domain, side):
    """Called only after explicit enabling and a fresh valid mapped input."""
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_
    from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
    ChannelFactoryInitialize(domain, interface)
    publisher = ChannelPublisher('rt/brainco/{}/cmd'.format(side), MotorCmds_)
    try:
        publisher.Init()
    except Exception:
        publisher.Close()
        raise
    return publisher, MotorCmds_, unitree_go_msg_dds__MotorCmd_


class BrainCoHandSession:
    """Single-threaded caller API; errors latch until a new session is constructed.

`original_filter_passed=True` is an in-process provenance assertion: the caller
must set it ONLY for a frame emitted by the locked original wuji_bridge.py reader.
It is not a request to bypass the reader, and must not come from an untrusted
network flag. HandDDSPreview cannot reconstruct confidence from joint_angles.
"""
    def __init__(self, mapper, *, enable_motion=False, interface=None, domain=0,
                 exclusive_control_confirmed=False, feedback_identity_confirmed=False,
                 command_service_confirmed=False, input_timeout_s=.2,
                 transport_factory=official_transport, clock=time.monotonic):
        if type(enable_motion) is not bool:
            raise ValueError('enable_motion must be an explicit bool')
        if type(domain) is not int or not 0 <= domain <= 232:
            raise ValueError('DDS domain must be 0..232')
        if (type(input_timeout_s) not in (int, float) or not math.isfinite(input_timeout_s)
                or not 0 < input_timeout_s <= .5):
            raise ValueError('input_timeout_s must be finite and in (0,0.5]')
        if enable_motion and (mapper.synthetic or not isinstance(interface, str) or not interface.strip()
                              or exclusive_control_confirmed is not True
                              or feedback_identity_confirmed is not True
                              or command_service_confirmed is not True):
            raise ValueError('Motion requires real binding, interface, exclusive control, verified feedback and service')
        self.mapper, self.enable_motion = mapper, enable_motion
        self.interface, self.domain, self.clock = interface, domain, clock
        self.timeout, self.transport_factory = input_timeout_s, transport_factory
        self.publisher = self.message_factory = self.motor_factory = None
        self.created_t = clock()
        self.last_input_t = self.last_clock = None
        self.fault = None
        self.closed = False
        self.write_count = 0
        self.write_attempted = self.delivery_unknown = False

    def refuse(self, reason, quality_passed=None, delivery_unknown=False):
        if self.fault is None:
            self.fault = reason
        self.delivery_unknown = self.delivery_unknown or delivery_unknown
        return {'status': 'latched', 'reason': self.fault, 'published': False,
                'hardware_output': False, 'actual_write_count': self.write_count,
                'acknowledged': False, 'input_quality_passed': quality_passed,
                'input_confidence_threshold': .5, 'allow_low_quality': False,
                'write_delivery_unknown': self.delivery_unknown, 'new_session_required': True}

    def check_timeout(self):
        """Must be called by the collector while no new accepted glove frame arrives."""
        if self.closed or self.fault:
            return self.refuse(self.fault or 'session_closed')
        now = self.clock()
        if not math.isfinite(now) or now < 0 or (self.last_clock is not None and now < self.last_clock):
            return self.refuse('invalid_or_regressed_clock')
        self.last_clock = now
        last = self.created_t if self.last_input_t is None else self.last_input_t
        if now - last >= self.timeout:
            return self.refuse('input_timeout')
        return None

    def step(self, record, *, original_filter_passed=False):
        blocked = self.check_timeout()
        if blocked:
            return blocked
        if original_filter_passed is not True:
            return self.refuse('original_input_quality_not_passed', quality_passed=False)
        now = self.clock()
        try:
            result = self.mapper.preview(record, now)
        except Exception as exc:
            return self.refuse('mapping_exception:' + str(exc), quality_passed=True)
        if result.get('status') != 'preview':
            return self.refuse('mapping:' + result.get('reason', 'rejected'), quality_passed=True)
        # The mapper reuses the original map and bounds the first and every later
        # candidate against fresh measured positions, never the last command.
        self.last_input_t = now
        result.update(input_quality_passed=True, input_confidence_threshold=.5,
                      allow_low_quality=False, actual_write_count=self.write_count)
        if not self.enable_motion:
            return result
        attempted = False
        try:
            if self.publisher is None:
                self.publisher, self.message_factory, self.motor_factory = self.transport_factory(
                    self.interface, self.domain, self.mapper.side)
            message = encode_command(result['command_preview'], self.message_factory, self.motor_factory)
            # Native initialization can take long enough to stale both inputs.
            write_now = self.clock()
            if (not math.isfinite(write_now) or write_now < now
                    or not 0 <= write_now - record['glove']['source_t'] <= self.mapper.glove_age
                    or not 0 <= write_now - record['measured']['received_t'] <= self.mapper.feedback_age
                    or write_now - now >= self.timeout):
                return self.refuse('inputs_expired_before_write', quality_passed=True)
            attempted = True
            self.write_attempted = True
            if self.publisher.Write(message) is not True:
                raise RuntimeError('DDS Write did not report success')
            self.write_count += 1
            result.update(status='published', published=True, hardware_output=True,
                          acknowledged=False, actual_write_count=self.write_count)
            return result
        except Exception as exc:
            return self.refuse('transport:{}:{}'.format(type(exc).__name__, exc),
                               quality_passed=True, delivery_unknown=attempted)

    def close(self):
        """Close local transport only; no open/close/home/hold motion is sent."""
        error = None
        if not self.closed and self.publisher is not None:
            try:
                self.publisher.Close()
            except Exception as exc:
                error = str(exc)
        self.closed = True
        return {'status': 'closed' if error is None else 'close_failed', 'error': error,
                'published': False, 'actual_write_count': self.write_count,
                'physical_stop_confirmed': False,
                'write_delivery_unknown': self.delivery_unknown,
                'service_timeout_policy_required': self.write_attempted}
