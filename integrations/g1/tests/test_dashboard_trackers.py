from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dashboard_trackers import TrackerManager

class Trackers(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'diagnostics').mkdir()
        self.m=TrackerManager(self.root,simulation=True);self.addCleanup(self.m.close)
    def test_constructor_has_no_sdk_or_subprocess(self):
        with patch('dashboard_trackers.subprocess.Popen') as spawn:
            m=TrackerManager(self.root);self.assertFalse(m.status()['running']);m.close()
        spawn.assert_not_called()
    def test_single_shared_reader_preserves_sample_and_stops(self):
        self.m.start();pid=self.m.process.pid
        self.assertTrue(self.m.frame_path.is_relative_to(self.root/'diagnostics/simulation-trackers'))
        self.m.start();self.assertEqual(self.m.process.pid,pid)
        deadline=time.monotonic()+2
        while not self.m.snapshot() and time.monotonic()<deadline:time.sleep(.01)
        frame=self.m.snapshot();self.assertIsNotNone(frame)
        self.assertEqual(frame['sample_mono_on_robot'],frame['sample_mono'])
        self.assertLessEqual(frame['sample_mono'],frame['received_mono'])
        self.assertEqual(set(frame['trackers']),{'SIM-LEFT','SIM-RIGHT'})
        self.m.stop();self.assertIsNone(self.m.snapshot());self.assertFalse(self.m.status()['running'])
    def test_other_owner_rejected_without_replacing_reader(self):
        self.m.start();pid=self.m.process.pid
        other=TrackerManager(self.root,simulation=True);self.addCleanup(other.close)
        with self.assertRaises(RuntimeError):other.start()
        self.assertIsNone(self.m.process.poll());self.assertEqual(pid,self.m.process.pid)
    def test_foreign_native_reader_rejected_before_spawn(self):
        m=TrackerManager(self.root);self.addCleanup(m.close)
        with patch('dashboard_session._conflicts',return_value=['pico_raw_probe.py']),patch('dashboard_trackers.subprocess.Popen') as spawn:
            with self.assertRaises(RuntimeError):m.start()
        spawn.assert_not_called()

if __name__=='__main__':unittest.main()
