"""跨机剪贴板同步。

两端对称：
  - 本机剪贴板变了 -> 回调把内容发出去
  - 收到远端内容 -> 写进本机剪贴板，同时记住它的指纹避免回环

分两种内容：
  - 文本：直接同步字符串。
  - 文件：Linux 上 Ctrl+C 一个文件，剪贴板里是 text/uri-list，
    不能当文本发过去 —— Windows 收到字符串只会得到一段路径文字，
    资源管理器右键根本没有"粘贴"，因为它要的是 CF_HDROP。
    所以文件走真正的传输：先推文件过去，再让对方把落盘路径塞进
    CF_HDROP，这样右键粘贴、Ctrl+V 都正常。

图片一律跳过 —— 传输量不可控，卡顿感很明显。
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
import struct
import subprocess
import sys
import threading
from ctypes import wintypes
from urllib.parse import unquote, urlparse

log = logging.getLogger("kvm.clip")

MAX_TEXT_BYTES = 256 * 1024   # 超过这个长度不同步，避免卡住网络
MAX_CLIP_FILES = 20           # 一次最多同步这么多个文件
POLL_INTERVAL = 0.6           # 轮询间隔（秒）
CF_UNICODETEXT = 13
CF_HDROP = 15

# DROPFILES: DWORD pFiles + POINT(pt) + BOOL fNC + BOOL fWide，64 位下 20 字节
_DROPFILES = struct.Struct("<IiiII")
_DROPFILES_SIZE = _DROPFILES.size


def digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "replace"),
                           digest_size=16).digest()


def parse_uri_list(text: str) -> list[str]:
    """把 text/uri-list 解析成本地路径，非 file:// 的一律丢掉。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line == "copy":
            continue
        if not line.startswith("file://"):
            continue
        path = unquote(urlparse(line).path)
        if path:
            out.append(path)
    return out


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

    try:
        _shell = ctypes.WinDLL("shell32", use_last_error=True)
        _shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                          wintypes.LPWSTR, wintypes.UINT]
        _shell.DragQueryFileW.restype = wintypes.UINT
    except OSError:      # pragma: no cover
        _shell = None

    GMEM_MOVEABLE = 0x0002


