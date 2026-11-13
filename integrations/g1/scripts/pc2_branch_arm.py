#!/usr/bin/env python3
"""Normal-motion arm transport with pinned PR321 waist posture support.

LAUNCH publishes zero arm authority; START blends in measured arm targets.
STOP/EOF releases authority to the existing motion service, without mode setters.
The shared arm_sdk weight also takes the waist; retain source waist gains.
"""
import argparse
import importlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import types


SOURCE = 'pr321:G1_29_ArmController+normal_motion_lifecycle'
CONTROL_MODE = 'normal_walking'
CONTROL_TOPIC = 'rt/arm_sdk'
WAIST_JOINTS = (12, 13, 14)


def load_runtime(vendor_root):
    """Import only on explicit execution; importing this adapter never opens DDS."""
    root = Path(vendor_root).resolve()
    if not (root / 'teleop/robot_control/robot_arm.py').is_file():
        raise ValueError('vendor root does not contain teleop/robot_control/robot_arm.py')
    sys.path.insert(0, str(root))
    try:
        importlib.import_module('logging_mp')
    except ModuleNotFoundError as exc:
        if exc.name != 'logging_mp':
            raise
        shim = types.ModuleType('logging_mp')
        shim.getLogger = logging.getLogger
        sys.modules['logging_mp'] = shim
    arm = importlib.import_module('teleop.robot_control.robot_arm')
    Path(arm.__file__).resolve().relative_to(root)
    from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    import numpy as np
    return types.SimpleNamespace(initialize=ChannelFactoryInitialize,
        switcher=MotionSwitcherClient, controller=motion_controller(arm),
        array=lambda values: np.asarray(values, dtype=float))


def vector(value, label):
    if hasattr(value, 'tolist'):
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or len(value) != 14:
        raise ValueError(label + ' must contain exactly 14 joint values')
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(label + ' contains a non-finite or non-numeric joint value')
    return [float(x) for x in value]


def verify_ownership(client, stop, emit):
    """Query only: a nonempty service is required, but is not a Regular-mode proof."""
    client.SetTimeout(2.)
    client.Init()
    code, current = client.CheckMode()
    if (type(code) is not int or code != 0 or not isinstance(current, dict) or
            not isinstance(current.get('name'), str) or not current['name'].strip()):
        raise RuntimeError('G1 motion service is inactive. Select the Regular mode documented for this robot firmware and waist configuration before LAUNCH.')
    emit({'type':'ownership', 'before':current, 'after':current,
          'release_sent':False, 'regular_mode_verified':False})
    return not stop.is_set()


