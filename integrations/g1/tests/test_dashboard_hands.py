import copy
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dashboard_hands import HandCoordinator


class FakeGloves:
    settings = {'max_step': 100}
    retarget = SimpleNamespace(CHANNELS=('thumb_flex','thumb_opposition','index','middle','ring','little'))

    def __init__(self):
        self.lock = threading.RLock()
        self.hands = {side: dict(running=True, input_live=True, live=True,
            model_ready=True, calibration_ready=True, mapping_ready=True)
            for side in ('left', 'right')}
        self.sequence = dict(left=1, right=1)
        self.target_calls, self.capture_calls, self.input_calls = [], [], []
        self.start_errors, self.target_errors = {}, {}
        self.closed = False
        self.refresh_calls = []
        self.cancel_calls = []
        self.invalidations = []

    def invalidate_calibration(self, side):
        self.invalidations.append(side)

    def refresh(self, side=None):
        self.refresh_calls.append(side)
        return self.status()

    def cancel_capture(self, side):
        self.cancel_calls.append(side)
        self.hands[side]['capture'] = None

    def status(self):
        with self.lock:
            return copy.deepcopy(self.hands)

    def start_input(self, side):
        self.input_calls.append(('start', side))
        if side in self.start_errors:
            raise RuntimeError(self.start_errors[side])
        self.hands[side]['running'] = True

    def stop_input(self, side):
        self.input_calls.append(('stop', side))
        self.hands[side]['running'] = False

    def target(self, side, measured):
        with self.lock:
            self.target_calls.append((side, list(measured)))
            if side in self.target_errors:
                raise RuntimeError(self.target_errors[side])
            if not self.hands[side]['input_live'] or not self.hands[side]['mapping_ready']:
                return None
            seq = self.sequence[side]
            return dict(seq=seq, source_t=float(seq), q_normalized=[.5] * 6)

    def capture(self, side, pose):
        self.capture_calls.append((side, pose))
        return dict(side=side, pose=pose)

    def close(self):
        self.closed = True

    def advance(self, side='both'):
        with self.lock:
            for name in ('left', 'right') if side == 'both' else (side,):
                self.sequence[name] += 1


