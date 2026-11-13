#!/usr/bin/env python3
"""G1 29DoF 双臂纯离线预览；无 SDK、网络订阅或机器人输出。"""

import argparse
import ast
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sys


COMMIT = "817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b"
DEFAULT_SOURCE = Path.home() / ".cache/unitree-g1-arms/source"
FILES = {
    "teleop/robot_control/robot_arm_ik.py": "42ea931d75ef70f67e55b7a61cf50cd7164dd1cef9ce83b8309ba7d144315250",
    "teleop/utils/weighted_moving_filter.py": "48d914f37de405351d327b9712a36c013f8375d550258fecbdd37997e526308b",
    "assets/g1/g1_body29_hand14.urdf": "8bbf006633fc50b616f665c7a970780cc296577a0adfd7d28b049e751c238735",
}
SIDES = ("left", "right")
JOINTS = [f"{side}_{joint}_joint" for side in SIDES for joint in (
    "shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow",
    "wrist_roll", "wrist_pitch", "wrist_yaw")]
# Default reference is a model configuration, never a measured robot posture.
REFERENCE_Q = [0, .15, 0, .4, 0, 0, 0, 0, -.15, 0, .4, 0, 0, 0]
HEADLESS_EDITS = (
    ("if os.path.exists(self.cache_path) and (not self.Visualization):",
     "if False:  # Offline: never load a pickle cache."),
    ("self.robot = pin.RobotWrapper.BuildFromURDF(self.urdf_path, self.model_dir)",
     "self.robot = pin.RobotWrapper(pin.buildModelFromUrdf(URDF_PATH), pin.GeometryModel(), pin.GeometryModel())"),
    ("if not os.path.exists(self.cache_path):",
     "if False:  # Offline: no persistent pickle writes."),
)


