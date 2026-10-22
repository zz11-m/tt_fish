# SPDX-License-Identifier: GPL-3.0-only
"""双向折返、采样错过入口、超时设置与有界输入期限的回归检查。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
import numpy as np
from fisher.contact import next_entry, ContactModel, DeliveryLatency, MAX_TRACK_AGE
from fisher.config import DEFAULTS, load_config, validate_config
from fisher.controller import Controller, Decision
from fisher.runtime import Runtime
from fisher.scheduling import CaptureSchedule
from fisher.trial_capture import TrialRecorder
from fisher.vision import Observation
from test_speed import sample
from test_trial import Window

# 历史策略回归显式使用其原始配置；正式默认策略见test_phase。
DEFAULTS = dict(DEFAULTS,timing_mode="contact",capture_every_n_frames=6)


class ArrivalTests(unittest.TestCase):
    def test_direct_and_rebound_use_different_entries_and_preserve_speed(self):
        cases=((120,100,.4,1,0,160),(220,-100,.3,-1,0,190),
               (210,100,1.,-1,1,190),(130,-100,1.1,1,1,160))
        for scale in (.5,1,2):
            for x,v,eta,direction,turns,dest in cases:
                result=next_entry(x*scale,v*scale,160*scale,190*scale,90*scale,250*scale)
                self.assertAlmostEqual(result[0],eta)
                self.assertEqual(result[1:3],(direction,turns))
                self.assertAlmostEqual(result[3],dest*scale)
        self.assertIsNone(next_entry(120,0,160,190,90,250))
        self.assertIsNone(next_entry(270,100,160,190,90,250))

    def test_long_rebound_plan_only_schedules_capture_then_uses_fresh_return_track(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for side,value in (("left",94),("right",250)):
            model.motion.turns[side].extend([value]*3)
        speed=.606*183
        schedule=CaptureSchedule(DEFAULTS)
        t=0.; plans=[]; hit=None
        while t<4:
            phase=86+speed*t
            phase%=312
            x=94+phase if phase<=156 else 406-phase
            d=model.step(sample(x,target=(120,150)),t,t+.025)
            if d.get("planned_turns"):
                plans.append(d)
                self.assertIsNone(d.get("action"))
                self.assertIsNotNone(d.get("check_at"))
            if d.get("action"):
                hit=d
                self.assertLess(d["velocity"],0)
                self.assertLessEqual(d["send_at"]-t,MAX_TRACK_AGE)
                break
            schedule.advance(t,t+.025)
            schedule.request_earlier(d.get("check_at"),t,t+.025)
            t=schedule.next_at
        self.assertTrue(plans)
        self.assertIsNotNone(hit)

    def test_missed_entry_can_hit_inside_but_cannot_hit_after_exit(self):
        speed=.606*183
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for i in range(3):
            t=i*.1
            d=model.step(sample(140+speed*t,target=(150,220)),t,t+.025)
        self.assertEqual(d.get("action"),"HIT")
        self.assertEqual(d["click_mode"],"inside")
        self.assertGreater(d["valid_until"],.225)
        for direction in (1,-1):
            model=ContactModel(DEFAULTS,DeliveryLatency(40))
            for i in range(3):
                t=i*.1
                x=(210+speed*t) if direction>0 else (130-speed*t)
                d=model.step(sample(x,target=(150,180)),t,t+.025)
            self.assertIsNone(d.get("action"))

    def test_moderate_processing_age_preserves_track_long_stall_cancels(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for i in range(3):
            t=i*.1
            d=model.step(sample(140+.606*183*t,target=(170,220)),t,t+.075)
        self.assertEqual(d.get("action"),"HIT")
        self.assertNotEqual(d["motion_event"],"stale")
        d=model.step(sample(180,target=(170,220)),.3,.5)
        self.assertEqual(d["motion_event"],"stale")
        self.assertIsNone(d.get("action"))
        self.assertIsNone(d.get("send_at"))

    def test_unknown_and_timing_timeouts_are_optional_and_independent(self):
        for limit in (0,60,240):
            c=Controller(dict(DEFAULTS,unknown_timeout_s=limit,continuous_fishing=True))
            c.step(Observation(),0)
            d=c.step(Observation(),limit+1 if limit else 500)
            self.assertEqual(d.pause,bool(limit))
        c=Controller(dict(DEFAULTS,timing_timeout_s=0,continuous_fishing=True))
        for t in (0,.1,200):
            d=c.step(sample(100),t)
        self.assertFalse(d.pause)
        c=Controller(dict(DEFAULTS,timing_timeout_s=60,continuous_fishing=True))
        for t in (0,.1,61): d=c.step(sample(100),t)
        self.assertEqual(d.pause_code,"timing_timeout")

    def test_old_default_timeout_migrates_but_custom_choice_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"config.json"
            for old,expected in ((12,60),(30,30),(0,0)):
                p.write_text(json.dumps(dict(DEFAULTS,config_version=4,unknown_timeout_s=old)))
                self.assertEqual(load_config(p)["unknown_timeout_s"],expected)
        for bad in (-1,3601,float("nan"),True):
            with self.assertRaises(ValueError): validate_config(dict(DEFAULTS,unknown_timeout_s=bad))

    def test_inside_deadline_accepts_small_delay_but_rejects_expired_window(self):
        win=Window(); runtime=Runtime(win,1,{},DEFAULTS)
        runtime.active=True
        c=Controller(DEFAULTS)
        runtime.capture_box=lambda: {}
        for expired in (False,True):
            now=time.perf_counter()
            d=Decision(action="HIT",send_at=now-.015,valid_until=now+(-.001 if expired else .030),click_mode="inside")
            sent=runtime.dispatch(d,c,{},now-.070,now-.015)
            self.assertEqual(sent,not expired)

    def test_unknown_crops_are_bounded_and_arrival_fields_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder=TrialRecorder(tmp); recorder.start(DEFAULTS)
            start=recorder.current["start"]
            c=Controller(DEFAULTS); frame=np.zeros((598,336,3),np.uint8)
            for i in range(80):
                d=Decision(arrival_ms=800,arrival_direction=-1,planned_turns=1,endpoint_source="geometry")
                recorder.frame(Observation(),d,c,frame,start+i,start+i+.02,10,10)
            self.assertEqual(recorder.current["crops"],12)
            recorder.finish(); recorder.join()
            with zipfile.ZipFile(recorder.last_bundle) as z:
                rows=[json.loads(line) for line in z.read("events_000.jsonl").splitlines()]
                self.assertEqual(rows[0]["planned_turns"],1)
                self.assertEqual(rows[0]["arrival_direction"],-1)
                self.assertEqual(len([n for n in z.namelist() if n.startswith("crops/")]),12)

    def test_long_input_call_does_not_train_latency_or_keep_old_motion(self):
        class BlockedWindow(Window):
            def tap_space(self,hwnd,hold,cancel,guard):
                if cancel() or not guard(): return False
                self.last_send_begin=time.perf_counter()
                self.last_send_end=self.last_send_begin+.2
                self.last_key_down_at=self.last_send_begin+.1
                return True
        win=BlockedWindow();runtime=Runtime(win,1,{},DEFAULTS);runtime.active=True
        runtime.capture_box=lambda: {}
        c=Controller(DEFAULTS);c.contact.motion.points.extend([(0,100),(.1,111)])
        now=time.perf_counter()
        self.assertTrue(runtime.dispatch(Decision(action="HIT"),c,{},now,now))
        self.assertEqual(c.last_press,win.last_send_end)
        self.assertEqual(c.latency.rejected,1)
        self.assertEqual(len(c.latency.samples),0)
        self.assertEqual(c.contact.motion.event,"input_stall")
        self.assertEqual(len(c.contact.motion.points),0)

    def test_blocked_native_input_is_released_without_extra_hold(self):
        from unittest.mock import patch
        from fisher.windows import Windows
        clock=[0.];calls=[]
        win=Windows.__new__(Windows);win.owns_space=False
        win.foreground_matches=lambda hwnd:True
        win.key_down=lambda key:False
        def send(up=False):
            calls.append(up)
            if not up: clock[0]+=.2
        win._send=send
        with patch("fisher.windows.time.perf_counter",side_effect=lambda:clock[0]), \
             patch("fisher.windows.time.sleep",side_effect=AssertionError("不应延长阻塞后的按住时间")):
            self.assertTrue(win.tap_space(1,35,lambda:False))
        self.assertEqual(calls,[False,True])
        self.assertFalse(win.owns_space)


if __name__=="__main__": unittest.main()
