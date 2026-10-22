# SPDX-License-Identifier: GPL-3.0-only
"""检查用户启动路径；使用模拟窗口，不发出任何系统按键。"""
import unittest
from fisher.config import DEFAULTS
from fisher.controller import Controller
from fisher.runtime import Runtime

class FakeWindow:
    def __init__(self):
        self.focused = True
        self.size = (336,598)
    def foreground_matches(self, hwnd): return self.focused
    def client_box(self, hwnd): return (10,20,*self.size)
    def release(self): pass

class StartupTests(unittest.TestCase):
    def setUp(self):
        self.win = FakeWindow()
        self.cfg = dict(DEFAULTS, dry_run=False)
        self.r = Runtime(self.win,1,{"client_size":[336,598],"crop":[0,0,336,598]},self.cfg)
        self.c = Controller(self.cfg)

    def test_monitoring_alone_remains_paused(self):
        self.r.process_controls(self.c,0)
        self.r.process_controls(self.c,10)
        self.assertFalse(self.r.active)
        self.assertIn("F8",self.r.pause_reason)

    def test_f8_activates_in_foreground(self):
        self.r.command("toggle")
        self.r.process_controls(self.c,2)
        self.assertTrue(self.r.active)
        self.assertEqual(self.c.started,2)

    def test_f8_rejected_without_foreground(self):
        self.win.focused=False
        self.r.command("toggle")
        self.r.process_controls(self.c,2)
        self.assertFalse(self.r.active)
        self.assertIn("不在前台",self.r.pause_reason)

    def test_button_waits_three_seconds_then_starts(self):
        self.win.focused=False
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.assertFalse(self.r.active)
        self.win.focused=True
        self.r.process_controls(self.c,12.9)
        self.assertFalse(self.r.active)
        self.r.process_controls(self.c,13)
        self.assertTrue(self.r.active)
        self.assertIsNone(self.r.pending_start_at)

    def test_countdown_without_focus_is_cancelled_not_retried(self):
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.win.focused=False
        self.r.process_controls(self.c,13)
        self.win.focused=True
        self.r.process_controls(self.c,14)
        self.assertFalse(self.r.active)
        self.assertIsNone(self.r.pending_start_at)

    def test_emergency_pause_cancels_pending_start(self):
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.r.command("pause")
        self.r.process_controls(self.c,11)
        self.r.process_controls(self.c,20)
        self.assertFalse(self.r.active)
        self.assertIsNone(self.r.pending_start_at)

    def test_stop_monitoring_cancels_pending_start(self):
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.r.stop.set()
        self.r.process_controls(self.c,13)
        self.assertFalse(self.r.active)
        self.assertIsNone(self.r.pending_start_at)

    def test_window_resize_prevents_start(self):
        self.win.size=(400,700)
        self.r.command("toggle")
        self.r.process_controls(self.c,10)
        self.assertFalse(self.r.active)
        self.assertIn("尺寸",self.r.pause_reason)

    def test_start_does_not_disable_observation_mode(self):
        self.r.cfg["dry_run"]=True
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.r.process_controls(self.c,13)
        self.assertTrue(self.r.active)
        self.assertTrue(self.r.cfg["dry_run"])
        self.assertIn("不发送",self.r.pause_reason)

    def test_start_blocked_during_recording(self):
        self.r.recorder=object()
        self.r.command("arm")
        self.r.process_controls(self.c,10)
        self.assertFalse(self.r.active)
        self.assertIsNone(self.r.pending_start_at)

if __name__ == "__main__": unittest.main()
