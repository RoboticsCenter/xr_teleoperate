"""Headset position and horizontal facing reference, with gravity kept upright.

Pure pose math only; no SDK, transport, or hardware access. Head pitch and roll
do not tilt the arm reference axes.
"""
from copy import deepcopy
import math

IDENTITY_POSE = [0., 0., 0., 0., 0., 0., 1.]
HEADSET_MAPPING = 'headset_heading_v1'


def validate_reference_frame(value='world'):
    if value not in ('world', 'headset'):
        raise ValueError('Arm reference frame must be world or headset')
    return value


def reference_mapping(mode='world'):
    return HEADSET_MAPPING if validate_reference_frame(mode) == 'headset' else 'world_v1'


def _valid(pose):
    return (isinstance(pose, (list, tuple)) and len(pose) == 7 and
            all(type(v) in (int, float) and math.isfinite(v) for v in pose) and
            .8 < sum(v*v for v in pose[3:]) < 1.2)


def _quaternion(pose):
    scale = math.sqrt(sum(v*v for v in pose[3:]))
    return [v / scale for v in pose[3:]]


def _heading_quaternion(head):
    """Use the original heading_basis horizontal -Z projection and threshold."""
    x, y, z, w = _quaternion(head)
    forward_x, forward_z = -2*(x*z+y*w), 2*(x*x+y*y)-1
    length = math.hypot(forward_x, forward_z)
    if length < .1:
        return None
    yaw = math.atan2(-forward_x/length, -forward_z/length)
    return [0., math.sin(yaw/2), 0., math.cos(yaw/2)]


def _relative(pose, head):
    # Invert horizontal head facing only, using xyzw quaternion convention.
    heading = _heading_quaternion(head)
    if heading is None:
        return None
    hx, hy, hz, hw = heading
    x, y, z, w = -hx, -hy, -hz, hw
    delta = [pose[i] - head[i] for i in range(3)]
    matrix = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
              [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
              [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    point = [sum(row[i]*delta[i] for i in range(3)) for row in matrix]
    px, py, pz, pw = _quaternion(pose)
    orientation = [w*px+x*pw+y*pz-z*py,
                   w*py-x*pz+y*pw+z*px,
                   w*pz+x*py-y*px+z*pw,
                   w*pw-x*px-y*py-z*pz]
    result = point + orientation
    return result if _valid(result) else None


def reference_frame(frame, mode='world'):
    """Use each wrist's paired Head position and horizontal facing as its base.

    New readers provide head_pose_by_tracker from the same packet as each pose.
    Legacy fixtures without that field use the current head. Input freshness is
    still enforced by select_frame; stationary samples are valid. Missing paired
    heads exclude only their corresponding poses. Missing current Head or an
    undefined horizontal facing makes the headset frame unavailable. The .1
    projection threshold is unchanged from the vendor heading_basis function.
    World input is returned unchanged.
    """
    mode = validate_reference_frame(mode)
    if mode == 'world' or not isinstance(frame, dict):
        return frame if mode == 'world' else None
    if frame.get('arm_reference_frame') == 'headset':
        raise ValueError('Headset reference frame has already been applied')
    head = frame.get('head_pose')
    if not _valid(head) or _heading_quaternion(head) is None:
        return None
    trackers = frame.get('trackers')
    if not isinstance(trackers, dict):
        return None
    has_pairs = 'head_pose_by_tracker' in frame
    pairs = frame.get('head_pose_by_tracker')
    mapped = {}
    for serial, pose in trackers.items():
        paired = pairs.get(serial) if isinstance(pairs, dict) else None
        if not has_pairs:
            paired = head
        if not _valid(pose) or not _valid(paired):
            continue
        relative = _relative(pose, paired)
        if relative is not None:
            mapped[serial] = relative
    result = deepcopy(frame)
    result['trackers'] = mapped
    result['head_pose'] = list(IDENTITY_POSE)
    result['head_pose_by_tracker'] = {serial: list(IDENTITY_POSE) for serial in mapped}
    result['arm_reference_frame'] = 'headset'
    result['arm_reference_mapping'] = reference_mapping(mode)
    return result
