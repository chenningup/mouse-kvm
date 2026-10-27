"""跨机剪贴板同步（纯文本）。

两端对称：
  - 本机剪贴板变了 -> 回调把内容发出去
  - 收到远端内容 -> 写进本机剪贴板，同时记住它的指纹避免回环

只同步文本。图片/文件一律跳过 —— 传输量不可控，而且卡顿感很明显。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from ctypes import wintypes

log = logging.getLogger("kvm.clip")

MAX_TEXT_BYTES = 256 * 1024   # 超过这个长度不同步，避免卡住网络
POLL_INTERVAL = 0.6           # 轮询间隔（秒）
CF_UNICODETEXT = 13


def digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "replace"),
                           digest_size=16).digest()


# ---------------------------------------------------------------------------
# Windows：直接调 Win32 剪贴板 API
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)

    _u.OpenClipboard.argtypes = [wintypes.HWND]
    _u.OpenClipboard.restype = wintypes.BOOL
    _u.CloseClipboard.restype = wintypes.BOOL
    _u.EmptyClipboard.restype = wintypes.BOOL
    _u.GetClipboardData.argtypes = [wintypes.UINT]
    _u.GetClipboardData.restype = wintypes.HANDLE
    _u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _u.SetClipboardData.restype = wintypes.HANDLE

    _k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _k.GlobalLock.restype = ctypes.c_void_p
    _k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _k.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _k.GlobalAlloc.restype = wintypes.HGLOBAL

    GMEM_MOVEABLE = 0x0002


class WinClipboard:
    name = "win32"
    available = True

    def _open(self, tries: int = 5) -> bool:
        for i in range(tries):
            if _u.OpenClipboard(None):
                return True
            # 别的程序正占着剪贴板，等一会儿再试
            ctypes.windll.kernel32.Sleep(10 + i * 10)
        return False

    def get_text(self) -> str | None:
        if not self._open():
            return None
        try:
            handle = _u.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = _k.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _k.GlobalUnlock(handle)
        except Exception:
            return None
        finally:
            _u.CloseClipboard()

    def set_text(self, text: str) -> bool:
        raw = text.encode("utf-16-le") + b"\x00\x00"
        handle = _k.GlobalAlloc(GMEM_MOVEABLE, len(raw))
        if not handle:
            return False
        ptr = _k.GlobalLock(handle)
        if not ptr:
            _k.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, raw, len(raw))
        _k.GlobalUnlock(handle)

        if not self._open():
            _k.GlobalFree(handle)
            return False
        try:
            _u.EmptyClipboard()
            if not _u.SetClipboardData(CF_UNICODETEXT, handle):
                _k.GlobalFree(handle)
                return False
            return True   # 成功后句柄归系统所有，不能 Free
        finally:
            _u.CloseClipboard()


# ---------------------------------------------------------------------------
# Linux：走 wl-clipboard（Wayland）或 xclip（X11）
# ---------------------------------------------------------------------------
class _CommandClipboard:
    """通过命令行工具读写剪贴板。"""

    name = "cmd"
    available = False
    read_cmd: list[str] = []
    write_cmd: list[str] = []
    env: dict | None = None

    def _run(self, cmd, data=None, timeout=3):
        return subprocess.run(cmd, input=data, capture_output=True,
                              timeout=timeout, env=self.env)

    def get_text(self) -> str | None:
        try:
            res = self._run(self.read_cmd)
        except (subprocess.SubprocessError, OSError):
            return None
        if res.returncode != 0:
            return None
        try:
            return res.stdout.decode("utf-8", "replace")
        except Exception:
            return None

    def set_text(self, text: str) -> bool:
        try:
            res = self._run(self.write_cmd, data=text.encode("utf-8"))
        except (subprocess.SubprocessError, OSError):
            return False
        return res.returncode == 0


class WaylandClipboard(_CommandClipboard):
    name = "wayland"

    def __init__(self) -> None:
        self.available = bool(shutil.which("wl-paste") and shutil.which("wl-copy"))
        self.environment = os.environ.get("WAYLAND_DISPLAY")
        self.read_cmd = ["wl-paste", "--no-newline", "--type", "text"]
        self.write_cmd = ["wl-copy", "--type", "text"]
        self.env = dict(os.environ)


class XClipClipboard(_CommandClipboard):
    name = "xclip"

    def __init__(self) -> None:
        self.available = bool(shutil.which("xclip") and os.environ.get("DISPLAY"))
        self.read_cmd = ["xclip", "-selection", "clipboard", "-o"]
        self.write_cmd = ["xclip", "-selection", "clipboard", "-i"]
        self.env = dict(os.environ)


class NullClipboard:
    name = "none"
    available = False

    def get_text(self):
        return None

    def set_text(self, text):
        return False


def create_clipboard():
    """挑一个当前环境能用的剪贴板实现。"""
    if sys.platform == "win32":
        return WinClipboard()
    if sys.platform.startswith("linux"):
        order = []
        if os.environ.get("WAYLAND_DISPLAY"):
            order.append(WaylandClipboard)
        order.append(XClipClipboard)
        if os.environ.get("WAYLAND_DISPLAY"):
            order.append(WaylandClipboard)
        seen = set()
        for cls in order:
            if cls in seen:
                continue
            seen.add(cls)
            try:
                inst = cls()
            except Exception:
                continue
            if inst.available:
                return inst
    return NullClipboard()


# ---------------------------------------------------------------------------
# 同步器
# ---------------------------------------------------------------------------
class ClipboardSync:
    """轮询本机剪贴板，变化时回调；收到远端内容时写入并防回环。"""

    def __init__(self, on_change=None, clip=None,
                 interval: float = POLL_INTERVAL) -> None:
        self.clip = clip if clip is not None else create_clipboard()
        self.on_change = on_change
        self.interval = interval
        self.enabled = bool(self.clip.available)
        self._last: bytes | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stats_sent = 0
        self.stats_recv = 0

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if not self.enabled or self._running:
            return
        # 以启动时的内容为基准，避免一启动就把旧内容推出去
        self._last = self._current_digest()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="kvm-clip",
                                        daemon=True)
        self._thread.start()
        log.info("剪贴板同步已启用（%s）", self.clip.name)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _current_digest(self) -> bytes | None:
        text = self.clip.get_text()
        if text is None or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            return None
        return digest(text)

    def _loop(self) -> None:
        while self._running:
            try:
                text = self.clip.get_text()
                if text is not None:
                    size = len(text.encode("utf-8", "replace"))
                    if size > MAX_TEXT_BYTES:
                        pass          # 太大，不同步
                    else:
                        d = digest(text)
                        with self._lock:
                            changed = d != self._last
                            if changed:
                                self._last = d
                        if changed and self.on_change:
                            self.on_change(text)
                            self.stats_sent += 1
            except Exception:
                log.debug("读取剪贴板失败", exc_info=True)
            # 用短睡眠分片，stop() 能及时生效
            slept = 0.0
            while self._running and slept < self.interval:
                threading.Event().wait(0.1)
                slept += 0.1

    # -- 远端内容 -----------------------------------------------------------
    def apply_remote(self, text: str) -> bool:
        """把远端内容写进本机剪贴板，并标记指纹。"""
        if not self.enabled:
            return False
        if text is None:
            return False
        if len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            return False
        d = digest(text)
        with self._lock:
            if d == self._last:
                return False        # 内容一样，不用重复写
            self._last = d
        ok = self.clip.set_text(text)
        if ok:
            self.stats_recv += 1
        return ok
