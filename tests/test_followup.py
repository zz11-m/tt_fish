# SPDX-License-Identifier: GPL-3.0-only
"""端点遮挡、未确认输入后的有界重试及诊断关联回归。"""
import json
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
import cv2
import numpy as np
from fisher.config import DEFAULTS, ROOT
from fisher.contact import ContactModel, DeliveryLatency, MAX_CONTACT_WAIT, MAX_TRACK_AGE
from fisher.controller import Controller, Decision
from fisher.runtime import Runtime
from fisher.scheduling import CaptureSchedule
from fisher.trial_capture import TrialRecorder
from fisher.vision import Detector, Observation
from test_contact import observation
from test_diagnostics import real_bar_frame
from test_trial import Window

# 保留处理耗时与首次外缘接触的旧基准对照，新默认基准见test_speed。
DEFAULTS=dict(DEFAULTS,timing_mode="contact",capture_every_n_frames=6,calibrate_speed=True,contact_overlap_ratio=.1)


class FollowupTests(unittest.TestCase):
    def test_right_anchor_occlusion_recovers_with_left_but_not_without_both(self):
        detector = Detector(ROOT/"assets/templates")
        frame = real_bar_frame("timing_right_approach.png")
        # 在实际条形局部图上遮住整个右端模板及其搜索范围。
        frame[382:423,241:270] = (20,20,20)
        with patch.object(detector,"match",wraps=detector.match) as match:
            obs,_ = detector.detect(frame)
        self.assertEqual(obs.state,"TIMING")
        self.assertLess(obs.scores["cap"],detector.threshold)
        self.assertGreaterEqual(obs.scores["cap_left"],detector.threshold)
        self.assertEqual(match.call_count,2)
        frame[382:423,73:108] = (20,20,20)
        self.assertEqual(detector.detect(frame)[0].state,"UNKNOWN")

    def test_effect_crop_does_not_invent_a_marker(self):
        obs,_ = Detector(ROOT/"assets/templates").detect(real_bar_frame("timing_hit_effect.png"))
        self.assertIsNone(obs.marker)
        model = ContactModel(DEFAULTS,DeliveryLatency(40))
        model.target=(105,178)
        model.commit(0)
        for t in (.1,.2,.3):
            self.assertIsNone(model.step(obs,t,t).get("action"))
        self.assertTrue(model.locked)

    def test_ordinary_processing_cost_allows_entry_or_safe_inside_fallback_both_directions(self):
        # 实测常规截图年龄约25~30ms，不能只用3ms的理想模型检验采样时序。
        for direction in (1,-1):
            for age in (.025,.030,.045):
                model = ContactModel(DEFAULTS,DeliveryLatency(40))
                schedule = CaptureSchedule(DEFAULTS)
                t=0.0
                hit=None
                speed=111*direction
                start=90 if direction>0 else 252
                target=(160,190)
                for _ in range(40):
                    x=start+speed*t
                    model.observe_processing(age*1000)
                    d=model.step(observation(x,target),t,t+age)
                    if d.get("action"):
                        hit=d
                        effect=start+speed*(d["send_at"]+.04)
                        expected=156 if direction>0 else 194
                        if d["click_mode"]=="entry":
                            self.assertLessEqual(abs(effect-expected),1.01)
                        else:
                            self.assertEqual(d["click_mode"],"inside")
                            self.assertTrue(target[0]<effect<target[1])
                        self.assertLessEqual(d["send_at"]-(t+age),MAX_CONTACT_WAIT+1e-6)
                        self.assertLessEqual(d["send_at"]-t,MAX_TRACK_AGE)
                        break
                    schedule.advance(t-.008,t+age)
                    schedule.request_earlier(d.get("check_at"),t-.008,t+age)
                    t=schedule.next_at+.008
                self.assertIsNotNone(hit,(direction,age))

    def test_unchanged_target_gets_at_most_two_new_pass_retries_without_six_second_pause(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        schedule=CaptureSchedule(DEFAULTS)
        t=0.0
        hits=[]
        while t<14:
            phase=(111*t)%312
            x=94+phase if phase<=156 else 406-phase
            d=model.step(observation(x,(148,221),x<136 or x>233),t,t+.025)
            self.assertFalse(d.get("pause",False))
            if d.get("action"):
                hits.append(d["send_at"])
                model.commit(d["send_at"])
            schedule.advance(t,t+.025)
            schedule.request_earlier(d.get("check_at"),t,t+.025)
            t=schedule.next_at
        self.assertEqual(len(hits),3)
        self.assertEqual(model.retry_count,2)
        self.assertTrue(model.locked)
        self.assertTrue(all(b-a>.8 for a,b in zip(hits,hits[1:])))

    def test_stale_frame_neither_unlocks_nor_changes_target(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for t in (0,.1,.2): model.step(observation(100+t*100),t,t)
        model.commit(.2)
        for t in (.3,.4):
            d=model.step(observation(100+t*100,(210,225)),t,t+.5)
            self.assertEqual(d["motion_event"],"stale")
        self.assertEqual(model.target,(172,244))
        self.assertTrue(model.locked)
        self.assertEqual(model.round_id,1)

    def test_observed_target_change_without_program_hit_is_not_lost(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for t in (0,.1,.2): model.step(observation(100+100*t),t,t)
        for t in (.3,.4): d=model.step(observation(100+100*t,(210,225)),t,t)
        self.assertEqual(d["feedback"],"target_changed")
        self.assertEqual(model.round_id,2)
        self.assertEqual(model.retry_count,0)
        self.assertFalse(model.locked)

    def test_feedback_sampling_is_bounded_and_no_catchup_after_stall(self):
        runtime=Runtime(Window(),1,{},DEFAULTS)
        schedule=CaptureSchedule(DEFAULTS)
        runtime.feedback_samples=8
        runtime.feedback_until=.65
        for i in range(8):
            t=i*.05
            schedule.advance(t,t+.03)
            runtime.schedule_feedback(schedule,t,t+.03)
            self.assertAlmostEqual(schedule.next_at,t+.05)
        schedule.advance(.4,.43)
        runtime.schedule_feedback(schedule,.4,.43)
        self.assertAlmostEqual(schedule.next_at,.5)
        runtime.feedback_samples=8
        schedule.advance(.5,1.0)
        runtime.schedule_feedback(schedule,.5,1.0)
        self.assertEqual(schedule.next_at,1.0)
        self.assertEqual(runtime.feedback_samples,0)

    def test_trace_links_input_followup_external_space_and_specific_end_reason(self):
        win=Window()
        runtime=Runtime(win,1,{},DEFAULTS)
        runtime.active=True
        with tempfile.TemporaryDirectory() as tmp:
            trace=runtime.trial=TrialRecorder(tmp)
            trace.start(DEFAULTS)
            controller=Controller(DEFAULTS)
            now=time.perf_counter()
            frame=np.zeros((598,336,3),np.uint8)
            trace.frame(observation(100),Decision(),controller,frame,now,now+.02,10,15)
            win.last_key_down_at=now+.04
            win.last_key_up_at=now+.075
            trace.input("HIT",now+.04,win,True)
            win.down={0x20}
            runtime.poll_trial_space(now+.08)  # 自己的输入刚刚释放，不记为外部按键。
            runtime.poll_trial_space(now+.2)
            runtime.poll_trial_space(now+.21)
            win.down=set()
            runtime.poll_trial_space(now+.25)
            trace.frame(Observation(),Decision(),controller,frame,now+.3,now+.32,10,15)
            trace.finish("pause","timing_timeout")
            trace.join()
            with zipfile.ZipFile(trace.last_bundle) as z:
                rows=[json.loads(s) for s in z.read("events_000.jsonl").splitlines()]
                meta=json.loads(z.read("meta.json"))
                self.assertEqual(meta["schema_version"],5)
                self.assertEqual(meta["external_space_presses"],1)
                self.assertEqual(rows[1]["frame_seq"],0)
                self.assertEqual([r["down"] for r in rows if r["kind"]=="external_space"],[True,False])
                self.assertEqual(rows[-2]["last_hit_seq"],1)
                self.assertAlmostEqual(rows[-2]["since_hit_ms"],260,places=3)
                self.assertEqual(rows[-1]["reason_code"],"timing_timeout")
                self.assertIn("crops/frame_00000004.png",z.namelist())


if __name__=="__main__": unittest.main()
