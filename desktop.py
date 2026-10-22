# SPDX-License-Identifier: GPL-3.0-only
"""便携版入口、单实例保护和本地环境检查。"""
import argparse
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import traceback

APP_ROOT = Path(__file__).resolve().parent


def prepare_paths(data_dir=None):
    # 隔离模式不从当前目录或用户 site-packages 导入，只添加程序自身目录。
    if str(APP_ROOT) not in sys.path:
        sys.path.insert(0, str(APP_ROOT))
    runtime = Path(sys.executable).resolve().parent
    if (runtime / "tcl/tcl8.6/init.tcl").is_file():
        os.environ["TCL_LIBRARY"] = str(runtime / "tcl/tcl8.6")
        os.environ["TK_LIBRARY"] = str(runtime / "tcl/tk8.6")
    data = Path(data_dir).resolve() if data_dir else APP_ROOT
    os.environ["TT_FISH_DATA_DIR"] = str(data)
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=data):
        pass
    return data


def notify(message, error=False):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, str(message), "tt_fish · 钓鱼助手", 0x10 if error else 0x40)
    elif sys.stderr:
        print(message, file=sys.stderr)


class InstanceLock:
    def __init__(self, data_dir):
        self.name = "Local\\tt_fish_" + sha256(str(Path(data_dir).resolve()).casefold().encode("utf-8")).hexdigest()
        self.handle = None
        self.api = None

    def acquire(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self.api.CreateMutexW.restype = ctypes.c_void_p
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.api.CloseHandle.restype = ctypes.c_bool
        self.handle = self.api.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.close()
            return False
        return True

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def check_environment():
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("便携版需要64位 Windows 10或11。")
    from fisher.windows import enable_dpi_awareness, Windows
    enable_dpi_awareness()
    import tkinter as tk
    from PIL import Image, ImageTk
    import PIL
    import cv2
    import mss
    import numpy as np
    from fisher.vision import Detector, read_image
    from fisher.config import ROOT
    from fisher import __version__
    root = tk.Tk()
    root.withdraw()
    try:
        ImageTk.PhotoImage(Image.new("RGB", (8, 8)), master=root)
        root.update_idletasks()
        tcl_version = root.tk.call("info", "patchlevel")
    finally:
        root.destroy()
    detector = Detector(ROOT / "assets/templates")
    frame = read_image(ROOT / "docs/images/crop-example.png")[58:710, 23:389]
    obs, _ = detector.detect(frame)
    if obs.state != "RESULT":
        raise RuntimeError("识别资源校验失败，请重新完整解压。")
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "codec.avi")
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 5, (336, 598))
        if not writer.isOpened():
            raise RuntimeError("录像组件不可用。")
        try:
            writer.write(np.zeros((598, 336, 3), dtype=np.uint8))
        finally:
            writer.release()
        reader = cv2.VideoCapture(path)
        try:
            ok, _ = reader.read()
            if not ok:
                raise RuntimeError("录像解码组件不可用。")
        finally:
            reader.release()
    Windows()
    with mss.mss() as screen:
        if not screen.monitors:
            raise RuntimeError("未找到可用显示器。")
    return {"application": __version__, "python": sys.version.split()[0], "numpy": np.__version__,
            "opencv": cv2.__version__, "pillow": PIL.__version__, "mss": mss.__version__,
            "tcl": str(tcl_version), "status": "ok"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动钓鱼助手或检查便携运行环境")
    parser.add_argument("--data-dir", type=Path, help="本机设置与运行数据目录")
    parser.add_argument("--check", action="store_true", help="检查环境，不截图、不发送按键")
    args = parser.parse_args(argv)
    lock = None
    data = None
    try:
        if sys.platform != "win32":
            raise RuntimeError("实时助手需要 Windows；离线检查请运行 selfcheck.py。")
        data = prepare_paths(args.data_dir)
        if args.check:
            print(json.dumps(check_environment(), ensure_ascii=False, indent=2))
            return 0
        lock = InstanceLock(data)
        if not lock.acquire():
            notify("程序已在运行，请切换到已打开的钓鱼助手窗口。")
            return 0
        from main import main as run_app
        return run_app(show_errors=False)
    except Exception as exc:
        detail = traceback.format_exc()
        log_path = None
        if data is not None:
            try:
                log_path = data / "logs" / "startup.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(detail, encoding="utf-8")
            except OSError:
                log_path = None
        message = f"无法启动：{exc}\n请确认已完整解压到可写目录，runtime与app文件夹均保留。"
        if log_path is not None:
            message += "\n详细错误已保存到data/logs/startup.log。"
        if args.check:
            print(detail, file=sys.stderr)
        else:
            notify(message, error=True)
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
