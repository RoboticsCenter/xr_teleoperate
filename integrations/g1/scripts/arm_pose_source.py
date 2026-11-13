"""Pure selection of current PICO wrist or controller poses; no SDK or I/O."""
import copy
import math
import time

CONTROLLER_IDS = {'left':'pico-controller-left', 'right':'pico-controller-right'}


def validate_source(value='trackers'):
    if value not in ('trackers', 'controllers'):
        raise ValueError('Arm input source must be trackers or controllers')
    return value


def source_ids(source='trackers'):
    return dict(CONTROLLER_IDS) if validate_source(source) == 'controllers' else {}


def effective_config(config, source='trackers'):
    """Use virtual controller IDs/zero offsets and only matching calibration."""
    source = validate_source(source)
    if not isinstance(config, dict):
        raise ValueError('Arm tracker configuration must be an object')
    result = copy.deepcopy(config)
    if source == 'controllers':
        result.update(left_sn=CONTROLLER_IDS['left'], right_sn=CONTROLLER_IDS['right'],
                      offset_tracker_m=[0.,0.,0.], right_offset_tracker_m=[0.,0.,0.])
        saved = result.get('arm_calibration')
        if isinstance(saved, dict) and isinstance(saved.get('sides'), dict):
            matching = {side:record for side,record in saved['sides'].items()
                        if side in CONTROLLER_IDS and isinstance(record,dict)
                        and record.get('serial') == CONTROLLER_IDS[side]}
            if matching:
                saved['sides'] = matching
            else:
                result.pop('arm_calibration',None)
        elif 'arm_calibration' in result:
            # Invalid calibration must remain invalid, never become an implicit
            # uncalibrated configuration; the existing validator reports it.
            pass
    return result


def _fresh(stamp, now, max_age):
    return (type(stamp) in (int,float) and math.isfinite(stamp)
            and 0. <= now-stamp <= max_age)


def select_frame(frame, source='trackers', *, now=None, max_age=.5):
    """Project one source into the original tracker schema after read_frame().

    Legacy tracker frames lack per-source timestamps and retain their existing
    outer-frame freshness contract. New sources cannot borrow a fresh timestamp
    from unrelated Head/Object/Controller packets. Missing sides remain missing.
    Present packet-paired head maps are projected even when empty: consumers must
    not replace a missing pair with the newer global head pose. Legacy frames
    without these maps retain their existing global-head behavior.
    """
    source = validate_source(source)
    if not isinstance(frame,dict):
        return None
    now = time.monotonic() if now is None else now
    if type(now) not in (int,float) or not math.isfinite(now):
        raise ValueError('Frame selection requires finite monotonic time')
    if type(max_age) not in (int,float) or not math.isfinite(max_age) or max_age < 0:
        raise ValueError('Frame age must be finite and nonnegative')
    if source == 'trackers':
        if 'tracker_sample_mono' in frame and not _fresh(frame['tracker_sample_mono'],now,max_age):
            return None
        stale_head = 'head_sample_mono' in frame and not _fresh(frame['head_sample_mono'],now,max_age)
        if not stale_head and 'tracker_head_poses' not in frame:
            return frame
        result = copy.deepcopy(frame)
        if stale_head:
            result['head_pose'] = None
        if 'tracker_head_poses' in frame:
            paired = frame['tracker_head_poses']
            poses = result.get('trackers')
            result['head_pose_by_tracker'] = {
                sn:copy.deepcopy(paired[sn]) for sn in poses if sn in paired
            } if isinstance(paired,dict) and isinstance(poses,dict) else {}
        return result
    poses = frame.get('controllers')
    if not isinstance(poses,dict):
        return None
    by_side = frame.get('controller_sample_mono_by_side')
    projected = {}
    for side,sn in CONTROLLER_IDS.items():
        stamp = by_side.get(side) if isinstance(by_side,dict) else frame.get('controller_sample_mono')
        if side in poses and _fresh(stamp,now,max_age):
            projected[sn] = copy.deepcopy(poses[side])
    result = copy.deepcopy(frame)
    result['trackers'] = projected
    if 'controller_head_poses' in frame:
        paired = frame['controller_head_poses']
        result['head_pose_by_tracker'] = {
            sn:copy.deepcopy(paired[side]) for side,sn in CONTROLLER_IDS.items()
            if sn in projected and side in paired
        } if isinstance(paired,dict) else {}
    if 'head_sample_mono' in frame and not _fresh(frame['head_sample_mono'],now,max_age):
        result['head_pose'] = None
    return result
