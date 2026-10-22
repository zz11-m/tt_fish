# SPDX-License-Identifier: GPL-3.0-only
"""持续运行、采样节奏、结果页重试和可配置快捷键的行为检查。"""
import json
import math
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from fisher.config import DEFAULTS, load_config, validate_config, hotkey_vk
from fisher.controller import Controller
from fisher.runtime import Runtime
from fisher.scheduling import CaptureSchedule, Hotkeys
from fisher.vision import Observation

DEFAULTS = dict(DEFAULTS, timing_mode="center", capture_every_n_frames=6)


class OptionsTests(unittest.TestCase):
    def test_old_config_keeps_profile_and_migrates_sampling_and_retries(self):
        old = {"dry_run": False, "capture_fps": 60, "result_wait_s": 3.5,
               "result_retry_s": 2, "result_max_presses": 3, "max_casts": 3,
               "input_latency_ms": 65,
               "profile": {"title": "雷电", "client_size": [557,994], "crop": [0,0,557,994]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            path.write_text(json.dumps(old), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(json.loads(path.read_text()), old)
        self.assertEqual(cfg["profile"], old["profile"])
        self.assertEqual(cfg["max_casts"], 3)
        self.assertEqual(cfg["input_latency_ms"], 65)
        self.assertEqual(cfg["capture_every_n_frames"], 6)
        self.assertEqual(cfg["result_wait_s"], 3)
        self.assertEqual(cfg["result_retry_s"], 1)
        self.assertNotIn("result_max_presses", cfg)
        self.assertNotIn("capture_fps", cfg)

    def test_new_config_values_survive_reload(self):
        cfg = dict(DEFAULTS, continuous_fishing=True, capture_every_n_frames=12,
                   toggle_hotkey="F6", pause_hotkey="F7", result_wait_s=4)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            self.assertEqual(load_config(path), cfg)

    def test_invalid_sampling_values_are_rejected(self):
        for value in (0, -1, 61, 2.5, True, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(dict(DEFAULTS, capture_every_n_frames=value))

    def test_invalid_or_colliding_hotkeys_are_rejected(self):
        for fields in ({"toggle_hotkey":"F9"}, {"pause_hotkey":"F10"},
                       {"toggle_hotkey":"SPACE"}, {"pause_hotkey":"F13"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_config(dict(DEFAULTS, **fields))

    def test_hotkeys_normalize(self):
        cfg = validate_config(dict(DEFAULTS, toggle_hotkey=" f6 ", pause_hotkey="f7"))
        self.assertEqual((cfg["toggle_hotkey"], cfg["pause_hotkey"]), ("F6", "F7"))


class ContinuousTests(unittest.TestCase):
    def test_continuous_ignores_both_session_limits(self):
        c = Controller(dict(DEFAULTS, continuous_fishing=True, max_casts=1, max_session_minutes=1))
        c.cast_count = 500
        c.step(Observation("IDLE", 1), 3600)
        c.step(Observation("IDLE", 1), 3600.1)
        d = c.step(Observation("IDLE", 1), 3600.5)
        self.assertEqual(d.action, "CAST")
        self.assertFalse(d.pause)
        c.commit("CAST", 3600.5)
        self.assertEqual(c.cast_count, 501)

    def test_unchecked_continuous_restores_both_limits(self):
        c = Controller(dict(DEFAULTS, continuous_fishing=False, max_casts=1, max_session_minutes=1))
        c.cast_count = 1
        c.step(Observation("IDLE", 1), 0)
        self.assertTrue(c.step(Observation("IDLE", 1), .1).pause)
        c.reset(0)
        self.assertTrue(c.step(Observation("WAIT", 1), 60).pause)

    def test_continuous_keeps_unknown_and_ack_timeout(self):
        c = Controller(dict(DEFAULTS, continuous_fishing=True))
        c.step(Observation(), 0)
        self.assertTrue(c.step(Observation(), c.cfg["unknown_timeout_s"]+1).pause)
        c.reset(0)
        c.step(Observation("IDLE", 1), 0)
        c.step(Observation("IDLE", 1), .1)
        c.commit("CAST", .5)
        self.assertTrue(c.step(Observation("IDLE", 1), 6).pause)


class ResultRetryTests(unittest.TestCase):
    def setUp(self):
        self.c = Controller(dict(DEFAULTS, continuous_fishing=True), random.Random(57))
        self.obs = Observation("RESULT", 1)
        self.c.step(self.obs, 0)
        self.c.step(self.obs, .1)

    def test_first_press_waits_three_seconds_after_confirmation(self):
        self.assertIsNone(self.c.step(self.obs, 3.099).action)
        self.assertEqual(self.c.step(self.obs, 3.1).action, "CLOSE")

    def test_retries_have_jitter_and_no_count_limit(self):
        now = 3.1
        intervals = []
        for _ in range(100):
            d = self.c.step(self.obs, now)
            self.assertEqual(d.action, "CLOSE")
            self.assertFalse(d.pause)
            self.c.commit(d.action, now)
            due = self.c.next_result_press_at
            intervals.append(due-now)
            self.assertIsNone(self.c.step(self.obs, due-.001).action)
            self.assertEqual(self.c.next_result_press_at, due)
            now = due
        self.assertEqual(self.c.result_presses, 100)
        self.assertTrue(all(.96-1e-9 <= dt <= 1.04+1e-9 for dt in intervals))
        self.assertGreater(len(set(round(dt, 3) for dt in intervals)), 30)

    def test_no_close_on_first_frame_outside_result_or_unknown(self):
        self.c.commit("CLOSE", 3.1)
        for state in ("IDLE", "UNKNOWN", "BITE", "WAIT", "TIMING"):
            self.assertIsNone(self.c.step(Observation(state, 1), 5).action)

    def test_return_to_idle_clears_retry_and_allows_new_cast(self):
        self.c.commit("CLOSE", 3.1)
        self.c.step(Observation("IDLE", 1), 3.2)
        self.c.step(Observation("IDLE", 1), 3.3)
        self.assertIsNone(self.c.next_result_press_at)
        d = self.c.step(Observation("IDLE", 1), 4)
        self.assertEqual(d.action, "CAST")
        self.c.commit(d.action, 4)
        self.c.step(self.obs, 5)
        self.c.step(self.obs, 5.1)
        self.assertAlmostEqual(self.c.next_result_press_at, 8.1)
        self.assertEqual(self.c.result_presses, 0)


class SamplingTests(unittest.TestCase):
    def test_budget_is_actual_capture_cadence(self):
        for n in (1, 3, 6, 12, 30, 60):
            schedule = CaptureSchedule(dict(DEFAULTS, capture_every_n_frames=n))
            captures = []
            for i in range(10000):
                t = i/10000
                if schedule.due(t):
                    captures.append(t)
                    schedule.advance(t, t+.001)
            self.assertLessEqual(len(captures), math.ceil(60/n))
            self.assertTrue(all(b-a >= n/60-1e-9 for a,b in zip(captures, captures[1:])))

    def test_slow_capture_does_not_catch_up_missed_frames(self):
        s = CaptureSchedule(DEFAULTS)
        s.advance(0, .6)
        self.assertTrue(s.due(.6))
        s.advance(.6, .61)
        self.assertFalse(s.due(.61))
        self.assertAlmostEqual(s.next_at, .7)

    def test_result_deadline_alignment_does_not_add_captures(self):
        s = CaptureSchedule(DEFAULTS)
        t = 0
        captures = [t]
        while t < .973-1e-9:
            s.advance(t, t+.008, .973)
            t = s.next_at
            captures.append(t)
        self.assertAlmostEqual(t, .973)
        self.assertTrue(all(b-a >= .1-1e-9 for a,b in zip(captures, captures[1:])))
        self.assertEqual(len(captures), 10)

    def test_timing_motion_works_at_reduced_sampling_rates(self):
        for n in (3, 6, 12):
            c = Controller(dict(DEFAULTS, capture_every_n_frames=n))
            hits = []
            for i in range(round(.8/(n/60))+1):
                t = i*n/60
                x = 100+160*t
                d = c.step(Observation("TIMING", 1, x, (172,244), 402, x<160 or x>256), t)
                if d.action:
                    hits.append(d)
                    c.commit(d.action, t)
            self.assertEqual(len(hits), 1, n)
            self.assertTrue(172 < hits[0].predicted_x < 244)


class HotkeyTests(unittest.TestCase):
    def test_custom_keys_replace_defaults_and_hold_does_not_repeat(self):
        h = Hotkeys(dict(DEFAULTS, toggle_hotkey="F6", pause_hotkey="F7"))
        down = {hotkey_vk("F8"), hotkey_vk("F9")}
        self.assertEqual(h.poll(down.__contains__), [])
        down = {hotkey_vk("F6")}
        self.assertEqual(h.poll(down.__contains__), ["toggle"])
        self.assertEqual(h.poll(down.__contains__), [])
        down = set()
        h.poll(down.__contains__)
        down = {hotkey_vk("F6"), hotkey_vk("F7")}
        self.assertEqual(h.poll(down.__contains__), ["toggle", "pause"])

    def test_runtime_cancellation_reads_custom_emergency_key(self):
        class Win:
            down = set()
            def key_down(self, key): return key in self.down
        win = Win()
        r = Runtime(win, 1, {}, dict(DEFAULTS, pause_hotkey="F7"))
        win.down = {hotkey_vk("F9")}
        self.assertFalse(r.input_cancelled())
        win.down = {hotkey_vk("F7")}
        self.assertTrue(r.input_cancelled())
        win.down = set()
        r.stop.set()
        self.assertTrue(r.input_cancelled())

    def test_old_f9_is_not_hidden_in_key_hold(self):
        from fisher.windows import Windows
        win = Windows.__new__(Windows)
        win.foreground_matches = lambda hwnd: True
        win.key_down = lambda key: key == hotkey_vk("F9")
        win._send = lambda up=False: None
        win.owns_space = False
        self.assertTrue(win.tap_space(1, 10, lambda: False))
        self.assertFalse(win.interrupted)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_capture_throttled_while_custom_hotkeys_remain_responsive(self):
        class Clock:
            t = .001
            def now(self): return self.t
        clock = Clock()
        class Stop:
            flag = False
            def is_set(self): return self.flag or clock.t >= 1.2
            def wait(self, delay): clock.t += max(delay, .00001)
            def set(self): self.flag = True
        class Win:
            polls = 0
            def foreground_matches(self, hwnd): return True
            def client_box(self, hwnd): return (0,0,336,598)
            def key_down(self, key):
                self.polls += 1
                return ((key == hotkey_vk("F6") and .04 <= clock.t < .08) or
                        (key == hotkey_vk("F7") and .35 <= clock.t < .39))
            def tap_space(self, *args): raise AssertionError("等待上钩时不应按空格")
            def release(self): pass
        grabs = []
        class Shot:
            width, height = 336,598
            bgra = bytes(336*598*4)
        class Screen:
            monitors = [{"left":0,"top":0,"width":1920,"height":1080}]
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, box):
                grabs.append(clock.t)
                clock.t += .002
                return Shot()
        class Detector:
            def __init__(self, *args): pass
            def detect(self, frame):
                clock.t += .008
                return Observation("WAIT", 1), np.zeros((598,336,3), dtype=np.uint8)
        win = Win()
        cfg = dict(DEFAULTS, toggle_hotkey="F6", pause_hotkey="F7")
        r = Runtime(win, 1, {"client_size":[336,598],"crop":[0,0,336,598]}, cfg)
        r.stop = Stop()
        changes = []
        publish = r.publish_control
        def tracked():
            changes.append((clock.t, r.active))
            publish()
        r.publish_control = tracked
        with tempfile.TemporaryDirectory() as tmp, \
             patch("fisher.runtime.DATA_ROOT", Path(tmp)), \
             patch("fisher.runtime.time.perf_counter", clock.now), \
             patch("fisher.runtime.mss.mss", Screen), \
             patch("fisher.runtime.Detector", Detector), \
             patch("fisher.runtime.annotate") as draw:
            r.run()
            draw.assert_not_called()
            logs = list((Path(tmp)/"logs").glob("*.jsonl"))
            self.assertEqual(logs, [])
            self.assertEqual(len(list((Path(tmp)/"trial_exports").glob("*.zip"))),0)
        self.assertTrue(any(.04 <= t < .09 and active for t, active in changes))
        self.assertTrue(any(.35 <= t < .4 and not active for t, active in changes))
        self.assertLessEqual(len(grabs), 12)
        self.assertTrue(all(b-a >= .1-1e-9 for a,b in zip(grabs, grabs[1:])))
        self.assertGreater(win.polls, len(grabs)*5)


if __name__ == "__main__":
    unittest.main()
