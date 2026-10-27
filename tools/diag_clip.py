#!/usr/bin/env python3
"""看一眼 Windows 剪贴板上到底有哪些格式。

    python tools/diag_clip.py

排查"Linux 复制了，Windows 右键没有粘贴"这类问题时用：
如果这里能看到 CF_UNICODETEXT，说明内容已经到位，
问题在目标程序的菜单逻辑；如果没有，就是写入环节失败了。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":
    raise SystemExit("这个脚本只能在 Windows 上跑")

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)

u.OpenClipboard.argtypes = [wintypes.HWND]
u.OpenClipboard.restype = wintypes.BOOL
u.CloseClipboard.restype = wintypes.BOOL
u.EnumClipboardFormats.argtypes = [wintypes.UINT]
u.EnumClipboardFormats.restype = wintypes.UINT
u.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR,
                                      ctypes.c_int]
u.GetClipboardFormatNameW.restype = ctypes.c_int
u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
u.IsClipboardFormatAvailable.restype = wintypes.BOOL
u.GetClipboardOwner.restype = wintypes.HWND
u.GetClipboardSequenceNumber.restype = wintypes.DWORD
u.GetClipboardData.argtypes = [wintypes.UINT]
u.GetClipboardData.restype = wintypes.HANDLE
k.GlobalLock.argtypes = [wintypes.HGLOBAL]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

STANDARD = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYSMETAFILE",
    5: "CF_DIF", 6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE",
    10: "CF_PENDATA", 11: "CF_RIFF", 12: "CF_WAVE", 13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE", 15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
}


def fmt_name(fid: int) -> str:
    if fid in STANDARD:
        return STANDARD[fid]
    buf = ctypes.create_unicode_buffer(256)
    n = u.GetClipboardFormatNameW(fid, buf, 256)
    return buf.value if n else "0x%04x" % fid


def main() -> int:
    seq = u.GetClipboardSequenceNumber()
    print("剪贴板序号（每改一次 +1）：%d" % seq)

    if not u.OpenClipboard(None):
        print("打不开剪贴板（err=%d）—— 有别的程序正占着它"
              % ctypes.get_last_error())
        return 1
    try:
        owner = u.GetClipboardOwner()
        print("剪贴板属主窗口：%s" % (("0x%X" % owner) if owner else "NULL（无窗口属主）"))

        print("可用格式：")
        fid = 0
        count = 0
        while True:
            fid = u.EnumClipboardFormats(fid)
            if not fid:
                break
            count += 1
            extra = ""
            if fid == 13:
                h = u.GetClipboardData(13)
                if h:
                    ptr = k.GlobalLock(h)
                    if ptr:
                        txt = ctypes.wstring_at(ptr)
                        k.GlobalUnlock(h)
                        extra = "  <- %r" % (txt[:60] + ("…" if len(txt) > 60 else ""))
            print("  %-24s (id=%d)%s" % (fmt_name(fid), fid, extra))
        if count == 0:
            print("  （空）")

        print("\n关键判定：")
        for cid, label in ((13, "CF_UNICODETEXT"), (1, "CF_TEXT"),
                           (15, "CF_HDROP（文件）")):
            ok = u.IsClipboardFormatAvailable(cid)
            print("  %-22s %s" % (label, "有" if ok else "没有"))
    finally:
        u.CloseClipboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
