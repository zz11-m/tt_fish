# SPDX-License-Identifier: GPL-3.0-only
"""下载校验、路径约束和便携源码发布边界。"""
from hashlib import sha256
from pathlib import Path
import tarfile
import tempfile
import unittest
import uuid
from unittest.mock import patch
import zipfile
import build_windows
from fisher.config import ROOT
from fisher.packaging import source_payloads


class WindowsPackageTests(unittest.TestCase):
    def test_valid_cache_needs_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "package.bin"
            path.write_bytes(b"verified")
            item = dict(filename=path.name, size=8, sha256=sha256(b"verified").hexdigest())
            with patch("build_windows.urllib.request.urlopen") as network:
                self.assertEqual(build_windows.fetch(item, tmp), path)
                network.assert_not_called()
            path.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                build_windows.fetch(item, tmp)

    def test_archive_paths_cannot_escape(self):
        for name in ("../private", "/tmp/secret", "C:/secret", "dir\\private", "a/../secret"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_windows.safe_name(name)

    def test_runtime_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "python.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("python/linked")
                member.type = tarfile.SYMTYPE
                member.linkname = "../private"
                tar.addfile(member)
            with self.assertRaises(ValueError):
                build_windows.unpack_runtime(archive, Path(tmp) / "runtime")

    def test_wheel_data_requires_explicit_install_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "package.whl"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("package.data/scripts/command", "echo")
            with self.assertRaises(ValueError):
                build_windows.unpack_wheel(archive, Path(tmp) / "site-packages")

    def test_portable_has_no_local_data_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = uuid.uuid4().hex
            for name, content in source_payloads().items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for name in ("data/config.json", "runtime/private.txt", ".build-cache/personal.bin", "logs/startup.log"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(marker)
            payloads = source_payloads(root)
            self.assertFalse(any(marker.encode() in value for value in payloads.values()))
            for name in ("desktop.py", "build_windows.py", "packaging/windows-runtime.lock.json", "fisher/templates.py"):
                self.assertIn(name, payloads)

    def test_bundle_checksum_covers_every_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tt_fish"
            root.mkdir()
            (root / "中文文件.txt").write_text("payload", encoding="utf-8")
            dest = Path(tmp) / "result.zip"
            build_windows.write_bundle(root, dest)
            with zipfile.ZipFile(dest) as z:
                manifest = z.read("tt_fish/SHA256SUMS").decode()
                for line in manifest.splitlines():
                    digest, name = line.split("  ", 1)
                    self.assertEqual(sha256(z.read("tt_fish/" + name)).hexdigest(), digest)

    def test_launchers_use_local_isolated_interpreter(self):
        for name in ("start.bat", "check.bat"):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            self.assertIn('"%~dp0runtime\\python', text)
            self.assertIn(' -I -B "%~dp0app\\desktop.py"', text)
            self.assertIn('--data-dir "%~dp0data"', text)
            self.assertNotIn("pip install", text)
            self.assertNotIn("powershell", text.lower())
