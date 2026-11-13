"""JSONL adapter tests with fake ownership RPCs and controllers only."""
import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pc2_branch_arm as adapter


class Input:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        value = self.lines.get()
        if value is None:
            raise StopIteration
        return value

    def send(self, **value):
        self.lines.put(json.dumps(value) + '\n')


class Controller:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.q = [0.] * 14
        self.targets = []
        self.starts = self.homes = self.releases = 0
        self.arm_weight = 0.

    def get_current_dual_arm_q(self):
        return self.q

    def get_current_dual_arm_dq(self):
        return [0.] * 14

    def activate(self):
        self.starts += 1

    def ctrl_dual_arm(self, q, tau):
        self.targets.append((q, tau))
        self.q = q

    def release(self):
        self.releases += 1
        self.arm_weight = 0.
        return True


class Switcher:
    def __init__(self, before='ai', after='', release_code=0):
        self.before, self.after, self.release_code = before, after, release_code
        self.calls = []

    def SetTimeout(self, value):
        self.calls.append(('timeout', value))

    def Init(self):
        self.calls.append(('init',))

    def CheckMode(self):
        self.calls.append(('check',))
        name = self.after if ('release',) in self.calls else self.before
        return 0, {'name': name}

    def ReleaseMode(self):
        self.calls.append(('release',))
        return self.release_code, {}


