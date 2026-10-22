# SPDX-License-Identifier: GPL-3.0-only
"""真实判定条局部图、JSON数值边界和异常导出的回归检查。"""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile
import cv2
import numpy as np
from fisher.config import DEFAULTS, ROOT, hotkey_vk
from fisher.controller import Controller, Decision
from fisher.jsonio import dumps
from fisher.runtime import Runtime
from fisher.trial_capture import TrialRecorder
from fisher.vision import Detector, Observation, read_image
from fixtures import make_frame
from test_trial import Window

# 历史策略回归显式使用其原始配置；正式默认策略见test_phase。
DEFAULTS = dict(DEFAULTS,timing_mode="contact",capture_every_n_frames=6)


def real_bar_frame(name):
    frame = make_frame("TIMING")
    frame[376:426,77:266] = read_image(ROOT/"tests/assets"/name)
    return frame


class DiagnosticTests(unittest.TestCase):
    def test_numpy_scalars_keep_json_types_without_stringifying_objects(self):
        row = {"clear": np.bool_(True), "count": np.int32(3),
               "values": [np.float32(1.5), np.int64(8), np.bool_(False)]}
        self.assertEqual(json.loads(dumps(row)), {"clear":True,"count":3,"values":[1.5,8,False]})
        for invalid in (object(), np.array([1,2]), np.float32(float("nan"))):
            with self.assertRaises((TypeError,ValueError)):
                dumps({"value":invalid})

    def test_real_clear_and_split_markers_are_detected_with_native_fields(self):
        detector = Detector(ROOT/"assets/templates")
        for name, expected_x, target, clear in (
            ("timing_marker_clear.png",103,(172,244),True),
            ("timing_marker_split.png",141,(115,187),False)):
            with self.subTest(name=name):
                obs,_ = detector.detect(real_bar_frame(name))
                self.assertEqual(obs.state,"TIMING")
                self.assertIsNotNone(obs.marker)
                self.assertAlmostEqual(obs.marker,expected_x,delta=2)
                self.assertAlmostEqual(obs.target[0],target[0],delta=1)
                self.assertAlmostEqual(obs.target[1],target[1],delta=1)
                self.assertIs(type(obs.geometry_clear),bool)
                self.assertIs(obs.geometry_clear,clear)
                json.dumps(asdict(obs),allow_nan=False)

    def test_real_detector_to_controller_to_recorder_exports_before_any_hit(self):
        detector = Detector(ROOT/"assets/templates")
        controller = Controller(DEFAULTS)
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TrialRecorder(tmp)
            recorder.start(DEFAULTS)
            now = time.perf_counter()
            with patch("fisher.trial_capture.cv2.imencode",wraps=cv2.imencode) as encoder:
                for i,name in enumerate(("timing_marker_clear.png","timing_marker_clear.png","timing_marker_split.png")):
                    obs,frame = detector.detect(real_bar_frame(name))
                    t=now+i*.1
                    decision=controller.step(obs,t,t+.02)
                    recorder.frame(obs,decision,controller,frame,t,t+.02,10,15)
                encoder.assert_not_called()
            recorder.finish()
            recorder.join()
            self.assertIsNone(recorder.last_error)
            with zipfile.ZipFile(recorder.last_bundle) as z:
                meta=json.loads(z.read("meta.json"))
                self.assertEqual(meta["state_counts"]["TIMING"],3)
                self.assertEqual(meta["fish_count"],1)
                self.assertEqual(meta["sent_actions"],{})
                self.assertGreater(meta["crop_count"],0)
                self.assertIn("crops/frame_00000000.png",z.namelist())
                rows=[json.loads(s) for s in z.read("events_000.jsonl").splitlines()]
                self.assertIs(rows[0]["geometry_clear"],True)

    def test_fault_bundle_preserves_type_and_reason_without_exception_message(self):
        private=uuid.uuid4().hex
        win=Window()
        runtime=Runtime(win,1,{"client_size":[336,598],"crop":[0,0,336,598]},DEFAULTS)
        with tempfile.TemporaryDirectory() as tmp:
            runtime.trial=TrialRecorder(tmp)
            runtime.trial.start(DEFAULTS)
            try:
                raise TypeError(private)
            except TypeError as exc:
                runtime.record_fault(exc,"trace_frame")
            runtime.pause("已暂停","runtime_error","error")
            runtime.trial.join()
            with zipfile.ZipFile(runtime.trial.last_bundle) as z:
                rows=[json.loads(s) for s in z.read("events_000.jsonl").splitlines()]
                self.assertEqual(rows[0]["exception"],"TypeError")
                self.assertEqual(rows[-1]["cause"],"error")
                self.assertEqual(rows[-1]["reason_code"],"runtime_error")
                self.assertEqual(json.loads(z.read("meta.json"))["errors"],1)
                self.assertFalse(any(private.encode() in z.read(n) for n in z.namelist()))

    def test_template_matching_only_runs_for_current_visual_candidates(self):
        detector=Detector(ROOT/"assets/templates")
        for state in ("WAIT","TIMING","IDLE","BITE","RESULT","CLAIM"):
            with patch.object(detector,"match",wraps=detector.match) as matcher:
                obs,_=detector.detect(make_frame(state))
                self.assertEqual(obs.state,"RESULT" if state=="CLAIM" else state)
                self.assertLessEqual(matcher.call_count,2)

    def test_overdue_ordinary_actions_skip_without_changing_active_state(self):
        for action in ("CAST","HOOK","CLOSE"):
            win=Window()
            runtime=Runtime(win,1,{"client_size":[336,598],"crop":[0,0,336,598]},DEFAULTS)
            runtime.active=True
            controller=Controller(DEFAULTS)
            now=time.perf_counter()
            self.assertFalse(runtime.dispatch(Decision(action=action),controller,runtime.capture_box(),now-.4,now))
            self.assertEqual(win.taps,0)
            self.assertTrue(runtime.active)

    def test_late_last_guard_skips_without_spurious_pause(self):
        class SlowWindow(Window):
            def tap_space(self,hwnd,hold,cancel,guard):
                self.clock[0] += .05
                return super().tap_space(hwnd,hold,cancel,guard)
        win=SlowWindow()
        win.clock=[1.0]
        runtime=Runtime(win,1,{"client_size":[336,598],"crop":[0,0,336,598]},DEFAULTS)
        runtime.active=True
        with patch("fisher.runtime.time.perf_counter",lambda:win.clock[0]):
            self.assertFalse(runtime.dispatch(Decision(action="HIT"),Controller(DEFAULTS),runtime.capture_box(),1,1))
        self.assertTrue(runtime.active)
        self.assertEqual(win.taps,0)

    def test_runtime_real_recognition_and_fault_paths_both_export(self):
        for fail in (False,True):
            clock=[.001]
            private=uuid.uuid4().hex
            frame=real_bar_frame("timing_marker_clear.png")
            pixels=cv2.cvtColor(frame,cv2.COLOR_BGR2BGRA).tobytes()
            class Stop:
                flag=False
                def is_set(self): return self.flag or clock[0] >= .9
                def wait(self,delay): clock[0]+=max(delay,.00001)
                def set(self): self.flag=True
            class Win(Window):
                def key_down(self,key):
                    return ((key == hotkey_vk("F8") and .04 <= clock[0] < .08) or
                            (key == hotkey_vk("F9") and .6 <= clock[0] < .64))
            class Shot:
                width,height=336,598
                bgra=pixels
            class Screen:
                monitors=[{"left":0,"top":0,"width":1920,"height":1080}]
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def grab(self,box):
                    clock[0]+=.003
                    return Shot()
            real_detector=Detector(ROOT/"assets/templates")
            calls=[0]
            class DetectorProxy:
                def __init__(self,*args): pass
                def detect(self,img):
                    calls[0]+=1
                    if fail and calls[0]==3:
                        raise TypeError(private)
                    clock[0]+=.004
                    return real_detector.detect(img)
            runtime=Runtime(Win(),1,{"client_size":[336,598],"crop":[0,0,336,598]},dict(DEFAULTS,dry_run=True))
            runtime.stop=Stop()
            with tempfile.TemporaryDirectory() as tmp, \
                 patch("fisher.runtime.DATA_ROOT",Path(tmp)), \
                 patch("fisher.runtime.time.perf_counter",lambda:clock[0]), \
                 patch("fisher.runtime.mss.mss",Screen), \
                 patch("fisher.runtime.Detector",DetectorProxy), \
                 patch("fisher.runtime.TRIAL_BUILD",True):
                runtime.run()
                bundles=list((Path(tmp)/"trial_exports").glob("*.zip"))
                self.assertEqual(len(bundles),1)
                with zipfile.ZipFile(bundles[0]) as z:
                    meta=json.loads(z.read("meta.json"))
                    self.assertGreater(meta["state_counts"].get("TIMING",0),0)
                    self.assertGreater(meta["crop_count"],0)
                    self.assertEqual(meta["errors"],int(fail))
                    rows=[json.loads(s) for s in z.read("events_000.jsonl").splitlines()]
                    self.assertEqual(rows[-1]["cause"],"error" if fail else "pause")
                    self.assertFalse(any(private.encode() in z.read(n) for n in z.namelist()))

    def test_crop_budget_is_bounded_without_live_png_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=TrialRecorder(tmp)
            r.start(DEFAULTS)
            image=np.zeros((50,189,3),np.uint8)
            with patch("fisher.trial_capture.cv2.imencode") as encoder:
                for seq in range(100): r._save_crop(seq,image,periodic=True)
                self.assertEqual(r.current["crops"],60)
                for seq in range(100,350): r._save_crop(seq,image)
                self.assertEqual(r.current["crops"],180)
                encoder.assert_not_called()
            r.current["stream"].close()


if __name__ == "__main__": unittest.main()
