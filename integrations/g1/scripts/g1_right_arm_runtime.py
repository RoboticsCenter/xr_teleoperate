#!/usr/bin/env python3
"""Bounded PC2 right-arm runner; default preview has no motor publisher."""
import argparse
import copy
from dataclasses import fields
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import secrets
import select
import signal
import sys
import threading
import time

import g1_lowcmd_session as lowcmd
from g1_lowcmd_session import Config, LowCmdSession, decode_lowstate, number, uint, vector

PERIOD_S = .005
MAX_LINE_BYTES = 4096
RPC_FILES = {
    'comm/motion_switcher/motion_switcher_client.py': '133d78f6619f6e32d971b40020868618f10db0fd191731085a4b08615448b195',
    'comm/motion_switcher/motion_switcher_api.py': '933210a39b46f9303de1991b5647b5780845886d94d491604de0444caaa9c60c',
    'rpc/client.py': '20840192dee42525b64ecc3a3e8d5ec5d1d7e122b7bfc4236c482cb2b0f6e186',
    'rpc/client_base.py': '2a815a5c798be95df1c8a4af525873c2ee47d5e0bbfbcc8a4ccf2d991a341321',
    'rpc/client_stub.py': '37c860bb4783c2f13b24bc369ebf99b2131a21dfb49f0f7a8b74e79960d879b0',
}


def strict_json(line):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate_json_key:' + key)
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('nonfinite_json:' + value)
    return json.loads(line, object_pairs_hook=pairs, parse_constant=bad_constant)


class ChallengeProtocol:
    """One outstanding single-use nonce, with PC2 issue time as age bound."""
    KEYS = {'type', 'session_id', 'nonce', 'sequence', 'input_sequence', 'enable_sequence',
            'generated_after_challenge', 'q', 'tracking_valid', 'held', 'calibration_id',
            'reference_id', 'enable_source', 'input_policy_id', 'supervised_input_accepted'}
    TRACKER_KEYS = KEYS - {'held', 'enable_sequence', 'enable_source'}

    def __init__(self, config, reference_id, max_age_s=.08, nonce_factory=lambda: secrets.token_hex(16)):
        if not isinstance(reference_id, str) or not 1 <= len(reference_id) <= 128:
            raise ValueError('reference_id_required')
        if not 0 < number(max_age_s, 'nonce_age') <= min(.08, config.max_target_age_s, config.max_enable_age_s):
            raise ValueError('nonce_lifetime_exceeds_input_budget')
        self.config, self.reference_id, self.max_age_s = config, reference_id, max_age_s
        self.nonce_factory, self.session_id = nonce_factory, secrets.token_hex(16)
        self.pending = None
        self.sequence, self.input_sequence, self.enable_sequence = 0, -1, -1

    def issue(self, now, feedback):
        if self.pending is not None:
            raise ValueError('only_one_outstanding_challenge')
        issued_t = number(now, 'challenge_time')
        issued_ns = math.floor(issued_t * 1e9)
        self.sequence += 1
        self.pending = dict(nonce=self.nonce_factory(), sequence=self.sequence,
                            issued_ns=issued_ns, issued_t=issued_t)
        return dict(type='challenge', protocol_version=1, session_id=self.session_id,
                    nonce=self.pending['nonce'], sequence=self.sequence,
                    issued_monotonic_ns=issued_ns, max_age_ms=self.max_age_s*1000,
                    reference_id=self.reference_id, q14=list(feedback['q'][15:29]),
                    feedback_age_s=max(0., now-feedback['received_t']),
                    q14_purpose='diagnostic_only_not_a_cross_clock_fresh_reference')

    def expired(self, now):
        return self.pending is not None and now > self.pending['issued_t'] + self.max_age_s

    def accept(self, packet, now):
        tracker_window = self.config.input_policy_id == 'supervised_tracker_window_v1'
        expected_keys = self.TRACKER_KEYS if tracker_window else self.KEYS
        if not isinstance(packet, dict) or set(packet) != expected_keys:
            raise ValueError('invalid_target_schema_or_remote_timestamp')
        pending = self.pending
        if pending is None:
            raise ValueError('no_outstanding_nonce_or_replay')
        expected_type = 'tracker_target' if tracker_window else 'target'
        if packet['type'] != expected_type or packet['session_id'] != self.session_id:
            raise ValueError('wrong_message_or_session')
        if packet['nonce'] != pending['nonce'] or type(packet['sequence']) is not int or packet['sequence'] != pending['sequence']:
            raise ValueError('nonce_or_challenge_sequence_mismatch')
        self.pending = None  # matched nonce consumed even if remaining validation fails
        # Preserve the exact PC2 float reading internally. Flooring only the
        # diagnostic ns field must not place a post-prepare nonce before prepare.
        lower_bound = pending['issued_t']
        if not lower_bound <= now <= lower_bound + self.max_age_s:
            raise ValueError('expired_nonce_or_clock_reversal')
        input_seq = uint(packet['input_sequence'], 2**63-1, 'input_sequence')
        enable_seq = pending['sequence'] if tracker_window else uint(packet['enable_sequence'], 2**63-1, 'enable_sequence')
        if input_seq <= self.input_sequence or enable_seq <= self.enable_sequence:
            raise ValueError('raw_source_sequences_must_advance')
        if packet['generated_after_challenge'] is not True:
            raise ValueError('post_challenge_sampling_contract_required')
        if (packet['reference_id'] != self.reference_id or packet['calibration_id'] != self.config.calibration_id or
                (not tracker_window and packet['enable_source'] != self.config.enable_source)):
            raise ValueError('reference_calibration_or_enable_source_mismatch')
        if ((not tracker_window and type(packet['held']) is not bool) or
                (packet['tracking_valid'] is not None and type(packet['tracking_valid']) is not bool)):
            raise ValueError('invalid_validity_or_enable_type')
        if packet['input_policy_id'] != self.config.input_policy_id:
            raise ValueError('input_policy_mismatch')
        supervised = packet['supervised_input_accepted']
        if self.config.input_policy_id == 'vendor_tracking':
            if supervised is not None and supervised is not False:
                raise ValueError('supervision_not_allowed_under_vendor_policy')
        elif type(supervised) is not bool:
            raise ValueError('supervised_acceptance_must_be_bool')
        if tracker_window and (packet['tracking_valid'] is not None or supervised is not True):
            raise ValueError('tracker_window_requires_unknown_vendor_validity_and_explicit_acceptance')
        q = vector(packet['q'], 7, 'right_target')
        self.input_sequence, self.enable_sequence = input_seq, enable_seq
        # Never replace this time with arrival time or a computer timestamp.
        target = dict(sequence=input_seq, received_t=lower_bound, q=q,
                      tracking_valid=packet['tracking_valid'], calibration_id=self.config.calibration_id,
                      reference_id=self.reference_id, input_policy_id=self.config.input_policy_id,
                      supervised_input_accepted=supervised)
        enable = dict(sequence=enable_seq, received_t=lower_bound, source_id=self.config.enable_source)
        if tracker_window:
            enable['window_active'] = True  # explicit local authorization window, never a human-held claim
        else:
            enable['held'] = packet['held']
        return target, enable


