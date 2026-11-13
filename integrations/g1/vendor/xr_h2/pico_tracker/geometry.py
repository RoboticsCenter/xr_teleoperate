"""Incremental wrist targets in Unitree's X-forward, Y-left, Z-up frame.

Reference: Wuji's published pico_input/incremental_controller.py. Tracker
poses come from the official XRoboToolkit SDK as x,y,z,qx,qy,qz,qw.
No hardware, SDK, DDS, or motor initialization occurs in this module.
"""
import math

PICO_TO_ROBOT = [[0., 0., -1.], [-1., 0., 0.], [0., 1., 0.]]


def pose_valid(pose):
    return (isinstance(pose, (list, tuple)) and len(pose) == 7 and
            all(isinstance(v, (float, int)) and math.isfinite(v) for v in pose) and
            .8 < sum(v*v for v in pose[3:]) < 1.2)


def transpose(a):
    return [list(row) for row in zip(*a)]


def matmul(a, b):
    return [[sum(x*y for x, y in zip(row, col)) for col in zip(*b)] for row in a]


def matvec(a, v):
    return [sum(x*y for x, y in zip(row, v)) for row in a]


def rotation(pose):
    if not pose_valid(pose):
        raise ValueError('Invalid tracker pose or quaternion')
    norm = math.sqrt(sum(v*v for v in pose[3:]))
    x, y, z, w = [v/norm for v in pose[3:]]
    return [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
            [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
            [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]


def heading_basis(head_pose):
    """Freeze horizontal headset forward at START; no continuous head following."""
    if not pose_valid(head_pose):
        raise ValueError('Enable Head in XRoboToolkit before START')
    forward = matvec(rotation(head_pose), [0., 0., -1.])
    length = math.hypot(forward[0], forward[2])
    if length < .1:
        raise ValueError('Look horizontally forward when pressing START')
    x, z = forward[0]/length, forward[2]/length
    return [[x, 0., z], [z, 0., -x], [0., 1., 0.]]


class IncrementalWrist:
    def __init__(self, tracker_initial, robot_initial, basis, offset=(0., 0., 0.)):
        self.basis = basis
        self.offset = offset
        self.tracker_rotation = rotation(tracker_initial)
        self.origin = self.wrist_position(tracker_initial)
        self.robot_initial = [list(row) for row in robot_initial]

    def wrist_position(self, pose):
        lever = matvec(rotation(pose), self.offset)
        return [pose[i]+lever[i] for i in range(3)]

    def target(self, pose):
        position = self.wrist_position(pose)
        delta = matvec(self.basis, [position[i]-self.origin[i] for i in range(3)])
        delta_rotation = matmul(rotation(pose), transpose(self.tracker_rotation))
        mapped = matmul(matmul(self.basis, delta_rotation), transpose(self.basis))
        orientation = matmul(mapped, [row[:3] for row in self.robot_initial[:3]])
        return [orientation[i] + [self.robot_initial[i][3]+delta[i]] for i in range(3)] + [[0., 0., 0., 1.]]
