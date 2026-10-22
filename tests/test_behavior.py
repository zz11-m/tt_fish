# SPDX-License-Identifier: GPL-3.0-only
"""覆盖识别组件与会导致误按键的状态边界；不发送系统输入。"""
import unittest
import cv2
import numpy as np
from fisher.config import ROOT, DEFAULTS
from fisher.vision import Detector, Observation, read_image
from fisher.controller import Controller
from fixtures import make_frame

DEFAULTS = dict(DEFAULTS, timing_mode="center", continuous_fishing=False)

class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector(ROOT/"assets/templates")

    def test_five_synthetic_scenes(self):
        for i,state in enumerate(["IDLE","WAIT","BITE","TIMING","RESULT"],1):
            with self.subTest(i=i):
                obs,_ = self.detector.detect(make_frame(state))
                self.assertEqual(obs.state,state)

    def test_resizing_and_jpeg(self):
        for i,state in enumerate(["IDLE","WAIT","BITE","TIMING","RESULT"],1):
            src = make_frame(state)
            for size in ((270,480),(540,960),(720,1280)):
                im = cv2.resize(src,size)
                ok,b = cv2.imencode(".jpg",im,[cv2.IMWRITE_JPEG_QUALITY,85])
                obs,_ = self.detector.detect(cv2.imdecode(b,cv2.IMREAD_COLOR))
                self.assertEqual(obs.state,state,(i,size))

    def test_blank_and_unrelated_frame_do_not_request_action(self):
        rng = np.random.default_rng(42)
        for im in (np.zeros((598,336,3),np.uint8), np.full((598,336,3),255,np.uint8),rng.integers(0,256,(598,336,3),dtype=np.uint8)):
            self.assertEqual(self.detector.detect(im)[0].state,"UNKNOWN")

    def test_timing_marker_and_target(self):
        obs,_ = self.detector.detect(make_frame("TIMING"))
        self.assertTrue(98 <= obs.marker <= 107)
        self.assertAlmostEqual(obs.target[0],172,delta=2)
        self.assertAlmostEqual(obs.target[1],244,delta=2)

    def test_countdown_bar_is_not_timing(self):
        obs,_ = self.detector.detect(make_frame("BITE"))
        self.assertEqual(obs.state,"BITE")
        self.assertIsNone(obs.target)

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.c = Controller(dict(DEFAULTS))
        self.c.reset(0)

    def known(self,state,t):
        self.c.step(Observation(state,1),t)
        return self.c.step(Observation(state,1),t+.02)

    def test_single_cast_and_ack_timeout(self):
        self.known("IDLE",0)
        d = self.c.step(Observation("IDLE",1),.5)
        self.assertEqual(d.action,"CAST")
        self.c.commit(d.action,.5)
        self.assertIsNone(self.c.step(Observation("IDLE",1),.8).action)
        self.assertTrue(self.c.step(Observation("IDLE",1),6).pause)

    def test_wait_never_presses(self):
        self.known("WAIT",0)
        for t in (1,20,100): self.assertIsNone(self.c.step(Observation("WAIT",1),t).action)
        self.assertTrue(self.c.step(Observation("WAIT",1),121).pause)

    def test_bite_once(self):
        d = self.known("BITE",0)
        self.assertEqual(d.action,"HOOK")
        self.c.commit(d.action,.02)
        self.assertIsNone(self.c.step(Observation("BITE",1),.5).action)

    def test_skip_timing_and_result_delay(self):
        d = self.known("BITE",0)
        self.c.commit(d.action,.02)
        self.known("RESULT",1)
        self.assertIsNone(self.c.step(Observation("RESULT",1),3).action)
        self.assertEqual(self.c.step(Observation("RESULT",1),5).action,"CLOSE")

    def test_result_retries_continue_past_old_limit(self):
        self.known("RESULT",0)
        for t in (4,7,10):
            d = self.c.step(Observation("RESULT",1),t)
            self.assertEqual(d.action,"CLOSE")
            self.c.commit(d.action,t)
        d = self.c.step(Observation("RESULT",1),13)
        self.assertFalse(d.pause)
        self.assertEqual(d.action,"CLOSE")

    def test_unknown_never_uses_previous_actionable_state(self):
        self.known("IDLE",0)
        for t in (.4,1,2):
            d = self.c.step(Observation(),t)
            self.assertIsNone(d.action)
        self.assertTrue(self.c.step(Observation(),self.c.cfg["unknown_timeout_s"]+1).pause)

    def test_no_press_on_one_frame_bite(self):
        self.assertIsNone(self.c.step(Observation("BITE",1),1).action)
        self.assertIsNone(self.c.step(Observation(),1.02).action)

    def test_missing_marker_is_not_a_press(self):
        self.known("TIMING",0)
        d = self.c.step(Observation("TIMING",1,target=(170,240)),.1)
        self.assertIsNone(d.action)

    def drive_timing(self, start=0, target=(172,244), origin=100, speed=200, count=50):
        hits=[]
        for i in range(count):
            t=start+i/60
            x=origin+speed*i/60
            obs=Observation("TIMING",1,x,target,402, x<target[0]-12 or x>target[1]+12)
            d=self.c.step(obs,t)
            if d.action:
                hits.append((t,d))
                self.c.commit(d.action,t)
        return hits

    def test_predicts_and_latches_same_round(self):
        hits=self.drive_timing()
        self.assertEqual(len(hits),1)
        self.assertGreater(hits[0][1].predicted_x,172)
        self.assertLess(hits[0][1].predicted_x,244)

    def test_reverse_direction_can_hit(self):
        hits=self.drive_timing(origin=265,speed=-200)
        self.assertEqual(len(hits),1)

    def test_shrinking_target_rearms_next_round(self):
        self.assertEqual(len(self.drive_timing(count=35)),1)
        hits=self.drive_timing(start=.8,target=(188,226),count=40)
        self.assertEqual(len(hits),1)

    def test_too_narrow_target_does_not_blindly_press(self):
        self.assertEqual(self.drive_timing(target=(200,204)),[])

    def test_brief_occlusion_does_not_unlock_old_round(self):
        self.assertEqual(len(self.drive_timing(count=35)),1)
        self.c.step(Observation(),.7)
        self.c.step(Observation(),.9)
        self.known("TIMING",1.0)
        self.c.step(Observation("TIMING",1,230,(172,244),402,False),1.05)
        self.assertTrue(self.c.shot_locked)

    def test_reset_clears_previous_target_before_next_hit(self):
        self.assertEqual(len(self.drive_timing(count=35)),1)
        self.c.step(Observation("TIMING",1,220,(172,244),402,False),.60)
        self.c.step(Observation("TIMING",1,100,(172,244),402,False),.62)
        self.c.step(Observation("TIMING",1,110,(172,244),402,False),.67)
        self.assertFalse(self.c.shot_locked)
        self.assertIsNone(self.c.target)

    def test_stationary_marker_does_not_repeatedly_press(self):
        self.known("TIMING",0)
        for i in range(50):
            d=self.c.step(Observation("TIMING",1,110,(170,240),402,True),.1+i/60)
            self.assertIsNone(d.action)

    def test_stale_frame_skipped(self):
        self.known("TIMING",0)
        for i in range(30):
            t=.1+i/60
            d=self.c.step(Observation("TIMING",1,100+200*i/60,(172,244),402,True),t,t+.2)
            self.assertIsNone(d.action)

    def test_limits_and_reset(self):
        self.c.cast_count=DEFAULTS["max_casts"]
        d=self.known("IDLE",0)
        self.assertTrue(d.pause)
        self.c.reset(100)
        self.assertEqual(self.c.cast_count,0)
        self.assertTrue(self.c.step(Observation("WAIT",1),100+16*60).pause)