def motion_controller(arm):
    """Keep source gains/velocity clipping; replace authority and thread lifecycle."""
    class NormalMotionArm(arm.G1_29_ArmController):
        def __init__(self, motion_mode=True):
            if motion_mode is not True:
                raise ValueError('Only normal motion arm_sdk control is supported')
            self._closing = threading.Event()
            self._zero_written = threading.Event()
            self._weight = self._weight_target = 0.
            self._publish_error = None
            self._state_mode = None
            self.actual_write_count = 0
            self._last_feedback_mono = None
            try:
                super().__init__(motion_mode=True)
                if not self._zero_written.wait(2.) or self._publish_error:
                    raise RuntimeError('No initial zero-authority arm_sdk frame was published')
            except BaseException:
                self._closing.set()
                for name in ('publish_thread','subscribe_thread'):
                    thread=getattr(self,name,None)
                    if thread is not None: thread.join(timeout=1.)
                raise

        def _subscribe_motor_state(self):
            while not self._closing.is_set():
                msg=self.lowstate_subscriber.Read()
                if msg is not None:
                    lowstate=arm.G1_29_LowState()
                    for index in range(arm.G1_29_Num_Motors):
                        lowstate.motor_state[index].q=msg.motor_state[index].q
                        lowstate.motor_state[index].dq=msg.motor_state[index].dq
                    self._state_mode=msg.mode_machine
                    self.lowstate_buffer.SetData(lowstate)
                    self._last_feedback_mono=time.monotonic()
                    self.lowstate_sub_ready=True
                self._closing.wait(.002)

        def get_mode_machine(self):
            return self._state_mode

        @property
        def arm_weight(self):
            with self.ctrl_lock: return self._weight

        def _write_frame(self, q, tau, weight):
            # On this firmware arm_sdk also replaces the three waist commands.
            # Keep their source-initialized gains and posture: clearing them
            # removes waist support at full weight (observed on the real G1).
            # Legs stay with the factory controller. This is posture holding,
            # not an independent adaptive waist-balancing policy.
            for index, motor in enumerate(self.msg.motor_cmd):
                if 12 <= index <= 28: continue
                motor.mode=0
                for name in ('q','dq','tau','kp','kd'): setattr(motor,name,0.)
            for index, joint in enumerate(arm.G1_29_JointArmIndex):
                motor=self.msg.motor_cmd[joint]
                motor.q=float(q[index]);motor.dq=0.;motor.tau=float(tau[index])
            self.msg.motor_cmd[29].q=float(weight)
            self.msg.crc=self.crc.Crc(self.msg)
            result=self.lowcmd_publisher.Write(self.msg)
            if result is False: raise RuntimeError('arm_sdk publisher rejected a frame')
            self.actual_write_count+=1
            if weight == 0.: self._zero_written.set()

        def _ctrl_motor_state(self):
            # Called by the source constructor only after ctrl_lock/msg are ready.
            with self.ctrl_lock:
                self.q_target=self.get_current_dual_arm_q().copy()
                self.tauff_target=arm.np.zeros(14)
            last=time.monotonic()
            try:
                while not self._closing.is_set():
                    now=time.monotonic();dt=min(.05,max(0.,now-last));last=now
                    with self.ctrl_lock:
                        delta=self._weight_target-self._weight
                        self._weight+=max(-dt,min(dt,delta)) # one-second authority transition
                        if abs(self._weight-self._weight_target)<1e-9:self._weight=self._weight_target
                        if self._weight_target==0.:
                            self.q_target=self.get_current_dual_arm_q().copy()
                            self.tauff_target=arm.np.zeros(14)
                        q=self.clip_arm_q_target(self.q_target,self.arm_velocity_limit)
                        self._write_frame(q,self.tauff_target,self._weight)
                    if self._speed_gradual_max:
                        elapsed=time.time()-self._gradual_start_time
                        self.arm_velocity_limit=20.+10.*min(1.,elapsed/5.)
                    self._closing.wait(max(0.,self.control_dt-(time.monotonic()-now)))
            except Exception as exc:
                self._publish_error=str(exc)

        def activate(self):
            with self.ctrl_lock:
                if self._publish_error or not self.publish_thread.is_alive():
                    raise RuntimeError(self._publish_error or 'arm_sdk publisher ended')
                # READY may last arbitrarily long while the factory controller
                # adjusts the waist. Capture again at START to avoid commanding
                # a return to the earlier LAUNCH posture.
                measured=self.get_current_motor_q()
                waist=[float(measured[index]) for index in WAIST_JOINTS]
                if not all(math.isfinite(value) for value in waist):
                    raise RuntimeError('Cannot capture a finite measured waist posture')
                for index,value in zip(WAIST_JOINTS,waist):
                    self.msg.motor_cmd[index].q=value
                self.q_target=self.get_current_dual_arm_q().copy()
                self.tauff_target=arm.np.zeros(14)
                self._weight_target=1.
            self.speed_gradual_max()

        def release(self):
            with self.ctrl_lock:
                self._zero_written.clear()
                self._weight_target=0.
            released=self._zero_written.wait(2.)
            self._closing.set()
            self.publish_thread.join(timeout=1.)
            self.subscribe_thread.join(timeout=1.)
            if self.publish_thread.is_alive():
                raise RuntimeError('arm_sdk publisher did not stop')
            # Also send zero if a failed publish thread could not complete the ramp.
            with self.ctrl_lock:
                self._weight=0.
                self._write_frame(self.get_current_dual_arm_q(),arm.np.zeros(14),0.)
            return bool(released or self._zero_written.is_set())
    return NormalMotionArm


