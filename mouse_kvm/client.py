"""从机端：接收主机事件并注入本机。

从机是"哑终端"——它不判断边缘、不维护坐标，
所有位置计算都在主机侧完成，这里只负责注入。
这样两端逻辑简单，也不会有状态不同步的问题。
"""

from __future__ import annotations

import logging
import socket
import time

from . import protocol as P
from .backend.base import Backend
from .clipboard import MAX_TEXT_BYTES, ClipboardSync
from .keys import key_name

log = logging.getLogger("kvm.client")

RECONNECT_DELAY = 2.0


class Client:
    def __init__(self, name: str, host: str, port: int, backend: Backend,
                 token: str = "", enable_clip: bool = True) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.token = token
        self.backend = backend
        self.screen = backend.screen_size()

        self.conn: P.FramedConnection | None = None
        self.running = False
        self.active = False           # 是否处于被主机接管状态
        self._pressed_keys: set[int] = set()
        self._pressed_btns: set[int] = set()
        self._host_name = "?"

        self.clip: ClipboardSync | None = ClipboardSync() if enable_clip else None

    # -- 主流程 -------------------------------------------------------------
    def run(self) -> None:
        self.running = True
        self.backend.start()
        if self.clip is not None:
            if self.clip.enabled:
                self.clip.on_change = self._on_local_clip
                self.clip.start()
            else:
                log.warning("剪贴板同步不可用（缺少 wl-clipboard / xclip）")
        log.info("从机 %s 启动，本机屏幕 %dx%d，目标 %s:%d",
                 self.name, self.screen[0], self.screen[1],
                 self.host, self.port)
        try:
            while self.running:
                try:
                    self._session()
                except (ConnectionError, OSError, P.ProtocolError) as exc:
                    log.warning("连接中断：%s", exc)
                finally:
                    self._release_all()
                    if self.conn:
                        self.conn.close()
                        self.conn = None
                if self.running:
                    log.info("%.0f 秒后重连…", RECONNECT_DELAY)
                    time.sleep(RECONNECT_DELAY)
        finally:
            self._release_all()
            if self.clip is not None:
                self.clip.stop()
            self.backend.stop()
            log.info("从机已停止")

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=8.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        conn = P.FramedConnection(sock, self.name)
        self.conn = conn

        conn.send({"t": P.MSG_HELLO, "name": self.name,
                   "screen": list(self.screen), "token": self.token})

        welcome = conn.recv(timeout=8.0)
        if not welcome:
            raise ConnectionError("等待 welcome 超时")
        if welcome.get("t") == P.MSG_ERROR:
            raise ConnectionError("主机拒绝：%s" % welcome.get("msg"))
        if welcome.get("t") != P.MSG_WELCOME:
            raise ConnectionError("协议异常：%r" % welcome)

        self._host_name = welcome.get("name", "?")
        log.info("已连上主机 %s，位置=%s。光标推到主机屏幕边缘即可接管。",
                 self._host_name, welcome.get("side"))

        while self.running:
            msg = conn.recv(timeout=1.0)
            if msg is None:
                continue
            self._dispatch(msg)

    # -- 消息处理 -----------------------------------------------------------
    def _dispatch(self, msg: dict) -> None:
        t = msg.get("t")

        if t == P.MSG_ENTER:
            self.active = True
            x, y = int(msg.get("x", 0)), int(msg.get("y", 0))
            self.backend.inject_move_abs(x, y, self.screen)
            log.info("主机接管本机，起点 (%d, %d)", x, y)

        elif t == P.MSG_MOVE:
            if self.active:
                self.backend.inject_move_abs(int(msg["x"]), int(msg["y"]),
                                             self.screen)

        elif t == P.MSG_BUTTON:
            btn, down = int(msg["btn"]), bool(msg["down"])
            if down:
                self._pressed_btns.add(btn)
            else:
                self._pressed_btns.discard(btn)
            self.backend.inject_button(btn, down)

        elif t == P.MSG_SCROLL:
            self.backend.inject_scroll(int(msg.get("dx", 0)),
                                       int(msg.get("dy", 0)))

        elif t == P.MSG_KEY:
            code, down = int(msg["code"]), bool(msg["down"])
            if down:
                self._pressed_keys.add(code)
            else:
                self._pressed_keys.discard(code)
            self.backend.inject_key(code, down)

        elif t == P.MSG_CLIP:
            text = msg.get("text") or ""
            if self.clip is not None and self.clip.apply_remote(text):
                log.info("<< 剪贴板来自主机（%d 字符）", len(text))

        elif t == P.MSG_LEAVE:
            self.active = False
            self._release_all()
            log.info("主机已离开本机")

        elif t == P.MSG_PING:
            if self.conn:
                self.conn.send({"t": P.MSG_PONG, "ts": msg.get("ts")})

        elif t == P.MSG_BYE:
            log.info("主机要求断开")
            self.running = False

    # -- 剪贴板 -------------------------------------------------------------
    def _on_local_clip(self, text: str) -> None:
        """本机剪贴板变了，推给主机。"""
        conn = self.conn
        if conn is None or not self.running:
            return
        if len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            return
        try:
            conn.send({"t": P.MSG_CLIP, "text": text})
            log.info("剪贴板 -> 主机（%d 字符）", len(text))
        except Exception:
            pass   # 断线了，重连后剪贴板不值得回放

    # -- 按键清理 -----------------------------------------------------------
    def _release_all(self) -> None:
        """切换走时把按下的键全部抬起，否则回到本机会一直卡着 Ctrl。"""
        if self._pressed_keys:
            log.debug("释放残留按键：%s",
                      ", ".join(key_name(c) for c in sorted(self._pressed_keys)))
        for code in list(self._pressed_keys):
            try:
                self.backend.inject_key(code, False)
            except Exception:
                pass
        self._pressed_keys.clear()

        for btn in list(self._pressed_btns):
            try:
                self.backend.inject_button(btn, False)
            except Exception:
                pass
        self._pressed_btns.clear()
