#!/usr/bin/env python3
"""从机端自测：假主机发消息，验证注入与切走时的按键释放。

    python tests/test_client.py
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
from mouse_kvm.client import Client

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
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

    def __init__(self):
        super().__init__()
        self.log: list[tuple] = []
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def screen_size(self):
        return (2560, 1440)

    def cursor_pos(self):
        return (0, 0)

    def set_cursor_pos(self, x, y):
        pass

    def inject_move_abs(self, x, y, screen):
        self.log.append(("move", x, y))

    def inject_move_rel(self, dx, dy):
        self.log.append(("rel", dx, dy))

    def inject_button(self, btn, down):
        self.log.append(("button", btn, down))

    def inject_scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))

    def inject_key(self, code, down):
        self.log.append(("key", code, down))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    port = free_port()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    backend = MockBackend()
    client = Client("slave", "127.0.0.1", port, backend, token="")
    threading.Thread(target=client.run, daemon=True).start()

    print("[1] 握手")
    conn, _ = srv.accept()
    conn.settimeout(5)

    def recv():
        hdr = b""
        while len(hdr) < 4:
            hdr += conn.recv(4 - len(hdr))
        (n,) = struct.unpack(">I", hdr)
        body = b""
        while len(body) < n:
            body += conn.recv(n - len(body))
        return json.loads(body)

    def send(obj):
        data = json.dumps(obj).encode()
        conn.sendall(struct.pack(">I", len(data)) + data)

    hello = recv()
    check("收到 hello", hello.get("t") == P.MSG_HELLO, str(hello))
    check("上报本机屏幕", hello.get("screen") == [2560, 1440], str(hello))
    send({"t": P.MSG_WELCOME, "name": "mockhost",
          "screen": [1920, 1080], "side": "right"})
    time.sleep(0.25)
    check("后端已启动", backend.started)

    print("[2] 接管后注入")
    send({"t": P.MSG_ENTER, "x": 0, "y": 700})
    send({"t": P.MSG_MOVE, "x": 120, "y": 710})
    send({"t": P.MSG_KEY, "code": 42, "down": True})    # LShift
    send({"t": P.MSG_BUTTON, "btn": 1, "down": True})
    send({"t": P.MSG_SCROLL, "dx": 0, "dy": -1})
    time.sleep(0.3)
    check("enter 注入绝对坐标", ("move", 0, 700) in backend.log, str(backend.log))
    check("move 注入绝对坐标", ("move", 120, 710) in backend.log, str(backend.log))
    check("按键被注入", ("key", 42, True) in backend.log, str(backend.log))
    check("鼠标键被注入", ("button", 1, True) in backend.log, str(backend.log))
    check("滚轮被注入", ("scroll", 0, -1) in backend.log, str(backend.log))
    check("处于接管状态", client.active is True)

    print("[3] 主机离开时清理按键")
    send({"t": P.MSG_LEAVE})
    time.sleep(0.3)
    check("Shift 被抬起", ("key", 42, False) in backend.log, str(backend.log))
    check("左键被抬起", ("button", 1, False) in backend.log, str(backend.log))
    check("已退出接管", client.active is False)

    print("[4] 主机断开")
    send({"t": P.MSG_BYE})
    time.sleep(0.4)
    check("从机停止运行", client.running is False)
    conn.close()
    srv.close()

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