class Runtime:
    """Single owner-loop; fake sources/transport exercise the same shutdown path."""
    def __init__(self, session, source, readiness, emit, *, reference_id, seconds=30.,
                 nonce_age_s=.08, clock=time.monotonic, mode_query=None,
                 nonce_factory=lambda: secrets.token_hex(16)):
        if not 0 < number(seconds, 'seconds') <= 60:
            raise ValueError('runtime_must_be_at_most_60_seconds')
        self.session, self.source, self.readiness, self.emit = session, source, readiness, emit
        self.clock, self.seconds, self.mode_query = clock, seconds, mode_query
        self.protocol = ChallengeProtocol(session.config, reference_id, nonce_age_s, nonce_factory)
        self.started = self.last_tick = self.last_status = None
        self.target = self.enable = None
        self.done, self.final, self.was_held = False, None, False
        self.tick_count = self.accepted = 0
        self.tracker_window_started = None
        self.last_feedback = None

    def start(self):
        feedback = self.source.snapshot()
        now = self.clock()
        self.session.feedback(feedback, now)
        self.last_feedback = feedback
        self.started = self.last_tick = now
        self.emit(dict(type='runtime_ready', protocol_version=1, session_id=self.protocol.session_id,
                       preview=not self.session.enable_motion, period_s=PERIOD_S,
                       maximum_runtime_s=self.seconds, reference_id=self.protocol.reference_id,
                       mode=dict(code=self.readiness['check_mode_code'], data=self.readiness.get('raw')),
                       readiness=self.readiness, physical_stop_verified=False))
        self.issue_challenge()

    def issue_challenge(self):
        # A callback can update state during snapshot; sample the clock AFTER it.
        feedback = self.source.snapshot()
        now = self.clock()
        self.session.feedback(feedback, now)
        self.last_feedback = feedback
        self.emit(self.protocol.issue(now, feedback))

    def telemetry(self):
        feedback = self.last_feedback or {}
        return dict(right_measured_q=list(feedback['q'][22:29]) if 'q' in feedback else None,
                    right_command_q=list(self.session.last_q) if self.session.last_q is not None else None,
                    right_anchor_q=list(self.session.reference) if self.session.reference is not None else None,
                    feedback_received_t=feedback.get('received_t'), feedback_sequence=feedback.get('sequence'),
                    feedback_tick=feedback.get('tick'), authorized_window_started_t=self.tracker_window_started,
                    telemetry_purpose='last_validated_feedback_and_position_target_not_physical_stop_proof')

    def status(self, now, reason):
        if self.last_status is None or now-self.last_status >= .05:
            self.emit(dict(type='runtime_status', session_id=self.protocol.session_id,
                           preview=not self.session.enable_motion, state=self.session.state,
                           reason=reason, accepted_targets=self.accepted,
                           write_count=self.session.write_count, physical_stop_verified=False, **self.telemetry()))
            self.last_status = now

    def tick(self, lines=(), *, eof=False, stop_reason=None):
        if self.done:
            return
        try:
            now = self.clock()
            if stop_reason or eof or now >= self.started+self.seconds:
                return self.finish(stop_reason or ('stdin_eof' if eof else 'runtime_deadline'))
            if self.tracker_window_started is not None and now >= self.tracker_window_started+self.session.config.max_session_s:
                return self.finish('authorized_window_deadline')
            if not 0 <= now-self.last_tick <= self.session.config.max_loop_gap_s:
                raise ValueError('runtime_loop_deadline_or_clock_reversal')
            self.last_tick = now
            self.tick_count += 1
            if len(lines) > 1:
                raise ValueError('multiple_messages_without_new_challenge')
            accepted = False
            for line in lines:
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError('input_line_too_long')
                packet = strict_json(line)
                if isinstance(packet, dict) and packet.get('type') == 'stop':
                    if set(packet) != {'type', 'session_id'} or packet['session_id'] != self.protocol.session_id:
                        raise ValueError('invalid_stop_packet')
                    return self.finish('client_stop')
                self.target, self.enable = self.protocol.accept(packet, now)
                if (self.session.config.input_policy_id == 'supervised_tracker_window_v1' and
                        self.tracker_window_started is None):
                    self.tracker_window_started = self.enable['received_t']
                if self.tracker_window_started is not None:
                    self.enable['window_started_t'] = self.tracker_window_started
                    self.enable['window_deadline_t'] = self.tracker_window_started+self.session.config.max_session_s
                self.accepted += 1
                accepted = True
            feedback = self.source.snapshot()
            now = self.clock()
            if self.tracker_window_started is not None and now >= self.tracker_window_started+self.session.config.max_session_s:
                return self.finish('authorized_window_deadline')
            self.session.feedback(feedback, now)
            self.last_feedback = feedback
            active = self.session.state == 'ACTIVE'
            if self.protocol.expired(now):
                if active:
                    raise ValueError('nonce_response_deadline')
                self.protocol.pending = None  # WAIT: unanswered challenge expires, never reused
                self.issue_challenge()
            if self.target is not None:
                held = (self.enable.get('window_active') is True
                        if self.session.config.input_policy_id == 'supervised_tracker_window_v1' else self.enable['held'])
                if active and not held:
                    return self.finish('trigger_released')
                if not active and not held:
                    self.was_held = False
                    self.status(now, 'waiting_for_trigger')
                else:
                    # Before the first enable, an old readiness query is refreshed;
                    # discard that target and demand a new post-query challenge.
                    if not active and held and not self.was_held and now-self.readiness['received_t'] > .5 and self.mode_query:
                        update = self.mode_query()
                        self.readiness.update(update)
                        self.was_held = True
                        self.target = self.enable = None
                        self.last_tick = self.clock()
                        self.protocol.pending = None
                        self.issue_challenge()
                        self.status(self.last_tick, 'mode_refreshed_need_fresh_target')
                        return
                    self.was_held = held
                    inactive = (type(self.readiness.get('check_mode_code')) is int and
                                self.readiness['check_mode_code'] == 0 and self.readiness.get('motion_name') == '')
                    confirmations = all(self.readiness.get(k) is True for k in ('suspended','operator_ready','exclusive_control_confirmed'))
                    if not active and not self.session.enable_motion and (not inactive or not confirmations):
                        self.status(now, 'motion_service_not_inactive' if not inactive else 'physical_or_control_readiness_missing')
                    else:
                        if not active and self.session.enable_motion and self.session.publisher is None:
                            prepared = self.session.step(now=now, feedback=feedback, target=self.target,
                                                         enable=self.enable, action='prepare', readiness=self.readiness)
                            if prepared['state'] == 'LATCHED':
                                return self.finish('gate:'+str(prepared['reason']), prepared)
                            if prepared.get('transport_prepared') is not True:
                                raise RuntimeError('transport_preparation_not_confirmed')
                            self.target = self.enable = None
                            self.last_tick = self.clock()
                            self.protocol.pending = None
                            self.issue_challenge()
                            self.status(self.last_tick, 'transport_prepared_need_fresh_target')
                            return
                        result = self.session.step(now=now, feedback=feedback, target=self.target,
                                                   enable=self.enable, action=None if active else 'enable',
                                                   readiness=self.readiness)
                        if result['state'] == 'LATCHED':
                            return self.finish('gate:'+str(result['reason']), result)
                        self.status(now, 'active' if self.session.enable_motion else 'preview_active')
            else:
                self.status(now, 'waiting_for_first_response')
            if accepted:
                self.issue_challenge()
        except Exception as exc:
            self.finish(type(exc).__name__+':'+str(exc))

    def finish(self, reason, last_result=None):
        if self.done:
            return self.final
        self.done = True
        damping, errors = None, []
        if self.session.write_count > 0:
            try:
                feedback = self.source.snapshot()
                damping = self.session.damp_and_latch(now=self.clock(), feedback=feedback)
                if damping.get('published'):
                    self.last_feedback = feedback
            except Exception as exc:
                errors.append('damping:'+str(exc))
        closed = self.session.close()
        unknown = bool(self.session.transport_failed or (last_result or {}).get('write_delivery_unknown') or
                       (self.session.write_count > 0 and not (damping or {}).get('published')))
        self.final = dict(type='runtime_final', session_id=self.protocol.session_id, reason=reason,
                          preview=not self.session.enable_motion, accepted_targets=self.accepted,
                          loop_ticks=self.tick_count, actual_write_count=self.session.write_count,
                          damping=damping, close=closed, errors=errors, last_action_unknown=unknown,
                          physical_stop_verified=False, mode_restored=False, **self.telemetry())
        try:
            self.emit(self.final)
        except Exception as exc:
            self.final['errors'].append('final_output:'+str(exc))
        return self.final


