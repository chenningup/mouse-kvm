#!/usr/bin/env python3
"""真实注入自测（Windows）：确认 SendInput 真的能移动光标。

会短暂移动光标，测完自动归位。键盘只发 F24 这种无副作用的键。
    python tests/test_inject.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mouse_kvm.backend import create_backend

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


def main() -> int:
    b = create_backend()
    if b.platform != "windows":
        print("这个脚本只测 Windows 后端，当前平台：%s" % b.platform)
        return 0

    w, h = b.screen_size()
    print("屏幕 %dx%d" % (w, h))
    origin = b.cursor_pos()
    print("原光标位置 %s" % (origin,))
    try:
        print("[1] 绝对坐标注入")
        b.inject_move_abs(120, 90, (w, h))
        time.sleep(0.15)
        p = b.cursor_pos()
        check("(120,90) 命中", abs(p[0] - 120) <= 2 and abs(p[1] - 90) <= 2, str(p))

        b.inject_move_abs(w // 2, h // 2, (w, h))
        time.sleep(0.15)
        p = b.cursor_pos()
        check("屏幕中心命中",
              abs(p[0] - w // 2) <= 3 and abs(p[1] - h // 2) <= 3, str(p))

        b.inject_move_abs(w - 1, h - 1, (w, h))
        time.sleep(0.15)
        p = b.cursor_pos()
        check("右下角命中",
              abs(p[0] - (w - 1)) <= 3 and abs(p[1] - (h - 1)) <= 3, str(p))

        print("[2] 相对位移注入（受系统鼠标加速影响，只验证方向）")
        b.inject_move_abs(500, 500, (w, h))
        time.sleep(0.12)
        before = b.cursor_pos()
        b.inject_move_rel(37, 21)
        time.sleep(0.15)
        after = b.cursor_pos()
        dx, dy = after[0] - before[0], after[1] - before[1]
        check("相对位移向右下移动", dx > 0 and dy > 0,
              "%s -> %s (dx=%d dy=%d)" % (before, after, dx, dy))
        print("       实际位移 %+d/%+d（>请求值 %+d/%+d 属正常，"
              "Windows 鼠标加速导致）" % (dx, dy, 37, 21))

        print("[3] 按键注入不炸")
        try:
            b.inject_key(194, True)    # F24
            b.inject_key(194, False)
            b.inject_button(1, False)
            b.inject_scroll(0, 1)
            b.inject_scroll(0, -1)
            check("键盘/按键/滚轮调用无异常", True)
        except Exception as exc:
            check("键盘/按键/滚轮调用无异常", False, repr(exc))
    finally:
        b.set_cursor_pos(*origin)
        print("光标已复位到 %s" % (origin,))

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