class InputGuardTests(unittest.TestCase):
    def instance(self,foreground=True):
        from fisher.windows import Windows
        win=Windows.__new__(Windows)
        win.foreground_matches=lambda hwnd:foreground
        win.key_down=lambda key:False
        win.owns_space=False
        win.events=[]
        win._send=lambda up=False:win.events.append("up" if up else "down")
        return win

    def test_no_input_without_focus(self):
        win=self.instance(False)
        self.assertFalse(win.tap_space(1,10,lambda:False))
        self.assertEqual(win.events,[])

    def test_no_input_when_cancelled_or_geometry_changes(self):
        for cancel,guard in ((True,True),(False,False)):
            win=self.instance()
            self.assertFalse(win.tap_space(1,10,lambda:cancel,lambda:guard))
            self.assertEqual(win.events,[])

    def test_key_released_after_press(self):
        win=self.instance()
        self.assertTrue(win.tap_space(1,10,lambda:False))
        self.assertEqual(win.events,["down","up"])
        self.assertFalse(win.owns_space)

    def test_manual_modifiers_pause_input(self):
        win=self.instance()
        win.key_down=lambda key:key==0x11
        with self.assertRaises(OSError): win.tap_space(1,10,lambda:False)
        self.assertEqual(win.events,[])

    def test_emergency_during_hold_releases_and_marks_interruption(self):
        win=self.instance()
        polls=[False, False, True]
        def cancelled():
            return polls.pop(0) if polls else True
        self.assertTrue(win.tap_space(1,35,cancelled))
        self.assertEqual(win.events,["down","up"])
        self.assertTrue(win.interrupted)

if __name__ == "__main__": unittest.main()
