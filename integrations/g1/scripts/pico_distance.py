"""In-memory wrist displacement meter; no files, SDK, network or robot output.

The caller supplies an already freshness-checked shared Tracker frame. Distances
are straight-line displacements in the Tracker's original metric scale, never
path length or a calibration gain estimated from those same Tracker readings.
"""
import copy
import math
import threading

from branch_arm_input import selected_sides
from arm_reference_frame import validate_reference_frame, reference_frame, reference_mapping
from pico_tracker.geometry import heading_basis, matvec, pose_valid, rotation

STAGES = ('origin', 'forward', 'left', 'up')


def _binding(settings):
    if not isinstance(settings, dict):
        raise ValueError('手臂设置不可用。')
    sides = selected_sides(settings.get('arms_mode', 'both'))
    config = settings.get('tracker_config')
    if not isinstance(config, dict):
        raise ValueError('请先选择手腕Tracker。')
    serials, offsets = {}, {}
    for side in sides:
        sn = config.get(side+'_sn')
        if not isinstance(sn, str) or not sn.strip():
            raise ValueError('请先选择'+side+'手腕Tracker。')
        serials[side] = sn
        key = 'offset_tracker_m' if side == 'left' else 'right_offset_tracker_m'
        offset = config.get(key, [0., 0., 0.])
        if not isinstance(offset, (list,tuple)) or len(offset) != 3 or any(
                type(value) not in (int,float) or not math.isfinite(value) for value in offset):
            raise ValueError('Tracker安装偏移必须包含三个有限数值。')
        offsets[side] = [float(value) for value in offset]
    if len(set(serials.values())) != len(serials):
        raise ValueError('所选左右手必须使用不同Tracker。')
    reference=validate_reference_frame(settings.get('arm_reference_frame','world'))
    return {'selected_sides':list(sides), 'serials':serials, 'offsets':offsets,
            'arm_reference_frame':reference, 'arm_reference_mapping':reference_mapping(reference)}


def _point(pose, offset):
    if not pose_valid(pose) or any(type(value) not in (int,float) for value in pose):
        return None
    lever = matvec(rotation(pose), offset)
    point = [pose[index]+lever[index] for index in range(3)]
    return point if all(math.isfinite(value) for value in point) else None


def _inspect(frame, binding):
    frame=reference_frame(frame,binding['arm_reference_frame'])
    valid = bool(isinstance(frame,dict) and isinstance(frame.get('session'),str) and frame['session']
                 and isinstance(frame.get('trackers'),dict))
    points = {side: _point(frame['trackers'].get(sn),binding['offsets'][side]) if valid else None
              for side,sn in binding['serials'].items()}
    basis = None
    if valid:
        try:
            basis = heading_basis(frame.get('head_pose'))
        except (ValueError,TypeError):
            pass
    return valid, points, basis


class DistanceMeter:
    def __init__(self):
        self._lock = threading.RLock()
        self._origin, self._invalid_reason = None, None

    def zero(self, frame, settings, calibration):
        """Explicitly replace only this page's origin; failed requests keep it."""
        with self._lock:
            binding = _binding(settings)
            valid, points, basis = _inspect(frame,binding)
            if not valid:
                raise ValueError('没有新鲜PICO数据，暂不能设置测量起点。')
            missing = [side for side,point in points.items() if point is None]
            if missing:
                raise ValueError('所选手腕Tracker数据无效：'+', '.join(missing))
            if basis is None:
                raise ValueError('设置起点需要有效Head方向；请开启Head并面向前方。')
            self._origin = {'binding':copy.deepcopy(binding),'session':frame['session'],
                            'basis':copy.deepcopy(basis),'points':copy.deepcopy(points)}
            self._invalid_reason = None
            return self.sample(frame,settings,calibration)

    def sample(self, frame, settings, calibration):
        with self._lock:
            calibration = calibration if isinstance(calibration,dict) else {}
            target = calibration.get('distance_cm',20.)
            if type(target) not in (int,float) or not math.isfinite(target) or target <= 0:
                target = None
            stages = calibration.get('captured_stages',[])
            stages = stages if isinstance(stages,(list,tuple)) else []
            stage = next((name for name in STAGES if name not in stages),'complete')
            result = {'zero_ready':False,'has_origin':self._origin is not None,
                'needs_zero':True,'origin_label':'本页测量起点','ready':False,
                'detail':'请先设置本页测量起点。','selected_sides':[], 'sides':{},
                'target_cm':float(target) if target is not None else None,
                'current_stage':stage,'session':None,
                'distance_definition':'距本页起点的直线位移，不是累计路径长度；标定距离请以尺量实距为准。'}
            if isinstance(settings,dict) and settings.get('arm_reference_frame')=='headset':
                result['distance_definition']='以头显位置为原点、水平朝向为前方的相对位移；上方始终竖直向上。标定距离仍以尺量实距为准。'
            try:
                binding = _binding(settings)
            except ValueError as exc:
                if self._origin is not None:
                    self._invalid_reason = '手腕设置已改变；请重新设置本页测量起点。'
                result['detail'] = str(exc)
                return result
            valid, points, basis = _inspect(frame,binding)
            session = frame['session'] if valid else None
            if self._origin is not None:
                if binding != self._origin['binding']:
                    self._invalid_reason = 'Tracker、手臂选择或安装偏移已改变；请重新设置本页测量起点。'
                elif valid and session != self._origin['session']:
                    self._invalid_reason = 'PICO数据会话已改变；请重新设置本页测量起点。'
            needs_zero = self._origin is None or self._invalid_reason is not None
            all_live = bool(points) and all(point is not None for point in points.values())
            result.update(zero_ready=bool(valid and all_live and basis is not None),
                needs_zero=needs_zero, selected_sides=list(binding['selected_sides']),session=session,
                ready=bool(not needs_zero and all_live))
            for side,sn in binding['serials'].items():
                point = points[side]
                values = {'sn':sn,'live':point is not None,'displacement_cm':None,
                          'forward_cm':None,'left_cm':None,'up_cm':None}
                if point is not None and not needs_zero:
                    delta = [point[index]-self._origin['points'][side][index] for index in range(3)]
                    projected = matvec(self._origin['basis'],delta)
                    metrics = [math.hypot(*delta)*100.]+[value*100. for value in projected]
                    if all(math.isfinite(value) for value in metrics):
                        values.update(zip(('displacement_cm','forward_cm','left_cm','up_cm'),metrics))
                    else:
                        values['live'] = False
                        all_live = False
                result['sides'][side] = values
            result['ready'] = bool(not needs_zero and all_live)
            if self._invalid_reason:
                result['detail'] = self._invalid_reason
            elif not valid:
                result['detail'] = '等待新鲜PICO数据；当前不显示旧位移。'
            elif needs_zero:
                result['detail'] = ('起点可用，点击设置本页测量起点。' if result['zero_ready'] else
                                    '设置起点需要所选手腕和有效Head方向。')
            elif not all_live:
                result['detail'] = '部分所选手腕暂不可用；其余有效手腕仍可测量。'
            else:
                result['detail'] = '正在显示距本页测量起点的直线位移。'
            return result
