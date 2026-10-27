"""本地控制通道：让 `kvm.py send` 联系上正在跑的 KVM 进程。

只监听 127.0.0.1，不接受外部连接。协议是"一行一个 JSON"，
请求和响应都是 JSON 对象，用换行分隔。

请求： {"cmd": "send", "path": "/abs/a.zip", "to": "linux"}
响应： {"t": "start", "to": "linux", "size": 123}
      {"t": "progress", "sent": 456, "total": 123}
      {"t": "done", "ok": true, "path": "/home/inper/Downloads/mouse-kvm/a.zip"}
      或 {"t": "done", "ok": false, "err": "..."}
"""

from __future__ import annotations

import json
import logging
import socket
import threading

log = logging.getLogger("kvm.control")

CTL_HOST = "127.0.0.1"
CONNECT_TIMEOUT = 5.0
IDLE_TIMEOUT = 3600.0     # 大文件传输期间连接会一直挂着，别太短
MAX_LINE = 64 * 1024


class ControlServer:
    """跑在主机/从机进程里的本地监听器。

    handler(req, reply) 由调用方提供，reply 可以多次调用（进度），
    最后一次必须是 {"t": "done", ...}。
    """

    def __init__(self, port: int, handler) -> None:
        self.port = port
        self.handler = handler
        self.running = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((CTL_HOST, self.port))
            srv.listen(4)
        except OSError as exc:
            # 端口被占不该让 KVM 起不来 —— 只是失去 send 能力
            log.warning("本地控制通道 %s:%d 不可用（%s），kvm.py send 将无法使用",
                        CTL_HOST, self.port, exc)
            return False
        self._sock = srv
        self.running = True
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="kvm-ctl", daemon=True)
        self._thread.start()
        log.info("本地控制通道 %s:%d（kvm.py send 用）", CTL_HOST, self.port)
        return True

    def stop(self) -> None:
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- 内部 ---------------------------------------------------------------
    def _accept_loop(self) -> None:
        while self.running:
            try:
                sock, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(sock,),
                             name="kvm-ctl-req", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        reply_lock = threading.Lock()

        def reply(obj: dict) -> None:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            with reply_lock:
                try:
                    sock.sendall(line.encode("utf-8"))
                except OSError:
                    pass

        try:
            sock.settimeout(IDLE_TIMEOUT)
            buf = b""
            while self.running:
                while b"\n" not in buf:
                    data = sock.recv(4096)
                    if not data:
                        return
                    buf += data
                    if len(buf) > MAX_LINE:
                        reply({"t": "done", "ok": False, "err": "请求过大"})
                        return
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    reply({"t": "done", "ok": False, "err": "请求不是合法 JSON"})
                    continue
                try:
                    self.handler(req, reply)
                except Exception as exc:   # noqa: BLE001
                    log.exception("控制请求失败")
                    reply({"t": "done", "ok": False, "err": str(exc)})
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


# ---- 命令行侧 -------------------------------------------------------------
class LineReader:
    """从控制连接上一行一行地读 JSON 响应。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def readline(self) -> dict | None:
        while b"\n" not in self.buf:
            data = self.sock.recv(4096)
            if not data:
                return None
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            return self.readline()
        return json.loads(line.decode("utf-8"))


def connect(port: int, host: str = CTL_HOST) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(None)
    return sock


def send_request(sock: socket.socket, req: dict) -> None:
    line = json.dumps(req, ensure_ascii=False) + "\n"
    sock.sendall(line.encode("utf-8"))
