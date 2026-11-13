import importlib.util
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_hand_preview", ROOT / "scripts/live_hand_preview.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now


class Lines:
    def __init__(self, clock, events):
        self.clock, self.events = clock, list(events)

    def get(self, timeout):
        if not self.events:
            self.clock.now += timeout
            raise queue.Empty
        self.clock.now += .01
        return self.events.pop(0)(self.clock.now)


class LivePreview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        binding = live.mapping.synthetic_fixture(self.base)
        retarget, settings = live.mapping.load_original()
        self.adapter = live.mapping.HandDDSPreview(binding, self.base, retarget, settings, allow_synthetic=True)
        self.clock = Clock()
        self.receiver = live.FeedbackReceiver(self.adapter, self.clock)
        self.receiver.receive(SimpleNamespace(states=[SimpleNamespace(q=.5)] * 6))

    def event(self, seq=1, age=.001, **changes):
        def result(now):
            frame = live.mapping.synthetic_record(self.adapter, seq=seq, now=now)["glove"]
            frame["source_t"] = now - age
            frame.update(changes)
            return "line", json.dumps(frame), now
        return result

    def collect(self, events, seconds=.08, idle=.1):
        output = []
        report = live.collect(self.adapter, self.receiver, Lines(self.clock, events),
                              seconds, idle, output.append, clock=self.clock)
        return report, output

    def test_valid_live_envelope_preserves_source_and_receipt_times(self):
        report, output = self.collect([self.event()])
        self.assertTrue(report["ok"])
        frame = output[0]
        self.assertFalse(frame["published"])
        self.assertFalse(frame["feedback_identity_verified"])
        self.assertEqual(frame["input"]["measured"]["received_t"], 100.)
        self.assertAlmostEqual(frame["input"]["glove"]["source_t"], 100.009)
        self.assertEqual(frame["result"]["status"], "preview")

    def test_no_glove_frames_times_out(self):
        report, output = self.collect([], seconds=1.)
        self.assertEqual(report["reason"], "glove_timeout")
        self.assertEqual(report["preview_count"], 0)

    def test_eof_stops_without_reusing_last_target(self):
        report, output = self.collect([self.event(), lambda now: ("eof", None, now)], seconds=1.)
        self.assertEqual(report["reason"], "glove_disconnected")
        self.assertEqual(report["preview_count"], 1)
        self.assertNotIn("command_preview", output[-1])

    def test_old_source_time_is_not_refreshed_on_receipt(self):
        report, output = self.collect([self.event(age=1.)])
        self.assertEqual(report["reason"], "invalid_glove_frame")
        self.assertEqual(report["preview_count"], 0)
        self.assertNotIn("result", output[0])

    def test_replayed_device_identity_stops(self):
        report, _ = self.collect([self.event(), self.event()], seconds=1.)
        self.assertEqual(report["reason"], "invalid_glove_frame")
        self.assertEqual(report["preview_count"], 1)

    def test_stale_feedback_refuses_without_timestamp_rewrite(self):
        self.clock.now += .25
        report, output = self.collect([self.event()], idle=.5)
        self.assertFalse(report["ok"])
        self.assertEqual(output[0]["result"]["reason"], "stale_or_future_feedback")
        self.assertEqual(output[0]["input"]["measured"]["received_t"], 100.)
        self.assertNotIn("command_preview", output[0]["result"])

    def test_missing_feedback_waits_then_times_out(self):
        self.receiver.latest = None
        events = [self.event(seq=i + 1) for i in range(30)]
        report, output = self.collect(events, seconds=1.)
        self.assertEqual(report["reason"], "feedback_timeout")
        self.assertEqual(report["preview_count"], 0)

    def test_invalid_dds_vector_is_not_used(self):
        self.receiver.receive(SimpleNamespace(states=[SimpleNamespace(q=float("nan"))] * 6))
        report, _ = self.collect([self.event()])
        self.assertEqual(report["reason"], "invalid_feedback")
        self.assertEqual(report["preview_count"], 0)

    def test_bad_identity_is_not_a_preview(self):
        for changes in ({"sn": "other"}, {"hand_side": "right"}, {"sdk_user": "other"}):
            with self.subTest(changes=changes):
                report, _ = self.collect([self.event(**changes)])
                self.assertEqual(report["reason"], "invalid_glove_frame")

    def test_missing_local_binding_never_starts_supervisor(self):
        with patch.object(live, "supervise") as supervise, patch("sys.stdout", io.StringIO()):
            self.assertEqual(live.main(["--side", "left", "--sn", "x", "--user", "named",
                                        "--interface", "lo"]), 2)
        supervise.assert_not_called()

    def test_active_user_model_hash_is_checked_before_device_connection(self):
        manager = Mock()
        manager.current_user.return_value = {"user_id": "test-user", "is_default": False,
                                             "display_name": self.adapter.binding["sdk_user"]}
        sdk = SimpleNamespace(SdkManager=SimpleNamespace(instance=lambda: manager))
        model = self.base / ".wuji/sdk/users/test-user/models/left_hand.urdf"
        model.parent.mkdir(parents=True)
        model.write_bytes((self.base / "left_hand.urdf").read_bytes())
        live.verify_sdk_context(self.adapter, sdk, home=self.base)
        manager.connect.assert_not_called()
        model.write_text("different")
        with self.assertRaisesRegex(ValueError, "hash"):
            live.verify_sdk_context(self.adapter, sdk, home=self.base)
        manager.connect.assert_not_called()

    def test_wrong_clock_or_side_binding_refuses_before_sdk(self):
        binding = dict(self.adapter.binding)
        binding["kind"] = "operator_calibrated"
        binding["sdk_model"] = dict(binding["sdk_model"], kind="operator_calibrated")
        binding["feedback_source_id"] = live.feedback_source_id("lo", 0, "left")
        path = self.base / "binding.json"
        path.write_text(json.dumps(binding))
        args = live.parser().parse_args(["--side", "left", "--sn", binding["glove_sn"],
                                        "--user", binding["sdk_user"], "--interface", "lo",
                                        "--binding", str(path)])
        with self.assertRaisesRegex(ValueError, "clock_id"):
            live.prepare(args)
        binding["clock_id"] = live.clock_id()
        path.write_text(json.dumps(binding))
        prepared, _ = live.prepare(args)
        self.assertEqual(prepared.side, "left")
        args.side = "right"
        with self.assertRaisesRegex(ValueError, "side/SN/user"):
            live.prepare(args)

    def test_timeout_kills_only_own_process_group(self):
        process = Mock(pid=123456)
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 1), 0]
        popen = Mock(return_value=process)
        output = []
        with patch.object(live.os, "killpg") as kill:
            code = live.supervise([], .1, output.append, popen=popen)
        self.assertEqual(code, 3)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(all(call.args[0] == 123456 for call in kill.call_args_list))
        self.assertEqual(output[0]["reason"], "worker_hard_timeout")

    def test_worker_always_cleans_bridge_and_subscriber_on_collection_error(self):
        process = Mock()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        manager, subscriber = Mock(), Mock()
        channel = SimpleNamespace(ChannelFactoryInitialize=Mock(), ChannelSubscriber=Mock(return_value=subscriber))
        modules = {"wuji_sdk": SimpleNamespace(), "unitree_sdk2py.core.channel": channel,
                   "unitree_sdk2py.idl.unitree_go.msg.dds_": SimpleNamespace(MotorStates_=object)}
        args = SimpleNamespace(side="left", sn="sn", user="user", rate_hz=20., idle_timeout=2.,
                               seconds=1., domain=0, interface="lo")
        with patch.dict("sys.modules", modules), patch.object(live, "verify_sdk_context", return_value=manager), \
                patch.object(live.subprocess, "Popen", return_value=process), \
                patch.object(live, "BridgeLines") as lines, patch.object(live.signal, "signal"), \
                patch.object(live, "collect", side_effect=ValueError("input failed")):
            with self.assertRaisesRegex(ValueError, "input failed"):
                live.worker(args, self.adapter, live.mapping.DEFAULT_SOURCE, lambda _: None)
        process.send_signal.assert_called()
        subscriber.Close.assert_called_once()
        manager.disconnect_all.assert_called_once()
        channel.ChannelSubscriber.assert_called_once_with("rt/brainco/left/state", object)

    def test_bridge_wrapper_separates_native_logging_without_changing_python_json(self):
        reader = self.base / "local_print_fixture.py"
        reader.write_text("import os,sys,json\nos.write(1,b'native SDK log\\n')\n"
                          "print(json.dumps({'argument':sys.argv[1]}),flush=True)\n")
        result = subprocess.run([sys.executable, "-u", "-c", live.BRIDGE_LAUNCHER,
                                 str(reader), "test-only"], capture_output=True, text=True, timeout=2.)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"argument": "test-only"})
        self.assertEqual(result.stderr.strip(), "native SDK log")


if __name__ == "__main__":
    unittest.main()
