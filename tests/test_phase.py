# SPDX-License-Identifier: GPL-3.0-only
"""稀疏观测、连续折返和跨截图期限的闭环回归。"""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from fisher import TRIAL_BUILD
from fisher.config import DEFAULTS, capture_interval, load_config, hotkey_vk
from fisher.contact import DeliveryLatency
from fisher.controller import Controller
from fisher.phase import PhaseClock, PhaseModel, LEFT_END, RIGHT_END
from fisher.runtime import Runtime
from fisher.speed import SpeedCalibration
from fisher.vision import Observation


def observation(x, target=(170,180), scale=1., clear=True):
    return Observation("TIMING",1.,x*scale,tuple(y*scale for y in target),402,clear,
                       marker_left=(x-5)*scale, marker_right=(x+5)*scale,
                       track_left=79*scale, track_right=263*scale)


class PhaseTests(unittest.TestCase):
    def test_direction_and_position_through_many_bounces_without_frames(self):
        c=PhaseClock(.5,0,1);c.observe(0,.8)
        for t,x,direction in ((0,.8,1),(.4,1,-1),(1.4,.5,-1),(2.4,0,1),
                               (3.4,.5,1),(4.4,1,-1),(10.4,0,1)):
            self.assertAlmostEqual(c.position_at(t),x)
            self.assertEqual(c.direction_at(t),direction)

    def test_sparse_observations_do_not_confuse_bounce_with_direct_direction(self):
        truth=PhaseClock(.606);truth.observe(0,.8)
        clock=PhaseClock(.606)
        for i in range(40):
            t=i*.5;x=truth.position_at(t)
            clock.observe(t,x)
            self.assertEqual(clock.direction_at(t),truth.direction_at(t))
            self.assertAlmostEqual(clock.position_at(t+.3),truth.position_at(t+.3))

    def test_both_directions_and_rebound_arrivals(self):
        c=PhaseClock(.5,0,1);c.observe(0,.8)
        arrival,direction,turns=c.next_visit(.3,0)
        self.assertAlmostEqual(arrival,1.8)
        self.assertEqual((direction,turns),(-1,1))
        arrival,direction,turns=c.next_visit(.3,2.0)
        self.assertAlmostEqual(arrival,3.0)
        self.assertEqual((direction,turns),(1,1))
        arrival,direction,turns=c.next_visit(.9,0)
        self.assertAlmostEqual(arrival,.2)
        self.assertEqual((direction,turns),(1,0))

    def test_first_observation_inside_clicks_without_three_point_fit(self):
        c=Controller(DEFAULTS)
        d=c.step(observation(175),0,.02)
        self.assertEqual(d.action,"HIT")
        self.assertEqual(d.send_at,.02)
        self.assertEqual(d.click_mode,"inside")

    def test_single_frame_can_schedule_between_half_second_captures(self):
        model=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
        d=model.step(observation(120),0,.02)
        expected=((175-120)/(184*.606))-.040
        self.assertEqual(d["action"],"HIT")
        self.assertAlmostEqual(d["send_at"],expected)
        self.assertLess(d["send_at"],.5)
        self.assertNotIn("check_at",d)

    def test_normalized_geometry_has_same_deadline_at_all_scales(self):
        times=[]
        for scale in (.5,1,2.7):
            model=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
            times.append(model.step(observation(205,(130,132),scale),0,.02)["send_at"])
        self.assertAlmostEqual(min(times),max(times))

    def test_narrow_target_never_rejected_due_to_safety_margin(self):
        for width in (1,2,6,40,90):
            model=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
            d=model.step(observation(120,(170,170+width)),0,.02)
            self.assertEqual(d["action"],"HIT")
            self.assertTrue(170 <= d["predicted_x"] <= 170+width)

    def test_same_pass_locked_next_pass_retries_without_target_change(self):
        model=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
        d=model.step(observation(172),0,0);model.commit(0)
        self.assertIsNone(model.step(observation(175),.025,.025).get("action"))
        truth=PhaseClock(.606);truth.observe(0,(172-79)/184)
        # 遮住期间仍然通过相位判断已经离开，后续经过不永久锁死。
        t=2.5
        obs=observation(79+184*truth.position_at(t))
        d=model.step(obs,t,t+.02)
        self.assertEqual(d.get("action"),"HIT")
        self.assertLess(d["send_at"],t+2/.606)

    def test_next_round_keeps_phase_and_accepts_new_target(self):
        m=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
        m.step(observation(175),0,0);m.commit(0)
        p=m.clock.phase_at(.5)
        d=m.step(observation(175+184*.606*.5,(225,235)),.5,.52)
        self.assertAlmostEqual(m.clock.phase_at(.5),p)
        self.assertEqual(d["action"],"HIT")
        self.assertEqual(m.round_id,2)

    def test_occlusion_keeps_full_known_target(self):
        m=PhaseModel(DEFAULTS,DeliveryLatency(40),SpeedCalibration())
        m.step(observation(120,(160,180)),0,.02)
        d=m.step(observation(170,(176,180),clear=False),.5,.52)
        self.assertEqual(m.target,(160.,180.))
        self.assertEqual(d["action"],"HIT")
        obs=observation(175);obs.target=None
        self.assertEqual(m.step(obs,1,1.02)["action"],"HIT")

    def test_migration_enables_phase_preserving_custom_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"config.json"
            old=dict(DEFAULTS,config_version=5,timing_mode="contact",capture_every_n_frames=6,
                     unknown_timeout_s=240,continuous_fishing=True,toggle_hotkey="F6")
            p.write_text(json.dumps(old))
            new=load_config(p)
            self.assertEqual((new["timing_mode"],capture_interval(new)),("phase",.1))
            self.assertEqual(new["unknown_timeout_s"],240)
            self.assertEqual(new["toggle_hotkey"],"F6")
            self.assertTrue(new["continuous_fishing"])
            self.assertEqual(json.loads(p.read_text()),old)

    def test_sparse_calibration_rejects_freezes_and_outliers(self):
        speed=SpeedCalibration(enabled=True)
        for i in range(35):
            speed.offer_sparse(0 if i%7==0 else .606+(i%3-1)*.002,1 if i%2 else -1,i//4,i,-10)
        self.assertTrue(speed.completed)
        self.assertLess(abs(speed.value-.606),.003)
        self.assertGreater(speed.rejected,0)
        frozen=SpeedCalibration(enabled=True)
        for i in range(80): frozen.offer_sparse(0,1,i,i,-10)
        self.assertFalse(frozen.completed)
        self.assertEqual(frozen.value,.606)


class SimulatedRuntime:
    """完整Runtime循环：虚拟时间、真实状态机，输入回执只供断言。"""
    def __init__(self, *, targets=((170,172),), start=.25, duration=7,
                 pause_at=None, focus_at=None, unknown_at=None, resize_at=None,
                 dry=False, calibrate=False, capture_frames=30):
        self.t=.001;self.duration=duration;self.pause_at=pause_at;self.focus_at=focus_at
        self.unknown_at=unknown_at;self.resize_at=resize_at
        self.grabs=[];self.hits=[];self.targets=targets;self.index=0;self.effects=[]
        self.truth=PhaseClock(.606);self.truth.observe(0,start)
        self.cfg=dict(DEFAULTS,continuous_fishing=True,dry_run=dry,calibrate_speed=calibrate,
                      capture_every_n_frames=capture_frames)
        sim=self
        class Stop:
            def is_set(self):return sim.t>=sim.duration
            def wait(self,seconds):sim.t+=max(.000001,seconds)
            def set(self):sim.t=sim.duration
        class Window:
            interrupted=False;owns_space=False
            last_key_down_at=None;last_send_begin=None;last_send_end=None
            def client_box(self,hwnd):
                w=337 if sim.resize_at is not None and sim.t>=sim.resize_at else 336
                return 0,0,w,598
            def foreground_matches(self,hwnd):return sim.focus_at is None or sim.t<sim.focus_at
            def key_down(self,key):return key==hotkey_vk("F9") and sim.pause_at is not None and sim.t>=sim.pause_at
            def release(self):pass
            def tap_space(self,hwnd,hold,cancel,guard):
                if cancel() or not guard():return False
                self.last_send_begin=self.last_send_end=self.last_key_down_at=sim.t
                if sim.index<len(sim.targets):
                    target=sim.targets[sim.index];x=79+184*sim.truth.position_at(sim.t+.040)
                    success=target[0]-1e-5 <= x <= target[1]+1e-5
                    sim.hits.append((sim.t,x,target,success))
                    if success:sim.effects.append((sim.t+.040,sim.index+1))
                sim.t+=hold/1000
                return True
        class Shot:
            width,height=336,598
            bgra=bytes(336*598*4)
        class Screen:
            monitors=[{"left":0,"top":0,"width":1920,"height":1080}]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def grab(self,box):
                sim.grabs.append(sim.t);sim.frame_time=sim.t+.001;sim.t+=.002
                return Shot()
        class Detector:
            def __init__(self,*args):pass
            def detect(self,raw):
                at=sim.frame_time
                while sim.effects and sim.effects[0][0]<=at:
                    _,sim.index=sim.effects.pop(0)
                sim.t+=.018
                if sim.unknown_at is not None and at>=sim.unknown_at:
                    obs=Observation()
                elif sim.index==len(sim.targets):
                    obs=Observation("WAIT",1.)
                else:
                    obs=observation(79+184*sim.truth.position_at(at),sim.targets[sim.index])
                return obs,np.zeros((598,336,3),np.uint8)
        self.window=Window();self.Screen=Screen;self.Detector=Detector;self.Stop=Stop

    def run(self):
        r=Runtime(self.window,1,{"client_size":[336,598],"crop":[0,0,336,598]},self.cfg)
        self.runtime=r;r.stop=self.Stop();r.command("toggle")
        messages=[]
        original=r.emit
        def emit(kind,data):messages.append((kind,data));original(kind,data)
        r.emit=emit
        with tempfile.TemporaryDirectory() as tmp, \
             patch("fisher.runtime.DATA_ROOT",Path(tmp)), \
             patch("fisher.runtime.time.perf_counter",lambda:self.t), \
             patch("fisher.runtime.mss.mss",self.Screen), \
             patch("fisher.runtime.Detector",self.Detector), \
             patch("fisher.runtime.annotate") as draw:
            r.run();draw.assert_not_called()
            self.files=[p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*") if p.is_file()]
        self.messages=messages
        return self


class PhaseRuntimeTests(unittest.TestCase):
    def test_ten_fps_runtime_handles_rebounds_and_multiple_targets(self):
        s=SimulatedRuntime(start=.85,targets=((127,147),(203,211),(145,147)),
                           duration=9,capture_frames=6).run()
        self.assertEqual(len(s.hits),3,s.hits)
        self.assertTrue(all(h[3] for h in s.hits),s.hits)
        self.assertTrue(all(b-a>=.1-1e-8 for a,b in zip(s.grabs,s.grabs[1:])))
        self.assertFalse(s.files)

    def test_ten_fps_calibration_uses_longer_intervals_and_only_saves_summary(self):
        s=SimulatedRuntime(calibrate=True,dry=True,duration=35,capture_frames=6).run()
        self.assertTrue(s.runtime.calibration_complete)
        self.assertEqual(s.files,["speed_profile.json"])
        self.assertLessEqual(len(s.grabs),350)
        self.assertTrue(all(b-a>=.1-1e-8 for a,b in zip(s.grabs,s.grabs[1:])))

    def test_click_occurs_between_two_fps_frames_with_old_image(self):
        s=SimulatedRuntime(start=.25,targets=((170,172),)).run()
        self.assertEqual(len(s.hits),1)
        self.assertTrue(s.hits[0][3],s.hits)
        t=s.hits[0][0]
        self.assertGreater(t-max(x for x in s.grabs if x<=t),.120)
        self.assertTrue(all(b-a>=.5-1e-8 for a,b in zip(s.grabs,s.grabs[1:])))
        self.assertFalse(s.files)
        self.assertFalse(TRIAL_BUILD)

    def test_rebound_and_three_changing_targets_continue_from_current_position(self):
        s=SimulatedRuntime(start=.85,targets=((127,147),(203,211),(145,147)),duration=9).run()
        self.assertEqual(len(s.hits),3,s.hits)
        self.assertTrue(all(h[3] for h in s.hits),s.hits)
        self.assertTrue(all(b-a>=.5-1e-8 for a,b in zip(s.grabs,s.grabs[1:])))

    def test_pause_focus_unknown_and_resize_cancel_pending_input(self):
        for field,value in (("pause_at",.15),("focus_at",.15),("unknown_at",.50),("resize_at",.15)):
            with self.subTest(field=field):
                s=SimulatedRuntime(start=.85,targets=((127,129),),duration=3,**{field:value}).run()
                self.assertEqual(s.hits,[])
                self.assertIsNone(s.runtime.pending_hit)
                self.assertFalse(s.files)

    def test_dry_run_never_sends(self):
        s=SimulatedRuntime(dry=True).run()
        self.assertEqual(s.hits,[])
        self.assertFalse(s.files)

    def test_calibration_does_not_increase_capture_rate(self):
        s=SimulatedRuntime(calibrate=True,dry=True,duration=8).run()
        self.assertLessEqual(len(s.grabs),16)
        self.assertTrue(all(b-a>=.5-1e-8 for a,b in zip(s.grabs,s.grabs[1:])))
        self.assertFalse(any("trial" in f or "logs" in f for f in s.files))


if __name__=="__main__":unittest.main()