class AdapterTests(unittest.TestCase):
    def launch(self, switcher=None):
        self.input, self.records, self.controllers = Input(), [], []
        self.condition = threading.Condition()
        self.result = []
        self.switcher = switcher or Switcher()
        self.initialize = mock.Mock()

        def controller(**kwargs):
            value = Controller(**kwargs)
            self.controllers.append(value)
            return value

        self.runtime = SimpleNamespace(initialize=self.initialize, switcher=lambda: self.switcher,
            controller=controller, array=lambda value: value)

        def emit(record):
            with self.condition:
                self.records.append(record)
                self.condition.notify_all()

        self.thread = threading.Thread(target=lambda: self.result.append(adapter.run(
            SimpleNamespace(interface='eth0', vendor_root='/fake', enable_motion=True,control_mode='normal_walking'),
            stdin=self.input, emit=emit, runtime_loader=lambda root: self.runtime)))
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.input.send(command='stop')
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def wait(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                for record in reversed(self.records):
                    if predicate(record):
                        return record
                self.condition.wait(timeout=.02)
        self.fail('record not received: ' + repr(self.records))

    def test_explicit_motion_opt_in_is_required_before_import(self):
        loader = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'enable-motion'):
            adapter.run(SimpleNamespace(enable_motion=False,control_mode='normal_walking'), runtime_loader=loader)
        loader.assert_not_called()

    def test_source_controller_lifecycle_and_feedback(self):
        self.launch()
        ready = self.wait(lambda r: r['type'] == 'ready')
        self.assertEqual(len(ready['q']), 14)
        self.assertIsNone(ready['actual_write_count'])
        self.initialize.assert_called_once_with(0, 'eth0')
        controller = self.controllers[0]
        self.assertEqual(controller.kwargs, {'motion_mode': True})
        self.assertEqual(controller.starts, 0)
        self.input.send(command='start')
        self.wait(lambda r: r.get('stage') == 'following')
        self.input.send(command='target', q=[.1] * 14, tau=[.2] * 14)
        feedback = self.wait(lambda r: r['type'] == 'feedback' and r['accepted_targets'] == 1)
        self.assertEqual(feedback['q'], [.1] * 14)
        self.input.send(command='stop')
        final = self.wait(lambda r: r['type'] == 'final')
        self.assertEqual(controller.releases, 1)
        self.assertEqual(controller.starts, 1)
        self.assertTrue(final['completed'])
        self.assertTrue(final['last_action_unknown'])
        self.assertFalse(final['physical_stop_verified'])
        self.assertIsNone(final['actual_write_count'])

    def test_targets_require_start_and_bad_targets_hold_then_resume(self):
        self.launch()
        self.wait(lambda r: r['type'] == 'ready')
        self.input.send(command='target', q=[0.] * 14, tau=[0.] * 14)
        self.wait(lambda r: r['type'] == 'input_warning' and 'START' in r['message'])
        self.input.send(command='start')
        self.wait(lambda r: r.get('stage') == 'following')
        self.input.send(command='target', q=[float('nan')] * 14, tau=[0.] * 14)
        self.wait(lambda r: r['type'] == 'input_warning' and 'non-finite' in r['message'])
        self.assertEqual(self.controllers[0].targets, [])
        self.input.send(command='target', q=[.3] * 14, tau=[0.] * 14)
        self.wait(lambda r: r['type'] == 'feedback' and r['accepted_targets'] == 1)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.controllers[0].homes, 0)

    def test_duplicate_start_is_idempotent(self):
        self.launch()
        self.wait(lambda r: r['type'] == 'ready')
        self.input.send(command='start')
        self.input.send(command='start')
        self.input.send(command='target', q=[0.] * 14, tau=[0.] * 14)
        self.wait(lambda r: r['type'] == 'feedback' and r['accepted_targets'] == 1)
        self.assertEqual(self.controllers[0].starts, 1)

    def test_eof_runs_source_go_home(self):
        self.launch()
        self.wait(lambda r: r['type'] == 'ready')
        self.input.lines.put(None)
        final = self.wait(lambda r: r['type'] == 'final')
        self.assertEqual(final['reason'], 'stdin_eof')
        self.assertFalse(final['home_requested'])
        self.assertTrue(final['arm_weight_released'])

    def test_dead_source_publisher_is_a_robot_failure(self):
        self.launch()
        self.wait(lambda r: r['type'] == 'ready')
        self.controllers[0].publish_thread = SimpleNamespace(is_alive=lambda: False)
        final = self.wait(lambda r: r['type'] == 'final')
        self.assertFalse(final['completed'])
        self.assertIn('publish_thread exited', final['error'])
        self.assertEqual(self.controllers[0].releases, 1)

    def test_ownership_failure_never_instantiates_controller(self):
        switcher = Switcher(before=None)
        self.launch(switcher)
        final = self.wait(lambda r: r['type'] == 'final')
        self.assertEqual(self.controllers, [])
        self.assertFalse(final['completed'])
        self.assertEqual(final['actual_write_count'], 0)
        self.assertNotIn(('release',), switcher.calls)

    def test_motion_service_is_queried_without_release(self):
        client=Switcher(before='ai');records=[]
        self.assertTrue(adapter.verify_ownership(client,threading.Event(),records.append))
        self.assertEqual([c for c in client.calls if c[0] in ('check','release')],[('check',)])
        self.assertFalse(records[-1]['release_sent'])
        self.assertFalse(records[-1]['regular_mode_verified'])

    def test_inactive_service_is_rejected_without_mode_change(self):
        client=Switcher(before='')
        with self.assertRaisesRegex(RuntimeError,'Regular mode'):
            adapter.verify_ownership(client,threading.Event(),lambda record:None)
        self.assertNotIn(('release',),client.calls)

    def test_stop_before_preparation_never_loads_sdk(self):
        stop, loader = threading.Event(), mock.Mock()
        stop.set()
        records = []
        stdin = Input()
        stdin.lines.put(None)
        self.assertEqual(adapter.run(SimpleNamespace(enable_motion=True,control_mode='normal_walking'), stdin=stdin,
            stop=stop, emit=records.append, runtime_loader=loader), 0)
        loader.assert_not_called()
        self.assertEqual(records[-1]['actual_write_count'], 0)

    def test_vector_validation_rejects_shape_bool_and_nonfinite(self):
        for value in ([0.] * 13, [0.] * 15, [True] * 14, [float('inf')] * 14,
                      ['0'] * 14, [[0.]] * 14, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter.vector(value, 'q')


if __name__ == '__main__':
    unittest.main()
