"""Verify dry-run invariants and execute the original mapper without hardware."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/preview_hand.py"
spec = importlib.util.spec_from_file_location("preview_hand", SCRIPT)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


@pytest.fixture
def source(tmp_path):
    original = preview.DEFAULT_SOURCE
    if not original.is_dir():
        pytest.skip("Existing Wuji→BrainCo source repository is required for integration tests")
    target = tmp_path / "original copy with spaces"
    target.mkdir()
    (target / "teleop").mkdir()
    for name in ("teleop_hand.py", "wuji_bridge.py", "settings.example.json", "calibration.json"):
        shutil.copyfile(original / name, target / name)
    for name in ("__init__.py", "retarget.py", "config.py", "timing.py", "live.py"):
        shutil.copyfile(original / "teleop" / name, target / "teleop" / name)
    # Only the temporary copy is instrumented; importing hardware must fail this test.
    (target / "teleop/hand.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).parents[1].joinpath('HARDWARE_IMPORTED').touch()\n"
        "raise AssertionError('Dry run imported the hardware driver')\n"
    )
    with (target / "teleop/retarget.py").open("a") as handle:
        handle.write(
            "\n_original_targets = targets\n"
            "def targets(*args, **kwargs):\n"
            "    from pathlib import Path\n"
            "    Path(__file__).parents[1].joinpath('ORIGINAL_MAPPING_CALLED').touch()\n"
            "    return _original_targets(*args, **kwargs)\n"
        )
    # A dangerous existing settings.json must never be selected.
    (target / "settings.json").write_text('{"motion_enabled": true}')
    return target


@pytest.mark.parametrize("mode", [["--demo"], ["--sn", "glove serial", "--user", "operator name"]])
def test_command_always_uses_original_dry_run_and_example_settings(source, mode):
    args = preview.parser().parse_args(["--source-dir", str(source), *mode])
    _, command, _ = preview.prepare(args)
    assert command[:4] == [sys.executable, "-u", str(source / "teleop_hand.py"), "run"]
    assert command.count("--dry-run") == 1
    assert command[command.index("--settings") + 1] == str(source / "settings.example.json")
    assert str(source / "settings.json") not in command
    if "--glove-cmd" in command:
        import shlex
        bridge = shlex.split(command[command.index("--glove-cmd") + 1])
        assert bridge[2] == str(source / "wuji_bridge.py")
        assert bridge[bridge.index("--sn") + 1] == "glove serial"


def test_wrong_side_calibration_is_rejected_before_spawning(source, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Wrong-side calibration spawned a child")
    monkeypatch.setattr(preview.subprocess, "Popen", forbidden)
    args = preview.parser().parse_args(["--source-dir", str(source), "--side", "left", "--demo"])
    with pytest.raises(ValueError, match="hand side"):
        preview.run(args)


def test_enabled_example_settings_are_rejected(source):
    (source / "settings.example.json").write_text('{"motion_enabled": true}')
    args = preview.parser().parse_args(["--source-dir", str(source), "--demo"])
    with pytest.raises(ValueError, match="motion_enabled=false"):
        preview.prepare(args)


@pytest.mark.parametrize("extra", [["--settings", "settings.json"], ["--motion-enabled"], ["--", "status"]])
def test_no_hardware_arguments_can_be_forwarded(extra):
    with pytest.raises(SystemExit) as exc:
        preview.parser().parse_args(["--demo", *extra])
    assert exc.value.code == 2


def test_finite_demo_runs_original_mapping_without_loading_hardware(source):
    result = subprocess.run([sys.executable, str(SCRIPT), "--source-dir", str(source),
                             "--demo", "--duration", ".9"],
                            capture_output=True, text=True, timeout=15.)
    assert result.returncode == 0, result.stderr
    samples = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(samples) >= 5
    assert list(samples[0]) == preview.CHANNELS
    assert list(samples[0].values()) == [100, 0, 0, 0, 0, 0]
    assert any(sample["index"] > 0 for sample in samples)
    assert all(100 <= sample["thumb_flex"] <= 900 for sample in samples)
    assert (source / "ORIGINAL_MAPPING_CALLED").exists()
    assert not (source / "HARDWARE_IMPORTED").exists()
    assert "未验证实机步长限制" in result.stderr


def test_help_does_not_require_optional_dependencies():
    result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"],
                            capture_output=True, text=True, timeout=5.)
    assert result.returncode == 0
    assert "--demo" in result.stdout and "--sn" in result.stdout


def test_live_preview_termination_reaps_runner_and_glove_child(source):
    # Synthetic bridge in the temporary copy: no SDK import or device discovery.
    pid_file = source / "preview_child_pids.json"
    (source / "wuji_bridge.py").write_text(
        "import json, os, signal, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(json.dumps([os.getpid(), os.getppid()]))\n"
        "signal.signal(signal.SIGINT, lambda *args: exit(0))\n"
        "signal.signal(signal.SIGTERM, lambda *args: exit(0))\n"
        "while True:\n"
        "    print(json.dumps({'hand_side':'right','joint_angles':"
        "{'thumb':0,'index':0,'middle':0,'ring':0,'little':0}}), flush=True)\n"
        "    time.sleep(.05)\n"
    )
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--source-dir", str(source),
                                "--sn", "synthetic", "--user", "test"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5.
        while not pid_file.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(.05)
        assert pid_file.exists()
        child_pids = json.loads(pid_file.read_text())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10.)
        assert process.returncode == 130
        for pid in child_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert not (source / "HARDWARE_IMPORTED").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10.)