def dropfiles_payload(paths: list[str]) -> bytes:
    """构造 CF_HDROP 的内存块：DROPFILES 头 + 双 \0 结尾的宽字符路径表。"""
    header = _DROPFILES.pack(_DROPFILES_SIZE, 0, 0, 0, 1)
    body = "".join(p + "\x00" for p in paths) + "\x00"
    return header + body.encode("utf-16-le")


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

    def _put(self, fmt: int, raw: bytes) -> bool:
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
            if not _u.SetClipboardData(fmt, handle):
                _k.GlobalFree(handle)
                return False
            return True   # 成功后句柄归系统所有，不能 Free
        finally:
            _u.CloseClipboard()

    def set_text(self, text: str) -> bool:
        return self._put(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")

    def set_files(self, paths: list[str]) -> bool:
        """把本地路径放进剪贴板当"复制的文件"。

        资源管理器的右键"粘贴"就是靠 CF_HDROP 判断的，
        只放文本的话菜单里根本不会出现粘贴。
        """
        clean = [os.path.abspath(p) for p in paths if os.path.exists(p)]
        if not clean:
            log.warning("要放进剪贴板的文件都不存在：%r", paths)
            return False
        return self._put(CF_HDROP, dropfiles_payload(clean))

    def get_file_uris(self) -> list[str]:
        """读剪贴板里的文件列表（Windows 侧，留给反方向用）。"""
        if _shell is None or not self._open():
            return []
        try:
            handle = _u.GetClipboardData(CF_HDROP)
            if not handle:
                return []
            count = _shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            out = []
            for i in range(count):
                need = _shell.DragQueryFileW(handle, i, None, 0)
                buf = ctypes.create_unicode_buffer(need + 1)
                if _shell.DragQueryFileW(handle, i, buf, need + 1):
                    out.append(buf.value)
            return out
        except Exception:
            return []
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
    uri_cmd: list[str] = []
    files_cmd: list[str] = []
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

    def get_file_uris(self) -> list[str]:
        if not self.uri_cmd:
            return []
        try:
            res = self._run(self.uri_cmd)
        except (subprocess.SubprocessError, OSError):
            return []
        if res.returncode != 0:
            return []
        return parse_uri_list(res.stdout.decode("utf-8", "replace"))

    def set_files(self, paths: list[str]) -> bool:
        """X11/Wayland 侧把路径写成 uri-list，供文件管理器粘贴。"""
        if not self.available or not self.files_cmd:
            return False
        uris = "\r\n".join("file://" + p for p in paths) + "\r\n"
        try:
            res = self._run(self.files_cmd, data=uris.encode("utf-8"))
        except (subprocess.SubprocessError, OSError):
            return False
        return res.returncode == 0


class WaylandClipboard(_CommandClipboard):
    name = "wayland"

    def __init__(self) -> None:
        self.available = bool(shutil.which("wl-paste") and shutil.which("wl-copy"))
        self.environment = os.environ.get("WAYLAND_DISPLAY")
        self.read_cmd = ["wl-paste", "--no-newline", "--type", "text"]
        self.write_cmd = ["wl-copy"]
        self.uri_cmd = ["wl-paste", "--no-newline", "--type", "text/uri-list"]
        self.files_cmd = ["wl-copy", "--type", "text/uri-list"]
        self.env = dict(os.environ)

    def set_text(self, text: str) -> bool:
        try:
            # wl-copy 拿到 stdin 就返回，但有的环境会挂住，--paste-once 不用
            res = self._run(["wl-copy", "--type", "text"],
                            data=text.encode("utf-8"))
        except (subprocess.SubprocessError, OSError):
            return False
        return res.returncode == 0


class XClipClipboard(_CommandClipboard):
    name = "xclip"

    def __init__(self) -> None:
        self.available = bool(shutil.which("xclip") and os.environ.get("DISPLAY"))
        self.read_cmd = ["xclip", "-selection", "clipboard", "-o"]
        self.write_cmd = ["xclip", "-selection", "clipboard", "-i"]
        self.uri_cmd = ["xclip", "-selection", "clipboard",
                        "-t", "text/uri-list", "-o"]
        self.files_cmd = ["xclip", "-selection", "clipboard",
                          "-t", "text/uri-list", "-i"]
        self.env = dict(os.environ)


class NullClipboard:
    name = "none"
    available = False

    def get_text(self):
        return None

    def set_text(self, text):
        return False

    def get_file_uris(self):
        return []

    def set_files(self, paths):
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
                 interval: float = POLL_INTERVAL,
                 on_files=None) -> None:
        self.clip = clip if clip is not None else create_clipboard()
        self.on_change = on_change
        self.on_files = on_files
        self.interval = interval
        self.enabled = bool(self.clip.available)
        self._last: bytes | None = None
        self._last_files: bytes | None = None
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
                files = self._read_files()
                if files:
                    self._handle_files(files)
                else:
                    self._handle_text()
            except Exception:
                log.debug("读取剪贴板失败", exc_info=True)
            # 用短睡眠分片，stop() 能及时生效
            slept = 0.0
            while self._running and slept < self.interval:
                threading.Event().wait(0.1)
                slept += 0.1

    def _read_files(self) -> list[str]:
        getter = getattr(self.clip, "get_file_uris", None)
        if getter is None:
            return []
        try:
            files = getter() or []
        except Exception:
            return []
        bad = [f for f in files if not os.path.isfile(f)]
        if bad:
            # 有任何一个文件读不到就不当文件处理，退回普通文本
            return []
        return files[:MAX_CLIP_FILES]

    def _handle_files(self, files: list[str]) -> None:
        d = digest("\x00".join(files))
        with self._lock:
            changed = d != self._last_files
            if changed:
                self._last_files = d
                # 文件复制时别再把路径串当文本推一遍
                self._last = self._current_digest()
        if changed and self.on_files:
            self.on_files(files)
            self.stats_sent += 1

    def _handle_text(self) -> None:
        self._last_files = None      # 剪贴板回到文本，下次复制同一个文件还能触发
        text = self.clip.get_text()
        if text is None:
            return
        if len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            return           # 太大，不同步
        d = digest(text)
        with self._lock:
            changed = d != self._last
            if changed:
                self._last = d
        if changed and self.on_change:
            self.on_change(text)
            self.stats_sent += 1

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

    def apply_remote_files(self, paths: list[str]) -> bool:
        """把远端传过来的文件放进本机剪贴板，右键就能粘贴。"""
        if not self.enabled:
            return False
        setter = getattr(self.clip, "set_files", None)
        if setter is None:
            return False
        ok = bool(setter(paths))
        if ok:
            self.stats_recv += 1
            log.info("剪贴板已放入 %d 个文件，可直接粘贴", len(paths))
        return ok
