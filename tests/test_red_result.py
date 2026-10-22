# SPDX-License-Identifier: GPL-3.0-only
"""红橙结算面板、铜色重量图标与关闭重试回归。"""
import unittest
import cv2
import numpy as np
from fisher.config import DEFAULTS, ROOT
from fisher.controller import Controller
from fisher.vision import Detector, read_image
from fixtures import make_frame


def red_record():
    frame=make_frame("RESULT","red")
    hsv=np.zeros((220,316,3),np.uint8)
    hsv[:,:,0]=np.linspace(0,22,220,dtype=np.uint8)[:,None]
    hsv[:,:,1:]=230
    frame[180:400,10:326]=cv2.cvtColor(hsv,cv2.COLOR_HSV2BGR)
    frame[365:388,104:122]=read_image(ROOT/"tests/assets/record_weight_bronze.png")
    return frame


class RedResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector=Detector(ROOT/"assets/templates")

    def test_red_hue_wrap_and_gradient_with_bronze_weight(self):
        for frame in (make_frame("RESULT","red"),make_frame("RESULT","red_wrap"),red_record()):
            obs,_=self.detector.detect(frame)
            self.assertEqual((obs.state,obs.variant),("RESULT","record"))

    def test_gradient_survives_resizing_and_compression(self):
        for size in ((270,480),(560,997),(720,1280)):
            for quality in (80,95):
                with self.subTest(size=size,quality=quality):
                    resized=cv2.resize(red_record(),size)
                    ok,buf=cv2.imencode(".jpg",resized,[cv2.IMWRITE_JPEG_QUALITY,quality])
                    self.assertTrue(ok)
                    obs,_=self.detector.detect(cv2.imdecode(buf,cv2.IMREAD_COLOR))
                    self.assertEqual((obs.state,obs.variant),("RESULT","record"))

    def test_red_still_requires_weight_panel_and_dim_background(self):
        for region,value in (((slice(350,405),slice(85,140)),(20,70,240)),
                             ((slice(180,350),slice(10,326)),(30,30,30)),
                             ((slice(475,542),slice(125,215)),(240,240,240))):
            frame=red_record();frame[region]=value
            obs,_=self.detector.detect(frame)
            self.assertNotEqual(obs.state,"RESULT")
        for state in ("IDLE","WAIT","BITE","TIMING"):
            obs,_=self.detector.detect(make_frame(state))
            self.assertEqual(obs.state,state)

    def test_color_changes_do_not_restart_wait_or_double_count(self):
        controller=Controller(DEFAULTS)
        red,_=self.detector.detect(red_record())
        blue,_=self.detector.detect(make_frame("RESULT"))
        controller.step(red,0);controller.step(red,.1)
        self.assertIsNone(controller.step(blue,3).action)
        self.assertEqual(controller.step(red,3.1).action,"CLOSE")
        controller.commit("CLOSE",3.1)
        due=controller.next_result_press_at
        self.assertTrue(.96 <= due-3.1 <= 1.04)
        self.assertEqual(controller.step(red,due).action,"CLOSE")
        self.assertEqual(controller.result_count,1)
        idle,_=self.detector.detect(make_frame("IDLE"))
        controller.commit("CLOSE",due)
        self.assertIsNone(controller.step(idle,due+.1).action)
        controller.step(idle,due+.2)
        self.assertEqual(controller.step(idle,due+.7).action,"CAST")
