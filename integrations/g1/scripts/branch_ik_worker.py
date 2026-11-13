#!/usr/bin/env python3
"""JSONL adapter for the vendored PR321 G1_29_ArmIK; no robot transport.

The upstream IK and filter run unchanged.  A private working directory supplies
their relative asset paths and contains the upstream-generated model cache.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS = Path.home() / '.cache/unitree-g1-reuse/runtime-pr321/assets/g1'
DEFAULT_CACHE = Path.home() / '.cache/unitree-g1-arms/branch-ik'


class BranchIK:
    """Schema conversion around the source solver, not a second IK algorithm."""

    def __init__(self, ik, pin, np):
        self.ik, self.pin, self.np = ik, pin, np

    def array(self, value, shape, name):
        try:
            raw = self.np.asarray(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{name} must be a finite numeric array of shape {shape}') from exc
        if raw.shape != shape or raw.dtype.kind not in 'iuf' or not self.np.isfinite(raw).all():
            raise ValueError(f'{name} must be a finite numeric array of shape {shape}')
        return self.np.asarray(raw, dtype=float)

    def process(self, request):
        if not isinstance(request, dict):
            raise ValueError('request must be a JSON object')
        command = request.get('command')
        if command == 'stop':
            return None
        if command not in ('fk', 'solve'):
            raise ValueError('command must be fk, solve, or stop')
        q = self.array(request.get('q'), (14,), 'q')
        robot = self.ik.reduced_robot
        if command == 'fk':
            self.pin.forwardKinematics(robot.model, robot.data, q)
            self.pin.updateFramePlacements(robot.model, robot.data)
            poses = [robot.data.oMf[robot.model.getFrameId(name)].homogeneous.copy()
                     for name in ('L_ee', 'R_ee')]
            return {'type': 'fk', 'left': poses[0].tolist(), 'right': poses[1].tolist()}
        left = self.array(request.get('left'), (4, 4), 'left')
        right = self.array(request.get('right'), (4, 4), 'right')
        dq = self.array(request.get('dq'), (14,), 'dq')
        fixed_left = request.get('fixed_left')
        if fixed_left is not None:
            fixed_left = self.array(fixed_left, (7,), 'fixed_left')
        fixed_right = request.get('fixed_right')
        if fixed_right is not None:
            fixed_right = self.array(fixed_right, (7,), 'fixed_right')
        solution, _ = self.ik.solve_ik(left, right, q, dq)
        solution = self.array(solution, (14,), 'IK solution')
        if fixed_left is not None:
            solution[:7] = fixed_left
        if fixed_right is not None:
            solution[7:] = fixed_right
        # Same post-IK gravity calculation as the branch's arm_teleop loop.
        torque = self.pin.rnea(robot.model, robot.data, solution,
                               self.np.zeros(14), self.np.zeros(14))
        torque = self.array(torque, (14,), 'IK torque')
        return {'type': 'target', 'q': solution.tolist(), 'tau': torque.tolist()}


@contextmanager
def runtime(vendor_root, assets_dir, cache_root):
    """Load the original class with its expected cwd, without editing source."""
    vendor_root = Path(vendor_root).expanduser().resolve()
    assets_dir = Path(assets_dir).expanduser().resolve()
    cache_root = Path(cache_root).expanduser().resolve()
    source = vendor_root / 'teleop/robot_control/robot_arm_ik.py'
    if not source.is_file():
        raise FileNotFoundError(f'vendored IK source not found: {source}')
    if not (assets_dir / 'g1_body29_hand14.urdf').is_file():
        raise FileNotFoundError(f'G1 model assets not found: {assets_dir}')
    cache_root.mkdir(parents=True, exist_ok=True)
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='worker-', dir=cache_root) as private:
        private = Path(private)
        (private / 'assets').mkdir()
        (private / 'assets/g1').symlink_to(assets_dir, target_is_directory=True)
        working = private / 'teleop'
        working.mkdir()
        sys.path.insert(0, str(vendor_root))
        try:
            os.chdir(working)
            module = importlib.import_module('teleop.robot_control.robot_arm_ik')
            if Path(module.__file__).resolve() != source:
                raise RuntimeError('a different teleop IK module was already imported')
            ik = module.G1_29_ArmIK(Unit_Test=False, Visualization=False)
            # The source adds L_ee/R_ee after BuildReducedRobot creates data.
            # Match its load_cache path: allocate data for the completed model.
            # Without this, first-run FK can index beyond the native frame data.
            ik.reduced_robot.data = ik.reduced_robot.model.createData()
            yield BranchIK(ik, module.pin, module.np)
        finally:
            os.chdir(old_cwd)
            sys.path.remove(str(vendor_root))


def emit(output, record):
    output.write(json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n')
    output.flush()


def serve(backend, incoming, outgoing):
    emit(outgoing, {'type': 'ready'})
    for line in incoming:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = backend.process(request)
            if response is None:
                break
            emit(outgoing, response)
        except Exception as exc:
            emit(outgoing, {'type': 'error', 'error': f'{type(exc).__name__}: {exc}'})


@contextmanager
def protocol_stdout():
    """Keep Python/native library stdout away from the JSON protocol pipe."""
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    with os.fdopen(protocol_fd, 'w', encoding='utf-8', buffering=1) as output:
        yield output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor-root', type=Path, default=ROOT / 'vendor/xr_h2')
    parser.add_argument('--assets-dir', type=Path, default=DEFAULT_ASSETS)
    parser.add_argument('--cache-root', type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)
    with protocol_stdout() as output:
        try:
            with runtime(args.vendor_root, args.assets_dir, args.cache_root) as backend:
                serve(backend, sys.stdin, output)
        except Exception as exc:
            emit(output, {'type': 'error', 'error': f'{type(exc).__name__}: {exc}'})
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
