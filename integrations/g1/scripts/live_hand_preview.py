#!/usr/bin/env python3
"""有限时长 Wuji + BrainCo DDS 反馈预览；从不发布手部命令。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_hand_dds_preview", ROOT / "scripts/preview_hand_dds.py")
mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapping)
# Keep the original reader's Python JSON print stream, while native SDK printf
# goes to stderr. Execute the locked file unchanged, not a replacement reader.
BRIDGE_LAUNCHER = (
    "import os,sys,runpy; "
    "stream=os.fdopen(os.dup(1),'w',buffering=1); os.dup2(2,1); sys.stdout=stream; "
    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
)


def clock_id():
    return "linux-monotonic:" + Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def feedback_source_id(interface, domain, side):
    return f"dds:domain={domain};interface={interface};topic=rt/brainco/{side}/state"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--side", choices=("left", "right"))
    p.add_argument("--sn")
    p.add_argument("--user")
    p.add_argument("--binding", type=Path)
    p.add_argument("--source-dir", type=Path, default=mapping.DEFAULT_SOURCE)
    p.add_argument("--interface")
    p.add_argument("--domain", type=int, default=0)
    p.add_argument("--seconds", type=float, default=5.)
    p.add_argument("--idle-timeout", type=float, default=2.)
    p.add_argument("--rate-hz", type=float, default=20.)
    p.add_argument("--show-clock-id", action="store_true", help="仅显示当前boot的monotonic域，无设备访问")
    p.add_argument("--check-binding", action="store_true", help="仅校验本地工件，不连接SDK或DDS")
    p.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return p


def prepare(args):
    """All local evidence is checked before any SDK/device initialization."""
    for field in ("side", "sn", "user", "binding", "interface"):
        if not getattr(args, field):
            raise ValueError(f"必须显式提供 --{field}")
    if args.user.lower() == "default":
        raise ValueError("需要已标定的命名 SDK user")
    for key, low, high in (("seconds", .1, 60), ("idle_timeout", .1, 5), ("rate_hz", 1, 120)):
        value = getattr(args, key)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} 必须在 [{low},{high}] 内")
    if not 0 <= args.domain <= 232:
        raise ValueError("DDS domain 必须在0–232内")
    socket.if_nametoindex(args.interface)  # local NIC lookup, no packets
    source = args.source_dir.expanduser().resolve()
    lock = mapping.strict_json((ROOT / "sources.lock.json").read_text())
    expected = lock["hand_source"]["sha256"]["wuji_bridge.py"]
    if mapping.digest((source / "wuji_bridge.py").read_bytes()) != expected:
        raise ValueError("原 wuji_bridge.py 与 sources.lock.json 不符")
    retarget, settings = mapping.load_original(source)
    binding_path = args.binding.expanduser().resolve()
    binding = mapping.strict_json(binding_path.read_text())
    adapter = mapping.HandDDSPreview(binding, binding_path.parent, retarget, settings)
    if binding["kind"] != "operator_calibrated":
        raise ValueError("实时入口不接受 synthetic_test_only binding")
    if (binding["side"], binding["glove_sn"], binding["sdk_user"]) != (args.side, args.sn, args.user):
        raise ValueError("CLI side/SN/user 与已确认 binding 不一致")
    if binding["clock_id"] != clock_id():
        raise ValueError("binding.clock_id 必须匹配本机当前boot；运行 --show-clock-id 查看")
    configured_source = feedback_source_id(args.interface, args.domain, args.side)
    if binding["feedback_source_id"] != configured_source:
        raise ValueError(f"binding.feedback_source_id 必须为 {configured_source}")
    return adapter, source


def verify_sdk_context(adapter, sdk, home=None):
    """Inspect the active user/model only; never switch users or connect a glove."""
    manager = sdk.SdkManager.instance()
    user = manager.current_user()
    if user.get("is_default") is not False or user.get("display_name") != adapter.binding["sdk_user"]:
        raise ValueError("当前SDK user与已确认binding不一致；未切换用户或连接手套")
    uid = user.get("user_id")
    if not isinstance(uid, str) or not uid or Path(uid).name != uid or uid in (".", ".."):
        raise ValueError("SDK user_id 无法用于模型文件核对")
    root = Path.home() if home is None else Path(home)
    model = root / ".wuji/sdk/users" / uid / "models" / f"{adapter.side}_hand.urdf"
    if mapping.digest(model.read_bytes()) != adapter.binding["sdk_model"]["sha256"]:
        raise ValueError("当前SDK实际使用的手模型hash与binding不一致")
    return manager


class FeedbackReceiver:
    def __init__(self, adapter, clock=time.monotonic):
        self.adapter, self.clock = adapter, clock
        self.lock = threading.Lock()
        self.latest, self.error, self.counter = None, None, 0

    def receive(self, message):
        received = self.clock()  # receipt time on this host, not sensor/source time
        try:
            states = list(message.states)
            q = mapping.vector([float(state.q) for state in states], "DDS states.q", 0, 1)
            value = {"hand_side": self.adapter.side,
                     "source_id": self.adapter.binding["feedback_source_id"],
                     "topic": f"rt/brainco/{self.adapter.side}/state", "channels": mapping.DDS_CHANNELS,
                     "received_t": received, "q_normalized": q}
        except Exception as exc:
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
            return
        with self.lock:
            self.counter += 1
            value["sample_id"] = self.counter
            self.latest = value

    def snapshot(self):
        with self.lock:
            return (None if self.latest is None else dict(self.latest)), self.error


class BridgeLines:
    """A one-item queue discards pipe backlog; source_t is never refreshed."""
    def __init__(self, stdout, clock=time.monotonic):
        self.stdout, self.clock = stdout, clock
        self.queue = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _put(self, item):
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.queue.put_nowait(item)

    def _read(self):
        try:
            while True:
                line = self.stdout.readline(65537)
                received = self.clock()
                if not line:
                    self._put(("eof", None, received))
                    return
                if len(line) > 65536 or not line.endswith("\n"):
                    self._put(("error", "bridge JSON line exceeds limit or is incomplete", received))
                    return
                self._put(("line", line, received))
        except (OSError, ValueError) as exc:
            self._put(("error", str(exc), self.clock()))

    def get(self, timeout):
        return self.queue.get(timeout=timeout)


def validate_glove(frame, adapter, received, now, previous):
    if not isinstance(frame, dict):
        raise ValueError("原bridge必须输出JSON对象")
    if (frame.get("hand_side"), frame.get("sn"), frame.get("sdk_user")) != (
            adapter.side, adapter.binding["glove_sn"], adapter.binding["sdk_user"]):
        raise ValueError("原bridge的side/SN/user与binding不一致")
    source_t = mapping.number(frame.get("source_t"), "glove.source_t")
    if not 0 <= source_t <= received <= now or now - source_t > adapter.glove_age:
        raise ValueError("glove source_t过期/超前；不得用收到时刻刷新原始时间")
    seq, device = frame.get("seq"), frame.get("timestamp_us")
    if type(seq) is not int or type(device) is not int or seq < 0 or device <= 0:
        raise ValueError("原bridge的设备seq/timestamp_us无效")
    if previous is not None and not all(a > b for a, b in zip((source_t, seq, device), previous)):
        raise ValueError("原bridge帧重复、回退或设备重启；结束本次会话")
    readings = frame.get("joint_angles")
    if not isinstance(readings, dict) or adapter.retarget.readings(frame, adapter.side) is None:
        raise ValueError("原bridge没有有效的joint_angles")
    for name, value in readings.items():
        mapping.number(value, f"joint_angles.{name}")
    return source_t, seq, device


def collect(adapter, receiver, lines, seconds, idle_timeout, emit, *, clock=time.monotonic, stopped=None):
    started = last_glove = clock()
    previous = None
    previews = refusals = 0
    reason = "duration_complete"
    while clock() - started < seconds:
        if stopped is not None and stopped.is_set():
            reason = "interrupted"
            break
        now = clock()
        measured, error = receiver.snapshot()
        if error:
            reason = "invalid_feedback"
            refusals += 1
            emit({"type": "refused", "reason": reason, "detail": error, "published": False})
            break
        if now - last_glove > idle_timeout:
            reason = "glove_timeout"
            break
        if now - (measured["received_t"] if measured else started) > idle_timeout:
            reason = "feedback_timeout"
            break
        try:
            kind, value, received = lines.get(timeout=min(.05, max(.001, seconds - (now - started))))
        except queue.Empty:
            continue
        if kind != "line":
            reason = "glove_disconnected" if kind == "eof" else "glove_pipe_error"
            refusals += 1
            emit({"type": "refused", "reason": reason, "detail": value, "published": False})
            break
        now = clock()
        try:
            frame = mapping.strict_json(value)
            previous = validate_glove(frame, adapter, received, now, previous)
        except (ValueError, TypeError) as exc:
            reason = "invalid_glove_frame"
            refusals += 1
            emit({"type": "refused", "reason": reason, "detail": str(exc), "published": False})
            break
        last_glove = received
        measured, error = receiver.snapshot()
        if error:
            reason = "invalid_feedback"
            refusals += 1
            emit({"type": "refused", "reason": reason, "detail": error, "published": False})
            break
        if measured is None:
            refusals += 1
            emit({"type": "refused", "reason": "waiting_for_measured_state", "published": False})
            continue
        now = clock()  # snapshot may race a newer DDS callback; now must follow receipt
        record = {"binding_id": adapter.binding_id, "clock_id": adapter.binding["clock_id"],
                  "sdk_model_sha256": adapter.binding["sdk_model"]["sha256"],
                  "now_monotonic_s": now, "glove": frame, "measured": measured}
        result = adapter.preview(record, now)
        previews += result["status"] == "preview"
        refusals += result["status"] != "preview"
        emit({"type": "live_hand_preview", "input": record, "result": result,
              "glove_received_monotonic_s": received, "published": False,
              "feedback_identity_verified": False,
              "feedback_identity_scope": "topic_and_configured_receiver_only"})
    return {"type": "summary", "reason": reason, "preview_count": previews, "refusal_count": refusals,
            "elapsed_s": clock() - started, "published": False, "hardware_output": False,
            "motion_ready": False, "command_publishers_created": 0,
            "ok": reason == "duration_complete" and previews > 0 and refusals == 0}


def stop_owned(process, *, group=False):
    """Signal only our known child (or our explicitly created child session)."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            if group:
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.send_signal(sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.)
            if group:
                # A worker can exit before its bridge; reap its remaining group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return
        except subprocess.TimeoutExpired:
            continue
    process.wait(timeout=1.)


