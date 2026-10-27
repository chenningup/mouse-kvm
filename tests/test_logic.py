#!/usr/bin/env python3
"""主机端逻辑自测：用假后端 + 假从机，验证握手、边缘切换、坐标映射、退出。

不碰真实输入设备，所以可以随时跑：
    python tests/test_logic.py
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mouse_kvm import protocol as P
from mouse_kvm.backend.base import Backend
from mouse_kvm.clipboard import ClipboardSync
from mouse_kvm.host import Host

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


class MockBackend(Backend):
    name = "mock"
    platform = "test"
    supports_abs = True
    can_move_cursor = True

    def __init__(self) -> None:
        super().__init__()
        self.size = (1920, 1080)
        self.pos = (960, 540)
        self.anchor = None
        self.suppressed = False
        self.injected: list[tuple] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def screen_size(self):
        return self.size

    def cursor_pos(self):
        return self.pos

    def set_cursor_pos(self, x, y):
        self.pos = (x, y)

    def set_anchor(self, x, y):
        self.anchor = (x, y)
        self.pos = (x, y)

    def set_suppress(self, on: bool) -> None:
        self.suppressed = bool(on)

    def inject_move_abs(self, x, y, screen):
        self.injected.append(("move", x, y))

    def inject_move_rel(self, dx, dy):
        self.injected.append(("rel", dx, dy))

    def inject_button(self, btn, down):
        self.injected.append(("button", btn, down))

    def inject_scroll(self, dx, dy):
        self.injected.append(("scroll", dx, dy))

    def inject_key(self, code, down):
        self.injected.append(("key", code, down))


class MemoryClipboard:
    """内存版剪贴板，避免测试动到真实剪贴板。"""
    name = "memory"
    available = True

    def __init__(self, text=""):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text
        return True


class FakePeer:
    """扮演从机的裸 socket 客户端。"""

    def __init__(self, name="slave", screen=(2560, 1440), port=0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""
        self.msgs: list[dict] = []
        self.send({"t": P.MSG_HELLO, "name": name, "screen": list(screen),
                   "token": ""})

    def send(self, obj):
        data = json.dumps(obj).encode()
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def pump(self, seconds=0.35):
        """读一段时间内到达的所有消息。"""
        end = time.monotonic() + seconds
        self.sock.settimeout(0.05)
        while time.monotonic() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
            while len(self._buf) >= 4:
                (n,) = struct.unpack_from(">I", self._buf, 0)
                if len(self._buf) < 4 + n:
                    break
                payload = self._buf[4:4 + n]
                self._buf = self._buf[4 + n:]
                self.msgs.append(json.loads(payload))
        return self.msgs

    def kinds(self, since=0):
        return [m["t"] for m in self.msgs[since:]]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    port = free_port()
    backend = MockBackend()
    mem = MemoryClipboard()
    host = Host(
        name="mockhost",
        port=port,
        peer_sides={"slave": "right"},
        backend=backend,
        bind="127.0.0.1",
        edge_threshold=3,
        enable_clip=False,
    )
    # 注入内存剪贴板；轮询间隔设得极大，让测试自己控制推送时机
    host.clip = ClipboardSync(clip=mem, interval=999)
    t = threading.Thread(target=host.run, daemon=True)
    t.start()
    time.sleep(0.4)

    print("[1] 握手")
    peer = FakePeer(port=port, screen=(2560, 1440))
    peer.pump(0.5)
    check("收到 welcome", peer.msgs and peer.msgs[0]["t"] == P.MSG_WELCOME,
          str(peer.msgs[:1]))
    check("主机屏幕为 1920x1080",
          peer.msgs[0]["screen"] == [1920, 1080], str(peer.msgs[:1]))
    check("从机已登记到右侧", host.peers_by_side.get("right") is not None)

    print("[2] 本机模式下键盘不外发")
    n0 = len(peer.msgs)
    host._sink(("key", 30, True))     # 按 A
    peer.pump(0.25)
    check("键盘未转发", len(peer.msgs) == n0, str(peer.kinds(n0)))

    print("[3] 边缘切换")
    # 光标推到右边缘并继续外推
    host.local_x, host.local_y = 1919, 540
    for _ in range(4):
        host._sink(("move", 1919, 540))
        time.sleep(0.02)
    peer.pump(0.4)
    check("收到 enter", P.MSG_ENTER in peer.kinds(n0), str(peer.kinds(n0)))
    check("已进入远程模式", host.remote is not None)
    check("本机输入被压制", backend.suppressed is True)
    check("光标被钳制在锚点", backend.anchor is not None, str(backend.anchor))

    enter = [m for m in peer.msgs if m["t"] == P.MSG_ENTER]
    check("enter 起始 x 贴左边缘", enter and enter[0]["x"] == 0, str(enter[:1]))
    expect_y = round(540 / 1079 * 1439)
    check("enter 的 y 按比例映射",
          enter and abs(enter[0]["y"] - expect_y) <= 1,
          "got=%s expect=%s" % (enter[0]["y"] if enter else None, expect_y))

    print("[4] 远程模式转发")
    n1 = len(peer.msgs)
    ax, ay = backend.anchor
    host._sink(("move", ax + 100, ay))   # 往右推 100 像素
    host._sink(("button", 1, True))
    host._sink(("key", 30, True))
    host._sink(("scroll", 0, -1))
    peer.pump(0.4)
    kinds = peer.kinds(n1)
    check("收到 move", P.MSG_MOVE in kinds, str(kinds))
    check("收到 button", P.MSG_BUTTON in kinds, str(kinds))
    check("收到 key", P.MSG_KEY in kinds, str(kinds))
    check("收到 scroll", P.MSG_SCROLL in kinds, str(kinds))

    moves = [m for m in peer.msgs[n1:] if m["t"] == P.MSG_MOVE]
    # 100 px * (2560/1920) ≈ 133 px，加上切入瞬间残余的几个像素
    check("move 按屏幕比例缩放",
          moves and 130 <= moves[0]["x"] <= 142, str(moves[:1]))

    print("[5] 推到底不退出，往左推出才回本机")
    n2 = len(peer.msgs)
    for _ in range(12):                  # 继续往右推，从机右侧没有屏幕了
        host._sink(("move", ax + 300, ay))
        time.sleep(0.01)
    peer.pump(0.3)
    check("右侧推到底不退出", host.remote is not None, str(peer.kinds(n2)))
    stuck = [m for m in peer.msgs[n2:] if m["t"] == P.MSG_MOVE]
    check("坐标被钳在从机右边缘",
          stuck and stuck[-1]["x"] == 2559, str(stuck[-1:] if stuck else []))

    n3 = len(peer.msgs)
    for _ in range(20):                  # 往回推，越过从机左边缘
        host._sink(("move", ax - 300, ay))
        time.sleep(0.01)
    peer.pump(0.4)
    check("收到 leave", P.MSG_LEAVE in peer.kinds(n3), str(peer.kinds(n3)))
    check("已退回本机模式", host.remote is None)
    check("本机输入恢复", backend.suppressed is False)
    check("光标回到主机右边缘", backend.pos[0] >= 1910, str(backend.pos))

    print("[6] 紧急退出 Ctrl+Alt+Esc")
    host._cooldown_until = 0.0
    host.local_x, host.local_y = 1919, 540
    for _ in range(4):
        host._sink(("move", 1919, 540))
        time.sleep(0.02)
    peer.pump(0.4)
    check("重新进入远程", host.remote is not None, str(peer.kinds()))

    n4 = len(peer.msgs)
    host._sink(("key", 29, True))    # LCtrl
    host._sink(("key", 56, True))    # LAlt
    host._sink(("key", 1, True))     # Esc
    peer.pump(0.4)
    check("紧急退出生效", host.remote is None, str(peer.kinds(n4)))
    escaped = [m for m in peer.msgs[n4:]
               if m["t"] == P.MSG_KEY and m["code"] == 1 and m["down"]]
    check("Esc 没有转发出去", not escaped, str(escaped))
    ups = {m["code"] for m in peer.msgs[n4:]
           if m["t"] == P.MSG_KEY and not m["down"]}
    check("Ctrl/Alt 都被抬起", {29, 56} <= ups, str(ups))
    check("本机输入恢复", backend.suppressed is False)

    print("[7] 剪贴板同步")
    n5 = len(peer.msgs)
    host._broadcast_clip("来自 Windows 的文本")
    peer.pump(0.4)
    clips = [m for m in peer.msgs[n5:] if m["t"] == P.MSG_CLIP]
    check("本机复制内容广播到从机",
          clips and clips[0]["text"] == "来自 Windows 的文本", str(clips[:1]))

    n6 = len(peer.msgs)
    host._on_peer_msg("slave", {"t": P.MSG_CLIP, "text": "来自 Linux 的文本"})
    peer.pump(0.3)
    check("从机复制的内容写进本机", mem.text == "来自 Linux 的文本", repr(mem.text))
    back = [m for m in peer.msgs[n6:] if m["t"] == P.MSG_CLIP]
    check("不会原样回传给来源", not back, str(back))

    n7 = len(peer.msgs)
    host._broadcast_clip("来自 Linux 的文本")
    peer.pump(0.3)
    dup = [m for m in peer.msgs[n7:] if m["t"] == P.MSG_CLIP]
    check("重复内容不重复广播", not dup, str(dup))

    print("[8] 从机断开")
    peer.close()
    time.sleep(0.6)
    check("从机被移除", host.peers_by_side.get("right") is None,
          str(host.peers_by_side))

    host.shutdown()

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
