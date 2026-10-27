"""从机端：接收主机事件并注入本机。

从机是"哑终端"——它不判断边缘、不维护坐标，
所有位置计算都在主机侧完成，这里只负责注入。
这样两端逻辑简单，也不会有状态不同步的问题。
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from pathlib import Path

from . import control, protocol as P
from .backend.base import Backend
from .clipboard import MAX_TEXT_BYTES, ClipboardSync
from .filetransfer import (MAX_FILE_SIZE, AckSlot, FileSender, ReceiveSession,
                           TransferError, collect_tree, human_size,
                           new_transfer_id, send_tree)
from .keys import key_name

log = logging.getLogger("kvm.client")

RECONNECT_DELAY = 2.0


class _SendJob:
    """把一批文件推给主机。

    set_clip=True 表示推完还要让主机把它们放进剪贴板 ——
    这样在 Linux 上 Ctrl+C 一个文件，到 Windows 上右键就有"粘贴"。
    """

    def __init__(self, paths, set_clip: bool = False) -> None:
        self.paths = list(paths)
        self.set_clip = set_clip
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: str | None = None
        self.total: int | None = None      # 目录传输时预先算好的总字节数
        self.start_cb = None
        self.progress_cb = None


class Client:
    def __init__(self, name: str, host: str, port: int, backend: Backend,
                 token: str = "", enable_clip: bool = True,
                 ctl_port: int | None = None) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.token = token
        self.backend = backend
        self.screen = backend.screen_size()
        self.ctl_port = ctl_port if ctl_port is not None else port + 1

        self.conn: P.FramedConnection | None = None
        self.running = False
        self.active = False           # 是否处于被主机接管状态
        self._pressed_keys: set[int] = set()
        self._pressed_btns: set[int] = set()
        self._host_name = "?"

        self.clip: ClipboardSync | None = ClipboardSync() if enable_clip else None

        # 文件传输：接收走这台机器的 rx（主机推过来的），
        # 发送走本地控制通道（kvm.py send 在这里跑）。
        self.rxfile = ReceiveSession(self._file_send, label="host")
        self.ctl: control.ControlServer | None = None
        self._send_q: queue.Queue = queue.Queue()
        self._send_worker: threading.Thread | None = None
        self._xfer_lock = threading.Lock()
        self._senders: dict[str, FileSender] = {}

    # -- 主流程 -------------------------------------------------------------
    def run(self) -> None:
        self.running = True
        self.backend.start()

        # 先把注入通道建起来，权限有问题要在启动时就暴露，
        # 不能等用户把鼠标推过来的那一瞬间才崩。
        try:
            self.backend.prewarm()
        except Exception as exc:
            log.error("输入注入初始化失败：%s", exc)
            log.error("Linux 上通常是权限问题 —— 确认当前用户在 input 组，"
                      "并且 systemd 服务里写了 SupplementaryGroups=input。"
                      "手动验证：python3 kvm.py probe")
            self.backend.stop()
            self.running = False
            return

        if self.clip is not None:
            if self.clip.enabled:
                self.clip.on_change = self._on_local_clip
                self.clip.on_files = self._on_local_files
                self.clip.start()
            else:
                log.warning("剪贴板同步不可用（缺少 wl-clipboard / xclip）")

        # 文件传输的控制通道和发送线程
        self._send_worker = threading.Thread(target=self._send_loop,
                                             name="kvm-send", daemon=True)
        self._send_worker.start()
        self.ctl = control.ControlServer(self.ctl_port, self._on_ctl_request)
        self.ctl.start()

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
            self.rxfile.close()
            if self.ctl is not None:
                self.ctl.stop()
                self.ctl = None
            self._send_q.put(None)
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

        elif t in (P.MSG_FILE_BATCH, P.MSG_FILE_REQ,
                   P.MSG_FILE_CHUNK, P.MSG_FILE_END):
            self.rxfile.handle(msg)

        elif t == P.MSG_FILE_ACK:
            fid = str(msg.get("id") or "")
            with self._xfer_lock:
                sender = self._senders.get(fid)
            if sender is not None:
                sender.deliver_ack(bool(msg.get("ok")), msg.get("path"),
                                   msg.get("err"))

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

    # -- 文件传输 -----------------------------------------------------------
    def _file_send(self, obj: dict) -> None:
        """给接收会话用的发送函数：连接没了就明确报错。"""
        conn = self.conn
        if conn is None:
            raise ConnectionError("尚未连上主机")
        conn.send(obj)

    def _on_ctl_request(self, req: dict, reply) -> None:
        cmd = (req or {}).get("cmd")
        if cmd == "send":
            self._ctl_send(req, reply)
        elif cmd == "status":
            reply({"t": "done", "ok": True, "client": self.name,
                   "host": self._host_name,
                   "connected": self.conn is not None,
                   "active": self.active})
        else:
            reply({"t": "done", "ok": False,
                   "err": "未知命令：%s（支持 send / status）" % cmd})

    def _ctl_send(self, req: dict, reply) -> None:
        raw = str(req.get("path") or "")
        if not raw:
            reply({"t": "done", "ok": False, "err": "缺少 path"})
            return
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        if not path.exists():
            reply({"t": "done", "ok": False, "err": "找不到：%s" % path})
            return
        job = _SendJob([path])
        if path.is_dir():
            try:
                _items, total = collect_tree(path)
            except TransferError as exc:
                reply({"t": "done", "ok": False, "err": str(exc)})
                return
            job.total = total
        job.start_cb = lambda peer_name, size: reply(
            {"t": "start", "to": peer_name, "size": size, "name": path.name})
        job.progress_cb = lambda sent, total: reply(
            {"t": "progress", "sent": sent, "total": total})
        self._send_q.put(job)
        job.done.wait()
        if job.error:
            reply({"t": "done", "ok": False, "err": job.error})
        else:
            result = job.result or {}
            reply({"t": "done", "ok": True,
                   "path": result.get("path"), "to": result.get("to")})

    def _send_loop(self) -> None:
        while True:
            job = self._send_q.get()
            if job is None:
                return
            try:
                if not self.running:
                    job.error = "从机正在退出"
                    continue
                job.result = self._do_send(job)
            except Exception as exc:   # noqa: BLE001
                job.error = str(exc)
                log.warning("发送失败：%s", exc)
            finally:
                job.done.set()

    def _do_send(self, job) -> dict:
        results = []
        for item in job.paths:
            if item.is_dir():
                results.append(self._send_dir(item, job))
            else:
                results.append(self._send_one(item, job))
        if job.set_clip and results:
            paths = [r.get("path") for r in results if r.get("path")]
            if paths:
                conn = self.conn
                if conn is None:
                    raise TransferError("与主机的连接已断开")
                conn.send({"t": P.MSG_CLIP_FILES, "paths": paths})
                log.info("已推到主机剪贴板（%d 项），可以在 Windows 上粘贴了",
                         len(paths))
        return results[-1] if results else {}

    def _send_dir(self, root: Path, job) -> dict:
        """整目录传输：先拿对端目录路径，再逐个文件推。"""

        def send_ctl(obj: dict) -> None:
            conn = self.conn
            if conn is None or not self.running:
                raise TransferError("与主机的连接已断开")
            conn.send(obj)

        def batch_msg(bid: str, root_name: str, count: int) -> None:
            slot = AckSlot()
            with self._xfer_lock:
                self._senders[bid] = slot
            send_ctl({"t": P.MSG_FILE_BATCH, "id": bid,
                      "root": root_name, "count": count})

        def wait_ack(bid: str) -> dict:
            with self._xfer_lock:
                slot = self._senders.get(bid)
            if slot is None:
                raise TransferError("内部错误：目录回执不见了")
            try:
                return slot.wait()
            finally:
                with self._xfer_lock:
                    self._senders.pop(bid, None)

        def send_file(path: Path, bid: str, rel: str, on_progress) -> dict:
            return self._send_one(path, job, batch=bid, rel=rel,
                                  on_progress=on_progress, quiet=True)

        log.info(">> 发送目录 %s 到主机", root.name)
        if job.start_cb:
            job.start_cb(self._host_name, job.total or 0)
        result = send_tree(root, batch_msg, wait_ack, send_file,
                           job.progress_cb)
        log.info(">> 目录已送达主机：%s（%d 个文件）",
                 result.get("path"), result.get("files", 0))
        return {"path": result.get("path"), "to": self._host_name}

    def _send_one(self, path: Path, job, batch: str | None = None,
                  rel: str | None = None, on_progress=None,
                  quiet: bool = False) -> dict:
        size = path.stat().st_size
        fid = new_transfer_id()

        def send_fn(obj: dict) -> None:
            conn = self.conn
            if conn is None or not self.running:
                raise TransferError("与主机的连接已断开")
            conn.send(obj)

        if job.start_cb and not quiet:
            job.start_cb(self._host_name, size)
        (log.debug if quiet else log.info)(">> 发送 %s（%s）到主机",
                                           path.name, human_size(size))

        cb = on_progress if on_progress is not None else job.progress_cb
        sender = FileSender(fid, path, send_fn, cb, batch=batch, rel=rel)
        with self._xfer_lock:
            self._senders[fid] = sender
        try:
            result = sender.run()
        finally:
            with self._xfer_lock:
                self._senders.pop(fid, None)

        (log.debug if quiet else log.info)(">> 已送达主机：%s",
                                           result.get("path"))
        return {"path": result.get("path"), "to": self._host_name}

    # -- 剪贴板 -------------------------------------------------------------
    def _on_local_files(self, files: list[str]) -> None:
        """本机复制的是一批文件（文件管理器里 Ctrl+C）。

        不能当文本发 —— Windows 收到字符串只是段路径文字，
        资源管理器右键不会出现"粘贴"，因为它认的是 CF_HDROP。
        所以这里走真正的传输，落盘后再让主机把路径放进剪贴板。
        """
        paths: list[Path] = []
        for raw in files:
            p = Path(raw)
            try:
                if p.is_dir():
                    paths.append(p)          # 文件夹整个传，大小在 collect_tree 里查
                    continue
                if not p.is_file():
                    continue
                if p.stat().st_size > MAX_FILE_SIZE:
                    log.warning("文件太大，不进剪贴板：%s（%s）",
                                p.name, human_size(p.stat().st_size))
                    continue
            except OSError:
                continue
            paths.append(p)
        if not paths:
            return
        if len(paths) < len(files):
            log.warning("只同步了 %d/%d 个文件", len(paths), len(files))
        log.info("剪贴板里有 %d 个文件，开始推到主机（%s）",
                 len(paths), ", ".join(p.name for p in paths[:3]))
        self._send_q.put(_SendJob(paths, set_clip=True))

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
