"""Read-only tracker motion measurements for the existing arm geometry.

No robot, network, SDK or output-service initialization occurs in this module.
Only explicit save() writes a calibration; simulation never writes files.
"""
import copy
from network_config import laptop_host
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid

import numpy as np
from branch_arm_input import (CALIBRATION_SCHEMA, MAX_TRANSLATION_SCALE,
    MIN_TRANSLATION_SCALE, selected_sides, validate_side_calibration, validate_reference_mapping)
from pico_tracker.geometry import heading_basis, pose_valid, rotation
from arm_reference_frame import validate_reference_frame, reference_frame, reference_mapping

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('origin', 'forward', 'left', 'up')
AXES = STAGES[1:]
SIDE_LABELS = {'left': '左腕', 'right': '右腕'}
AXIS_LABELS = {'origin': '原点', 'forward': '向前', 'left': '向左', 'up': '向上'}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _record_offset(record):
    if 'capture_binding' not in record:
        return None
    binding = record['capture_binding']
    if not isinstance(binding, dict) or binding.get('serial') != record.get('serial'):
        raise ValueError('saved arm calibration capture binding is invalid')
    offset = binding.get('offset_tracker_m')
    if not isinstance(offset, list) or len(offset) != 3 or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in offset):
        raise ValueError('saved arm calibration offset must contain three finite numbers')
    return offset


def _binding(config, arms_mode):
    sides = selected_sides(arms_mode)
    serials, offsets = {}, {}
    for side in sides:
        sn = config.get(side + '_sn')
        if not isinstance(sn, str) or not sn.strip():
            raise ValueError('请先绑定'+SIDE_LABELS[side]+' Tracker 序列号。')
        serials[side] = sn
        key = 'offset_tracker_m' if side == 'left' else 'right_offset_tracker_m'
        value = config.get(key, [0., 0., 0.])
        if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in value):
            raise ValueError('Tracker 安装偏移必须是三个有限数值。')
        offsets[side] = [float(v) for v in value]
    if len(set(serials.values())) != len(serials):
        raise ValueError('左右手腕必须使用不同 Tracker。')
    reference = validate_reference_frame(config.get('arm_reference_frame','world'))
    return {'selected_sides': list(sides), 'serials': serials, 'offsets_m': offsets,
            'arm_reference_frame':reference, 'arm_reference_mapping':reference_mapping(reference)}


def _sample(frame, binding, *, origin=False):
    # The caller supplies TrackerManager's already freshness-checked snapshot.
    frame=reference_frame(frame,binding['arm_reference_frame'])
    if not isinstance(frame, dict) or not frame.get('session') or not isinstance(frame.get('trackers'), dict):
        raise ValueError('没有新鲜 Tracker 数据；请检查 PICO Send。')
    poses, points = {}, {}
    for side, sn in binding['serials'].items():
        pose = frame['trackers'].get(sn)
        if not pose_valid(pose) or any(type(v) not in (int, float) for v in pose):
            raise ValueError(SIDE_LABELS[side]+' Tracker 位姿无效，请重新采集。')
        poses[side] = list(pose)
        points[side] = (np.asarray(pose[:3], dtype=float) +
                       np.asarray(rotation(pose)) @ np.asarray(binding['offsets_m'][side])).tolist()
        if not all(math.isfinite(v) for v in points[side]):
            raise ValueError(SIDE_LABELS[side]+'位姿计算超出有限数值范围。')
    value = {'session': copy.deepcopy(frame['session']), 'seq': frame.get('seq'),
             'captured_at': timestamp(), 'poses': poses, 'points_m': points}
    if origin:
        value['heading_basis'] = heading_basis(frame.get('head_pose'))
        value['head_pose'] = list(frame['head_pose'])
    return value


