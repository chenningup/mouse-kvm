"""Windows 输入后端：Win32 低级钩子捕获 + SendInput 注入。

为什么不用 pynput：
  1. pynput 的 suppress 只能在启动时设定，无法运行时切换，
     而 KVM 必须在"本机"和"转发"两种模式之间来回切。
  2. 低级钩子能拿到真实的屏幕坐标与 VK 码，控制更精确。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from ..keys import EVDEV_TO_VK, EXTENDED_VKS, VK_TO_EVDEV
from .base import Backend, Event, EventSink

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# -- 常量 -------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

SM_CXSCREEN, SM_CYSCREEN = 0, 1

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

XBUTTON1, XBUTTON2 = 0x0001, 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
WHEEL_DELTA = 120


# -- 结构体 -----------------------------------------------------------------
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT),
                             ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

# 64 位下必须显式声明指针返回类型，否则模块句柄被截断成 int，
# SetWindowsHookEx 会返回 0 并报 ERROR_MOD_NOT_FOUND(126)
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def enable_dpi_awareness() -> None:
    """让本进程拿到真实屏幕像素坐标，而不是被 DPI 缩放后的逻辑坐标。"""
    try:
        # PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class Win32Backend(Backend):
    name = "win32"
    platform = "windows"
    supports_abs = True
    supports_suppress = True

    def __init__(self, capture: bool = True) -> None:
        super().__init__(capture)
        enable_dpi_awareness()
        self._hmod = kernel32.GetModuleHandleW(None)
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._ready = threading.Event()
        self._running = False
        self._error: str | None = None
        self._anchor = (0, 0)
        # 回调对象必须持引用，否则被 GC 后系统回调会崩
        self._kb_cb = HOOKPROC(self._kb_proc)
        self._ms_cb = HOOKPROC(self._mouse_proc)
        self._kb_hook = None
        self._ms_hook = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if self._running or not self.capture:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="win32-hooks",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("安装 Win32 钩子超时")
        if self._error:
            raise RuntimeError("安装 Win32 钩子失败: %s" % self._error)

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb,
                                                 self._hmod, 0)
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb,
                                                 self._hmod, 0)
        if not self._kb_hook or not self._ms_hook:
            self._error = "SetWindowsHookEx 返回 0, GetLastError=%d" % (
                ctypes.get_last_error(),)
            self._ready.set()
            return
        self._ready.set()

        msg = MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for hook in (self._kb_hook, self._ms_hook):
            if hook:
                user32.UnhookWindowsHookEx(hook)
        self._kb_hook = self._ms_hook = None

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    # -- 屏幕 / 光标 --------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        return (user32.GetSystemMetrics(SM_CXSCREEN),
                user32.GetSystemMetrics(SM_CYSCREEN))

    def cursor_pos(self) -> tuple[int, int]:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)

    def set_cursor_pos(self, x: int, y: int) -> None:
        user32.SetCursorPos(int(x), int(y))

    def set_anchor(self, x: int, y: int) -> None:
        """远程模式下把光标钳制在这个点。"""
        self._anchor = (int(x), int(y))
        self.set_cursor_pos(*self._anchor)

    # -- 钩子 ---------------------------------------------------------------
    def _mouse_proc(self, nCode, wParam, lParam):
        if nCode < 0:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        try:
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if info.flags & LLMHF_INJECTED:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            remote = self._suppress

            if wParam == WM_MOUSEMOVE:
                px, py = info.pt.x, info.pt.y
                if remote:
                    ax, ay = self._anchor
                    if px != ax or py != ay:
                        self.sink(("move", px, py))
                        user32.SetCursorPos(ax, ay)
                    return 1
                self.sink(("move", px, py))
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            if wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                self.sink(("button", 1, wParam == WM_LBUTTONDOWN))
            elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                self.sink(("button", 2, wParam == WM_MBUTTONDOWN))
            elif wParam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
                self.sink(("button", 3, wParam == WM_RBUTTONDOWN))
            elif wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                xbtn = (info.mouseData >> 16) & 0xFFFF
                btn = 4 if xbtn == XBUTTON1 else 5
                self.sink(("button", btn, wParam == WM_XBUTTONDOWN))
            elif wParam == WM_MOUSEWHEEL:
                delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                self.sink(("scroll", 0, delta // WHEEL_DELTA))
            elif wParam == WM_MOUSEHWHEEL:
                delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                self.sink(("scroll", delta // WHEEL_DELTA, 0))
            else:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            return 1 if remote else user32.CallNextHookEx(
                None, nCode, wParam, lParam)
        except Exception:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _kb_proc(self, nCode, wParam, lParam):
        if nCode < 0:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        try:
            info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if info.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            remote = self._suppress
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            code = VK_TO_EVDEV.get(int(info.vkCode))
            if code is not None:
                self.sink(("key", code, down))

            if remote:
                return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # -- 注入 ---------------------------------------------------------------
    def _send(self, inp: INPUT) -> None:
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_mouse(self, dx, dy, data, flags) -> None:
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(int(dx), int(dy), int(data), int(flags), 0, 0)
        self._send(inp)

    def inject_move_abs(self, x: int, y: int, screen: tuple[int, int]) -> None:
        sw, sh = screen
        nx = 0 if sw <= 1 else int(round(x * 65535.0 / (sw - 1)))
        ny = 0 if sh <= 1 else int(round(y * 65535.0 / (sh - 1)))
        nx = max(0, min(65535, nx))
        ny = max(0, min(65535, ny))
        self._send_mouse(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)

    def inject_move_rel(self, dx: int, dy: int) -> None:
        if dx or dy:
            self._send_mouse(dx, dy, 0, MOUSEEVENTF_MOVE)

    def inject_button(self, btn: int, down: bool) -> None:
        if btn == 1:
            self._send_mouse(0, 0, 0,
                             MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP)
        elif btn == 2:
            self._send_mouse(0, 0, 0,
                             MOUSEEVENTF_MIDDLEDOWN if down else MOUSEEVENTF_MIDDLEUP)
        elif btn == 3:
            self._send_mouse(0, 0, 0,
                             MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP)
        elif btn in (4, 5):
            data = XBUTTON1 if btn == 4 else XBUTTON2
            flags = MOUSEEVENTF_XDOWN if down else MOUSEEVENTF_XUP
            self._send_mouse(0, 0, data, flags)

    def inject_scroll(self, dx: int, dy: int) -> None:
        if dy:
            self._send_mouse(0, 0, int(dy) * WHEEL_DELTA, MOUSEEVENTF_WHEEL)
        if dx:
            self._send_mouse(0, 0, int(dx) * WHEEL_DELTA, MOUSEEVENTF_HWHEEL)

    def inject_key(self, code: int, down: bool) -> None:
        vk = EVDEV_TO_VK.get(code)
        if vk is None:
            return
        flags = 0x0001 if vk in EXTENDED_VKS else 0
        if not down:
            flags |= KEYEVENTF_KEYUP
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        self._send(inp)