class FakeTransport:
    def __init__(self):
        self.lock = threading.RLock()
        self.open_calls, self.send_calls, self.enable_calls = [], [], []
        self.snapshot_calls, self.close_calls = 0, 0
        self.feedback = {side: dict(q=[.4] * 6, fresh=True) for side in ('left', 'right')}
        self.fail_open = False
        self.snapshot_error = None
        self.send_error = None
        self.alive = False
        self.open_entered, self.send_entered = threading.Event(), threading.Event()
        self.open_gate, self.send_gate, self.closing = threading.Event(), threading.Event(), threading.Event()
        self.open_gate.set()
        self.send_gate.set()
        self.counts = dict(left=0, right=0)

    def open(self, enable_commands=False, *, cancel_event=None):
        with self.lock:
            self.open_calls.append(enable_commands)
            self.open_entered.set()
            self.closing.clear()
            while not self.open_gate.wait(.005):
                if self.closing.is_set() or cancel_event.is_set():
                    raise RuntimeError('opening cancelled')
            if cancel_event.is_set():
                raise RuntimeError('opening cancelled')
            if self.fail_open:
                raise RuntimeError('startup failed')
            self.alive = True
            return {'type': 'ready'}

    def snapshot(self):
        with self.lock:
            self.snapshot_calls += 1
            if not self.alive:
                raise RuntimeError('hand transport is not open')
            if self.snapshot_error:
                raise RuntimeError(self.snapshot_error)
            return copy.deepcopy(self.feedback)

    def enable(self, side, enabled):
        with self.lock:
            self.enable_calls.append((side, enabled))

    def send(self, side, q, speed, *, should_send=None):
        with self.lock:
            if not self.alive or self.send_error:
                raise RuntimeError(self.send_error or 'hand transport is not open')
            self.send_entered.set()
            while not self.send_gate.wait(.005):
                if self.closing.is_set():
                    return dict(published=False, error='closed')
            if should_send is not None and not should_send():
                return dict(published=False, error='hand target cancelled')
            self.send_calls.append((side, list(q), list(speed)))
            self.counts[side] += 1
            return dict(published=True, dds_write_count=self.counts[side])

    def hold(self, side):
        raise AssertionError('STOP must not call blocking transport.hold')

    def close(self):
        self.closing.set()
        with self.lock:
            self.close_calls += 1
            self.alive = False


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.gloves, self.transport = FakeGloves(), FakeTransport()
        self.manager = HandCoordinator(gloves=self.gloves, transport=self.transport)
        self.addCleanup(self.manager.close)

    def until(self, predicate, message='condition not reached', timeout=1.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(.005)
        self.fail(message)

    def stage(self, side):
        return self.manager.status()[side]['stage']

    def test_constructor_and_status_never_open_or_poll_transport(self):
        before = self.gloves.status()
        for _ in range(3):
            self.manager.status()
        threading.Event().wait(.05)
        self.assertEqual(self.transport.open_calls, [])
        self.assertEqual(self.transport.snapshot_calls, 0)
        self.assertEqual(self.gloves.target_calls, [])
        self.assertEqual(self.gloves.input_calls, [])
        self.assertEqual(self.gloves.status(), before)

    def test_missing_left_does_not_block_valid_right(self):
        self.gloves.hands['left'].update(mapping_ready=False, calibration_ready=False,
                                         calibration_error='left calibration missing')
        result = self.manager.start('both')
        self.assertIn('left', result['errors'])
        self.assertNotIn('right', result['errors'])
        self.until(lambda: self.transport.counts['right'] == 1)
        self.assertEqual(self.transport.counts['left'], 0)
        self.assertFalse(self.manager.status()['left']['running'])
        self.assertEqual(self.stage('right'), 'following')

    def test_stop_left_keeps_right_running(self):
        self.manager.start('both')
        self.until(lambda: all(self.transport.counts.values()))
        self.manager.stop('left')
        left_count = self.transport.counts['left']
        right_count = self.transport.counts['right']
        self.gloves.advance()
        self.until(lambda: self.transport.counts['right'] > right_count)
        self.assertEqual(self.transport.counts['left'], left_count)
        self.assertEqual(self.stage('left'), 'stopped')
        self.assertEqual(self.stage('right'), 'following')
        self.assertTrue(self.manager.status()['right']['running'])

    def test_low_input_holds_and_resumes_only_affected_side(self):
        self.manager.start('both')
        self.until(lambda: all(self.transport.counts.values()))
        self.gloves.hands['left']['input_live'] = False
        left_count, right_count = self.transport.counts['left'], self.transport.counts['right']
        self.gloves.advance()
        self.until(lambda: self.stage('left') == 'holding' and self.transport.counts['right'] > right_count)
        self.assertEqual(self.transport.counts['left'], left_count)
        self.assertTrue(self.manager.status()['left']['running'])
        self.gloves.hands['left']['input_live'] = True
        self.gloves.advance('left')
        self.until(lambda: self.transport.counts['left'] > left_count)
        self.assertEqual(self.stage('left'), 'following')
        self.assertEqual(self.transport.open_calls, [True])

    def test_stale_feedback_does_not_generate_targets_and_recovers(self):
        self.manager.start('both')
        self.until(lambda: all(self.transport.counts.values()))
        self.transport.feedback['left']['fresh'] = False
        self.until(lambda: self.stage('left') == 'holding')
        target_count = sum(side == 'left' for side, _ in self.gloves.target_calls)
        left_count, right_count = self.transport.counts['left'], self.transport.counts['right']
        self.gloves.advance()
        self.until(lambda: self.transport.counts['right'] > right_count)
        self.assertEqual(self.transport.counts['left'], left_count)
        self.assertEqual(sum(side == 'left' for side, _ in self.gloves.target_calls), target_count)
        self.transport.feedback['left']['fresh'] = True
        self.until(lambda: self.transport.counts['left'] > left_count)
        self.assertEqual(self.stage('left'), 'following')
        self.assertEqual(self.gloves.target_calls[-1][1], [400.] * 6)

    def test_one_side_mapping_exception_does_not_skip_other_side(self):
        self.gloves.target_errors['left'] = 'left source unavailable'
        self.manager.start('both')
        self.until(lambda: self.transport.counts['right'] == 1)
        self.assertEqual(self.stage('left'), 'holding')
        self.assertIn('left source unavailable', self.manager.status()['left']['error'])
        del self.gloves.target_errors['left']
        self.until(lambda: self.transport.counts['left'] == 1)
        self.assertIsNone(self.manager.status()['left']['error'])

    def test_rejected_target_response_keeps_per_side_hold_and_original_resume(self):
        from unittest.mock import patch
        original=self.transport.send
        reject=threading.Event();reject.set()
        def send(side,*args,**kwargs):
            if side=='left' and reject.is_set():
                return {'published':False,'error':'target exceeds measured-position step bound'}
            return original(side,*args,**kwargs)
        with patch.object(self.transport,'send',side_effect=send):
            self.manager.start('both')
            self.until(lambda:self.stage('left')=='holding' and self.transport.counts['right']==1)
            self.assertTrue(self.manager.status()['left']['running'])
            self.assertTrue(self.manager.transport_ready)
            self.assertEqual(self.transport.open_calls,[True])
            self.assertEqual(self.transport.close_calls,0)
            reject.clear()
            self.until(lambda:self.transport.counts['left']==1)
        self.assertEqual(self.stage('left'),'following')

    def test_invalid_side_target_does_not_invalidate_shared_transport(self):
        from unittest.mock import patch
        original=self.transport.send
        def send(side,*args,**kwargs):
            if side=='left':raise ValueError('target contains a nonfinite value')
            return original(side,*args,**kwargs)
        with patch.object(self.transport,'send',side_effect=send):
            self.manager.start('both')
            self.until(lambda:self.stage('left')=='holding' and self.transport.counts['right']==1)
            self.assertTrue(self.manager.status()['left']['running'])
            self.assertTrue(self.manager.transport_ready)
            self.assertEqual(self.transport.close_calls,0)
        self.until(lambda:self.transport.counts['left']==1)

    def test_calibration_requires_only_that_side_stopped_and_maps_closed_to_fist(self):
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right'] == 1)
        self.assertEqual(self.manager.calibrate('left', 'closed'), {'side': 'left', 'pose': 'fist'})
        self.assertEqual(self.gloves.capture_calls, [('left', 'fist')])
        with self.assertRaises(RuntimeError):
            self.manager.calibrate('right', 'open')
        self.manager.stop('right')
        self.manager.calibrate('right', 'open')
        self.assertEqual(self.gloves.capture_calls[-1], ('right', 'open'))

    def test_failed_open_requires_explicit_retry(self):
        self.transport.fail_open = True
        self.manager.start('right')
        self.until(lambda: self.stage('right') == 'error')
        self.assertFalse(self.manager.status()['right']['running'])
        threading.Event().wait(.35)
        self.assertEqual(self.transport.open_calls, [True])
        self.assertEqual(self.transport.snapshot_calls, 0)
        self.transport.fail_open = False
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right'] == 1)
        self.assertEqual(self.transport.open_calls, [True, True])

    def test_feedback_rpc_failure_invalidates_transport_until_explicit_retry(self):
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right'] == 1)
        self.transport.snapshot_error = 'feedback temporarily unavailable'
        self.until(lambda: self.stage('right') == 'error')
        self.assertFalse(self.manager.status()['right']['running'])
        self.assertFalse(self.manager.transport_ready)
        self.assertFalse(self.manager.command_mode)
        self.assertEqual(self.manager.feedback,{})
        self.gloves.advance('right')
        self.transport.snapshot_error = None
        threading.Event().wait(.08)
        self.assertEqual(self.transport.counts['right'],1)
        self.assertEqual(self.transport.open_calls,[True])
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right'] == 2)
        self.assertEqual(self.stage('right'), 'following')
        self.assertEqual(self.transport.open_calls, [True,True])

    def test_robot_reboot_cancels_both_outputs_and_retry_only_reopens_selected_side(self):
        self.manager.start('both')
        self.until(lambda: all(self.transport.counts.values()))
        self.transport.alive=False
        self.until(lambda: not self.manager.status()['running'])
        self.assertFalse(self.manager.transport_ready)
        self.assertFalse(self.manager.command_mode)
        self.assertTrue(self.manager.io_cancel.is_set())
        self.assertIsNone(self.manager.status()['left']['feedback'])
        self.assertIn('not open',self.manager.status()['left']['motion_error'])
        self.gloves.advance()
        threading.Event().wait(.08)
        self.assertEqual(self.transport.open_calls,[True])
        self.assertEqual(self.transport.counts,{'left':1,'right':1})
        self.manager.start('left')
        self.until(lambda: self.transport.counts['left']==2)
        self.assertEqual(self.transport.open_calls,[True,True])
        self.assertFalse(self.manager.io_cancel.is_set())
        self.assertFalse(self.manager.status()['right']['running'])
        self.assertEqual(self.transport.counts['right'],1)

    def test_dead_feedback_transport_reopens_readonly_only_after_explicit_check(self):
        self.manager.input_start()
        self.until(lambda: self.manager.transport_ready)
        self.transport.alive=False
        self.until(lambda: not self.manager.transport_ready)
        threading.Event().wait(.05)
        self.assertFalse(self.manager.feedback_requested)
        self.assertEqual(self.transport.open_calls,[False])
        self.manager.input_start()
        self.until(lambda: self.manager.transport_ready)
        self.assertEqual(self.transport.open_calls,[False,False])
        self.assertEqual(self.transport.send_calls,[])

    def test_send_exception_invalidates_shared_bridge_but_preserves_reader_error(self):
        self.manager.start('both')
        self.until(lambda: all(self.transport.counts.values()))
        self.gloves.hands['right']['error']='right reader ended independently'
        self.transport.send_error='SSH connection closed during target'
        self.gloves.advance('left')
        self.until(lambda: not self.manager.status()['running'])
        self.assertFalse(self.manager.transport_ready)
        self.assertIn('SSH connection closed',self.manager.status()['left']['motion_error'])
        self.assertEqual(self.manager.status()['right']['reader_error'],'right reader ended independently')
        self.assertEqual(self.transport.counts,{'left':1,'right':1})

    def test_stop_does_not_wait_for_service_startup(self):
        self.transport.open_gate.clear()
        self.manager.start('right')
        self.assertTrue(self.transport.open_entered.wait(1))
        stopped = threading.Event()
        thread = threading.Thread(target=lambda: (self.manager.stop('right'), stopped.set()))
        thread.start()
        try:
            self.assertTrue(stopped.wait(.2), 'STOP blocked behind service startup')
        finally:
            self.transport.open_gate.set()
            thread.join(timeout=1)
        self.until(lambda: self.manager.transport_ready)
        self.assertEqual(self.transport.send_calls, [])
        self.assertEqual(self.stage('right'), 'stopped')

    def test_failed_transport_cleanup_does_not_block_stop_and_keeps_explicit_new_request(self):
        from unittest.mock import patch
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right']==1)
        entered,release=threading.Event(),threading.Event()
        original=self.transport.close
        def close():
            entered.set();release.wait(2);original()
        with patch.object(self.transport,'close',side_effect=close):
            self.transport.alive=False
            self.assertTrue(entered.wait(1))
            stopped=threading.Event()
            thread=threading.Thread(target=lambda:(self.manager.stop(),stopped.set()))
            thread.start()
            try:
                self.assertTrue(stopped.wait(.2),'STOP blocked behind failed SSH cleanup')
                self.manager.start('left')
            finally:
                release.set();thread.join(1)
            self.until(lambda: self.transport.counts['left']==1)
        self.assertFalse(self.manager.status()['right']['running'])
        self.assertEqual(self.transport.open_calls,[True,True])

    def test_stop_cancels_queued_send_without_waiting_for_rpc_lock(self):
        self.transport.send_gate.clear()
        self.manager.start('both')
        self.assertTrue(self.transport.send_entered.wait(1))
        stopped = threading.Event()
        thread = threading.Thread(target=lambda: (self.manager.stop('left'), stopped.set()))
        thread.start()
        try:
            self.assertTrue(stopped.wait(.2), 'STOP blocked behind send')
        finally:
            self.transport.send_gate.set()
            thread.join(timeout=1)
        self.until(lambda: self.transport.counts['right'] == 1)
        self.assertEqual(self.transport.counts['left'], 0)
        self.assertEqual(self.stage('left'), 'stopped')

    def test_close_cancels_open_and_joins_owned_worker(self):
        self.transport.open_gate.clear()
        self.manager.start('right')
        self.assertTrue(self.transport.open_entered.wait(1))
        self.manager.close()
        self.assertFalse(self.manager.worker.is_alive())
        self.assertFalse(self.transport.alive)
        self.assertTrue(self.gloves.closed)
        self.assertTrue(self.manager.io_cancel.is_set())
        self.assertEqual(self.transport.send_calls, [])
        with self.assertRaises(RuntimeError):
            self.manager.start('right')

    def test_readonly_check_is_per_side_and_does_not_enable_motion(self):
        self.gloves.start_errors['left'] = 'missing left glove'
        result = self.manager.input_start()
        self.assertIn('missing left glove', result['left']['error'])
        self.until(lambda: self.transport.snapshot_calls > 0)
        self.assertEqual(self.transport.open_calls, [False])
        self.assertEqual(self.transport.send_calls, [])
        self.assertIn(('start', 'right'), self.gloves.input_calls)
        self.assertFalse(self.manager.status()['running'])

    def test_calibration_input_only_starts_selected_reader_without_robot_transport(self):
        self.manager.glove_input('left')
        threading.Event().wait(.04)
        self.assertEqual(self.gloves.input_calls, [('start', 'left')])
        self.assertEqual(self.gloves.refresh_calls, ['left'])
        self.assertEqual(self.transport.open_calls, [])
        self.assertEqual(self.transport.snapshot_calls, 0)
        self.assertEqual(self.transport.send_calls, [])
        self.assertFalse(self.manager.feedback_requested)
        self.manager.glove_input('left', start=False)
        self.assertEqual(self.gloves.input_calls[-1], ('stop', 'left'))

    def test_cannot_change_reader_of_following_hand(self):
        self.manager.start('right')
        self.until(lambda: self.transport.counts['right'] > 0)
        with self.assertRaises(RuntimeError):
            self.manager.glove_input('right', start=False)
        self.assertEqual(self.gloves.input_calls, [])
        self.assertTrue(self.manager.status()['right']['running'])

    def test_capture_blocks_hand_start_without_affecting_other_hand(self):
        self.gloves.hands['left']['capture'] = {'pose':'open','samples':1}
        result=self.manager.start('both')
        self.assertIn('left', result['errors'])
        self.until(lambda: self.transport.counts['right'] > 0)
        self.assertEqual(self.transport.counts['left'], 0)
        self.manager.cancel_calibration('left')
        self.assertEqual(self.gloves.cancel_calls, ['left'])

    def test_model_operation_blocks_all_hand_outputs_and_capture(self):
        from unittest.mock import patch
        with patch.object(self.manager.model, 'status', return_value={'active':True}):
            for action in (lambda:self.manager.start('right'), lambda:self.manager.glove_input('left'),
                           lambda:self.manager.calibrate('left','open'), self.manager.input_start):
                with self.assertRaises(RuntimeError):action()
        self.assertEqual(self.transport.open_calls, [])
        self.assertEqual(self.gloves.input_calls, [])

    def test_model_cannot_start_while_any_hand_output_or_capture_active(self):
        from unittest.mock import patch
        with patch.object(self.manager.model, 'start') as start:
            self.manager.requested['right']=True
            with self.assertRaises(RuntimeError):self.manager.model_start('left')
            self.manager.requested['right']=False
            self.gloves.hands['right']['capture']={'pose':'open'}
            with self.assertRaises(RuntimeError):self.manager.model_start('left')
            start.assert_not_called()

    def test_model_callbacks_stop_readers_then_refresh_and_start_selected_only(self):
        self.manager._model_prepare('left')
        self.assertEqual(self.gloves.input_calls,[('stop','left'),('stop','right')])
        self.manager._model_finished('left',True)
        self.assertEqual(self.gloves.input_calls[-1],('start','left'))
        self.assertEqual(self.gloves.refresh_calls,[None,None,None])
        self.assertEqual(self.gloves.invalidations,['left'])
        self.assertEqual(self.transport.open_calls,[])
        self.manager.done.set()
        calls=list(self.gloves.input_calls)
        self.manager._model_finished('right',True)
        self.assertEqual(self.gloves.input_calls,calls)

    def test_failed_model_operation_invalidates_partially_changed_model_without_reader_restart(self):
        self.manager._model_prepare('left')
        self.gloves.hands['left']['sdk_model_sha256']='changed-by-cli-before-error'
        self.manager._model_finished('left',False)
        self.assertEqual(self.gloves.invalidations,['left'])
        self.assertEqual(self.gloves.input_calls,[('stop','left'),('stop','right')])
        self.assertEqual(self.transport.open_calls,[])

    def test_incomplete_mapping_is_readiness_not_motion_error_and_recovers(self):
        self.gloves.hands['right'].update(mapping_ready=False, calibration_ready=False,
            calibration_pending=True, calibration_error='Personal model recalibrated; capture open and fist again')
        self.manager.start('right')
        value=self.manager.status()['right']
        self.assertEqual(value['error_kind'],'readiness')
        self.assertIsNone(value['motion_error'])
        self.assertTrue(value['calibration_pending'])
        self.gloves.hands['right'].update(mapping_ready=True,calibration_ready=True,
            calibration_pending=False,calibration_error='')
        value=self.manager.status()['right']
        self.assertFalse(value['error'])
        self.assertIsNone(value['error_kind'])
        self.assertEqual(self.transport.open_calls,[])

    def test_real_reader_error_is_not_masked_by_rejected_start(self):
        self.gloves.hands['right'].update(mapping_ready=False,calibration_ready=False,
            calibration_error='Personal model recalibrated; capture open and fist again')
        self.manager.start('right')
        self.gloves.hands['right']['error']='Original glove reader ended'
        value=self.manager.status()['right']
        self.assertEqual(value['reader_error'],'Original glove reader ended')
        self.assertEqual(value['error_kind'],'reader')
        self.assertEqual(value['error'],'Original glove reader ended')

    def test_cancel_during_model_finish_prevents_reader_restart(self):
        from unittest.mock import patch
        self.manager._model_prepare('left')
        with patch.object(self.manager.model,'status',return_value={'cancel_requested':True}):
            self.manager._model_finished('left',True)
        self.assertEqual(self.gloves.input_calls,[('stop','left'),('stop','right')])
        self.assertEqual(self.gloves.invalidations,['left'])

    def test_simulation_has_explicit_synthetic_readiness_without_readers_or_transport(self):
        self.manager.close()
        self.manager = HandCoordinator(gloves=self.gloves, transport=self.transport, simulation=True)
        self.addCleanup(self.manager.close)
        self.assertFalse(self.manager.status()['left']['input_live'])
        self.manager.input_start()
        for side in ('left', 'right'):
            state = self.manager.status()[side]
            self.assertTrue(state['simulation'])
            for key in ('input_live', 'model_ready', 'calibration_ready', 'command_ready'):
                self.assertTrue(state[key], key)
            self.assertEqual(state['sn'], 'SIM-' + side.upper())
        self.manager.start('both')
        self.until(lambda: self.stage('left') == 'following' and self.stage('right') == 'following')
        self.manager.input_stop()
        self.assertFalse(self.manager.status()['left']['input_live'])
        self.assertFalse(self.manager.status()['right']['command_ready'])
        self.assertEqual(self.transport.open_calls, [])
        self.assertEqual(self.transport.send_calls, [])
        self.assertFalse(any(action == 'start' for action, _ in self.gloves.input_calls))


if __name__ == '__main__':
    unittest.main()