def input_readiness(frame, config, arms_mode):
    """Inspect capture's selected poses and heading without recording samples."""
    frame=reference_frame(frame,validate_reference_frame(config.get('arm_reference_frame','world')))
    selected = list(selected_sides(arms_mode))
    serials = {side: config.get(side+'_sn', '') for side in selected}
    stream = bool(isinstance(frame, dict) and frame.get('session') and isinstance(frame.get('trackers'), dict))
    head_ready = bool(stream and pose_valid(frame.get('head_pose')))
    heading_ready = False
    if head_ready:
        try:
            heading_basis(frame['head_pose'])
            heading_ready = True
        except (ValueError, TypeError):
            pass
    binding_error = None
    try:
        _binding(config, arms_mode)
    except (ValueError, TypeError) as exc:
        binding_error = str(exc)
    wrists = {}
    for side in ('left', 'right'):
        pose = frame['trackers'].get(config.get(side+'_sn')) if stream else None
        wrists[side+'_ready'] = bool(pose_valid(pose) and all(type(v) in (int, float) for v in pose))
    missing = ([] if stream else ['stream']) + ([] if head_ready else ['head'])
    if head_ready and not heading_ready:
        missing.append('heading')
    missing += [side for side in selected if not wrists[side+'_ready']]
    if binding_error:
        missing.append('binding')
    detail = (binding_error if binding_error else
        f'等待新鲜姿态；请在头显选择{laptop_host()}并开启 Head + Object + Send。' if not stream else
        'Head 姿态无效；请在 XRoboToolkit 开启 Head。' if not head_ready else
        '请将头部朝向水平正前方，再检查输入。' if not heading_ready else
        '等待'+ '、'.join('左腕' if side=='left' else '右腕' for side in selected if not wrists[side+'_ready'])+'有效 Tracker 数据。' if missing else
        'Head 与所选腕部输入有效，可以采集手臂校准；无需 LAUNCH。')
    return dict(ready=not missing, input_live=stream, head_ready=head_ready,
        heading_ready=heading_ready, selected_sides=selected, selected_serials=serials,
        session=frame.get('session') if stream else None, missing=missing, detail=detail, **wrists)


