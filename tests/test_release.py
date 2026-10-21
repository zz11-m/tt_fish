# SPDX-License-Identifier: GPL-3.0-only
"""结算界面、公开发布边界及项目入口检查。"""
import json
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import zipfile
import cv2
from fisher.config import DEFAULTS, ROOT
from fisher.controller import Controller
from fisher.packaging import PUBLIC_FILES, source_payloads, write_archive
from fisher.project import PROJECT_URL, BILIBILI_URL, open_page
from fisher.vision import Detector, Observation, read_image
from fixtures import make_frame


class SettlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector(ROOT/"assets/templates")

    def test_claim_is_result_with_own_variant(self):
        obs,_ = self.detector.detect(make_frame("CLAIM"))
        self.assertEqual((obs.state,obs.variant),("RESULT","claim"))

    def test_claim_needs_title_button_and_dark_background(self):
        for region in ((slice(50,125),slice(65,265)), (slice(495,560),slice(85,255)),
                       (slice(130,430),slice(0,65))):
            frame = make_frame("CLAIM")
            frame[region] = (240,240,240)
            obs,_ = self.detector.detect(frame)
            self.assertNotEqual(obs.variant,"claim")

    def test_green_fishing_button_does_not_look_like_claim(self):
        obs,_ = self.detector.detect(make_frame("IDLE"))
        self.assertEqual(obs.state,"IDLE")
        self.assertIsNone(obs.variant)

    def test_claim_survives_resizing_and_jpeg(self):
        for size in ((270,480),(560,997),(720,1280)):
            frame = cv2.resize(make_frame("CLAIM"),size)
            _,buf = cv2.imencode(".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,85])
            obs,_ = self.detector.detect(cv2.imdecode(buf,cv2.IMREAD_COLOR))
            self.assertEqual((obs.state,obs.variant),("RESULT","claim"),size)

    def test_record_colors(self):
        for variant in ("blue","purple","gold"):
            with self.subTest(variant=variant):
                obs,_ = self.detector.detect(make_frame("RESULT",variant))
                self.assertEqual((obs.state,obs.variant),("RESULT","record"))

    def test_record_colors_survive_resizing_and_jpeg(self):
        for variant in ("blue","purple","gold"):
            for size in ((270,480),(560,997),(720,1280)):
                with self.subTest(variant=variant,size=size):
                    frame = cv2.resize(make_frame("RESULT",variant),size)
                    _,buf = cv2.imencode(".jpg",frame,[cv2.IMWRITE_JPEG_QUALITY,85])
                    obs,_ = self.detector.detect(cv2.imdecode(buf,cv2.IMREAD_COLOR))
                    self.assertEqual((obs.state,obs.variant),("RESULT","record"))

    def test_gold_record_requires_weight_panel_and_dim_background(self):
        cases = {
            "missing_weight": (slice(350,405),slice(85,140),(35,200,255)),
            "missing_panel": (slice(180,350),slice(10,326),(30,30,30)),
            "bright_background": (slice(475,542),slice(125,215),(240,240,240)),
        }
        for name,(rows,cols,color) in cases.items():
            with self.subTest(case=name):
                frame = make_frame("RESULT","gold")
                frame[rows,cols] = color
                obs,_ = self.detector.detect(frame)
                self.assertNotEqual(obs.state,"RESULT")

    def test_gold_record_waits_retries_and_returns_to_fishing(self):
        c = Controller(dict(DEFAULTS,continuous_fishing=True))
        result,_ = self.detector.detect(make_frame("RESULT","gold"))
        idle,_ = self.detector.detect(make_frame("IDLE"))
        self.assertIsNone(c.step(result,0).action)
        self.assertIsNone(c.step(result,.1).action)
        self.assertIsNone(c.step(result,3).action)
        decision = c.step(result,3.1)
        self.assertEqual(decision.action,"CLOSE")
        c.commit(decision.action,3.1)
        due = c.next_result_press_at
        self.assertIsNone(c.step(result,due-.01).action)
        decision = c.step(result,due)
        self.assertEqual(decision.action,"CLOSE")
        c.commit(decision.action,due)
        self.assertIsNone(c.step(idle,due+.1).action)
        self.assertIsNone(c.step(idle,due+.2).action)
        self.assertEqual(c.step(idle,due+.7).action,"CAST")
        self.assertEqual(c.result_count,1)

    def test_documented_crop_example_is_recognized(self):
        frame = read_image(ROOT/"docs/images/crop-example.png")[58:710,23:389]
        obs,_ = self.detector.detect(frame)
        self.assertEqual((obs.state,obs.variant),("RESULT","record"))

    def test_claim_uses_delay_and_retries_until_idle(self):
        c = Controller(dict(DEFAULTS,continuous_fishing=True))
        obs = Observation("RESULT",1,variant="claim")
        c.step(obs,0)
        c.step(obs,.1)
        self.assertIsNone(c.step(obs,3).action)
        now = 3.1
        for _ in range(6):
            d = c.step(obs,now)
            self.assertEqual(d.action,"CLOSE")
            c.commit(d.action,now)
            now = c.next_result_press_at
        self.assertIsNone(c.step(Observation(),now).action)
        self.assertIsNone(c.step(Observation("IDLE",1),now+.1).action)
        c.step(Observation("IDLE",1),now+.2)
        self.assertEqual(c.step(Observation("IDLE",1),now+.6).action,"CAST")

    def test_record_then_claim_is_one_settlement(self):
        c = Controller(DEFAULTS)
        record = Observation("RESULT",1,variant="record")
        claim = Observation("RESULT",1,variant="claim")
        c.step(record,0)
        c.step(record,.1)
        c.commit("CLOSE",3.1)
        due = c.next_result_press_at
        self.assertEqual(c.step(claim,due).action,"CLOSE")
        self.assertEqual(c.result_count,1)


class ReleaseTests(unittest.TestCase):
    def test_public_archive_excludes_local_data_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_marker = uuid.uuid4().hex
            for name,data in source_payloads().items():
                p=root/name
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(data)
            for name in ("config.json","config.tmp","private.json",".env","logs/local.jsonl",
                         "snapshots/private.png","recordings/private.avi","docs/personal_notes.md"):
                p=root/name
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(private_marker,encoding="utf-8")
            payloads = source_payloads(root)
            self.assertFalse(any(private_marker.encode() in content for content in payloads.values()))
            dest = write_archive(root/"release.zip",payloads)
            with zipfile.ZipFile(dest) as archive:
                self.assertEqual(set(archive.namelist()),set(PUBLIC_FILES)|{"SHA256SUMS"})
                for line in archive.read("SHA256SUMS").decode().splitlines():
                    digest,name = line.split("  ",1)
                    self.assertEqual(sha256(archive.read(name)).hexdigest(),digest)

    def test_example_configuration_contains_no_profile(self):
        data=json.loads((ROOT/"config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(data,DEFAULTS)

    def test_link_requires_explicit_function_call_and_known_destination(self):
        with patch("fisher.project.webbrowser.open_new_tab",return_value=True) as opener:
            opener.assert_not_called()
            for url in (PROJECT_URL,BILIBILI_URL):
                open_page(url)
            self.assertEqual(opener.call_count,2)
            with self.assertRaises(ValueError):
                open_page("https://example.invalid")
            self.assertEqual(opener.call_count,2)

    def test_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_archive(Path(tmp)/"bad.zip",{"../private.txt":b"x"})


if __name__ == "__main__":
    unittest.main()
