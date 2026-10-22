# SPDX-License-Identifier: GPL-3.0-only
"""模拟器筛选、选区隔离和配置升级的回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from fisher.config import DEFAULTS, load_config, validate_config
from fisher.emulators import select_windows, profile_matches
from fisher.windows import Windows


class EmulatorTests(unittest.TestCase):
    def test_window_families_and_renamed_fallback(self):
        windows = [(1,"雷电模拟器"),(2,"MuMu安卓设备"),(3,"MuMu Player 12"),
                   (4,"MuMu多开器"),(5,"tt_fish · 钓鱼助手"),(6,"游戏实例A")]
        self.assertEqual(select_windows(windows,"ldplayer"),[(1,"雷电模拟器")])
        self.assertEqual({w[0] for w in select_windows(windows,"mumu")},{2,3})
        expanded = select_windows(windows,"mumu",True)
        self.assertEqual({w[0] for w in expanded},{1,2,3,6})
        self.assertEqual({w[0] for w in expanded[:2]},{2,3})
        self.assertEqual(select_windows([],"mumu"),[])

    def test_profiles_never_cross_emulators_or_sizes(self):
        p={"title":"游戏实例A","client_size":[560,997],"crop":[0,0,560,997],"emulator_type":"mumu"}
        self.assertTrue(profile_matches(p,"mumu","游戏实例A",(560,997)))
        self.assertFalse(profile_matches(p,"ldplayer","游戏实例A",(560,997)))
        self.assertFalse(profile_matches(p,"mumu","游戏实例B",(560,997)))
        self.assertFalse(profile_matches(p,"mumu","游戏实例A",(336,598)))
        old=dict(p,title="MuMu安卓设备");old.pop("emulator_type")
        self.assertTrue(profile_matches(old,"mumu","MuMu安卓设备",(560,997)))
        old["title"]="游戏实例A"
        self.assertFalse(profile_matches(old,"mumu","游戏实例A",(560,997)))

    def test_foreground_accepts_child_but_not_another_instance_or_manager(self):
        class User32:
            foreground = 101
            def GetForegroundWindow(self):return self.foreground
            def GetAncestor(self, hwnd, flag):return {101:10,201:20,301:30}.get(hwnd,hwnd)
        win=Windows.__new__(Windows);win.u=User32()
        self.assertTrue(win.foreground_matches(10))
        for hwnd in (201,301,0):
            win.u.foreground=hwnd
            self.assertFalse(win.foreground_matches(10))


class UpgradeTests(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"config.json";p.write_text(json.dumps(data),encoding="utf-8")
            cfg=load_config(p)
            self.assertEqual(json.loads(p.read_text()),data)
            return cfg

    def test_defaults_and_upgrade_from_previous_defaults(self):
        self.assertTrue(DEFAULTS["continuous_fishing"])
        self.assertEqual(DEFAULTS["capture_every_n_frames"],6)
        old=dict(DEFAULTS,config_version=6,continuous_fishing=False,capture_every_n_frames=30,
                 input_latency_ms=75,unknown_timeout_s=240,toggle_hotkey="F6")
        old.pop("emulator_type")
        new=self.read(old)
        self.assertTrue(new["continuous_fishing"])
        self.assertEqual(new["capture_every_n_frames"],6)
        for key in ("input_latency_ms","unknown_timeout_s","toggle_hotkey"):
            self.assertEqual(new[key],old[key])

    def test_custom_sampling_preserved_and_mumu_profile_migrated(self):
        old=dict(DEFAULTS,config_version=6,capture_every_n_frames=12,
                 profile={"title":"MuMu安卓设备","client_size":[560,997],"crop":[0,0,560,997]})
        old.pop("emulator_type")
        new=self.read(old)
        self.assertEqual(new["capture_every_n_frames"],12)
        self.assertEqual(new["emulator_type"],"mumu")
        self.assertEqual(new["profile"],old["profile"])

    def test_new_version_preserves_unchecked_continuous_and_two_fps(self):
        cfg=dict(DEFAULTS,continuous_fishing=False,capture_every_n_frames=30,emulator_type="mumu")
        self.assertEqual(self.read(cfg),cfg)

    def test_invalid_emulator_is_rejected(self):
        for value in ("invalid",None,True):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate_config(dict(DEFAULTS,emulator_type=value))
