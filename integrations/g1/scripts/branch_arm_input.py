"""Side selection around the branch's unchanged IncrementalWrist geometry.

Neutral capture comes from h2/pico_tracker/arm_teleop.py. The project accepts
stationary tracker poses; callers retain the source read_frame freshness check.
Explicit per-side calibration adjusts the source mapping basis and translation
gain; absent calibration retains the original mapping exactly.
This module has no SDK, transport, controller, or hardware initialization.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from arm_reference_frame import validate_reference_frame, reference_mapping

VENDOR = Path(__file__).resolve().parents[1] / 'vendor/xr_h2'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
from pico_tracker.geometry import IncrementalWrist, heading_basis, matmul, matvec, pose_valid, transpose

CALIBRATION_SCHEMA = 'g1-arm-calibration-v1'
MIN_TRANSLATION_SCALE = .1
MAX_TRANSLATION_SCALE = 10.
ROTATION_TOLERANCE = 1e-6


def validate_reference_mapping(record, reference):
    """Old world records remain compatible; full-rotation headset records do not."""
    default = reference_mapping('world') if reference == 'world' else None
    if not isinstance(record, dict) or record.get('arm_reference_mapping', default) != reference_mapping(reference):
        raise ValueError('Arm reference mapping changed; capture a new reference/calibration')


def validate_side_calibration(record, serial):
    """Validate and copy a saved side; shared with the guided calibration writer."""
    if not isinstance(record, dict):
        raise ValueError('Arm calibration must contain a side object')
    if not isinstance(serial, str) or not serial or record.get('serial') != serial:
        raise ValueError('Arm calibration tracker serial does not match the selected wrist')
    rotation = record.get('rotation_from_heading')
    if (not isinstance(rotation, (list, tuple)) or len(rotation) != 3 or
            any(not isinstance(row, (list, tuple)) or len(row) != 3 for row in rotation) or
            any(type(value) not in (int, float) or not math.isfinite(value)
                for row in rotation for value in row)):
        raise ValueError('Arm calibration rotation must be a finite numeric 3x3 matrix')
    rotation = [[float(value) for value in row] for row in rotation]
    product = matmul(rotation, transpose(rotation))
    determinant = sum(rotation[0][j] * (
        rotation[1][(j+1) % 3]*rotation[2][(j+2) % 3] -
        rotation[1][(j+2) % 3]*rotation[2][(j+1) % 3]) for j in range(3))
    if (any(abs(product[i][j] - (1. if i == j else 0.)) > ROTATION_TOLERANCE
            for i in range(3) for j in range(3)) or
            abs(determinant - 1.) > ROTATION_TOLERANCE):
        raise ValueError('Arm calibration rotation must be orthonormal with determinant +1')
    gain = record.get('translation_scale')
    if (type(gain) not in (int, float) or not math.isfinite(gain) or
            not MIN_TRANSLATION_SCALE <= gain <= MAX_TRANSLATION_SCALE):
        raise ValueError('Arm calibration translation scale must be between 0.1 and 10')
    value = {'serial': serial, 'rotation_from_heading': rotation, 'translation_scale': float(gain)}
    if 'translation_correction' in record:
        correction = record['translation_correction']
        if (not isinstance(correction, (list, tuple)) or len(correction) != 3 or
                any(not isinstance(row, (list, tuple)) or len(row) != 3 for row in correction) or
                any(type(v) not in (int, float) or not math.isfinite(v) for row in correction for v in row)):
            raise ValueError('Arm calibration translation correction must be a finite numeric 3x3 matrix')
        correction = [[float(v) for v in row] for row in correction]
        # Position corrections may include shear, unequal scale and reflection.
        # Only numerical singularity is invalid; this is not a quality gate.
        scale = max(abs(v) for row in correction for v in row)
        if scale == 0.:
            raise ValueError('Arm calibration translation correction is singular')
        work = [[v/scale for v in row] for row in correction]
        for col in range(3):
            pivot = max(range(col, 3), key=lambda row: abs(work[row][col]))
            if abs(work[pivot][col]) <= 3*sys.float_info.epsilon:
                raise ValueError('Arm calibration translation correction is numerically singular')
            work[col], work[pivot] = work[pivot], work[col]
            for row in range(col+1, 3):
                ratio = work[row][col]/work[col][col]
                for j in range(col+1, 3):
                    work[row][j] -= ratio*work[col][j]
        value['translation_correction'] = correction
    return value


def selected_sides(sides):
    if sides in ('left', 'right'):
        return (sides,)
    if sides == 'both' or sides in (('left', 'right'), ['left', 'right']):
        return ('left', 'right')
    if sides in (('left',), ['left']):
        return ('left',)
    if sides in (('right',), ['right']):
        return ('right',)
    raise ValueError('Choose left, right, or both arms')


def live(frame, serials, session=None):
    """Accept valid selected poses in the captured session, including stationary ones.

    Callers use the unchanged read_frame(max_age=.5) before this predicate.
    pose_age_ms measures pose changes and is diagnostic, not packet freshness.
    """
    return bool(frame and serials and (session is None or frame['session'] == session)
                and all(sn in frame['trackers'] and pose_valid(frame['trackers'][sn])
                        for sn in serials.values()))


def _pose_copy(pose):
    return [list(row) for row in pose]


@dataclass
class CapturedArms:
    sides: tuple
    serials: dict
    session: object
    mappers: dict
    basis: list
    initial_trackers: dict
    initial_wrists: dict
    fixed_left: list | None
    fixed_right: list | None
    calibration: dict
    reference: str

    def targets(self, frame, fkposes):
        if not live(frame, self.serials, self.session):
            raise ValueError('Tracker input unavailable; hold the last arm target')
        if validate_reference_frame(frame.get('arm_reference_frame', 'world')) != self.reference:
            raise ValueError('Arm reference frame changed; capture a new START reference')
        validate_reference_mapping(frame, self.reference)
        result = {side: (self.mappers[side].target(frame['trackers'][self.serials[side]])
                         if side in self.mappers else _pose_copy(fkposes[side]))
                  for side in ('left', 'right')}
        for side in self.sides:
            calibration = self.calibration['sides'][side]
            gain = calibration['translation_scale']
            correction = calibration.get('translation_correction')
            if correction is not None:
                delta = [result[side][axis][3]-self.initial_wrists[side][axis][3] for axis in range(3)]
                delta = matvec(correction, delta)
                for axis in range(3):
                    result[side][axis][3] = self.initial_wrists[side][axis][3] + delta[axis]
            elif gain != 1.:
                for axis in range(3):
                    origin = self.initial_wrists[side][axis][3]
                    result[side][axis][3] = origin + gain*(result[side][axis][3]-origin)
        if self.fixed_left is not None:
            result['fixed_left'] = list(self.fixed_left)
        if self.fixed_right is not None:
            result['fixed_right'] = list(self.fixed_right)
        return result

    def metadata(self):
        return {'neutral_captured': True, 'sides': list(self.sides),
                'arm_reference_frame': self.reference,
                'arm_reference_mapping': reference_mapping(self.reference),
                'serials': dict(self.serials), 'session': self.session,
                'trackers': {side: list(pose) for side, pose in self.initial_trackers.items()},
                'robot_wrists': {side: _pose_copy(pose) for side, pose in self.initial_wrists.items()},
                'heading_basis': _pose_copy(self.basis),
                'arm_calibration': deepcopy(self.calibration),
                'fixed_left': None if self.fixed_left is None else list(self.fixed_left),
                'fixed_right': None if self.fixed_right is None else list(self.fixed_right)}


def capture(frame, q, fkposes, config, sides='right'):
    sides = selected_sides(sides)
    reference = validate_reference_frame(config.get('arm_reference_frame', 'world'))
    serials = {side: config.get(side + '_sn') for side in sides}
    if any(not isinstance(sn, str) or not sn for sn in serials.values()):
        raise ValueError('Choose a tracker serial for every selected arm')
    if len(set(serials.values())) != len(serials):
        raise ValueError('Use a different tracker serial for each wrist')
    if not live(frame, serials):
        raise ValueError('Fresh valid input is required from each selected wrist tracker')
    if validate_reference_frame(frame.get('arm_reference_frame', 'world')) != reference:
        raise ValueError('Input arm reference frame does not match the selected reference')
    validate_reference_mapping(frame, reference)
    if len(q) != 14 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in q):
        raise ValueError('Measured q must contain 14 finite joint values')
    basis = heading_basis(frame.get('head_pose'))
    saved_sides = {}
    if 'arm_calibration' in config:
        saved = config['arm_calibration']
        if (not isinstance(saved, dict) or saved.get('schema') != CALIBRATION_SCHEMA or
                not isinstance(saved.get('sides'), dict)):
            raise ValueError('Arm calibration must use g1-arm-calibration-v1 with a sides object')
        if saved.get('simulation') is True:
            raise ValueError('Simulated arm calibration cannot be applied to robot targets')
        if validate_reference_frame(saved.get('arm_reference_frame', 'world')) != reference:
            raise ValueError('Saved arm calibration reference frame does not match; recalibrate in this reference')
        validate_reference_mapping(saved, reference)
        saved_sides = saved['sides']
    calibration = {'schema': CALIBRATION_SCHEMA, 'arm_reference_frame': reference,
                   'arm_reference_mapping': reference_mapping(reference), 'sides': {}}
    mappers = {}
    for side, sn in serials.items():
        offset = config.get('offset_tracker_m' if side == 'left' else 'right_offset_tracker_m', [0., 0., 0.])
        applied = side in saved_sides
        if applied:
            try:
                correction = validate_side_calibration(saved_sides[side], sn)
                binding = saved_sides[side].get('capture_binding', {})
                if (not isinstance(binding, dict) or
                        validate_reference_frame(binding.get('arm_reference_frame', 'world')) != reference):
                    raise ValueError('Arm calibration capture reference frame does not match')
                validate_reference_mapping(binding, reference)
                if 'capture_binding' in saved_sides[side]:
                    binding = saved_sides[side]['capture_binding']
                    if not isinstance(binding, dict) or binding.get('serial') != sn:
                        raise ValueError('Arm calibration capture binding does not match the selected wrist')
                    bound_offset = binding.get('offset_tracker_m')
                    if (not isinstance(bound_offset, (list, tuple)) or len(bound_offset) != 3 or
                            any(type(value) not in (int, float) or not math.isfinite(value) for value in bound_offset) or
                            list(bound_offset) != list(offset)):
                        raise ValueError('Arm calibration tracker offset changed; capture the calibration again')
                    correction['capture_binding'] = deepcopy(binding)
            except ValueError as exc:
                raise ValueError(f'{side} arm calibration: {exc}') from None
            side_basis = matmul(correction['rotation_from_heading'], basis)
        else:
            correction = {'serial': sn, 'rotation_from_heading':
                [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]], 'translation_scale': 1.}
            side_basis = _pose_copy(basis)
        calibration['sides'][side] = dict(correction, applied=applied, basis=_pose_copy(side_basis))
        mappers[side] = IncrementalWrist(frame['trackers'][sn], fkposes[side], side_basis, list(offset))
    return CapturedArms(sides=sides, serials=serials, session=frame['session'],
        reference=reference,
        mappers=mappers, basis=basis,
        initial_trackers={side: list(frame['trackers'][sn]) for side, sn in serials.items()},
        initial_wrists={side: _pose_copy(fkposes[side]) for side in sides},
        fixed_left=list(q[:7]) if 'left' not in sides else None,
        fixed_right=list(q[7:]) if 'right' not in sides else None,
        calibration=calibration)


def targets(captured, frame, fkposes):
    return captured.targets(frame, fkposes)
