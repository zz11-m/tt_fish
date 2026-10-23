# SPDX-License-Identifier: GPL-3.0-only
"""真实按钮局部、缩放状态和结算返回流程回归。"""
import unittest

import cv2

from fisher.config import DEFAULTS, ROOT
from fisher.controller import Controller
from fisher.vision import Detector, read_image
from fixtures import make_frame


def scaled_claim():
    frame = make_frame("CLAIM")
    frame[500:554, 80:255] = read_image(ROOT / "tests/assets/claim_button_scaled.png")
    return frame


class ClaimResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector(ROOT / "assets/templates")

    def test_captured_scaled_button_is_recognized(self):
        obs, _ = self.detector.detect(scaled_claim())
        self.assertEqual((obs.state, obs.variant), ("RESULT", "claim"))

    def test_captured_button_survives_resizing_and_compression(self):
        for size in ((225, 400), (270, 480), (336, 598), (560, 997), (720, 1280)):
            for quality in (65, 85, 95):
                with self.subTest(size=size, quality=quality):
                    frame = cv2.resize(scaled_claim(), size)
                    ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    self.assertTrue(ok)
                    obs, _ = self.detector.detect(cv2.imdecode(data, cv2.IMREAD_COLOR))
                    self.assertEqual((obs.state, obs.variant), ("RESULT", "claim"))

    def test_green_button_without_caption_is_not_claim(self):
        frame = scaled_claim()
        frame[509:543, 120:218] = (30, 210, 135)
        obs, _ = self.detector.detect(frame)
        self.assertNotEqual(obs.state, "RESULT")

    def test_scaled_button_still_requires_title_and_dark_background(self):
        for region in ((slice(50, 125), slice(65, 265)),
                       (slice(130, 430), slice(0, 65))):
            with self.subTest(region=region):
                frame = scaled_claim()
                frame[region] = (240, 240, 240)
                obs, _ = self.detector.detect(frame)
                self.assertNotEqual(obs.state, "RESULT")

    def test_caption_at_another_position_is_not_claim(self):
        frame = scaled_claim()
        frame[430:484, 80:255] = frame[500:554, 80:255].copy()
        frame[509:543, 120:218] = (30, 210, 135)
        obs, _ = self.detector.detect(frame)
        self.assertNotEqual(obs.state, "RESULT")

    def test_button_size_changes_keep_wait_retry_and_return_flow(self):
        controller = Controller(DEFAULTS)
        small, _ = self.detector.detect(make_frame("CLAIM"))
        large, _ = self.detector.detect(scaled_claim())
        idle, _ = self.detector.detect(make_frame("IDLE"))
        controller.step(small, 0)
        controller.step(large, .1)
        self.assertIsNone(controller.step(small, 3).action)
        self.assertEqual(controller.step(large, 3.1).action, "CLOSE")
        controller.commit("CLOSE", 3.1)
        due = controller.next_result_press_at
        self.assertTrue(.96 <= due - 3.1 <= 1.04)
        self.assertIsNone(controller.step(small, due - .01).action)
        self.assertEqual(controller.step(large, due).action, "CLOSE")
        controller.commit("CLOSE", due)
        self.assertEqual(controller.result_count, 1)
        self.assertIsNone(controller.step(idle, due + .1).action)
        self.assertIsNone(controller.step(idle, due + .2).action)
        self.assertEqual(controller.step(idle, due + .7).action, "CAST")
