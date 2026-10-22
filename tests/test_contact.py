# SPDX-License-Identifier: GPL-3.0-only
"""接触策略在宽窄目标、双向运动、停帧和输入耗时下的行为检查。"""
import unittest
import math
from fisher.config import DEFAULTS
from fisher.contact import ContactModel, DeliveryLatency, SegmentMotion, MAX_CONTACT_WAIT
from fisher.controller import Controller
from fisher.scheduling import CaptureSchedule
from fisher.vision import Observation

# 本组保留动态估计和旧外缘基准的对照；固定比例和新入条基准另行验证。
DEFAULTS=dict(DEFAULTS,timing_mode="contact",capture_every_n_frames=6,calibrate_speed=True,contact_overlap_ratio=.1)


def observation(x, target=(172,244), clear=True):
    return Observation("TIMING", 1, x, target, 402, clear,
                       marker_left=x-5, marker_right=x+5,track_left=80,track_right=263)


class ContactTests(unittest.TestCase):
    def test_first_edge_contact_is_independent_of_target_width(self):
        for width in (2, 6, 10, 30, 72):
            for speed in (80, 160, 240):
                for direction in (-1, 1):
                    with self.subTest(width=width, speed=speed, direction=direction):
                        cfg = dict(DEFAULTS)
                        model = ContactModel(cfg, DeliveryLatency(40))
                        schedule = CaptureSchedule(cfg)
                        t, hit = 0.0, None
                        target = (172,172+width) if direction > 0 else (172-width,172)
                        start, v = (85 if direction > 0 else 254), speed*direction
                        for _ in range(40):
                            x = start+v*t
                            now = t+.003
                            d = model.step(observation(x,target), t, now)
                            if d.get("action") == "HIT":
                                hit = d
                                effect_x = start+v*(d["send_at"]+.040)
                                entry = target[0]-4 if direction > 0 else target[1]+4
                                self.assertLessEqual(abs(effect_x-entry), 1.0)
                                self.assertLessEqual(d["send_at"]-now, MAX_CONTACT_WAIT+.000001)
                                break
                            schedule.advance(t,now)
                            schedule.request_earlier(d.get("check_at"),t,now)
                            t = schedule.next_at
                        self.assertIsNotNone(hit)

    def test_next_round_keeps_velocity_and_position_history(self):
        m = ContactModel(DEFAULTS,DeliveryLatency(40))
        for t in (0,.1,.2):
            m.step(observation(100+100*t,(210,220)),t,t)
        self.assertAlmostEqual(m.motion.velocity,100)
        m.commit(.2)
        for t in (.3,.4,.5):
            d = m.step(observation(100+100*t,(211,219)),t,t)
        self.assertEqual(m.round_id,2)
        self.assertFalse(m.locked)
        self.assertEqual(m.motion.segment,0)
        self.assertAlmostEqual(d["velocity"],100)

    def test_occlusion_does_not_unlock_a_shot(self):
        m = ContactModel(DEFAULTS,DeliveryLatency(40))
        for t in (0,.1,.2):
            m.step(observation(100+100*t),t,t)
        m.commit(.2)
        for t in (.3,.4,.5,.6):
            d = m.step(observation(100+100*t,(190,210),False),t,t)
            self.assertIsNone(d.get("action"))
        self.assertTrue(m.locked)
        self.assertEqual(m.target,(172,244))

    def test_frozen_or_delayed_frame_never_creates_a_hit(self):
        for now, x, expected in ((.3,132,"freeze"),(.7,180,"gap"),(.3,190,"jump")):
            m = ContactModel(DEFAULTS,DeliveryLatency(40))
            for t in (0,.1,.2):
                m.step(observation(100+160*t),t,t)
            d = m.step(observation(x),now,now)
            self.assertIsNone(d.get("action"))
            self.assertEqual(d["motion_event"],expected)
        m = ContactModel(DEFAULTS,DeliveryLatency(40))
        for t in (0,.1):
            m.step(observation(100+160*t),t,t)
        d = m.step(observation(132),.2,.33)
        self.assertEqual(d["motion_event"],"stale")
        self.assertIsNone(d.get("action"))

    def test_unknown_frame_discards_velocity_and_click_deadline(self):
        c = Controller(DEFAULTS)
        for t in (0,.1,.2,.3):
            c.step(observation(100+100*t),t,t)
        d = c.step(Observation(),.35,.35)
        self.assertIsNone(d.action)
        self.assertIsNone(d.send_at)
        self.assertEqual(len(c.contact.motion.points),0)

    def test_rebound_rebuilds_direction_and_records_endpoint_candidates(self):
        m = SegmentMotion(.1)
        for cycle in range(3):
            m.clear("new_pass")
            for t,x in ((0,200),(.1,220),(.2,240),(.3,230),(.4,210),(.5,190)):
                m.add(cycle+t,x)
                m.estimate()
        self.assertAlmostEqual(m.velocity,-200)
        self.assertAlmostEqual(m.endpoints()["right"],245)
        self.assertNotIn("left",m.endpoints())

    def test_speed_change_relearns_without_extrapolating_old_speed(self):
        m = SegmentMotion(.1)
        for t,x in ((0,100),(.1,110),(.2,120)):
            m.add(t,x)
            m.estimate()
        m.add(.3,140)
        self.assertEqual(m.event,"speed_change")
        self.assertIsNone(m.estimate())
        m.add(.4,160)
        m.add(.5,180)
        self.assertAlmostEqual(m.estimate()[1],200)

    def test_contact_sampling_does_not_create_catch_up_bursts(self):
        s = CaptureSchedule(DEFAULTS)
        s.advance(1,1.01)
        s.request_earlier(1.012,1,1.01)
        self.assertAlmostEqual(s.next_at,1.025)
        s.advance(1.025,1.5)
        s.request_earlier(1.04,1.025,1.5)
        self.assertEqual(s.next_at,1.5)
        s.advance(1.5,1.51)
        self.assertAlmostEqual(s.next_at,1.6)

    def test_display_frame_quantization_does_not_require_perfect_adjacent_slopes(self):
        for speed in (80,160,240,400):
            c = Controller(DEFAULTS)
            schedule = CaptureSchedule(DEFAULTS)
            t, hit = 0.0, None
            for _ in range(140):
                display_time = math.floor(t*60+1e-8)/60
                phase = (speed*display_time)%340
                x = 85+phase if phase <= 170 else 425-phase
                d = c.step(observation(x,(172,182),x<160 or x>194),t,t+.003)
                if d.action:
                    hit = d
                    break
                schedule.advance(t,t+.003)
                schedule.request_earlier(d.check_at,t,t+.003)
                t = schedule.next_at
            self.assertIsNotNone(hit,speed)
            self.assertLess(abs(abs(hit.velocity)-speed),max(20,speed*.15))

    def test_stationary_scene_only_gets_limited_warmup_refinements(self):
        m = ContactModel(DEFAULTS,DeliveryLatency(40))
        refinements = 0
        for i in range(30):
            d = m.step(observation(100),i*.1,i*.1)
            refinements += d.get("check_at") is not None
            self.assertIsNone(d.get("action"))
        self.assertEqual(refinements,4)


class LatencyTests(unittest.TestCase):
    def test_adapts_slowly_and_rejects_stalls(self):
        m = DeliveryLatency(40)
        prior = 0
        for _ in range(24):
            m.observe(1,1.008)
            self.assertLessEqual(abs(m.host_ms-prior),2)
            prior = m.host_ms
        self.assertAlmostEqual(m.total_ms,48,delta=.05)
        for value in (1.3, .9, 1.040):
            m.observe(1,value)
        self.assertEqual(m.rejected,3)
        self.assertEqual(m.host_ms,prior)

    def test_host_compensation_is_bounded_and_optional(self):
        m = DeliveryLatency(40)
        for _ in range(100): m.observe(1,1.045)
        self.assertLessEqual(m.host_ms,30)
        off = DeliveryLatency(40,False)
        off.observe(1,1.01)
        self.assertEqual(off.total_ms,40)


if __name__ == "__main__": unittest.main()
