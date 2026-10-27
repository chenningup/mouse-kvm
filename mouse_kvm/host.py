"""主机端：捕获本地输入，在屏幕边缘切换到从机。

屏幕布局（--peer name=side）:

        [ top ]
  [left] HOST [right]
       [ bottom ]

光标推到某条边并继续外推，就切到该方向的从机；
从机的对应边缘继续外推则回到本机。
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time

from . import protocol as P
from .backend import Backend
from .clipboard import MAX_TEXT_BYTES, ClipboardSync, digest
from .keys import key_name

log = logging.getLogger("kvm.host")

SIDES = ("left", "right", "top", "bottom")
EDGE_PULLBACK = 3      # Windows 上把光标从边缘拉回几像素，保证能持续收到移动事件
EXIT_MARGIN = 2        # 远程侧越界多少像素才判定离开，防止手抖误触
REENTRY_COOLDOWN = 0.5   # 秒，防止刚回到本机又立刻切出去
PING_INTERVAL = 5.0

# 紧急退出组合键用到的 evdev 码
KEY_ESC = 1
KEY_LCTRL, KEY_RCTRL = 29, 97
KEY_LALT, KEY_RALT = 56, 100


class Peer:
    """一个已连接的从机。"""

    def __init__(self, name: str, side: str, conn: P.FramedConnection,
                 screen: tuple[int, int]) -> None:
        self.name = name
        self.side = side
        self.conn = conn
        self.screen = screen
        self.alive = True

    def send(self, obj: dict) -> None:
        if not self.alive:
            return
        try:
            self.conn.send(obj)
        except Exception as exc:
            log.warning("[%s] 发送失败，标记断开: %s", self.name, exc)
            self.alive = False

    def close(self) -> None:
        self.alive = False
        self.conn.close()

    def __repr__(self) -> str:
        return "<Peer %s side=%s %dx%d>" % (self.name, self.side,
                                            self.screen[0], self.screen[1])


class Host:
    def __init__(self, name: str, port: int, peer_sides: dict[str, str],
                 backend: Backend, token: str = "", bind: str = "0.0.0.0",
                 edge_threshold: int = 3, enable_clip: bool = True,
                 clip=None) -> None:
        self.name = name
        self.port = port
        self.bind = bind
        self.token = token
        self.peer_sides = dict(peer_sides)
        self.edge_threshold = max(1, edge_threshold)
        self.backend = backend

        self.local_screen = backend.screen_size()
        self._screen_cache = self.local_screen

        self.peers: dict[str, Peer] = {}
        self.peers_by_side: dict[str, Peer] = {}

        self.q: queue.Queue = queue.Queue(maxsize=20000)
        self.running = False
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

        # 本地光标状态
        self.local_x = self.local_screen[0] // 2
        self.local_y = self.local_screen[1] // 2
        self._edge_hits = 0
        self._edge_side: str | None = None
        self._cooldown_until = 0.0

        # 远程状态
        self.remote: Peer | None = None
        self.rx = 0.0
        self.ry = 0.0
        self._anchor_point: tuple[int, int] = (0, 0)
        self._entry_side: str = "right"
        self._held_keys: set[int] = set()
        self._last_ping = 0.0

        # 剪贴板
        if clip is not None:
            self.clip = clip
        else:
            self.clip = ClipboardSync() if enable_clip else None
        self._clip_hash: bytes | None = None

    # -- 启动/停止 ----------------------------------------------------------
    def run(self) -> None:
        self.running = True
        self.backend.sink = self._sink
        self.backend.start()
        self._start_server()
        if self.clip is not None:
            if self.clip.enabled:
                self.clip.on_change = self._on_local_clip
                self.clip.start()
            else:
                log.warning("剪贴板同步不可用（缺少 wl-clipboard / xclip）")
        log.info("主机 %s 已启动，屏幕 %dx%d，监听 %s:%d",
                 self.name, self.local_screen[0], self.local_screen[1],
                 self.bind, self.port)
        if self.peer_sides:
            log.info("等待从机连接：%s", ", ".join(
                "%s(%s)" % (n, s) for n, s in self.peer_sides.items()))
        else:
            log.warning("没有配置 --peer，任何从机将以 side=right 接入")

        try:
            self._loop()
        finally:
            self.shutdown()

    def _loop(self) -> None:
        while self.running:
            try:
                src, data = self.q.get(timeout=0.2)
            except queue.Empty:
                self._tick()
                continue
            try:
                if src == "local":
                    self._on_local(data)
                elif src == "ctl":
                    self._on_control(data)
                elif src == "clip":
                    self._broadcast_clip(data)
                else:
                    self._on_peer_msg(src, data)
            except Exception:
                log.exception("处理事件 %r 失败", src)
        self._tick()

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_ping >= PING_INTERVAL:
            self._last_ping = now
            for peer in list(self.peers.values()):
                if not peer.alive:
                    self._drop_peer(peer, "连接失效")
                    continue
                peer.send({"t": P.MSG_PING, "ts": now})

    def shutdown(self) -> None:
        self.running = False
        if self.clip is not None:
            self.clip.stop()
        if self.remote:
            self._exit_remote("程序退出")
        for peer in list(self.peers.values()):
            try:
                peer.send({"t": P.MSG_BYE})
            except Exception:
                pass
            peer.close()
        self.peers.clear()
        self.peers_by_side.clear()
        try:
            self.backend.stop()
        except Exception:
            pass
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        log.info("主机已停止")

    # -- 事件入口 -----------------------------------------------------------
    def _sink(self, ev) -> None:
        try:
            self.q.put_nowait(("local", ev))
        except queue.Full:
            pass

    def _post(self, kind: str, payload) -> None:
        try:
            self.q.put_nowait((kind, payload))
        except queue.Full:
            pass

    # -- 剪贴板 -------------------------------------------------------------
    def _on_local_clip(self, text: str) -> None:
        """剪贴板轮询线程的回调，转投主线程处理。

        peers 字典只在主线程增删，这里不能直接动。
        """
        self._post("clip", text)

    def _broadcast_clip(self, text: str) -> None:
        d = digest(text)
        if d == self._clip_hash:
            return
        self._clip_hash = d
        sent = 0
        for peer in list(self.peers.values()):
            if peer.alive:
                peer.send({"t": P.MSG_CLIP, "text": text})
                sent += 1
        if sent:
            log.info("剪贴板 -> %d 台（%d 字符）", sent, len(text))

    def _relay_clip(self, src: str, text: str) -> None:
        """从某台从机收到的剪贴板：写进本机，再转给其它从机。"""
        if len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            log.debug("来自 %s 的剪贴板过大，忽略", src)
            return
        d = digest(text)
        if d == self._clip_hash:
            return
        self._clip_hash = d
        if self.clip is not None:
            self.clip.apply_remote(text)
        for name, peer in list(self.peers.items()):
            if name != src and peer.alive:
                peer.send({"t": P.MSG_CLIP, "text": text})
        log.info("<< 剪贴板来自 %s（%d 字符）", src, len(text))

    # -- 本地事件 -----------------------------------------------------------
    def _on_local(self, ev) -> None:
        kind = ev[0]
        if self.remote is not None:
            self._on_local_remote(ev)
        else:
            self._on_local_native(ev)

    def _on_local_native(self, ev) -> None:
        """本机模式：只关心光标位置，用来判断是否该切出。"""
        kind = ev[0]
        if kind == "move":
            _, x, y = ev
            self.local_x, self.local_y = x, y
            self._check_edges(x, y)
        elif kind == "rel":
            _, dx, dy = ev
            w, h = self.local_screen
            self.local_x = max(0, min(w - 1, self.local_x + dx))
            self.local_y = max(0, min(h - 1, self.local_y + dy))
            if dx or dy:
                self._check_edges(self.local_x, self.local_y)

    def _on_local_remote(self, ev) -> None:
        """远程模式：把事件转发给从机。"""
        assert self.remote is not None
        peer = self.remote
        kind = ev[0]

        if kind == "move":
            _, x, y = ev
            ax, ay = self._anchor_point
            dx, dy = x - ax, y - ay
            if abs(dx) > 400 or abs(dy) > 400:
                # 钳制失败（比如被 UAC 桌面挡住），别让坐标一路狂飙
                self._anchor_point = (x, y)
                self.backend.set_anchor(x, y)
                return
            self._remote_move(dx, dy)
        elif kind == "rel":
            _, dx, dy = ev
            self._remote_move(dx, dy)
        elif kind == "button":
            _, btn, down = ev
            peer.send({"t": P.MSG_BUTTON, "btn": btn, "down": down})
        elif kind == "scroll":
            _, dx, dy = ev
            peer.send({"t": P.MSG_SCROLL, "dx": dx, "dy": dy})
        elif kind == "key":
            _, code, down = ev
            if down:
                self._held_keys.add(code)
            else:
                self._held_keys.discard(code)

            # 紧急退出：Ctrl + Alt + Esc，立刻抢回本机控制权，不转发
            if (down and code == KEY_ESC
                    and (KEY_LCTRL in self._held_keys or KEY_RCTRL in self._held_keys)
                    and (KEY_LALT in self._held_keys or KEY_RALT in self._held_keys)):
                log.warning("!! 紧急退出：Ctrl+Alt+Esc")
                self._release_remote_keys()
                self._exit_remote("紧急退出")
                return

            peer.send({"t": P.MSG_KEY, "code": code, "down": down})

    def _release_remote_keys(self) -> None:
        """退出前把已经转发过去的按键全部抬起，免得对端卡键。"""
        if not self.remote:
            self._held_keys.clear()
            return
        for code in list(self._held_keys):
            self.remote.send({"t": P.MSG_KEY, "code": code, "down": False})
        self._held_keys.clear()

    # -- 边缘切换 -----------------------------------------------------------
    def _check_edges(self, x: int, y: int) -> None:
        if time.monotonic() < self._cooldown_until:
            return
        w, h = self.local_screen
        side = None
        if x >= w - 1:
            side = "right"
        elif x <= 0:
            side = "left"
        elif y >= h - 1:
            side = "bottom"
        elif y <= 0:
            side = "top"

        peer = self.peers_by_side.get(side) if side else None
        if peer is None or not peer.alive:
            self._edge_hits = 0
            self._edge_side = None
            return

        if side != self._edge_side:
            self._edge_side = side
            self._edge_hits = 1
            self._pull_back(side)
            return

        self._edge_hits += 1
        self._pull_back(side)
        if self._edge_hits >= self.edge_threshold:
            self._edge_hits = 0
            self._edge_side = None
            self._enter_remote(peer, side, x, y)

    def _pull_back(self, side: str) -> None:
        """Windows 上把光标从屏幕边缘拉回几像素。

        否则光标一旦贴在边缘，系统就不再上报新的移动事件，
        我们就没法判断用户是不是还在继续往外推。
        """
        if not getattr(self.backend, "can_move_cursor", False):
            return
        w, h = self.local_screen
        x, y = self.local_x, self.local_y
        if side == "right":
            x = w - 1 - EDGE_PULLBACK
        elif side == "left":
            x = EDGE_PULLBACK
        elif side == "bottom":
            y = h - 1 - EDGE_PULLBACK
        elif side == "top":
            y = EDGE_PULLBACK
        self.backend.set_cursor_pos(x, y)
        self.local_x, self.local_y = x, y

    def _enter_remote(self, peer: Peer, side: str, x: int, y: int) -> None:
        w, h = self.local_screen
        cw, ch = peer.screen

        if side in ("right", "left"):
            ry = (y / max(1, h - 1)) * (ch - 1)
            rx = 0.0 if side == "right" else float(cw - 1)
        else:
            rx = (x / max(1, w - 1)) * (cw - 1)
            ry = 0.0 if side == "bottom" else float(ch - 1)

        self._anchor_point = self.backend.cursor_pos()
        self._entry_side = side
        self.remote = peer
        self.rx, self.ry = rx, ry
        self.backend.set_suppress(True)
        self.backend.set_anchor(*self._anchor_point)
        self._held_keys.clear()
        peer.send({"t": P.MSG_ENTER, "x": round(rx), "y": round(ry)})
        log.info(">> 切换到 %s (%s)，起始坐标 (%.0f, %.0f)",
                 peer.name, side, rx, ry)

    def _remote_move(self, dx: int, dy: int) -> None:
        peer = self.remote
        if peer is None:
            return
        w, h = self.local_screen
        cw, ch = peer.screen
        self.rx += dx * (cw / max(1, w))
        self.ry += dy * (ch / max(1, h))

        side = self._entry_side
        if side in ("right", "left"):
            # 从主机右侧进入的，落点在从机左边缘，只有往左推出才算离开；
            # 反方向推到底就卡在边缘，不会莫名其妙跳回主机。
            if side == "right" and self.rx < -EXIT_MARGIN:
                self._exit_remote("从机左边缘")
                return
            if side == "left" and self.rx > cw - 1 + EXIT_MARGIN:
                self._exit_remote("从机右边缘")
                return
        else:
            if side == "bottom" and self.ry > ch - 1 + EXIT_MARGIN:
                self._exit_remote("从机下边缘")
                return
            if side == "top" and self.ry < -EXIT_MARGIN:
                self._exit_remote("从机上边缘")
                return

        self.rx = max(0.0, min(float(cw - 1), self.rx))
        self.ry = max(0.0, min(float(ch - 1), self.ry))
        peer.send({"t": P.MSG_MOVE, "x": round(self.rx), "y": round(self.ry)})

    def _exit_remote(self, reason: str) -> None:
        peer = self.remote
        if peer is None:
            return
        try:
            peer.send({"t": P.MSG_LEAVE})
        except Exception:
            pass
        self.backend.set_suppress(False)
        self.remote = None
        self._held_keys.clear()

        # 回到主机时贴着当初切出去的那条边，形成连续的手感
        w, h = self.local_screen
        cw, ch = peer.screen
        side = self._entry_side
        if side in ("right", "left"):
            nx = (w - 1 - EDGE_PULLBACK) if side == "right" else EDGE_PULLBACK
            ny = self.ry / max(1, ch - 1) * (h - 1)
        else:
            ny = (h - 1 - EDGE_PULLBACK) if side == "bottom" else EDGE_PULLBACK
            nx = self.rx / max(1, cw - 1) * (w - 1)

        nx = int(max(1, min(w - 2, nx)))
        ny = int(max(1, min(h - 2, ny)))
        self.backend.set_cursor_pos(nx, ny)
        self.local_x, self.local_y = nx, ny
        self._edge_hits = 0
        self._edge_side = None
        self._cooldown_until = time.monotonic() + REENTRY_COOLDOWN
        log.info("<< 回到本机（%s）", reason)

    # -- 网络 ---------------------------------------------------------------
    def _start_server(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(8)
        self._server = srv
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="kvm-accept", daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while self.running:
            try:
                sock, addr = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(sock, addr),
                             name="kvm-handshake", daemon=True).start()

    def _handshake(self, sock: socket.socket, addr) -> None:
        conn = P.FramedConnection(sock, "pending")
        try:
            sock.settimeout(10.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = conn.recv(timeout=10.0)
            if not hello or hello.get("t") != P.MSG_HELLO:
                conn.send({"t": P.MSG_ERROR, "msg": "expected hello"})
                conn.close()
                return
            if self.token and hello.get("token") != self.token:
                log.warning("拒绝 %s：token 不匹配", addr[0])
                conn.send({"t": P.MSG_ERROR, "msg": "bad token"})
                conn.close()
                return

            name = str(hello.get("name") or addr[0])
            screen = hello.get("screen") or [1920, 1080]
            screen = (int(screen[0]), int(screen[1]))

            side = self.peer_sides.get(name)
            if side is None:
                if not self.peer_sides:
                    side = "right"
                else:
                    log.warning("拒绝 %s：未在 --peer 中配置", name)
                    conn.send({"t": P.MSG_ERROR, "msg": "unknown peer"})
                    conn.close()
                    return
            if side not in SIDES:
                log.error("配置错误：%s 的 side=%r 无效", name, side)
                conn.close()
                return

            old = self.peers.get(name)
            if old:
                log.info("同名从机重连，关闭旧连接：%s", name)
                old.close()

            conn.name = name
            sock.settimeout(None)
            conn.send({"t": P.MSG_WELCOME, "name": self.name,
                       "screen": list(self.local_screen), "side": side})
            peer = Peer(name, side, conn, screen)
            self._post("ctl", ("connected", peer))
            log.info("从机已连接：%s @ %s  屏幕 %dx%d  位置=%s",
                     name, addr[0], screen[0], screen[1], side)
            self._reader(peer)
        except Exception as exc:
            log.warning("握手失败 %s: %s", addr[0] if addr else "?", exc)
            conn.close()

    def _reader(self, peer: Peer) -> None:
        try:
            while self.running and peer.alive:
                msg = peer.conn.recv(timeout=1.0)
                if msg is None:
                    continue
                self._post(peer.name, msg)
        except (ConnectionError, P.ProtocolError) as exc:
            log.info("从机 %s 断开：%s", peer.name, exc)
        finally:
            self._post("ctl", ("disconnected", peer))

    def _on_control(self, data) -> None:
        action, peer = data
        if action == "connected":
            self.peers[peer.name] = peer
            old = self.peers_by_side.get(peer.side)
            if old and old.name != peer.name:
                log.warning("位置 %s 被 %s 占用，替换掉 %s",
                            peer.side, peer.name, old.name)
            self.peers_by_side[peer.side] = peer
        elif action == "disconnected":
            self._drop_peer(peer, "连接关闭")

    def _drop_peer(self, peer: Peer, reason: str) -> None:
        if self.remote is peer:
            self._exit_remote("从机断开")
        peer.alive = False
        self.peers.pop(peer.name, None)
        if self.peers_by_side.get(peer.side) is peer:
            self.peers_by_side.pop(peer.side, None)
        peer.close()
        log.info("从机 %s 已移除：%s", peer.name, reason)

    def _on_peer_msg(self, src: str, msg: dict) -> None:
        t = msg.get("t")
        if t == P.MSG_BYE:
            peer = self.peers.get(src)
            if peer:
                self._drop_peer(peer, "对端主动退出")
        elif t == P.MSG_CLIP:
            self._relay_clip(src, msg.get("text") or "")
        elif t == P.MSG_PONG:
            pass