def verify_source(source):
    """Load only the three audited upstream files; never import upstream packages."""
    result = {}
    for name, expected in FILES.items():
        data = (source / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise ValueError(f"官方源码校验失败：{name}，需要固定 commit {COMMIT}")
        result[name] = data.decode()
    return result


def class_source(source, name):
    matches = [node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"需要且只允许一个官方类 {name}")
    return ast.get_source_segment(source, matches[0]) + "\n"


def load_ik(source):
    """Only G1_29_ArmIK and its filter are executed, with explicit dependencies.

    No upstream module import, main, visualization, controller or SDK is executed.
    The three exact substitutions only remove geometry/pickle I/O; the official
    optimizer, bounds, weights, smoothing and failure behavior are unchanged.
    """
    files = verify_source(source)
    import numpy as np
    import pinocchio as pin
    import casadi
    from pinocchio import casadi as cpin

    namespace = {"np": np, "pin": pin, "casadi": casadi, "cpin": cpin,
                 "os": os, "logger_mp": logging.getLogger("official_g1_29_ik"),
                 "URDF_PATH": str(source / "assets/g1/g1_body29_hand14.urdf")}
    filter_code = class_source(files["teleop/utils/weighted_moving_filter.py"], "WeightedMovingFilter")
    exec(compile(filter_code, "audited:weighted_moving_filter.py", "exec"), namespace)
    code = class_source(files["teleop/robot_control/robot_arm_ik.py"], "G1_29_ArmIK")
    for before, after in HEADLESS_EDITS:
        if code.count(before) != 1:
            raise ValueError("无图形离线适配与固定官方代码不匹配")
        code = code.replace(before, after, 1)
    exec(compile(code, "audited:robot_arm_ik.py:G1_29_ArmIK", "exec"), namespace)
    ik = namespace["G1_29_ArmIK"](Unit_Test=False, Visualization=False)
    if list(ik.reduced_robot.model.names)[1:] != JOINTS:
        raise ValueError("官方模型关节顺序不符合左7+右7合同")
    return ik, {"pinocchio": pin.__version__, "casadi": casadi.__version__,
                "numpy": np.__version__}


def transform(raw, label):
    import numpy as np
    value = np.asarray(raw, dtype=float)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError(f"{label} 必须为有限的 4×4 变换")
    if not np.allclose(value[3], [0, 0, 0, 1], atol=1e-8):
        raise ValueError(f"{label} 的齐次末行错误")
    rotation = value[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-6):
        raise ValueError(f"{label} 不是有效的右手旋转矩阵")
    return value


def pose_transform(raw):
    import numpy as np
    import pinocchio as pin
    pose = np.asarray(raw, dtype=float)
    if pose.shape != (7,) or not np.isfinite(pose).all():
        raise ValueError("Tracker 位姿必须是有限的 [x,y,z,qx,qy,qz,qw]")
    norm = np.linalg.norm(pose[3:])
    if not .99 <= norm <= 1.01:
        raise ValueError("Tracker 四元数未归一化或为空")
    quaternion = pose[3:] / norm
    result = np.eye(4)
    result[:3, :3] = pin.Quaternion(quaternion[3], *quaternion[:3]).toRotationMatrix()
    result[:3, 3] = pose[:3]
    return result


def forward(ik, q):
    import pinocchio as pin
    model = ik.reduced_robot.model
    data = model.createData()
    pin.framesForwardKinematics(model, data, q)
    return [data.oMf[index].homogeneous.copy() for index in (ik.L_hand_id, ik.R_hand_id)]


def read_samples(path):
    samples = []
    with path.open() as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("type") == "sample":
                samples.append(record)
    if not samples:
        raise ValueError("PICO JSONL 没有 sample 记录")
    return samples


def tracker_pose(sample, serial):
    trackers = [t for t in sample.get("trackers", []) if t.get("serial_number") == serial]
    if len(trackers) != 1:
        raise ValueError(f"样本中 Tracker SN {serial!r} 缺失或重复")
    return pose_transform(trackers[0]["xyzw_pose"])


def replay_targets(samples, calibration, reference_ee, index):
    """Offline coordinate replay only: a saved sample does not establish freshness."""
    import numpy as np
    if calibration.get("schema") != "g1-arms-offline-calibration-v1":
        raise ValueError("标定 schema 必须为 g1-arms-offline-calibration-v1")
    serials = [calibration[f"{side}_sn"] for side in SIDES]
    if any(not isinstance(sn, str) or not sn.strip() for sn in serials) or serials[0] == serials[1]:
        raise ValueError("左右 Tracker 必须显式绑定两个不同的 SN")
    if calibration.get("pose_unit") != "m":
        raise ValueError("必须显式提供已确认的 pose_unit=m；不猜测单位")
    scale = float(calibration["position_scale"])
    if not math.isfinite(scale) or not 0 < scale <= 2:
        raise ValueError("position_scale 必须在 (0,2] 内")
    axis = transform(calibration["T_robot_axes_xr_axes"], "T_robot_axes_xr_axes")
    if not np.allclose(axis[:3, 3], 0):
        raise ValueError("T_robot_axes_xr_axes 只允许旋转；位置使用相对基准")
    reference_index = calibration["reference_sample_index"]
    if type(reference_index) is not int or not 0 <= reference_index < len(samples):
        raise ValueError("reference_sample_index 越界")
    if not 0 <= index < len(samples):
        raise ValueError("sample-index 越界")
    output = []
    for side, serial, ee in zip(SIDES, serials, reference_ee):
        mount = transform(calibration["T_tracker_wrist"][side], f"{side} T_tracker_wrist")
        offset = transform(calibration["T_robot_wrist_ik"][side], f"{side} T_robot_wrist_ik")
        # Distinct transforms: human Tracker mount and robot wrist→official IK.
        origin = axis @ tracker_pose(samples[reference_index], serial) @ mount
        current = axis @ tracker_pose(samples[index], serial) @ mount
        wrist = ee @ np.linalg.inv(offset)
        target = wrist.copy()
        target[:3, 3] += scale * (current[:3, 3] - origin[:3, 3])
        target[:3, :3] = current[:3, :3] @ origin[:3, :3].T @ wrist[:3, :3]
        output.append(transform(target @ offset, f"{side} IK target"))
    return output


def evaluate(ik, q, targets, position_tolerance, rotation_tolerance):
    import numpy as np
    import pinocchio as pin
    q = np.asarray(q, dtype=float)
    if q.shape != (14,) or not np.isfinite(q).all():
        raise ValueError("求解器没有返回14个有限关节值")
    model = ik.reduced_robot.model
    margin = np.minimum(q - model.lowerPositionLimit, model.upperPositionLimit - q)
    achieved = forward(ik, q)
    residuals = {}
    for side, actual, target in zip(SIDES, achieved, targets):
        residuals[side] = {
            "position_m": float(np.linalg.norm(actual[:3, 3] - target[:3, 3])),
            "rotation_rad": float(np.linalg.norm(pin.log3(actual[:3, :3] @ target[:3, :3].T))),
        }
    within = bool(np.all(margin >= -1e-7))
    residual_ok = all(r["position_m"] <= position_tolerance and r["rotation_rad"] <= rotation_tolerance
                      for r in residuals.values())
    return {"q_rad": q.tolist(), "q_deg": np.rad2deg(q).tolist(),
            "residuals": residuals, "joint_limits_ok": within,
            "minimum_joint_limit_margin_rad": float(margin.min()),
            "residuals_ok": residual_ok,
            "achieved_ee": {side: pose.tolist() for side, pose in zip(SIDES, achieved)}}


def write_svg(path, ik, q, targets):
    """Two static orthographic views of kinematics, without a network viewer."""
    import pinocchio as pin
    data = ik.reduced_robot.model.createData()
    pin.forwardKinematics(ik.reduced_robot.model, data, q)
    ee = forward(ik, q)
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="540" viewBox="0 0 1000 540">',
           '<rect width="1000" height="540" fill="#f8fafc"/>',
           '<text x="25" y="30" font-size="20">G1 arms: offline model preview (no hardware)</text>']
    for panel, (haxis, vaxis, title) in enumerate(((1, 2, 'Front: y / z (m)'), (0, 2, 'Side: x / z (m)'))):
        def point(p):
            return (panel * 500 + 250 - 430 * p[haxis], 390 - 430 * p[vaxis])
        svg.append(f'<text x="{panel * 500 + 30}" y="65" font-size="16">{title}</text>')
        for side_i, color in enumerate(('#2563eb', '#e11d48')):
            positions = [data.oMi[j].translation for j in range(1 + side_i * 7, 8 + side_i * 7)]
            positions.append(ee[side_i][:3, 3])
            points = ' '.join(f'{x:.2f},{y:.2f}' for x, y in map(point, positions))
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="4"/>')
            for p in positions:
                x, y = point(p)
                svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"/>')
            x, y = point(targets[side_i][:3, 3])
            svg.append(f'<path d="M{x-7:.2f},{y:.2f}h14 M{x:.2f},{y-7:.2f}v14" stroke="{color}" stroke-width="2"/>')
    svg += ['<text x="25" y="500" font-size="14">Blue: left / Red: right / Cross: target. No meshes or collision check. Revo2 payload unverified.</text>', '</svg>']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(svg))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    result.add_argument("--pico-jsonl", type=Path, help="只读已保存的 pico_probe JSONL；不连接 SDK")
    result.add_argument("--calibration", type=Path, help="两侧SN、安装变换、相对参考与轴变换；不自动猜测")
    result.add_argument("--sample-index", type=int, default=-1, help="JSONL sample序号；-1为最后一帧")
    result.add_argument("--translation-m", type=float, default=.01, help="合成目标在模型前向x轴平移；默认1cm")
    result.add_argument("--iterations", type=int, default=8, help="同一离线目标的优化/滤波次数，1–100")
    result.add_argument("--position-tolerance-m", type=float, default=.02)
    result.add_argument("--rotation-tolerance-rad", type=float, default=.1)
    result.add_argument("--output", type=Path, help="保存完整JSON报告")
    result.add_argument("--svg", type=Path, help="保存纯静态前/侧视图")
    return result