def worker(args, adapter, source, emit):
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: stopped.set())
    receiver = FeedbackReceiver(adapter)
    manager = subscriber = process = lines = None
    summary = None
    try:
        import wuji_sdk
        manager = verify_sdk_context(adapter, wuji_sdk)
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
        ChannelFactoryInitialize(args.domain, args.interface)
        subscriber = ChannelSubscriber(f"rt/brainco/{args.side}/state", MotorStates_)
        subscriber.Init(receiver.receive, 0)
        command = [sys.executable, "-u", "-c", BRIDGE_LAUNCHER, str(source / "wuji_bridge.py"), "--sn", args.sn,
                   "--side", args.side, "--user", args.user, "--rate-hz", str(args.rate_hz),
                   "--timeout-s", str(args.idle_timeout)]
        process = subprocess.Popen(command, cwd=source, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, text=True,
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        lines = BridgeLines(process.stdout)
        emit({"type": "session", "binding_id": adapter.binding_id, "clock_id": adapter.binding["clock_id"],
              "feedback_source_id": adapter.binding["feedback_source_id"], "hand_side": adapter.side,
              "feedback_identity_verified": False, "published": False, "motion_ready": False,
              "clock_semantics": "same-host monotonic; glove source_t preserved; DDS received_t is local arrival"})
        summary = collect(adapter, receiver, lines, args.seconds, args.idle_timeout, emit, stopped=stopped)
    finally:
        # Cleanup the glove before native DDS close, which may itself hang.
        if process is not None:
            stop_owned(process)
            if lines is not None:
                lines.thread.join(timeout=1.)
            process.stdout.close()
        if subscriber is not None:
            subscriber.Close()
        if manager is not None:
            manager.disconnect_all()  # this worker manager owns no glove connections
    emit(summary)
    return 0 if summary["ok"] else 2


def supervise(argv, seconds, emit, popen=subprocess.Popen):
    process = popen([sys.executable, "-u", str(Path(__file__).resolve()), *argv, "--_worker"],
                    stdin=subprocess.DEVNULL, start_new_session=True,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        return process.wait(timeout=seconds + 12.)
    except subprocess.TimeoutExpired:
        emit({"type": "summary", "reason": "worker_hard_timeout", "ok": False,
              "published": False, "command_publishers_created": 0})
        return 3
    finally:
        stop_owned(process, group=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    output = sys.stdout

    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=output, flush=True)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    try:
        if args.show_clock_id:
            emit({"clock_id": clock_id(), "published": False})
            return 0
        adapter, source = prepare(args)
        if args.check_binding:
            emit({"binding_valid_locally": True, "binding_id": adapter.binding_id,
                  "active_sdk_user_not_checked": True, "hardware_output": False, "published": False})
            return 0
        if args._worker:
            # Native SDK printf must not corrupt JSONL; only this saved FD is data.
            output = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            return worker(args, adapter, source, emit)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, interrupted)
        return supervise(argv, args.seconds, emit)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit({"type": "refused", "reason": "initialization_or_cleanup_failed",
              "detail": f"{type(exc).__name__}: {exc}", "published": False, "hardware_output": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