class LatestFeedback:
    def __init__(self, crc, clock=time.monotonic):
        self.crc, self.clock = crc, clock
        self.lock = threading.Lock()
        self.sequence, self.value, self.error = 0, None, None

    def receive(self, message):
        received = self.clock()
        with self.lock:
            self.sequence += 1
            try:
                value = decode_lowstate(message, received_t=received, sequence=self.sequence, crc=self.crc)
                if self.error is None:
                    self.value = value
            except Exception as exc:
                self.error = 'lowstate_callback:'+str(exc)

    def snapshot(self):
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            if self.value is None:
                raise ValueError('no_lowstate_received')
            return copy.deepcopy(self.value)


class RobotInputs:
    """Only lowstate subscription and guarded CheckMode; never mode setters."""
    def __init__(self, interface, clock=time.monotonic):
        self.interface, self.clock = interface, clock
        self.subscriber = self.source = None

    def open(self):
        spec = importlib.util.find_spec('unitree_sdk2py')
        if spec is None or not spec.submodule_search_locations or platform.system() != 'Linux':
            raise RuntimeError('reviewed_Linux_SDK_required')
        root = Path(next(iter(spec.submodule_search_locations)))
        binary, binary_hash = lowcmd.CRC_BINARIES[platform.machine()]
        hashes = dict(lowcmd.SDK_FILES, **RPC_FILES, **{'utils/lib/'+binary: binary_hash})
        for path, expected in hashes.items():
            if hashlib.sha256((root/path).read_bytes()).hexdigest() != expected:
                raise RuntimeError('SDK_hash_mismatch:'+path)
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        from unitree_sdk2py.utils.crc import CRC
        ChannelFactoryInitialize(0, self.interface)
        self.source = LatestFeedback(CRC(), self.clock)
        self.subscriber = ChannelSubscriber('rt/lowstate', LowState_)
        self.subscriber.Init(self.source.receive, 0)

    def check_mode(self):
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
        class QueryOnly(MotionSwitcherClient):
            calls = 0
            def _Call(self, api_id, parameter):
                if api_id != 1001 or strict_json(parameter) != {} or self.calls:
                    raise RuntimeError('only_one_CheckMode_query_per_client')
                self.calls += 1
                return super()._Call(api_id, parameter)
        client = QueryOnly()
        try:
            client.SetTimeout(.5)
            client.Init()
            sent = self.clock()
            code, data = client.CheckMode()
            return dict(received_t=sent, check_mode_code=code,
                        motion_name=data.get('name') if isinstance(data, dict) else None,
                        raw=data, query_elapsed_s=self.clock()-sent)
        finally:
            stub = getattr(client, '_ClientBase__stub')
            getattr(stub, '_ClientStub__recvChannel').CloseReader()
            getattr(stub, '_ClientStub__sendChannel').CloseWriter()

    def close(self):
        if self.subscriber is not None:
            self.subscriber.Close()


