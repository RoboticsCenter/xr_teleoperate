"""Independent left/right hand controls around the original Wuji mapping."""
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
HAND_SPEED_NORMALIZED = .6  # BrainCo position+speed limit: 600 / 1000.


def sides(value):
    if value == 'both':
        return ('left', 'right')
    if value in ('left', 'right'):
        return (value,)
    raise ValueError('Choose left, right, or both hands')


class HandCoordinator:
    def __init__(self, root=ROOT, password='', simulation=False, gloves=None, transport=None, model_manager=None):
        from dashboard_gloves import DashboardGloves
        from dashboard_glove_model import GloveModelManager
        self.root, self.simulation = Path(root), simulation
        self.gloves = gloves if gloves is not None else DashboardGloves(self.root)
        self.transport, self.password = transport, password
        self.lock, self.transport_lock = threading.RLock(), threading.RLock()
        self.operations = threading.RLock()
        self.done = threading.Event()
        self.io_cancel = threading.Event()
        self.requested = {side: False for side in sides('both')}
        self.generation = {side: 0 for side in sides('both')}
        self.states = {side: {'running': False, 'stage': 'idle', 'error': None,
                             'error_kind': None, 'dds_write_count': 0} for side in sides('both')}
        self.preparing = self.command_mode = self.transport_ready = False
        self.feedback_requested = False
        self.feedback, self.last_seq = {}, {}
        self.sim_inputs = dict.fromkeys(sides('both'), False)
        self.sim_models = {'left': False, 'right': True}
        self.sim_calibrated = {'left': False, 'right': True}
        self.sim_captures, self.sim_samples, self.sim_results = {}, {'left': {}, 'right': {}}, {}
        self.model_bindings_before = None
        self.model = model_manager if model_manager is not None else GloveModelManager(
            self.root, simulation=simulation, before_start=self._model_prepare, on_finish=self._model_finished)
        self.worker = threading.Thread(target=self._loop, name='dashboard-hands', daemon=True)
        self.worker.start()

    def _ensure_transport(self, motion=False):
        if self.simulation:
            return
        with self.transport_lock:
            with self.lock:
                cancel = self.io_cancel
                if self.done.is_set() or cancel.is_set():
                    return
                if self.transport is None:
                    from dashboard_hand_transport import HandTransport
                    step = int(self.gloves.settings['max_step'] * .8) / 1000.
                    self.transport = HandTransport(root=self.root, password=self.password, max_step=step)
                transport = self.transport
                command_mode = motion or self.command_mode
                self.preparing = True
            try:
                # Cancellation also covers close-before-open scheduling races.
                transport.open(enable_commands=command_mode, cancel_event=cancel)
                with self.lock:
                    if not self.done.is_set() and not cancel.is_set() and transport is self.transport:
                        self.command_mode = command_mode
                        self.transport_ready = True
            finally:
                with self.lock:
                    self.preparing = False

    def input_start(self):
        with self.operations:
            return self._input_start()

    def _input_start(self):
        if self.model.status().get('active'):
            raise RuntimeError('Finish or cancel personal hand-model calibration first')
        with self.lock:
            if self.done.is_set():
                raise RuntimeError('Hand coordinator is closed')
        errors = {}
        if not self.simulation:
            for side in sides('both'):
                try:
                    self.gloves.start_input(side)
                except Exception as exc:
                    errors[side] = str(exc)
        with self.lock:
            if self.io_cancel.is_set():
                self.io_cancel = threading.Event()
            self.feedback_requested = True
            if self.simulation:
                self.sim_inputs = dict.fromkeys(sides('both'), True)
                self.sim_models = dict.fromkeys(sides('both'), True)
                self.sim_calibrated = dict.fromkeys(sides('both'), True)
            for side in sides('both'):
                self.states[side]['error'] = errors.get(side)
                self.states[side]['error_kind'] = 'reader' if errors.get(side) else None
        # The one owned worker starts feedback; status never performs RPCs.
        return self.status()

    def input_stop(self):
        with self.operations:
            return self._input_stop()

    def _input_stop(self):
        self.stop('both')
        self.model.cancel()
        with self.lock:
            self.io_cancel.set()
            self.feedback_requested = False
            self.command_mode = self.transport_ready = False
            self.feedback = {}
            self.sim_inputs = dict.fromkeys(sides('both'), False)
            self.sim_captures.clear()
            transport = self.transport
        error = None
        try:
            # close sets transport cancellation before acquiring its I/O lock.
            # Do not wait for our startup lock before requesting cancellation.
            if transport is not None and not self.simulation:
                transport.close()
        except Exception as exc:
            error = exc
        finally:
            with self.transport_lock:
                with self.lock:
                    if self.transport is transport:
                        self.transport = None
            if not self.simulation:
                for side in sides('both'):
                    try:
                        self.gloves.stop_input(side)
                    except Exception as exc:
                        error = error or exc
        if error:
            raise error
        return self.status()

    def glove_input(self, side, start=True):
        """Selected glove input only; calibration never opens robot transport."""
        if side not in ('left', 'right'):
            raise ValueError('Choose one glove')
        with self.operations:
            if self.done.is_set():
                raise RuntimeError('Hand coordinator is closed')
            if self.model.status().get('active'):
                raise RuntimeError('Finish or cancel personal hand-model calibration first')
            with self.lock:
                if self.requested[side]:
                    raise RuntimeError('Stop this hand following before changing its glove reader')
            if self.simulation:
                with self.lock:
                    self.sim_inputs[side] = bool(start)
                    if not start:
                        self.sim_captures.pop(side, None)
            elif start:
                self.gloves.refresh(side)
                self.gloves.start_input(side)
            else:
                self.gloves.stop_input(side)
            with self.lock:
                self.states[side]['error'] = None
                self.states[side]['error_kind'] = None
            return self.status()

    def refresh_gloves(self):
        with self.operations:
            if self.model.status().get('active'):
                raise RuntimeError('Personal hand-model calibration is still running')
            if not self.simulation:
                self.gloves.refresh()
            return self.status()

    def model_start(self, side):
        if side not in ('left', 'right'):
            raise ValueError('Choose one glove')
        with self.operations:
            if self.done.is_set():
                raise RuntimeError('Hand coordinator is closed')
            with self.lock:
                if any(self.requested.values()):
                    raise RuntimeError('Stop both hand outputs before personal hand-model calibration')
                if any(item.get('capture') for item in self.status().values() if isinstance(item, dict)):
                    raise RuntimeError('Finish or cancel open/fist capture first')
            return self.model.start(side)

    def _model_prepare(self, side):
        # Called by the owned model worker, outside the dashboard HTTP lock.
        self.model_bindings_before = None
        for name in sides('both'):
            if self.simulation:
                with self.lock:
                    self.sim_inputs[name] = False
            else:
                self.gloves.stop_input(name)
        if not self.simulation:
            current = self.gloves.refresh()
            self.model_bindings_before = {name: current[name].get('sdk_model_sha256') for name in sides('both')}

    def _model_finished(self, side, success):
        if self.simulation:
            with self.lock:
                if success:
                    self.sim_models[side] = True
                    self.sim_calibrated[side] = False
                    self.sim_samples[side] = {}
                    self.sim_results.pop(side, None)
                    self.sim_inputs[side] = not self.done.is_set() and not self.model.status(side).get('cancel_requested')
            return
        current = self.gloves.refresh()
        for name in sides('both'):
            changed = (self.model_bindings_before is not None and
                self.model_bindings_before[name] != current[name].get('sdk_model_sha256'))
            if changed or success and name == side:
                self.gloves.invalidate_calibration(name)
        self.gloves.refresh()
        if success and not self.done.is_set() and not self.model.status(side).get('cancel_requested'):
            self.gloves.start_input(side)

    def cancel_calibration(self, side):
        if side not in ('left', 'right'):
            raise ValueError('Choose one glove')
        with self.operations:
            if self.simulation:
                with self.lock:
                    self.sim_captures.pop(side, None)
                    self.sim_samples[side] = {}
                    self.sim_results[side] = {'state': 'cancelled'}
            else:
                self.gloves.cancel_capture(side)
            return self.status()

    def start(self, side):
        with self.operations:
            return self._start(side)

    def _start(self, side):
        selected = sides(side)
        if self.model.status().get('active'):
            raise RuntimeError('Finish or cancel personal hand-model calibration before hand following')
        snapshots = self.gloves.status()
        errors = {}
        with self.lock:
            if self.done.is_set():
                raise RuntimeError('Hand coordinator is closed')
            if self.io_cancel.is_set():
                self.io_cancel = threading.Event()
            for name in selected:
                state = snapshots[name]
                if state.get('capture') or (self.simulation and name in self.sim_captures):
                    errors[name] = 'Finish or cancel open/fist capture before hand following'
                elif self.simulation and not (self.sim_models[name] and self.sim_calibrated[name] and self.sim_inputs[name]):
                    errors[name] = 'Complete this simulated glove input and calibration first'
                elif not self.simulation and not state.get('mapping_ready'):
                    errors[name] = state.get('sdk_error') or state.get('calibration_error') or 'Complete this hand calibration first'
                elif not self.simulation and not state.get('input_live', state.get('live')):
                    errors[name] = 'Click check inputs and supply valid glove frames first'
                if name in errors:
                    if not self.requested[name]:
                        self.states[name].update(running=False, error=errors[name], error_kind='readiness', stage='unready')
                    continue
                if self.requested[name]:
                    continue
                self.requested[name] = True
                self.generation[name] += 1
                self.last_seq.pop(name, None)
                self.states[name].update(running=True, stage='preparing', error=None, error_kind=None)
        return {'sides': self.status(), 'errors': errors}

    def stop(self, side='both'):
        with self.lock:
            for name in sides(side):
                self.requested[name] = False
                self.generation[name] += 1
                self.states[name].update(running=False, stage='stopped')
        # No transport call: STOP must not wait on a service open or in-flight RPC.
        # The service retains its last target; this is not physical stop proof.
        return self.status()

    def calibrate(self, side, pose):
        with self.operations:
            return self._calibrate(side, pose)

    def _calibrate(self, side, pose):
        if side not in ('left', 'right'):
            raise ValueError('Choose one hand for calibration')
        if pose not in ('open', 'closed', 'fist'):
            raise ValueError('Choose open or closed hand calibration')
        if self.model.status().get('active'):
            raise RuntimeError('Finish personal hand-model calibration before open/fist capture')
        with self.lock:
            if self.done.is_set():
                raise RuntimeError('Hand coordinator is closed')
            if self.requested[side]:
                raise RuntimeError('Stop this hand before capturing calibration')
            if self.simulation:
                pose = 'fist' if pose == 'closed' else pose
                if not self.sim_inputs[side] or not self.sim_models[side]:
                    raise RuntimeError('Prepare this glove and its personal model first')
                if side in self.sim_captures:
                    raise RuntimeError('A calibration capture is already in progress')
                if pose == 'fist' and not self.sim_samples[side].get('open'):
                    raise ValueError('Capture the open hand first')
                if pose == 'open':
                    self.sim_samples[side] = {}
                self.sim_results.pop(side, None)
                self.sim_captures[side] = {'pose': pose, 'end': time.monotonic()+2.}
                return {'side': side, 'pose': pose, 'simulation': True}
            result = self.gloves.capture(side, 'fist' if pose == 'closed' else pose)
            if self.states[side].get('error_kind') == 'readiness':
                self.states[side].update(error=None, error_kind=None)
            return result

    def status(self):
        snapshot = self.gloves.status()
        models = {side: self.model.status(side) for side in sides('both')}
        model_active = self.model.status().get('active', False)
        raw = {}
        with self.lock:
            for side in sides('both'):
                item = dict(snapshot[side])
                if self.simulation:
                    ready = self.sim_inputs[side]
                    capture = self.sim_captures.get(side)
                    remaining = max(0., capture['end']-time.monotonic()) if capture else 0.
                    item.update(simulation=True, sn='SIM-' + side.upper(), running=ready,
                        live=ready, input_live=ready, model_ready=self.sim_models[side],
                        calibration_ready=self.sim_calibrated[side], mapping_ready=self.sim_models[side] and self.sim_calibrated[side],
                        sdk_error=None, calibration_error=None, error=None, capture_error='',
                        capture=None if not capture else {'pose':capture['pose'], 'remaining_s':remaining,
                            'samples':int((2.-remaining)*120), 'progress':(2.-remaining)/2.},
                        capture_cancel_ready=capture is not None, captured_poses=dict(self.sim_samples[side]),
                        capture_result=self.sim_results.get(side),
                        preview={'channels':list(self.gloves.retarget.CHANNELS), 'closure':[.5]*6,
                            'goals':[400]*6, 'published':False, 'simulation':True,
                            'ready':ready and self.sim_calibrated[side] and not capture,
                            'input_live':ready, 'units':'0-1000', 'error':''})
                input_running, input_error = item.get('running', False), item.get('error')
                state = dict(self.states[side])
                if (state.get('error_kind') == 'readiness' and item.get('mapping_ready')
                        and item.get('input_live', item.get('live')) and not item.get('capture')):
                    state.update(error=None, error_kind=None)
                kind = state.get('error_kind') if state.get('error') else None
                reader_error = input_error or (state.get('error') if kind == 'reader' else None)
                motion_error = state.get('error') if state.get('error') and kind not in ('readiness','reader') else None
                item.update(state)
                item.update(reader_error=reader_error, motion_error=motion_error,
                    error=reader_error or motion_error or state.get('error'),
                    error_kind='reader' if reader_error else 'motion' if motion_error else kind)
                item.update(input_live=item.get('input_live', item.get('live', False)),
                    speed_normalized=HAND_SPEED_NORMALIZED,
                    model_ready=item.get('model_ready', item.get('model_available', False)),
                    calibration_ready=item.get('calibration_ready', item.get('calibration_available', False)),
                    command_ready=bool(item.get('mapping_ready') and item.get('input_live', item.get('live', False))),
                    feedback=self.feedback.get(side), enabled=self.requested[side], input_running=input_running)
                item.update(model_calibration=models[side], calibration_busy=bool(model_active or item.get('capture')))
                item['detail'] = item.get('error') or item.get('sdk_error') or item.get('calibration_error') or (
                    '手指跟随中' if item['stage'] == 'following' else
                    '输入或反馈缺失时保持；有效数据恢复后继续' if item['stage'] == 'holding' else
                    '已停止发送新目标；服务保留最后目标' if item['stage'] == 'stopped' else
                    '原 Wuji 读取与 Revo2 映射')
                raw[side] = item
            raw.update(running=any(self.requested.values()),
                input_running=any(item['input_running'] for item in raw.values()),
                command_ready=any(raw[s]['command_ready'] for s in sides('both')),
                preparing=self.preparing, detail='左右手独立启停；使用原 Wuji 读取与手指映射')
        return raw

    def _allowed(self, side, generation, transport, cancel):
        with self.lock:
            return (not self.done.is_set() and not cancel.is_set() and self.requested[side]
                    and self.generation[side] == generation and self.transport is transport)

    def _side(self, side, generation, transport, cancel, feedback):
        phase = 'input'
        try:
            measured = feedback.get(side, {})
            target = self.gloves.target(side, [x * 1000 for x in measured['q']]) if measured.get('fresh') is True else None
            with self.lock:
                if not self._allowed(side, generation, transport, cancel):
                    return
                if target is None:
                    self.states[side].update(stage='holding', error=None)
                    return
                sequence = (target.get('seq'), target.get('source_t'))
                if self.last_seq.get(side) == sequence:
                    return
            phase = 'transport'
            transport.enable(side, True)
            result = transport.send(side, target['q_normalized'], [HAND_SPEED_NORMALIZED] * 6,
                should_send=lambda: self._allowed(side, generation, transport, cancel))
            with self.lock:
                if result.get('published'):
                    self.states[side]['dds_write_count'] = result.get('dds_write_count')
                if not self._allowed(side, generation, transport, cancel):
                    return
                self.states[side].update(stage='following' if result.get('published') else 'holding',
                                         error=result.get('error'), error_kind='motion' if result.get('error') else None)
                if result.get('published'):
                    self.last_seq[side] = sequence
        except Exception as exc:
            if phase == 'transport' and not isinstance(exc, (ValueError, TypeError)):
                # A failed shared bridge must be invalidated for both sides.
                # Mapping/input failures above remain independent hold/resume.
                raise
            with self.lock:
                if self._allowed(side, generation, transport, cancel):
                    self.states[side].update(stage='holding', error=str(exc), error_kind='motion')

    def _loop(self):
        while not self.done.wait(.01):
            with self.lock:
                active = dict((side, self.generation[side]) for side in sides('both') if self.requested[side])
                needs_motion = bool(active) and (not self.command_mode or not self.transport_ready)
                needs_feedback = self.feedback_requested and not self.transport_ready
                cancel = self.io_cancel
            if self.simulation:
                with self.lock:
                    for side, capture in list(self.sim_captures.items()):
                        if time.monotonic() >= capture['end']:
                            self.sim_samples[side][capture['pose']] = 240
                            if capture['pose'] == 'fist':
                                self.sim_calibrated[side] = True
                            self.sim_results[side] = {'state':'saved' if capture['pose']=='fist' else 'captured',
                                'pose':capture['pose'], 'samples':240, 'simulation':True}
                            del self.sim_captures[side]
                    for side, generation in active.items():
                        if self.requested[side] and self.generation[side] == generation:
                            self.states[side]['stage'] = 'following'
                continue
            transport = None
            try:
                if needs_motion or needs_feedback:
                    self._ensure_transport(needs_motion)
                with self.lock:
                    transport = self.transport if self.transport_ready else None
                if transport is None or cancel.is_set():
                    continue
                feedback = transport.snapshot()
                with self.lock:
                    if cancel.is_set() or transport is not self.transport:
                        continue
                    self.feedback = feedback
                for side, generation in active.items():
                    self._side(side, generation, transport, cancel, feedback)
            except Exception as exc:
                with self.lock:
                    if cancel.is_set() or self.done.is_set():
                        continue
                    if transport is not None and transport is not self.transport:
                        continue
                    failed_transport = self.transport
                    cancel.set()
                    self.feedback = {}
                    self.feedback_requested = self.transport_ready = self.command_mode = False
                    for side in sides('both'):
                        if self.requested[side] or needs_feedback:
                            self.requested[side] = False
                            self.generation[side] += 1
                            self.states[side].update(running=False, stage='error', error=str(exc), error_kind='motion')
                # No automatic reconnect or resumed output after robot/SSH loss.
                # Cleanup may wait on SSH; STOP/status must remain responsive.
                if failed_transport is not None:
                    try:
                        failed_transport.close()
                    except Exception:
                        pass  # Retain the original failure; explicit retry reopens.

    def close(self):
        with self.operations:
            return self._close()

    def _close(self):
        self.done.set()
        error = None
        try:
            self.model.close()
            self.input_stop()
        except Exception as exc:
            error = exc
        finally:
            self.worker.join(timeout=10)
            try:
                self.gloves.close()
            except Exception as exc:
                error = error or exc
        if self.worker.is_alive():
            raise RuntimeError('Hand worker is still closing')
        if error:
            raise error
