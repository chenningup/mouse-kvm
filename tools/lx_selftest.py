#!/usr/bin/env python3
"""Linux 从机自检：设备发现、uinput 注入、屏幕尺寸探测。"""

import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/mouse-kvm"))

print("=== 1. evdev 导入 ===")
try:
    import evdev
    print("   ok, evdev", getattr(evdev, "__version__", "(no version attr)"))
except Exception as exc:
    print("   FAIL", exc)
    sys.exit(1)

print("=== 2. 屏幕尺寸 ===")
from mouse_kvm.backend.linux import detect_screen_size
print("   ", detect_screen_size())

print("=== 3. 设备发现 ===")
from mouse_kvm.backend.linux import LinuxBackend
b = LinuxBackend()
try:
    b.start()
except Exception as exc:
    print("   FAIL start:", exc)
    sys.exit(1)
print("   找到 %d 个设备:" % len(b._devices))
for d in b._devices:
    caps = d.capabilities()
    kinds = []
    if evdev.ecodes.EV_REL in caps:
        kinds.append("REL")
    if evdev.ecodes.EV_KEY in caps:
        kinds.append("KEY")
    print("     %-16s %-32s %s" % (d.path, d.name, "/".join(kinds)))

print("=== 4. uinput 注入 ===")
try:
    before = len(evdev.list_devices())
    b.inject_move_abs(400, 300, (1920, 1080))
    time.sleep(0.2)
    b.inject_key(30, True)
    b.inject_key(30, False)
    b.inject_button(1, True)
    b.inject_button(1, False)
    b.inject_scroll(0, 1)
    time.sleep(0.2)
    after = len(evdev.list_devices())
    print("   注入调用成功，设备数 %d -> %d" % (before, after))
    names = [evdev.InputDevice(p).name for p in evdev.list_devices()]
    mine = [n for n in names if n.startswith("mouse-kvm")]
    print("   创建的虚拟设备:", mine)
except Exception as exc:
    print("   FAIL 注入:", type(exc).__name__, exc)

print("=== 5. 事件读取 ===")
got = []
b.sink = lambda ev: got.append(ev)
time.sleep(0.5)
print("   0.5 秒内收到 %d 个本地事件" % len(got))
for ev in got[:5]:
    print("     ", ev)

b.stop()
print("=== 完成 ===")
