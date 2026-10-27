#!/usr/bin/env python3
"""诊断 Windows 绝对坐标注入的真实映射关系。"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mouse_kvm.backend.win32 import (INPUT, INPUT_MOUSE, MOUSEINPUT,
                                    MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_MOVE,
                                    POINT, user32)

user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
print("SetProcessDpiAwarenessContext(-4) ->",
      user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CMONITORS = 80

for name, idx in [("SM_CXSCREEN", SM_CXSCREEN), ("SM_CYSCREEN", SM_CYSCREEN),
                  ("SM_CXVIRTUALSCREEN", SM_CXVIRTUALSCREEN),
                  ("SM_CYVIRTUALSCREEN", SM_CYVIRTUALSCREEN),
                  ("SM_XVIRTUALSCREEN", SM_XVIRTUALSCREEN),
                  ("SM_YVIRTUALSCREEN", SM_YVIRTUALSCREEN),
                  ("SM_CMONITORS", SM_CMONITORS)]:
    print("%-20s = %s" % (name, user32.GetSystemMetrics(idx)))


def inject_raw(nx, ny, extra=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(nx, ny, 0,
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | extra, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


w = user32.GetSystemMetrics(SM_CXSCREEN)
h = user32.GetSystemMetrics(SM_CYSCREEN)
origin = pos()

print("\n归一化坐标 -> 实际光标（映射到主屏 %dx%d）" % (w, h))
print("%10s %10s | %12s %12s | %10s %10s" %
      ("nx", "ny", "got_x", "got_y", "want_x", "want_y"))
for nx, ny in [(0, 0), (16384, 16384), (32768, 32768),
               (49152, 49152), (65535, 65535),
               (2048, 1536), (32768, 16384)]:
    inject_raw(nx, ny)
    time.sleep(0.08)
    gx, gy = pos()
    print("%10d %10d | %12d %12d | %10.0f %10.0f" %
          (nx, ny, gx, gy, nx * (w - 1) / 65535, ny * (h - 1) / 65535))

print("\n测试物理像素 -> 归一化 -> 回读")
for x, y in [(200, 200), (1000, 800), (w // 2, h // 2), (w - 100, h - 100)]:
    nx = int(round(x * 65535 / max(1, w - 1)))
    ny = int(round(y * 65535 / max(1, h - 1)))
    inject_raw(nx, ny)
    time.sleep(0.08)
    print("  想要 (%5d,%5d)  nx/ny (%5d,%5d)  实到 %s" %
          (x, y, nx, ny, pos()))

inject_raw(int(origin[0] * 65535 / max(1, w - 1)),
           int(origin[1] * 65535 / max(1, h - 1)))
