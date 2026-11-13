#!/usr/bin/env python3
"""仅预览：调用现有 Wuji→BrainCo 运行器的 --dry-run，不发送手部命令。"""

import argparse
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time


DEFAULT_SOURCE = Path.home() / "branco-wuji-teleop/brainco-wuji-teleop"
CHANNELS = ["thumb_flex", "thumb_opposition", "index", "middle", "ring", "little"]
SOURCES = ["thumb", "thumb_opposition", "index", "middle", "ring", "little"]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE,
                        help="原 Wuji→BrainCo 项目目录；不会修改其中的文件")
    result.add_argument("--side", choices=("left", "right"), default="right")
    result.add_argument("--calibration", type=Path,
                        help="对应侧的原格式标定文件；默认使用原项目 calibration.json")
    result.add_argument("--sn", help="实时预览使用的 Wuji 手套序列号")
    result.add_argument("--user", help="实时预览要求的已标定 Wuji SDK 用户")
    result.add_argument("--demo", action="store_true",
                        help="无需硬件，按原标定生成合成输入，有限时长后退出")
    result.add_argument("--duration", type=float, default=2.,
                        help="demo 首帧输出后的运行秒数，0.1–60；默认 2")
    return result


def prepare(args):
    source = args.source_dir.expanduser().resolve()
    calibration_path = (args.calibration or source / "calibration.json").expanduser().resolve()
    settings_path = source / "settings.example.json"
    if not (source / "teleop_hand.py").is_file():
        raise ValueError(f"找不到原运行器：{source / 'teleop_hand.py'}")
    settings = json.loads(settings_path.read_text())
    if not isinstance(settings, dict) or settings.get("motion_enabled") is not False:
        raise ValueError("原 settings.example.json 必须显式设置 motion_enabled=false")
    calibration = json.loads(calibration_path.read_text())
    if not isinstance(calibration, dict):
        raise ValueError("标定文件必须是 JSON 对象")
    if calibration.get("side") != args.side:
        raise ValueError("标定文件 hand side 与 --side 不一致")
    if calibration.get("channels") != CHANNELS:
        raise ValueError("标定文件通道顺序不是原 Revo2 六通道顺序")
    for key in ("open", "closed"):
        values = calibration.get(key)
        if not isinstance(values, list) or len(values) != 6:
            raise ValueError(f"标定 {key} 必须包含六个值")
        for index, value in enumerate(values):
            if index == 1 and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"标定 {key}[{index}] 必须为有限数值")
    for index, (opened, closed) in enumerate(zip(calibration["open"], calibration["closed"])):
        if index == 1 and opened is None and closed is None:
            continue
        if opened is None or closed is None or abs(closed - opened) < 1e-3:
            raise ValueError(f"标定通道 {index} 缺失或张开/握拳范围过小")
    if not math.isfinite(args.duration) or not .1 <= args.duration <= 60:
        raise ValueError("--duration 必须为 0.1–60 秒")
    if args.demo and (args.sn or args.user):
        raise ValueError("--demo 不连接手套，不能同时指定 --sn/--user")
    if not args.demo and (not args.sn or not args.user):
        raise ValueError("实时预览需要 --sn 和 --user；无需硬件请使用 --demo")

    # 参数使用固定白名单；不提供 --settings、硬件模式或参数透传入口。
    command = [sys.executable, "-u", str(source / "teleop_hand.py"), "run",
               "--dry-run", "--settings", str(settings_path),
               "--calibration", str(calibration_path), "--side", args.side,
               "--rate-hz", "20"]
    if not args.demo:
        bridge = source / "wuji_bridge.py"
        if not bridge.is_file():
            raise ValueError(f"找不到原手套桥接程序：{bridge}")
        glove = [sys.executable, "-u", str(bridge), "--sn", args.sn,
                 "--user", args.user, "--side", args.side]
        # 原运行器使用 shlex.split，不经过 shell。
        command += ["--glove-cmd", shlex.join(glove)]
    return source, command, calibration


def synthetic_message(calibration, side, fraction):
    """只生成原标定同量纲的输入；目标映射仍由原运行器完成。"""
    values = {}
    for name, opened, closed in zip(SOURCES, calibration["open"], calibration["closed"]):
        if opened is not None and closed is not None:
            values[name] = opened + fraction * (closed - opened)
    return {"hand_side": side, "joint_angles": values, "source_t": time.monotonic()}


def stop_group(process):
    """子进程有独立进程组，清理时包含它启动的原手套桥接进程。"""
    for sig, timeout in ((signal.SIGINT, 5.), (signal.SIGTERM, 2.), (signal.SIGKILL, 2.)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
            break
        except subprocess.TimeoutExpired:
            continue
    # 正常运行器退出前会回收手套桥接；异常退出时清除仍在同组的后代。
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(args):
    source, command, calibration = prepare(args)
    print("仅预览：复用原映射，20 Hz 输出；没有实测位置，未验证实机步长限制。", file=sys.stderr)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(command, cwd=source, env=environment,
                               stdin=subprocess.PIPE if args.demo else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, text=True, start_new_session=True)
    output_ready = threading.Event()
    output_failed = threading.Event()

    def relay():
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                output_ready.set()
        except (BrokenPipeError, OSError):
            output_failed.set()

    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    try:
        if not args.demo:
            while process.poll() is None:
                if output_failed.wait(.05):
                    raise RuntimeError("预览输出已关闭")
            return process.returncode
        startup_deadline = time.monotonic() + 10.
        started = None
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"原运行器提前退出，退出码 {process.returncode}")
            now = time.monotonic()
            if output_failed.is_set():
                raise RuntimeError("预览输出已关闭")
            if output_ready.is_set() and started is None:
                started = now
            if started is not None and now - started >= args.duration:
                break
            if started is None and now >= startup_deadline:
                raise RuntimeError("原运行器 10 秒内没有输出目标；检查 numpy 和原项目依赖")
            # 张开、握拳和中间姿态循环，数值均取自原标定。
            fraction = 0. if started is None else (0., 1., .5)[int((now - started) / .4) % 3]
            message = synthetic_message(calibration, args.side, fraction)
            process.stdin.write(json.dumps(message, allow_nan=False) + "\n")
            process.stdin.flush()
            time.sleep(.05)
        stop_group(process)
        if process.returncode != 0:
            raise RuntimeError(f"原运行器退出码 {process.returncode}")
        return 0
    finally:
        stop_group(process)
        if process.stdin is not None:
            process.stdin.close()
        reader.join(timeout=2.)
        process.stdout.close()


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"预览失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