def run(args):
    import numpy as np
    if bool(args.pico_jsonl) != bool(args.calibration):
        raise ValueError("保存的PICO样本必须同时提供 --pico-jsonl 和完整 --calibration")
    if not 1 <= args.iterations <= 100:
        raise ValueError("iterations 必须在1–100内")
    for name in ("translation_m", "position_tolerance_m", "rotation_tolerance_rad"):
        if not math.isfinite(getattr(args, name)):
            raise ValueError(f"{name} 必须有限")
    if min(args.position_tolerance_m, args.rotation_tolerance_rad) <= 0:
        raise ValueError("残差阈值必须大于零")
    ik, versions = load_ik(args.source_dir.expanduser().resolve())
    calibration = json.loads(args.calibration.read_text()) if args.calibration else None
    q_reference = np.asarray(calibration["q_reference_rad"] if calibration else REFERENCE_Q, dtype=float)
    model = ik.reduced_robot.model
    if q_reference.shape != (14,) or not np.isfinite(q_reference).all() or np.any(q_reference < model.lowerPositionLimit) or np.any(q_reference > model.upperPositionLimit):
        raise ValueError("参考关节必须为模型限位内的14个有限值")
    reference = forward(ik, q_reference)
    if calibration:
        samples = read_samples(args.pico_jsonl)
        index = len(samples) - 1 if args.sample_index == -1 else args.sample_index
        targets = replay_targets(samples, calibration, reference, index)
        mode = "saved_pico_offline_replay_unverified_freshness"
    else:
        targets = [pose.copy() for pose in reference]
        for pose in targets:
            pose[0, 3] += args.translation_m
        mode = "synthetic_fk_reference"
    q = q_reference.copy()
    iterations = []
    for index in range(args.iterations):
        q, _unused_model_torque = ik.solve_ik(*targets, q.copy(), np.zeros(14))
        stats = ik.opti.stats()
        check = evaluate(ik, q, targets, args.position_tolerance_m, args.rotation_tolerance_rad)
        solved = bool(stats.get("success", False))
        iterations.append({"iteration": index + 1, "solver_success": solved,
                           "solver_status": stats.get("return_status", "unknown"),
                           "residuals": check["residuals"]})
        if not solved or not check["joint_limits_ok"]:
            break  # The official method returns its input q on failure; never label that success.
    success = all(i["solver_success"] for i in iterations) and check["joint_limits_ok"] and check["residuals_ok"]
    result = {"type": "offline_arm_preview", "mode": mode, "offline_ok": success,
              "motion_ready": False, "robot_accessed": False, "commands_published": 0,
              "upstream_commit": COMMIT, "source_sha256": FILES, "dependencies": versions,
              "joint_names": JOINTS, "q_reference_rad": q_reference.tolist(),
              "target_ee": {side: pose.tolist() for side, pose in zip(SIDES, targets)},
              "tolerances": {"position_m": args.position_tolerance_m, "rotation_rad": args.rotation_tolerance_rad},
              "iterations": iterations, **check,
              "limitations": ["Offline model computation only; not robot validation.",
                              "No collision, velocity, acceleration, clutch or watchdog validation.",
                              "Official URDF includes Unitree hands; BrainCo Revo2 geometry/inertia unverified.",
                              "Tracker SN binding, mount/axis/unit calibration and freshness require hardware verification.",
                              "Model torque is discarded; no command or torque output exists."]}
    if args.svg:
        write_svg(args.svg, ik, q, targets)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        result = {"type": "offline_arm_preview", "offline_ok": False, "motion_ready": False,
                  "robot_accessed": False, "commands_published": 0,
                  "error": f"{type(exc).__name__}: {exc}"}
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)
    return 0 if result["offline_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
