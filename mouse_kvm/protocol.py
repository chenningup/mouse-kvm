"""长度前缀 + JSON 的 TCP 帧协议。

帧格式: 4 字节大端无符号长度 + UTF-8 JSON 负载
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

HEADER = struct.Struct(">I")
MAX_PAYLOAD = 8 * 1024 * 1024

# ---- 消息类型常量 ----------------------------------------------------------
# 握手
MSG_HELLO = "hello"          # client -> host  {name, screen:[w,h], token}
MSG_WELCOME = "welcome"      # host -> client  {name, screen:[w,h], peer}
MSG_ERROR = "error"          # 双向           {msg}

# 主机 -> 从机
MSG_ENTER = "enter"          # {x, y}   进入该从机（绝对坐标）
MSG_LEAVE = "leave"          # {}       离开
MSG_MOVE = "move"            # {x, y}   绝对坐标
MSG_BUTTON = "button"        # {btn, down}
MSG_SCROLL = "scroll"        # {dx, dy}
MSG_KEY = "key"              # {code, down}
MSG_CLIP = "clip"            # {text}   剪贴板文本
MSG_CLIP_FILES = "clip_files"  # {paths}  把对端收到的文件放进本机剪贴板（CF_HDROP）

# 文件传输
MSG_FILE_BATCH = "file_batch"  # {id, root, count} 宣告一次整目录传输，回执带目录路径
MSG_FILE_REQ = "file_req"      # {id, name, size, batch?, rel?}
MSG_FILE_CHUNK = "file_chunk"  # {id, seq, data}
MSG_FILE_END = "file_end"      # {id}
MSG_FILE_ACK = "file_ack"      # {id, ok, path, err}

MSG_PING = "ping"            # {t}
MSG_PONG = "pong"            # {ts}
MSG_BYE = "bye"              # {}


class ProtocolError(Exception):
    pass


class FramedConnection:
    """在线程内安全收发带长度前缀的 JSON 帧。

    recv() 返回 dict，超时返回 None，对端关闭抛 ConnectionError。
    """

    def __init__(self, sock: socket.socket, name: str = "?"):
        self.sock = sock
        self.name = name
        self._buf = bytearray()
        self._closed = False
        # 剪贴板线程和主线程都可能发消息，不加锁会写出交错的帧
        self._send_lock = threading.Lock()

    # -- 发送 ---------------------------------------------------------------
    def send(self, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_PAYLOAD:
            raise ProtocolError("payload too large: %d" % len(payload))
        frame = HEADER.pack(len(payload)) + payload
        with self._send_lock:
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                self._closed = True
                raise ConnectionError("send failed: %s" % exc) from exc

    # -- 接收 ---------------------------------------------------------------
    def recv(self, timeout: float | None = None) -> dict | None:
        """读一帧。返回 None 表示本次超时且没有完整帧。"""
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            frame = self._pop_frame()
            if frame is not None:
                return frame
            if deadline is not None:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return None
            else:
                remain = None
            if not self._fill(remain):
                return None

    def _pop_frame(self) -> dict | None:
        if len(self._buf) < HEADER.size:
            return None
        (length,) = HEADER.unpack_from(self._buf, 0)
        if length > MAX_PAYLOAD:
            raise ProtocolError("frame too large: %d" % length)
        total = HEADER.size + length
        if len(self._buf) < total:
            return None
        payload = bytes(self._buf[HEADER.size:total])
        del self._buf[:total]
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("bad frame: %s" % exc) from exc
        if not isinstance(obj, dict):
            raise ProtocolError("frame is not a JSON object")
        return obj

    def _fill(self, timeout: float | None) -> bool:
        """尝试从 socket 读一段数据进缓冲区。返回 False 表示超时。"""
        try:
            self.sock.settimeout(timeout)
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        except OSError as exc:
            self._closed = True
            raise ConnectionError("recv failed: %s" % exc) from exc
        if not chunk:
            self._closed = True
            raise ConnectionError("peer closed connection")
        self._buf.extend(chunk)
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