def fit_side(side, binding, samples):
    origin = np.asarray(samples['origin']['points_m'][side], dtype=float)
    measured = np.column_stack([np.asarray(samples[axis]['points_m'][side])-origin for axis in AXES])
    if not np.all(np.isfinite(measured)):
        raise ValueError(SIDE_LABELS[side]+'位移计算超出有限数值范围。')
    lengths = np.asarray([math.hypot(*measured[:, i]) for i in range(3)])
    if not np.all(np.isfinite(lengths)) or np.any(lengths == 0.):
        raise ValueError(SIDE_LABELS[side]+'存在零位移或无法计算的位移，请重新采集对应轴。')
    measured_scale = float(np.max(np.abs(measured)))
    normalized = measured/measured_scale
    singular = np.linalg.svd(normalized, compute_uv=False)
    if singular[-1] <= singular[0]*3*np.finfo(float).eps:
        raise ValueError(SIDE_LABELS[side]+'三轴数据线性相关，无法求解独立方向修正；请重新采集。')
    unit = measured / lengths
    pair_errors = {}
    for i in range(3):
        for j in range(i+1, 3):
            angle = math.degrees(math.acos(float(np.clip(unit[:, i] @ unit[:, j], -1., 1.))))
            pair_errors[AXES[i]+'_'+AXES[j]] = abs(angle-90.)
    desired_lengths = np.asarray([samples[axis]['distance_cm']/100. for axis in AXES])
    desired = np.diag(desired_lengths)
    desired_scale = float(np.max(desired_lengths))
    u, _, vt = np.linalg.svd((desired/desired_scale) @ normalized.T)
    handedness = np.eye(3)
    handedness[-1, -1] = 1. if np.linalg.det(u @ vt) >= 0 else -1.
    transform = u @ handedness @ vt
    rotated = transform @ normalized
    # The operator's three actual movements are the ground truth. The full
    # position-only correction maps all three samples exactly, including unequal
    # scale, shear or mirrored tracking. Orientation keeps a proper rigid rotation.
    translation = np.linalg.solve(rotated.T, (desired/measured_scale).T).T
    similarity_gain = float(np.sum((desired/desired_scale)*rotated) /
                            np.sum(normalized*normalized) * (desired_scale/measured_scale))
    gain = similarity_gain if MIN_TRANSLATION_SCALE <= similarity_gain <= MAX_TRANSLATION_SCALE else 1.
    axis_errors = np.degrees(np.arccos(np.clip(np.diag(transform @ unit), -1., 1.)))
    corrected = translation @ rotated * measured_scale
    residual = np.linalg.norm(corrected-desired, axis=0)/desired_lengths
    if not all(np.all(np.isfinite(value)) for value in (translation, corrected, lengths*100., residual)):
        raise ValueError(SIDE_LABELS[side]+'修正计算超出有限数值范围，请重新采集。')
    correction = transform @ np.asarray(samples['origin']['heading_basis']).T
    value = validate_side_calibration({'serial': binding['serials'][side],
        'rotation_from_heading': correction.tolist(), 'translation_scale': gain,
        'translation_correction': translation.tolist()}, binding['serials'][side])
    value['capture_binding'] = {'serial': binding['serials'][side],
        'arm_reference_frame':binding['arm_reference_frame'],
        'arm_reference_mapping':binding['arm_reference_mapping'],
        'offset_tracker_m': list(binding['offsets_m'][side]),
        'session': copy.deepcopy(samples['origin']['session']),
        'heading_basis': copy.deepcopy(samples['origin']['heading_basis']),
        'origin_captured_at': samples['origin']['captured_at']}
    value['diagnostics'] = {
        'reference': 'operator',
        'axis_error_deg': dict(zip(AXES, axis_errors.tolist())),
        'relative_residual': dict(zip(AXES, residual.tolist())),
        'measured_distance_cm': dict(zip(AXES, (lengths*100.).tolist())),
        'desired_distance_cm': dict(zip(AXES, (desired_lengths*100.).tolist())),
        'pair_angle_error_deg': pair_errors,
        'max_axis_error_deg': float(np.max(axis_errors)),
        'max_relative_residual': float(np.max(residual)),
        'rotation_pico_to_robot_at_capture': transform.tolist(),
    }
    return value


