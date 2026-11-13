import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import branch_ik_worker as worker


class FakePin:
    def __init__(self):
        self.calls = []

    def forwardKinematics(self, model, data, q):
        self.calls.append(('fk', q.copy()))
        for index, frame in enumerate(data.oMf):
            frame.homogeneous = np.eye(4)
            frame.homogeneous[0, 3] = q[index * 7]

    def updateFramePlacements(self, model, data):
        self.calls.append(('frames',))

    def rnea(self, model, data, q, velocity, acceleration):
        self.calls.append(('rnea', q.copy(), velocity.copy(), acceleration.copy()))
        return 2 * q


class FakeIK:
    def __init__(self):
        self.reduced_robot = SimpleNamespace(
            model=SimpleNamespace(getFrameId=lambda name: {'L_ee': 0, 'R_ee': 1}[name]),
            data=SimpleNamespace(oMf=[SimpleNamespace(homogeneous=np.eye(4)) for _ in range(2)]))
        self.solution = np.arange(14, dtype=float) / 10
        self.calls = []

    def solve_ik(self, left, right, q, dq):
        self.calls.append(tuple(value.copy() for value in (left, right, q, dq)))
        return self.solution, np.full(14, -999.)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.ik, self.pin = FakeIK(), FakePin()
        self.backend = worker.BranchIK(self.ik, self.pin, np)
        self.request = {'command': 'solve', 'left': np.eye(4).tolist(),
                        'right': np.eye(4).tolist(), 'q': [0.] * 14,
                        'dq': [0.1] * 14}

    def test_forward_kinematics_uses_source_end_effector_frames(self):
        q = [0.2] * 7 + [-0.3] * 7
        result = self.backend.process({'command': 'fk', 'q': q})
        self.assertEqual(result['type'], 'fk')
        self.assertEqual(result['left'][0][3], 0.2)
        self.assertEqual(result['right'][0][3], -0.3)
        self.assertEqual([c[0] for c in self.pin.calls], ['fk', 'frames'])
        self.assertFalse(self.ik.calls)

    def test_solve_preserves_targets_and_source_solution(self):
        self.request['right'][0][3] = 0.6
        result = self.backend.process(self.request)
        self.assertEqual(result['q'], self.ik.solution.tolist())
        left, right, q, dq = self.ik.calls[0]
        np.testing.assert_array_equal(left, self.request['left'])
        np.testing.assert_array_equal(right, self.request['right'])
        np.testing.assert_array_equal(q, self.request['q'])
        np.testing.assert_array_equal(dq, self.request['dq'])

    def test_fixed_left_is_applied_before_recomputing_gravity(self):
        self.request['fixed_left'] = [0.35] * 7
        original_right = self.ik.solution[7:].copy()
        result = self.backend.process(self.request)
        np.testing.assert_array_equal(result['q'][:7], [0.35] * 7)
        np.testing.assert_array_equal(result['q'][7:], original_right)
        gravity = self.pin.calls[-1]
        self.assertEqual(gravity[0], 'rnea')
        np.testing.assert_array_equal(gravity[1], result['q'])
        np.testing.assert_array_equal(gravity[2], np.zeros(14))
        np.testing.assert_array_equal(gravity[3], np.zeros(14))
        np.testing.assert_array_equal(result['tau'], np.array(result['q']) * 2)
        # Preserve upstream array aliasing, including the filter's solution.
        np.testing.assert_array_equal(self.ik.solution[:7], [0.35] * 7)

    def test_invalid_schema_rejected_before_solver(self):
        cases = [('q', [0.] * 13), ('dq', [float('nan')] * 14),
                 ('left', [[0.] * 4] * 3), ('right', [[True] * 4] * 4),
                 ('fixed_left', [0.] * 14), ('fixed_right', [0.] * 14), ('q', ['0'] * 14)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                request = dict(self.request, **{field: value})
                with self.assertRaises(ValueError):
                    self.backend.process(request)
        self.assertFalse(self.ik.calls)

    def test_fixed_right_keeps_left_solution_and_updates_gravity(self):
        self.request['fixed_right'] = [-0.15] * 7
        original_left = self.ik.solution[:7].copy()
        result = self.backend.process(self.request)
        np.testing.assert_array_equal(result['q'][:7], original_left)
        np.testing.assert_array_equal(result['q'][7:], [-0.15] * 7)
        np.testing.assert_array_equal(result['tau'], np.array(result['q']) * 2)

    def test_nonfinite_solver_result_is_not_serialized_as_target(self):
        self.ik.solution[8] = float('inf')
        with self.assertRaises(ValueError):
            self.backend.process(self.request)
        self.assertFalse(self.pin.calls)

    def test_stream_errors_do_not_desynchronize_following_requests(self):
        source = io.StringIO('not json\n[]\n' + json.dumps({'command': 'fk', 'q': [0.] * 14}) + '\n')
        output = io.StringIO()
        worker.serve(self.backend, source, output)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r['type'] for r in records], ['ready', 'error', 'error', 'fk'])

    def test_stop_prevents_any_later_solver_request(self):
        source = io.StringIO(json.dumps({'command': 'stop'}) + '\n' + json.dumps(self.request) + '\n')
        output = io.StringIO()
        worker.serve(self.backend, source, output)
        self.assertEqual(json.loads(output.getvalue()), {'type': 'ready'})
        self.assertFalse(self.ik.calls)