def run(args, *, stdin=None, emit=None, runtime_loader=load_runtime, stop=None):
    """Dependency-injected loop; tests use only inert controllers and local queues."""
    if getattr(args,'control_mode',None) != CONTROL_MODE:
        raise ValueError('--control-mode normal_walking is required')
    if not args.enable_motion:
        raise ValueError('--enable-motion is required before loading a robot controller')
    stdin = sys.stdin if stdin is None else stdin
    emit = (lambda value: print(json.dumps(value, allow_nan=False), flush=True)) if emit is None else emit
    raw_emit=emit
    emit=lambda value: raw_emit(dict(value,control_mode=CONTROL_MODE,control_topic=CONTROL_TOPIC))
    stop = threading.Event() if stop is None else stop
    commands = queue.Queue()
    context = {'reason': 'stop_requested', 'reader_error': None}
    controller = None
    accepted = 0
    started = False
    arm_weight_released = False
    controller_attempted = False
    error = None

    def read_commands():
        try:
            for line in stdin:
                try:
                    command = json.loads(line)
                    if not isinstance(command, dict):
                        raise ValueError('command must be a JSON object')
                except (TypeError, ValueError):
                    commands.put({'command': '_invalid', 'message': 'invalid JSON command'})
                    continue
                if command.get('command') == 'stop':
                    context['reason'] = 'client_stop'
                    stop.set()
                    return
                commands.put(command)
            context['reason'] = 'stdin_eof'
        except Exception as exc:
            context['reason'] = 'stdin_error'
            context['reader_error'] = type(exc).__name__ + ': ' + str(exc)
        finally:
            stop.set()

    threading.Thread(target=read_commands, daemon=True).start()
    try:
        if stop.is_set():
            return_code = 0
        else:
            runtime = runtime_loader(args.vendor_root)
            if not stop.is_set():
                runtime.initialize(0, args.interface)
                client = runtime.switcher()
                if verify_ownership(client, stop, emit):
                    controller_attempted = True
                    controller = runtime.controller(motion_mode=True)
            if controller is not None and not stop.is_set():
                state = {'q': vector(controller.get_current_dual_arm_q(), 'q'),
                         'dq': vector(controller.get_current_dual_arm_dq(), 'dq')}
                emit(dict(type='ready', stage='ready', controller_source=SOURCE,
                          actual_write_count=getattr(controller,'actual_write_count',None), arm_weight=controller.arm_weight, accepted_targets=0, **state))
            next_feedback = 0.
            while controller is not None and not stop.is_set():
                now = time.monotonic()
                if now >= next_feedback:
                    for name in ('publish_thread', 'subscribe_thread'):
                        thread = getattr(controller, name, None)
                        if thread is not None and not thread.is_alive():
                            raise RuntimeError('source controller ' + name + ' exited')
                    emit(dict(type='feedback', q=vector(controller.get_current_dual_arm_q(), 'q'),
                        dq=vector(controller.get_current_dual_arm_dq(), 'dq'),
                        accepted_targets=accepted, arm_weight=controller.arm_weight, actual_write_count=getattr(controller,'actual_write_count',None)))
                    next_feedback = now + .01
                try:
                    command = commands.get(timeout=max(0., min(.01, next_feedback - time.monotonic())))
                except queue.Empty:
                    continue
                if stop.is_set():
                    break
                action = command.get('command')
                if action == 'start':
                    if not started:
                        controller.activate()
                        started = True
                        emit({'type': 'state', 'stage': 'following'})
                elif action == 'target':
                    try:
                        q = vector(command.get('q'), 'q')
                        tau = vector(command.get('tau'), 'tau')
                        if not started:
                            raise ValueError('START is required before targets')
                    except ValueError as exc:
                        emit({'type': 'input_warning', 'message': str(exc)})
                        continue
                    controller.ctrl_dual_arm(runtime.array(q), runtime.array(tau))
                    accepted += 1
                else:
                    emit({'type': 'input_warning', 'message': 'unknown or invalid command'})
            return_code = 0
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        context['reason'] = 'robot_error'
        return_code = 1
    finally:
        if controller is not None:
            try:
                emit({'type': 'state', 'stage': 'stopping'})
                arm_weight_released = controller.release()
                if not arm_weight_released: raise RuntimeError('Arm authority release was not confirmed by publisher')
            except Exception as exc:
                error = error or (type(exc).__name__ + ': ' + str(exc))
                return_code = 1
        error = error or context['reader_error']
        if error:
            return_code = 1
        emit(dict(type='final', completed=error is None, reason=context['reason'],
            error=error, accepted_targets=accepted,
            actual_write_count=getattr(controller, 'actual_write_count', None) if controller_attempted else 0,
            last_action_unknown=controller_attempted, home_requested=False, arm_weight_released=arm_weight_released,
            physical_stop_verified=False, controller_source=SOURCE))
    return return_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-mode',choices=[CONTROL_MODE],required=True)
    parser.add_argument('--interface', default='eth0')
    parser.add_argument('--vendor-root', required=True)
    parser.add_argument('--enable-motion', action='store_true', required=True)
    args = parser.parse_args(argv)
    # Preserve one clean protocol descriptor; native DDS stdout goes to stderr.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _number, _frame: stop.set())
    try:
        return run(args, stop=stop,
            emit=lambda value: print(json.dumps(value, allow_nan=False), file=protocol, flush=True))
    finally:
        protocol.close()


if __name__ == '__main__':
    raise SystemExit(main())
