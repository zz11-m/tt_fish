# SPDX-License-Identifier: GPL-3.0-only
"""比例速度、校准离群值、跨启动保存与入条修正回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
from fisher.config import DEFAULTS, ROOT, save_config, load_config, validate_config
from fisher.contact import ContactModel, DeliveryLatency, SegmentMotion
from fisher.controller import Controller
from fisher.scheduling import CaptureSchedule
from fisher.speed import SpeedCalibration, load_profile, save_profile, reference_profile
from fisher.runtime import Runtime
from fisher.vision import Observation, Detector
from test_diagnostics import real_bar_frame
from test_trial import Window

# 历史策略回归显式使用其原始配置；正式默认策略见test_phase。
DEFAULTS = dict(DEFAULTS,timing_mode="contact",capture_every_n_frames=6)


def sample(x,width=183,target=(160,200)):
    return Observation("TIMING",1,x,target,402,True,marker_left=x-width*.05,
                       marker_right=x+width*.05,track_left=80,track_right=80+width)


def feed_window(model,i,rate,direction=1,error=0,stall=False):
    step=.075 if not stall else .25
    t=i*1.2
    width=183
    points=[(t+j*step,100+rate*width*j*step*direction) for j in range(5)]
    model.offer(points,rate*width*direction,error,width,i//4,t+step*4,-100)


class SpeedTests(unittest.TestCase):
    def test_fixed_speed_scales_with_track_and_does_not_fit_dynamic_slopes(self):
        for width in (120,183,240):
            model=ContactModel(DEFAULTS,DeliveryLatency(40))
            with patch.object(SegmentMotion,"estimate",side_effect=AssertionError("不应运行动态回归")):
                for i in range(5):
                    t=i*.1
                    d=model.step(sample(90+DEFAULTS["speed_fraction_per_s"]*width*t,width),t,t+.02)
            self.assertAlmostEqual(d["velocity"]/width,DEFAULTS["speed_fraction_per_s"])
            self.assertEqual(len(model.speed.samples),0)

    def test_fixed_prediction_does_not_accept_persistent_wrong_speed(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for i in range(12):
            t=i*.1
            d=model.step(sample(90+170*t),t,t+.02)
            self.assertIsNone(d.get("action"))
        self.assertEqual(model.speed.value,DEFAULTS["speed_fraction_per_s"])

    def test_logged_observation_is_distinct_from_fixed_prediction_speed(self):
        model=ContactModel(DEFAULTS,DeliveryLatency(40))
        for i in range(5):
            t=i*.1
            d=model.step(sample(100+.59*183*t),t,t+.01)
        self.assertAlmostEqual(model.motion.observed_velocity/183,.59)
        self.assertAlmostEqual(d["velocity"]/183,.606)
        self.assertEqual(model.motion.estimate_source,"fixed")

    def test_calibration_discards_outliers_and_requires_both_directions(self):
        model=SpeedCalibration(.606,True)
        for i in range(35):
            rate=1.7 if i in (1,7,12,18) else .68+(i%3-1)*.003
            feed_window(model,i,rate,1 if i%8<4 else -1)
        self.assertTrue(model.completed)
        self.assertFalse(model.calibrating)
        self.assertAlmostEqual(model.value,.68,delta=.004)
        self.assertGreaterEqual(model.inliers,18)
        unilateral=SpeedCalibration(.606,True)
        for i in range(40): feed_window(unilateral,i,.68,1)
        self.assertFalse(unilateral.completed)
        self.assertEqual(unilateral.value,.606)

    def test_stalls_freezes_and_large_residuals_do_not_calibrate(self):
        model=SpeedCalibration(.606,True)
        for i in range(30):
            feed_window(model,i,0 if i%3==0 else .68,-1 if i%2 else 1,
                        error=8 if i%3==1 else 0,stall=i%3==2)
        self.assertFalse(model.completed)
        self.assertEqual(len(model.samples),0)
        self.assertEqual(model.value,.606)

    def test_short_overlapping_windows_cannot_inflate_sample_count(self):
        model=SpeedCalibration(.606,True)
        for i in range(40):
            t=i*.02
            pts=[(t+j*.075,100+j*.075*110.9) for j in range(5)]
            model.offer(pts,110.9,0,183,i,t+.3,-100)
        self.assertLessEqual(len(model.samples),3)
        self.assertFalse(model.completed)

    def test_pause_reset_preserves_speed_and_atomic_profile_survives_new_controller(self):
        c=Controller(dict(DEFAULTS,calibrate_speed=True))
        for i in range(35): feed_window(c.speed,i,.68,1 if i%8<4 else -1)
        self.assertTrue(c.speed.completed)
        speed=c.speed
        c.reset(100)
        self.assertIs(c.speed,speed)
        self.assertAlmostEqual(c.contact.speed.value,.68)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"speed_profile.json"
            runtime=Runtime(Window(),1,{},DEFAULTS)
            runtime.controller=c
            with patch("fisher.runtime.DATA_ROOT",Path(tmp)):
                runtime.persist_calibration()
            self.assertTrue(speed.saved)
            profile=load_profile(path)
            new=Controller(dict(DEFAULTS,speed_fraction_per_s=profile["fraction_per_second"]))
            self.assertAlmostEqual(new.speed.value,.68)
            self.assertFalse(new.speed.calibrating)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_invalid_profiles_and_config_do_not_write_unchecked_speed(self):
        for value in (True,0,float("nan"),99):
            with self.assertRaises(ValueError):
                validate_config(dict(DEFAULTS,speed_fraction_per_s=value))
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    save_profile(Path(tmp)/"speed_profile.json",dict(reference_profile(),fraction_per_second=value))
        with tempfile.TemporaryDirectory() as tmp, patch("fisher.config.DATA_ROOT",Path(tmp)):
            save_config(dict(DEFAULTS,calibrate_speed=True))
            self.assertFalse(load_config()["calibrate_speed"])

    def test_trigger_position_uses_overlap_and_stays_inside_narrow_target(self):
        for direction in (-1,1):
            for width in (2,6,18,72):
                model=ContactModel(DEFAULTS,DeliveryLatency(40))
                schedule=CaptureSchedule(DEFAULTS)
                t=0.0
                target=(160,160+width)
                start=90 if direction>0 else 260
                speed=DEFAULTS["speed_fraction_per_s"]*183*direction
                hit=None
                for _ in range(60):
                    d=model.step(sample(start+speed*t,target=target),t,t+.025)
                    if d.get("action"):
                        hit=d
                        position=start+speed*(d["send_at"]+.04)
                        self.assertGreaterEqual(position,target[0]-.01)
                        self.assertLessEqual(position,target[1]+.01)
                        break
                    schedule.advance(t,t+.025)
                    schedule.request_earlier(d.get("check_at"),t,t+.025)
                    t=schedule.next_at
                self.assertIsNotNone(hit,(direction,width))

    def test_actual_bar_scale_and_aspect_normalization_preserve_fraction(self):
        detector=Detector(ROOT/"assets/templates")
        original=real_bar_frame("timing_right_approach.png")
        for w,h in ((336,598),(672,1196),(500,1000),(640,900)):
            obs,_=detector.detect(cv2.resize(original,(w,h)))
            self.assertEqual(obs.state,"TIMING")
            self.assertAlmostEqual(obs.track_right-obs.track_left,183,delta=2)

    def test_missing_track_freeze_and_gap_never_use_cached_speed_blindly(self):
        for event in ("track","freeze","gap"):
            model=ContactModel(DEFAULTS,DeliveryLatency(40))
            for t in (0,.1,.2):model.step(sample(100+110.898*t),t,t+.01)
            obs=sample(122.1796)
            if event=="track":obs.track_left=None
            t=.6 if event=="gap" else .3
            d=model.step(obs,t,t+.01)
            self.assertIsNone(d.get("action"))
            self.assertEqual(len(model.motion.points),0 if event=="track" else 1)

    def test_runtime_calibration_saves_then_next_start_uses_fixed_value(self):
        import numpy as np
        from fisher.config import hotkey_vk
        clock=[.001]
        class Stop:
            flag=False
            def is_set(self):return self.flag or clock[0]>=31
            def wait(self,delay):clock[0]+=max(delay,.00001)
            def set(self):self.flag=True
        class Win(Window):
            def key_down(self,key):
                return ((key==hotkey_vk("F8") and .03<=clock[0]<.08) or
                        (key==hotkey_vk("F9") and 30<=clock[0]<30.05))
        pixels=np.zeros((598,336,4),np.uint8).tobytes()
        class Shot:
            width,height=336,598
            bgra=pixels
        class Screen:
            monitors=[{"left":0,"top":0,"width":1920,"height":1080}]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def grab(self,box):clock[0]+=.003;return Shot()
        class Scene:
            def __init__(self,*args):pass
            def detect(self,raw):
                phase=(.68*183*(clock[0]-.0015))%316
                x=94+phase if phase<=158 else 410-phase
                obs=sample(x)
                obs.geometry_clear=x<148 or x>212
                clock[0]+=.004
                return obs,np.zeros((598,336,3),np.uint8)
        with tempfile.TemporaryDirectory() as tmp, patch("fisher.runtime.DATA_ROOT",Path(tmp)), \
             patch("fisher.runtime.time.perf_counter",lambda:clock[0]), \
             patch("fisher.runtime.mss.mss",Screen),patch("fisher.runtime.Detector",Scene):
            runtime=Runtime(Win(),1,{"client_size":[336,598],"crop":[0,0,336,598]},
                            dict(DEFAULTS,calibrate_speed=True,dry_run=True))
            runtime.stop=Stop()
            runtime.run()
            self.assertTrue(runtime.controller.speed.saved)
            self.assertAlmostEqual(load_profile(Path(tmp)/"speed_profile.json")["fraction_per_second"],.68,delta=.003)
            again=Runtime(Win(),1,{},dict(DEFAULTS,calibrate_speed=False))
            again.stop.set()
            again.run()
            self.assertAlmostEqual(again.controller.speed.value,.68,delta=.003)
            self.assertFalse(again.controller.speed.calibrating)
            self.assertEqual(len(again.controller.speed.samples),0)


if __name__=="__main__": unittest.main()
