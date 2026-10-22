# SPDX-License-Identifier: GPL-3.0-only
"""便携入口的数据隔离和失败处理。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import desktop
from fisher import config


class DesktopTests(unittest.TestCase):
    def test_portable_config_is_separate_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "中文路径 with space" / "data"
            with patch.dict(os.environ), patch.object(config, "DATA_ROOT", data):
                self.assertEqual(desktop.prepare_paths(data), data.resolve())
                cfg = dict(config.DEFAULTS, continuous_fishing=True, toggle_hotkey="F6")
                config.save_config(cfg)
                self.assertEqual(config.load_config(), cfg)
                self.assertFalse((Path(tmp) / "config.json").exists())
                self.assertFalse((data / "config.tmp").exists())

    def test_writable_check_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            path = Path(tmp) / "config.json"
            path.write_text('{"keep":"private"}', encoding="utf-8")
            desktop.prepare_paths(tmp)
            self.assertEqual(json.loads(path.read_text()), {"keep":"private"})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["config.json"])

    def test_failed_start_leaves_local_log_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), \
             patch.object(desktop.sys, "platform", "win32"), \
             patch.object(desktop, "InstanceLock") as lock, \
             patch.object(desktop, "notify") as notify, \
             patch("main.main", side_effect=RuntimeError("startup failure")):
            lock.return_value.acquire.return_value = True
            self.assertEqual(desktop.main(["--data-dir", tmp]), 1)
            self.assertIn("startup failure", (Path(tmp) / "logs/startup.log").read_text())
            notify.assert_called_once()
            lock.return_value.close.assert_called_once()

    def test_second_launch_does_not_enter_application(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ), \
             patch.object(desktop.sys, "platform", "win32"), \
             patch.object(desktop, "InstanceLock") as lock, \
             patch.object(desktop, "notify"), patch("main.main") as app:
            lock.return_value.acquire.return_value = False
            self.assertEqual(desktop.main(["--data-dir", tmp]), 0)
            app.assert_not_called()

    def test_mutex_releases_duplicate_handle(self):
        api = MagicMock()
        api.CreateMutexW.return_value = 123
        with patch.object(desktop.ctypes, "WinDLL", return_value=api, create=True), \
             patch.object(desktop.ctypes, "get_last_error", return_value=183, create=True):
            lock = desktop.InstanceLock(Path("data"))
            self.assertFalse(lock.acquire())
            lock.close()
            api.CloseHandle.assert_called_once_with(123)
