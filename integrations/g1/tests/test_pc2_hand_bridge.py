import importlib.util
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import pc2_hand_bridge as bridge


def message(q=.5):
    return SimpleNamespace(states=[SimpleNamespace(q=q, dq=0.) for _ in range(6)])


class BridgeTests(unittest.TestCase):
    def backend(self, enabled=True):
        backend = bridge.DDSHands(enable_commands=enabled, max_step=.08)
        self.publishers = []
        def factory(topic, message_type):
            publisher = mock.Mock()
            publisher.Write.return_value = True
            self.publishers.append(publisher)
            return publisher
        backend.publisher_factory = factory
        backend.message_factory = lambda cmds: SimpleNamespace(cmds=cmds)
        backend.motor_factory = SimpleNamespace
        backend.receive('right', message())
        backend.receive('left', message())
        return backend

    def test_construction_and_feedback_mode_never_publish(self):
        with mock.patch.object(bridge.subprocess, 'Popen') as spawn:
            bridge.ManagedService('/not-opened')
            backend = self.backend(enabled=False)
            self.assertTrue(backend.snapshot()['right']['fresh'])
            with self.assertRaisesRegex(ValueError, 'not enabled'):
                backend.send('right', [.5]*6, [.2]*6)
            self.assertEqual(self.publishers, [])
            spawn.assert_not_called()

    def test_exact_six_channels_and_no_mapping_changes(self):
        backend = self.backend()
        q = [.51, .52, .53, .54, .55, .56]
        speeds = [.1, .2, .3, .4, .5, .6]
        result = backend.send('right', q, speeds)
        sent = self.publishers[0].Write.call_args.args[0]
        self.assertEqual([m.q for m in sent.cmds], q)
        self.assertEqual([m.dq for m in sent.cmds], speeds)
        self.assertTrue(result['published'])
        self.assertFalse(result['acknowledged'])

    def test_invalid_or_missing_feedback_is_independent_and_resumes(self):
        backend = self.backend()
        backend.receive('right', message(float('nan')))
        with self.assertRaisesRegex(ValueError, 'feedback unavailable'):
            backend.send('right', [.5]*6, [.2]*6)
        self.assertTrue(backend.send('left', [.5]*6, [.2]*6)['published'])
        backend.receive('right', message())
        self.assertTrue(backend.send('right', [.5]*6, [.2]*6)['published'])

    def test_bad_target_does_not_latch_and_step_uses_measured(self):
        backend = self.backend()
        for invalid in ([.9]*6, [float('nan')]*6, [True]*6, [0.]*5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                backend.send('right', invalid, [.2]*6)
        backend.send('right', [.57]*6, [.2]*6)
        with self.assertRaisesRegex(ValueError, 'step bound'):
            backend.send('right', [.64]*6, [.2]*6)
        backend.receive('right', message(.57))
        self.assertTrue(backend.send('right', [.64]*6, [.2]*6)['published'])

    def test_publisher_init_cannot_refresh_old_feedback(self):
        backend = self.backend()
        original = backend.publisher_factory
        def slow(topic, kind):
            publisher = original(topic, kind)
            def init():
                with backend.lock:
                    backend.samples['right']['received'] -= 1
            publisher.Init.side_effect = init
            return publisher
        backend.publisher_factory = slow
        with self.assertRaisesRegex(ValueError, 'setup'):
            backend.send('right', [.5]*6, [.2]*6)
        self.publishers[0].Write.assert_not_called()

    def test_dds_write_failure_is_not_claimed_as_success(self):
        backend = self.backend()
        publisher = mock.Mock()
        publisher.Write.return_value = False
        backend.publishers['right'] = publisher
        with self.assertRaisesRegex(RuntimeError, 'delivery unknown'):
            backend.send('right', [.5]*6, [.2]*6)
        self.assertEqual(backend.write_count['right'], 0)
        self.assertTrue(backend.send('left', [.5]*6, [.2]*6)['published'])
        with self.assertRaisesRegex(RuntimeError, 'delivery unknown'):
            backend.send('right', [.5]*6, [.2]*6)
        self.assertEqual(publisher.Write.call_count, 1)

    def test_stop_after_build_cannot_start_the_service(self):
        with tempfile.TemporaryDirectory() as directory:
            service = bridge.ManagedService('/patch', base=Path(directory)/'base')
            stopped = threading.Event()
            stopped.set()
            with mock.patch.object(bridge, 'build_service', return_value=Path(directory)/'binary'), \
                    mock.patch.object(bridge.subprocess, 'Popen') as popen:
                self.assertIsNone(service.start(True, stopped=stopped))
                popen.assert_not_called()
            service.close()

    def test_cleanup_signals_only_the_owned_group_and_hides_password(self):
        service = bridge.ManagedService('/patch', password='secret-XYZ')
        process = mock.Mock(pid=34567, returncode=0)
        process.poll.return_value = None
        service.process = process
        with mock.patch.object(bridge.subprocess, 'run') as run:
            self.assertEqual(service.close(), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[-3:], ['-TERM', '--', '-34567'])
            self.assertNotIn('secret-XYZ', repr(command))
            self.assertEqual(run.call_args.kwargs['input'], 'secret-XYZ\n')
        self.assertEqual(service.password, '')

    def test_source_hash_mismatch_prevents_any_build(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(bridge.subprocess, 'run') as run:
            base = Path(directory)
            (base/'main.cpp').write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'dependency differs'):
                bridge.build_service('/unused', base)
            run.assert_not_called()

    def test_continuous_cpp_gate_with_fake_feedback_only(self):
        compiler = shutil.which('g++')
        baseline = Path('/tmp/brainco-hand-service-pinned')
        if compiler is None or not baseline.is_dir():
            self.skipTest('offline audited source/compiler unavailable')
        source = r'''
#include "command_gate.h"
#include <cassert>
using namespace brainco_gate;
int main() {
  auto t=Clock::now(); Feedback feedback; feedback.valid=true; feedback.read_started=t;
  feedback.positions.fill(500); int writes=0; Six last{};
  auto send=[&](const Six& q,const Six&){++writes;last=q;};
  CommandGate disabled(false,std::chrono::milliseconds(200),std::chrono::milliseconds(100));
  disabled.submit(std::vector<float>(6,.5f),std::vector<float>(6,.2f),t);
  disabled.tick(t,feedback,send); assert(writes==0);
  CommandGate gate(true,std::chrono::milliseconds(200),std::chrono::milliseconds(100));
  gate.tick(t,feedback,send); assert(writes==0);
  gate.submit(std::vector<float>(6,.5f),std::vector<float>(6,.2f),t);
  gate.tick(t,feedback,send); assert(writes==1);
  auto later=t+std::chrono::hours(1); feedback.read_started=later;
  gate.tick(later,feedback,send); assert(writes==2);
  gate.submit(std::vector<float>(3,.2f),std::vector<float>(6,.2f),later);
  gate.tick(later,feedback,send); assert(writes==3 && last[0]==500);
  feedback.valid=false; gate.tick(later,feedback,send); assert(writes==3);
  feedback.valid=true;
  gate.submit(std::vector<float>(6,.6f),std::vector<float>(6,.2f),later);
  gate.tick(later,feedback,send); assert(writes==4 && last[0]==600);
  gate.shutdown(); gate.tick(later,feedback,send); assert(writes==5 && last[0]==500);
  gate.tick(later,feedback,send); assert(writes==5);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            shutil.copytree(baseline/'include', path/'include')
            shutil.copy2(baseline/'main.cpp', path/'main.cpp')
            subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT/'patches/brainco-dashboard-hold-resume.patch')],
                           cwd=path, check=True, stdout=subprocess.PIPE)
            (path/'gate_test.cpp').write_text(source)
            subprocess.run([compiler, '-std=c++17', '-pthread', '-I'+str(path/'include'),
                            str(path/'gate_test.cpp'), '-o', str(path/'test')], check=True)
            subprocess.run([str(path/'test')], check=True)

    def test_real_patched_service_with_fake_sdk_rejects_motor_state_three(self):
        compiler = shutil.which('g++')
        baseline = Path('/tmp/brainco-hand-service-pinned')
        if compiler is None or not baseline.is_dir():
            self.skipTest('offline audited source/compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            shutil.copytree(baseline/'include', path/'include')
            shutil.copy2(baseline/'main.cpp', path/'main.cpp')
            subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT/'patches/brainco-dashboard-hold-resume.patch')],
                           cwd=path, check=True, stdout=subprocess.PIPE)
            # Run the existing nine real-worker fake-SDK cases, with state 3
            # instead of 255: original RevoHand rejects all states outside 0..2.
            source = (ROOT/'tests/brainco_service_mock/worker_test.cpp').read_text()
            source = source.replace('data[18] = 255', 'data[18] = 3')
            # The preceding fake worker intentionally stops the process. A new
            # startup check must explicitly restore its process lifecycle state.
            source = source.replace('test_open_settling = true;', 'running = true; test_open_settling = true;')
            (path/'worker_test.cpp').write_text(source)
            subprocess.run([compiler, '-std=c++17', '-pthread',
                '-I'+str(ROOT/'tests/brainco_service_mock/stubs'), '-I'+str(path/'include'),
                '-I'+str(path), str(path/'worker_test.cpp'), '-o', str(path/'test')], check=True)
            subprocess.run([str(path/'test')], check=True, stdout=subprocess.PIPE)


    def test_real_patched_startup_retries_only_failed_known_side_and_cleans_up(self):
        compiler = shutil.which('g++')
        baseline = Path('/tmp/brainco-hand-service-pinned')
        if compiler is None or not baseline.is_dir():
            self.skipTest('offline audited source/compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            shutil.copytree(baseline/'include', path/'include')
            shutil.copy2(baseline/'main.cpp', path/'main.cpp')
            subprocess.run(['patch', '--batch', '-p1', '-i', str(ROOT/'patches/brainco-dashboard-hold-resume.patch')],
                           cwd=path, check=True, stdout=subprocess.PIPE)
            subprocess.run([compiler, '-std=c++17', '-pthread',
                '-I'+str(ROOT/'tests/brainco_service_mock/stubs'), '-I'+str(path/'include'),
                '-I'+str(path), str(ROOT/'tests/brainco_service_mock/startup_test.cpp'),
                '-o', str(path/'test')], check=True)
            result = subprocess.run([str(path/'test')], check=True, stdout=subprocess.PIPE,
                                    text=True, timeout=12)
            self.assertIn('retries, ownership, identity and cancellation passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