class ArmCalibration:
    def __init__(self, root=ROOT, simulation=False, *, filename='arm-calibration.json', reference='world'):
        self.root, self.simulation = Path(root), bool(simulation)
        self.reference=validate_reference_frame(reference)
        if filename not in ('arm-calibration.json', 'arm-calibration-controllers.json',
                            'arm-calibration-headset.json', 'arm-calibration-controllers-headset.json'):
            raise ValueError('Unknown arm calibration store')
        if ('-headset.json' in filename) != (self.reference=='headset'):
            raise ValueError('Arm calibration store does not match reference frame')
        self.path = self.root/'config'/filename
        self._lock = threading.RLock()
        self._saved = self._candidate = self._binding = None
        self._samples, self._error, self._load_error = {}, None, None
        self._dirty = False
        if not self.simulation and self.path.exists():
            try:
                value = json.loads(self.path.read_text())
                if not isinstance(value, dict) or value.get('schema') != CALIBRATION_SCHEMA or not isinstance(value.get('sides'), dict):
                    raise ValueError('saved arm calibration schema is invalid')
                if value.get('simulation') is True:
                    raise ValueError('simulation arm calibration cannot be loaded for real use')
                if validate_reference_frame(value.get('arm_reference_frame','world'))!=self.reference:
                    raise ValueError('saved arm calibration reference frame does not match store')
                validate_reference_mapping(value, self.reference)
                for side, record in value['sides'].items():
                    if side not in ('left', 'right'):
                        raise ValueError('saved arm calibration has an unknown side')
                    validate_side_calibration(record, record.get('serial'))
                    _record_offset(record)
                    validate_reference_mapping(record.get('capture_binding', {}), self.reference)
                self._saved = value
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self._load_error = '无法读取已有手臂校准：'+str(exc)

    @property
    def saved(self):
        with self._lock:
            return copy.deepcopy(self._saved)

    def _matching(self, config, arms_mode):
        current = _binding(config, arms_mode)
        if not self._binding or current != self._binding:
            raise ValueError('Tracker、手臂选择、参考坐标或安装偏移已改变；请重新采集原点。')
        return current

    def capture(self, stage, frame, config, arms_mode='both', distance_cm=20):
        with self._lock:
            try:
                if stage not in STAGES:
                    raise ValueError('请选择原点、向前、向左或向上的采集步骤。')
                binding = _binding(config, arms_mode) if stage == 'origin' else self._matching(config, arms_mode)
                if binding['arm_reference_frame']!=self.reference:
                    raise ValueError('校准存储与所选参考坐标不一致。')
                sample = _sample(frame, binding, origin=stage == 'origin')
                if type(distance_cm) not in (int, float) or not math.isfinite(distance_cm) or not 3 <= distance_cm <= 100:
                    raise ValueError('请输入3–100cm之间的实际移动距离。')
                sample['distance_cm'] = float(distance_cm)
                if stage != 'origin':
                    if sample['session'] != self._samples['origin']['session']:
                        raise ValueError('Tracker 数据会话已改变；请重新采集原点。')
                    for side in binding['selected_sides']:
                        delta = np.asarray(sample['points_m'][side])-np.asarray(self._samples['origin']['points_m'][side])
                        if not np.all(np.isfinite(delta)) or not np.any(delta != 0.):
                            raise ValueError(SIDE_LABELS[side]+AXIS_LABELS[stage]+'没有可计算的非零位移，请重新采集。')
                if stage == 'origin':
                    self._samples = {}
                    self._binding = binding
                self._samples[stage] = sample
                self._candidate, self._error, self._dirty = None, None, True
                if all(name in self._samples for name in STAGES):
                    self._candidate = {'schema': CALIBRATION_SCHEMA, 'calibration_id': uuid.uuid4().hex,
                        'created_at': timestamp(), 'simulation': self.simulation, 'reference': 'operator',
                        'arm_reference_frame':self.reference,
                        'arm_reference_mapping':reference_mapping(self.reference),
                        'sides': {side: fit_side(side, binding, self._samples) for side in binding['selected_sides']},
                        'capture': {'binding': copy.deepcopy(binding), 'samples': copy.deepcopy(self._samples)}}
            except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as exc:
                self._error = str(exc)
                raise ValueError(self._error) from None
            return self.status(config, arms_mode)

    def save(self, config, arms_mode='both'):
        with self._lock:
            try:
                self._matching(config, arms_mode)
                if not self._candidate or self._error or not self._dirty:
                    raise ValueError('请完成有效的原点和三轴测量后再保存。')
                value = copy.deepcopy(self._candidate)
                # Keep a previously calibrated unselected arm when its tracker
                # binding still matches; a one-arm save never silently erases it.
                preserved = []
                for side, record in (self._saved or {}).get('sides', {}).items():
                    if side not in value['sides'] and config.get(side+'_sn') == record.get('serial'):
                        old_offset = _record_offset(record)
                        current_offset = _binding(config, side)['offsets_m'][side]
                        if old_offset is not None and old_offset != current_offset:
                            continue
                        value['sides'][side] = copy.deepcopy(record)
                        preserved.append(side)
                value.update(saved_at=timestamp(), preserved_sides=preserved)
                if not self.simulation:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    backup = None
                    if self.path.exists():
                        backup = self.path.with_name(self.path.stem+'.backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.json')
                        shutil.copy2(self.path, backup)
                    if backup:
                        value['backup_path'] = str(backup)
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                prefix='.arm-calibration-', suffix='.tmp', delete=False) as stream:
                            temporary = Path(stream.name)
                            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
                            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
                        os.replace(temporary, self.path)
                    finally:
                        if temporary:
                            temporary.unlink(missing_ok=True)
                self._saved, self._dirty, self._load_error = value, False, None
            except (ValueError, OSError, TypeError) as exc:
                self._error = str(exc)
                raise
            return self.status(config, arms_mode)

    def reset(self):
        with self._lock:
            self._samples, self._candidate, self._binding = {}, None, None
            self._error, self._dirty = None, False
            return self.status()

    def status(self, config=None, arms_mode='both'):
        with self._lock:
            binding_error, saved_error = None, self._load_error
            saved_complete = bool(self._saved)
            selected = list(selected_sides(arms_mode))
            if config is not None:
                try:
                    current = _binding(config, arms_mode)
                    if current['arm_reference_frame']!=self.reference:
                        raise ValueError('校准存储与所选参考坐标不一致。')
                    selected = current['selected_sides']
                    if self._binding is not None and current != self._binding:
                        binding_error = 'Tracker、手臂选择、参考坐标或安装偏移已改变；请重新采集原点。'
                    if self._saved:
                        for side in selected:
                            if side not in self._saved['sides']:
                                saved_complete = False
                                continue
                            record = self._saved['sides'][side]
                            validate_side_calibration(record, current['serials'][side])
                            if validate_reference_frame(record.get('capture_binding',{}).get('arm_reference_frame','world'))!=self.reference:
                                raise ValueError('已有校准使用不同参考坐标，请重新采集。')
                            validate_reference_mapping(record.get('capture_binding', {}), self.reference)
                            captured_offset = _record_offset(record)
                            if captured_offset is not None and captured_offset != current['offsets_m'][side]:
                                raise ValueError(SIDE_LABELS[side]+' Tracker安装偏移已改变；请重新测量该侧校准。')
                except ValueError as exc:
                    saved_error = str(exc)
                    if self._binding is not None:
                        try: self._matching(config, arms_mode)
                        except ValueError as mismatch: binding_error = str(mismatch)
            elif self._binding is not None:
                selected = list(self._binding['selected_sides'])
            captured = [stage for stage in STAGES if stage in self._samples]
            missing = next((stage for stage in STAGES if stage not in self._samples), None)
            instructions = {'origin': '面向前方，双手保持舒适起始姿态，采集原点。',
                'forward': '从原点向前移动填写的距离，再采集向前。',
                'left': '先回同一原点，再向左移动填写的距离并采集。',
                'up': '先回同一原点，再向上移动填写的距离并采集。'}
            return copy.deepcopy({'origin_captured': 'origin' in self._samples,
                'origin_session': self._samples.get('origin', {}).get('session'),
                'captured_stages': captured, 'selected_sides': selected,
                'distance_cm': self._samples.get('origin', {}).get('distance_cm', 20.),
                'instruction': instructions.get(missing, '已按你输入的实际方向和距离计算修正，确认后保存；下次LAUNCH/START生效。'),
                'error': self._error or binding_error, 'binding_error': binding_error,
                'candidate': self._candidate, 'saved': self._saved,
                'saved_compatible': bool(self._saved and not saved_error), 'saved_error': saved_error,
                'saved_complete': bool(saved_complete and not saved_error),
                'can_save': bool(self._candidate and self._dirty and not self._error and not binding_error),
                'candidate_saved': bool(self._candidate and self._saved and not self._dirty and
                    self._candidate['calibration_id'] == self._saved.get('calibration_id')),
                'reference': 'operator', 'reference_frame':self.reference,
                'reference_mapping':reference_mapping(self.reference),
                'simulation': self.simulation, 'robot_output': False})