@unittest.skipUnless(os.environ.get('G1_BRANCH_IK_SMOKE') == '1',
                     'set G1_BRANCH_IK_SMOKE=1 in the Pinocchio/CasADi environment')
class SourceIntegrationTests(unittest.TestCase):
    def test_real_branch_fk_ik_jsonl_and_fixed_left_gravity(self):
        """Offline numerical solve against real source/assets; no DDS imports."""
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryFile(mode='w+') as stderr:
            process = subprocess.Popen([sys.executable, '-u', '-B',
                str(ROOT / 'scripts/branch_ik_worker.py'), '--cache-root', cache],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                text=True, bufsize=1)
            def receive():
                self.assertTrue(select.select([process.stdout], [], [], 45)[0], 'IK worker response timed out')
                line = process.stdout.readline()
                if not line:
                    stderr.seek(0)
                    self.fail('IK worker exited: ' + stderr.read()[-5000:])
                return json.loads(line)
            def send(record):
                process.stdin.write(json.dumps(record) + '\n')
                process.stdin.flush()
                return receive()
            try:
                self.assertEqual(receive(), {'type': 'ready'})
                q = [0., .15, 0., .4, 0., 0., 0., 0., -.15, 0., .4, 0., 0., 0.]
                poses = send({'command': 'fk', 'q': q})
                self.assertEqual(poses['type'], 'fk', poses)
                right = np.array(poses['right'])
                right[0, 3] += .01
                result = send({'command': 'solve', 'left': poses['left'],
                               'right': right.tolist(), 'q': q, 'dq': [0.] * 14,
                               'fixed_left': q[:7]})
                self.assertEqual(result['type'], 'target', result)
                np.testing.assert_array_equal(result['q'][:7], q[:7])
                self.assertTrue(np.isfinite(result['q']).all())
                self.assertTrue(np.isfinite(result['tau']).all())
                self.assertGreater(np.linalg.norm(np.array(result['q'])[7:] - q[7:]), 1e-5)
                moved = send({'command': 'fk', 'q': result['q']})
                before = np.linalg.norm(np.array(poses['right'])[:3, 3] - right[:3, 3])
                after = np.linalg.norm(np.array(moved['right'])[:3, 3] - right[:3, 3])
                self.assertLess(after, before)
                np.testing.assert_allclose(moved['left'], poses['left'], atol=1e-12)
                left = np.array(poses['left'])
                left[0, 3] += .01
                left_result = send({'command': 'solve', 'left': left.tolist(),
                                    'right': poses['right'], 'q': q, 'dq': [0.] * 14,
                                    'fixed_right': q[7:]})
                self.assertEqual(left_result['type'], 'target', left_result)
                np.testing.assert_array_equal(left_result['q'][7:], q[7:])
                self.assertTrue(np.isfinite(left_result['tau']).all())
                left_moved = send({'command': 'fk', 'q': left_result['q']})
                left_error = np.linalg.norm(np.array(left_moved['left'])[:3, 3] - left[:3, 3])
                self.assertLess(left_error, .01)
                np.testing.assert_allclose(left_moved['right'], poses['right'], atol=1e-12)
                process.stdin.write('{"command":"stop"}\n')
                process.stdin.flush()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stdout.read(), '')
                self.assertEqual(list(Path(cache).iterdir()), [])
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()


if __name__ == '__main__':
    unittest.main()
