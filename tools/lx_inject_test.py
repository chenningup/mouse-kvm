#!/usr/bin/env python3
"""Linux 从机注入验证：确认 uinput 虚拟鼠标能真正驱动 Wayland/X 光标。

先记下光标位置 -> 注入绝对移动 -> 再读位置，比对。
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/mouse-kvm"))

DISPLAY = os.environ.get("TEST_DISPLAY", ":0")
ENV = dict(os.environ, DISPLAY=DISPLAY)


def where():
    try:
        out = subprocess.run(["xdotool", "getmouselocation"],
                             capture_output=True, text=True, timeout=3,
                             env=ENV).stdout.strip()
        parts = dict(p.split(":") for p in out.split())
        return int(parts["x"]), int(parts["y"])
    except Exception as exc:
        return None


def xinput_names():
    try:
        out = subprocess.run(["xinput", "list", "--short"],
                             capture_output=True, text=True, timeout=5,
                             env=ENV).stdout
        return [l.split("\t")[-1].strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


from mouse_kvm.backend.linux import LinuxBackend

print("=== 注入前 ===")
print("  DISPLAY =", DISPLAY)
print("  xdotool 读到光标:", where())
print("  xinput 设备:", xinput_names())

b = LinuxBackend(use_abs=True, capture=False)
print("\n=== 创建虚拟鼠标并注入 ===")
try:
    b.inject_move_abs(640, 400, (3840, 2160))
    time.sleep(0.5)
    print("  注入 (640,400) 后 ->", where())
    b.inject_move_abs(1920, 1080, (3840, 2160))
    time.sleep(0.5)
    print("  注入 (1920,1080) 后 ->", where())
    b.inject_move_abs(3000, 1800, (3840, 2160))
    time.sleep(0.5)
    print("  注入 (3000,1800) 后 ->", where())
    print("  xinput 设备:", xinput_names())
except Exception as exc:
    print("  FAIL:", type(exc).__name__, exc)

print("\n=== 相对模式对比 ===")
b2 = LinuxBackend(use_abs=False, capture=False)
try:
    time.sleep(0.3)
    before = where()
    b2.inject_move_rel(200, 120)
    time.sleep(0.4)
    after = where()
    print("  相对 +200/+120:", before, "->", after)
except Exception as exc:
    print("  FAIL:", type(exc).__name__, exc)

b.stop()
b2.stop()
print("\n=== 完 ===")
