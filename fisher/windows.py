# SPDX-License-Identifier: GPL-3.0-only
"""Windows窗口坐标、前台检查和空格输入；不访问游戏进程内存。"""
import ctypes as C
from ctypes import wintypes as T
import sys
import time
from .emulators import select_windows

def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        C.windll.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            C.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            C.windll.user32.SetProcessDPIAware()

# INPUT必须保留完整union；仅定义KEYBDINPUT会导致64位结构尺寸错误。
UPTR = C.c_size_t
class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", T.WORD), ("wScan", T.WORD), ("dwFlags", T.DWORD), ("time", T.DWORD), ("dwExtraInfo", UPTR)]
class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", T.LONG), ("dy", T.LONG), ("mouseData", T.DWORD), ("dwFlags", T.DWORD), ("time", T.DWORD), ("dwExtraInfo", UPTR)]
class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", T.DWORD), ("wParamL", T.WORD), ("wParamH", T.WORD)]
class INPUTUNION(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
class INPUT(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", T.DWORD), ("u", INPUTUNION)]

class Windows:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("自动按键和实时截图入口需要Windows；离线回放可在其他系统运行")
        self.u = C.WinDLL("user32", use_last_error=True)
        self.u.GetForegroundWindow.restype = T.HWND
        self.u.GetAncestor.argtypes = [T.HWND, T.UINT]
        self.u.GetAncestor.restype = T.HWND
        self.u.GetClientRect.argtypes = [T.HWND, C.POINTER(T.RECT)]
        self.u.ClientToScreen.argtypes = [T.HWND, C.POINTER(T.POINT)]
        self.u.GetWindowTextLengthW.argtypes = [T.HWND]
        self.u.GetWindowTextW.argtypes = [T.HWND, T.LPWSTR, C.c_int]
        self.u.IsWindowVisible.argtypes = [T.HWND]
        self.u.IsWindow.argtypes = [T.HWND]
        self.u.IsIconic.argtypes = [T.HWND]
        self.u.GetAsyncKeyState.argtypes = [C.c_int]
        self.u.GetAsyncKeyState.restype = T.SHORT
        self.u.SendInput.argtypes = [T.UINT, C.POINTER(INPUT), C.c_int]
        self.u.SendInput.restype = T.UINT
        self.callback_type = C.WINFUNCTYPE(T.BOOL, T.HWND, T.LPARAM)
        self.u.EnumWindows.argtypes = [self.callback_type, T.LPARAM]
        self.owns_space = False
        expected = 40 if C.sizeof(C.c_void_p) == 8 else 28
        if C.sizeof(INPUT) != expected:
            raise RuntimeError(f"Windows输入结构尺寸异常：{C.sizeof(INPUT)} / {expected}")

    def title(self, hwnd):
        buf = C.create_unicode_buffer(self.u.GetWindowTextLengthW(hwnd)+1)
        self.u.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def windows(self, emulator_type="ldplayer", include_other=False):
        found = []
        @self.callback_type
        def callback(hwnd, param):
            title = self.title(hwnd)
            if title and self.u.IsWindowVisible(hwnd):
                try:
                    box = self.client_box(hwnd)
                    if box[2] > 100 and box[3] > 100:
                        found.append((int(hwnd), title))
                except OSError:
                    pass
            return True
        self.u.EnumWindows(callback, 0)
        return select_windows(found, emulator_type, include_other)

    def client_box(self, hwnd):
        rect, pt = T.RECT(), T.POINT(0, 0)
        if not self.u.IsWindow(hwnd) or self.u.IsIconic(hwnd):
            raise OSError("模拟器已关闭或最小化")
        if not self.u.GetClientRect(hwnd, C.byref(rect)) or not self.u.ClientToScreen(hwnd, C.byref(pt)):
            raise OSError("无法取得窗口位置")
        return pt.x, pt.y, rect.right-rect.left, rect.bottom-rect.top

    def foreground_matches(self, hwnd):
        return self.u.GetAncestor(self.u.GetForegroundWindow(), 2) == hwnd

    def key_down(self, key):
        # 使用高位当前状态，避免依赖不可靠的“自上次调用后按过”低位。
        return bool(self.u.GetAsyncKeyState(key) & 0x8000)

    def _send(self, up=False):
        inp = INPUT(type=1, ki=KEYBDINPUT(0, 0x39, 0x0008 | (0x0002 if up else 0), 0, 0))
        if self.u.SendInput(1, C.byref(inp), C.sizeof(INPUT)) != 1:
            raise OSError("空格输入失败。请确认窗口在前台、程序与模拟器权限一致，且映射已生效")

    def release(self):
        if self.owns_space:
            # 失焦时仍释放本程序按下的空格，避免按键卡住；不产生新的keydown。
            self._send(up=True)
            self.owns_space = False
            self.last_key_up_at = time.perf_counter()

    def tap_space(self, hwnd, hold_ms, cancelled, guard=None):
        self.interrupted = False
        self.last_key_down_at = self.last_key_up_at = None
        self.last_send_begin = self.last_send_end = None
        if cancelled() or not self.foreground_matches(hwnd) or (guard and not guard()):
            return False
        if any(self.key_down(k) for k in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x20)):
            raise OSError("检测到手动空格或修饰键按下，自动操作已暂停")
        # 系统查询也可能遇到调度卡顿；在注入前复核取消、焦点和截图时效。
        if cancelled() or not self.foreground_matches(hwnd) or (guard and not guard()):
            return False
        try:
            self.last_send_begin = time.perf_counter()
            self._send()
            self.last_send_end = time.perf_counter()
            self.last_key_down_at = (self.last_send_begin+self.last_send_end)/2
            self.owns_space = True
            # 已阻塞的系统调用结束后尽快释放，避免再叠加一次完整按住时间。
            blocked=self.last_send_end-self.last_send_begin>.050
            deadline = time.perf_counter()+(0 if blocked else hold_ms/1000)
            while time.perf_counter() < deadline:
                if cancelled() or not self.foreground_matches(hwnd):
                    self.interrupted = True
                    break
                time.sleep(.002)
            return True
        finally:
            self.release()