class JsonLinesIO:
    """Bounded nonblocking pipes: slow SSH output cannot stall the owner loop."""
    def __init__(self, input_fd, output_fd):
        self.input_fd, self.output_fd = input_fd, output_fd
        self.inbound = self.outbound = b''
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)

    def emit(self, value):
        data = (json.dumps(value, allow_nan=False, separators=(',', ':'))+'\n').encode()
        if len(self.outbound)+len(data) > 65536:
            raise BufferError('stdout_backpressure_limit')
        self.outbound += data
        self.flush()

    def flush(self):
        if self.outbound:
            try:
                count = os.write(self.output_fd, self.outbound)
                self.outbound = self.outbound[count:]
            except BlockingIOError:
                pass

    def read(self):
        if not select.select([self.input_fd], [], [], 0)[0]:
            return [], False
        try:
            chunk = os.read(self.input_fd, 8192)
        except BlockingIOError:
            return [], False
        if not chunk:
            return [], True
        self.inbound += chunk
        if len(self.inbound) > 8192:
            raise ValueError('stdin_buffer_limit')
        lines = []
        while b'\n' in self.inbound:
            line, self.inbound = self.inbound.split(b'\n', 1)
            if len(line) > MAX_LINE_BYTES:
                raise ValueError('input_line_too_long')
            lines.append(line.decode('utf-8'))
        if len(self.inbound) > MAX_LINE_BYTES:
            raise ValueError('partial_input_line_too_long')
        return lines, False


def configuration(text, seconds):
    value = strict_json(text)
    if not isinstance(value, dict) or set(value)-{f.name for f in fields(Config)}:
        raise ValueError('unknown_Config_fields')
    if 'max_command_hz' in value and value['max_command_hz'] != 200:
        raise ValueError('runner_requires_200_Hz')
    value['max_command_hz'] = 200.
    # Separate startup/wait runtime from the first-enable motion time limit.
    value['max_session_s'] = min(number(value.get('max_session_s', seconds), 'max_session_s'), seconds)
    return Config(**value)


def serve(args):
    config = configuration(args.config_json, args.seconds)
    output_fd = os.dup(1)
    os.dup2(2, 1)  # SDK native stdout goes to stderr; protocol gets a dedicated fd.
    io = JsonLinesIO(sys.stdin.fileno(), output_fd)
    stopped, finished = threading.Event(), threading.Event()
    stop_reason = ['signal']
    def on_signal(signum, _frame):
        stop_reason[0] = 'signal:'+str(signum)
        stopped.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    deadline = [time.monotonic()+args.init_timeout_s]
    def hard_watchdog():
        while not finished.wait(.05):
            if time.monotonic() > deadline[0]:
                os._exit(3)  # no final report => last physical action must be treated as unknown
    threading.Thread(target=hard_watchdog, daemon=True).start()
    robot, runtime = RobotInputs(args.interface), None
    code = 1
    try:
        robot.open()
        readiness = robot.check_mode()
        readiness.update(suspended=args.suspended_confirmed, operator_ready=args.operator_ready,
                         exclusive_control_confirmed=args.exclusive_control_confirmed)
        while True:
            if stopped.is_set():
                raise RuntimeError('signal_during_initialization')
            try:
                robot.source.snapshot()
                break
            except ValueError as exc:
                if str(exc) != 'no_lowstate_received':
                    raise
            if time.monotonic() >= deadline[0]-.1:
                raise TimeoutError('initial_lowstate_timeout')
            stopped.wait(.005)
        session = LowCmdSession(config, enable_motion=args.enable_motion, interface=args.interface)
        runtime = Runtime(session, robot.source, readiness, io.emit, reference_id=args.reference_id,
                          seconds=args.seconds, nonce_age_s=args.nonce_max_age_ms/1000,
                          mode_query=robot.check_mode)
        runtime.start()
        next_tick = time.monotonic()
        while not runtime.done:
            deadline[0] = time.monotonic()+1.
            lines, eof = io.read()
            runtime.tick(lines, eof=eof, stop_reason=stop_reason[0] if stopped.is_set() else None)
            io.flush()
            next_tick += PERIOD_S
            delay = next_tick-time.monotonic()
            if delay > 0:
                stopped.wait(delay)
            else:
                next_tick = time.monotonic()  # do not send catch-up bursts
        normal = runtime.final['reason'] in ('stdin_eof','client_stop','runtime_deadline','trigger_released','authorized_window_deadline')
        code = 0 if (normal and not runtime.final['last_action_unknown'] and not runtime.final['errors']
                     and not runtime.final['close'].get('close_error')) else 1
    except Exception as exc:
        if runtime is not None:
            runtime.finish(type(exc).__name__+':'+str(exc))
        else:
            try:
                io.emit(dict(type='runtime_error', error=type(exc).__name__+':'+str(exc),
                             actual_write_count=0, physical_stop_verified=False))
            except Exception:
                pass
    finally:
        deadline[0] = time.monotonic()+2.
        if runtime is not None and not runtime.done:
            runtime.finish('final_cleanup')
        try:
            robot.close()
        except Exception:
            code = 1
        until = time.monotonic()+.2
        while io.outbound and time.monotonic() < until:
            try:
                io.flush()
            except OSError:
                break
            time.sleep(.005)
        finished.set()
        os.close(output_fd)
    return code


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface', default='eth0')
    p.add_argument('--seconds', type=float, default=30.)
    p.add_argument('--reference-id')
    p.add_argument('--config-json')
    p.add_argument('--enable-motion', action='store_true', help='motor output AND one explicit damping attempt on shutdown')
    p.add_argument('--suspended-confirmed', action='store_true')
    p.add_argument('--operator-ready', action='store_true')
    p.add_argument('--exclusive-control-confirmed', action='store_true')
    p.add_argument('--nonce-max-age-ms', type=float, default=80.)
    p.add_argument('--init-timeout-s', type=float, default=5.)
    p.add_argument('--plan', action='store_true', help='print scope without SDK/network')
    args = p.parse_args(argv)
    if args.plan:
        print(json.dumps(dict(protocol_version=1, period_s=PERIOD_S, default='preview',
                              source_sampling='after_single_use_challenge_arrival',
                              timestamp_bound='PC2_challenge_issue_time', nonce_max_age_ms=80,
                              no_mode_setters=True, shutdown='explicit_one_damping_attempt_after_output',
                              config=['right_kp[7]','right_kd[7]','nonright_kd[22]',
                                      'calibration_id','enable_source','gain_review_id']), indent=2))
        return 0
    if not args.reference_id or not args.config_json:
        p.error('--reference-id and --config-json required')
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 60:
        p.error('--seconds must be in (0,60]')
    if not math.isfinite(args.init_timeout_s) or not 1 <= args.init_timeout_s <= 10:
        p.error('--init-timeout-s must be in [1,10]')
    try:
        config = configuration(args.config_json, args.seconds)
        ChallengeProtocol(config, args.reference_id, args.nonce_max_age_ms/1000)
    except (TypeError, ValueError) as exc:
        p.error(str(exc))
    if args.enable_motion and not (args.suspended_confirmed and args.operator_ready and args.exclusive_control_confirmed):
        p.error('motion requires explicit suspended/operator/exclusive confirmations')
    return serve(args)


if __name__ == '__main__':
    raise SystemExit(main())
