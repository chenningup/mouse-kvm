"""文件传输：分块经现有 TCP 通道推到对端，落地到对方的下载目录。

设计取舍：
  - 复用 KVM 已有连接，不额外开端口，不用 SCP/SFTP。
  - 用 base64 分块塞进 JSON 帧，简单、跨平台、无需改协议格式；
    局域网带宽下 33% 的编码开销无所谓。
  - 落盘先写 .part 临时文件，校验完大小再改名，中途断了不会留半截文件。

收发两端共用本模块：
  发送方  FileSender    —— 自己跑在独立线程里，ACK 由接收线程喂进来
  接收方  ReceiveSession —— 主机/从机各持一个（主机按从机名分开）
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import protocol as P

log = logging.getLogger("kvm.xfer")

CHUNK_SIZE = 256 * 1024
MAX_FILE_SIZE = 512 * 1024 * 1024   # 512MB，再大就不该走这条路了
PROGRESS_INTERVAL = 0.15            # 秒，进度回调节流
ACK_TIMEOUT = 60.0                  # 秒，等对端回执的上限

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class TransferError(Exception):
    """传输失败，消息直接给用户看。"""


def safe_name(name: str) -> str:
    """只保留文件名本身，挡掉路径穿越和非法字符。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        name = "unnamed"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    return name[:180]


def inbox_dir() -> Path:
    """接收目录：<home>/Downloads/mouse-kvm。"""
    target = Path.home() / "Downloads" / "mouse-kvm"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path.home() / "mouse-kvm"
        target.mkdir(parents=True, exist_ok=True)
    return target


def unique_path(directory: Path, name: str) -> Path:
    """重名就加 -1、-2，不覆盖已有文件。"""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for i in range(1, 1000):
        alt = directory / ("%s-%d%s" % (stem, i, suffix))
        if not alt.exists():
            return alt
    return directory / ("%s-%d%s" % (stem, int(time.time()), suffix))


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % num
        value /= 1024
    return "%.1f GB" % value


def iter_chunks(path: Path):
    """逐块产出 (序号, base64 文本, 原始字节数)。"""
    with open(path, "rb") as fh:
        seq = 0
        while True:
            data = fh.read(CHUNK_SIZE)
            if not data:
                break
            yield seq, base64.b64encode(data).decode("ascii"), len(data)
            seq += 1


def new_transfer_id() -> str:
    return uuid.uuid4().hex[:16]


# ---- 接收端 ---------------------------------------------------------------
class FileReceiver:
    """把一个文件的分块拼回本地磁盘。

    同一时刻只跟踪一个传输（KVM 场景够用），新请求会打断旧的。
    """

    def __init__(self) -> None:
        self.fid: str | None = None
        self.temp_path: Path | None = None
        self.final_path: Path | None = None
        self.expected = 0
        self.received = 0
        self._fh = None

    def begin(self, fid: str, name: str, size: int) -> Path:
        self.abort()
        if size < 0 or size > MAX_FILE_SIZE:
            raise ValueError("文件过大：%s（上限 %s）"
                             % (human_size(size), human_size(MAX_FILE_SIZE)))
        target_dir = inbox_dir()
        self.final_path = unique_path(target_dir, safe_name(name))
        self.temp_path = self.final_path.with_suffix(
            self.final_path.suffix + ".part")
        self._fh = open(self.temp_path, "wb")
        self.fid = fid
        self.expected = size
        self.received = 0
        return self.final_path

    def chunk(self, fid: str, data_b64: str) -> None:
        if fid != self.fid or self._fh is None:
            raise ValueError("收到未知传输块：%s" % fid)
        data = base64.b64decode(data_b64)
        self._fh.write(data)
        self.received += len(data)

    def finish(self, fid: str) -> Path:
        if fid != self.fid or self._fh is None:
            raise ValueError("未知传输：%s" % fid)
        self._fh.close()
        self._fh = None
        if self.expected and self.received != self.expected:
            self.abort()
            raise ValueError("大小不符：期望 %d 收到 %d"
                             % (self.expected, self.received))
        os.replace(self.temp_path, self.final_path)
        path = self.final_path
        self._reset()
        return path

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        if self.temp_path and self.temp_path.exists():
            try:
                self.temp_path.unlink()
            except OSError:
                pass
        self._reset()

    def _reset(self) -> None:
        self._fh = None
        self.fid = None
        self.temp_path = None
        self.final_path = None
        self.expected = 0
        self.received = 0


class ReceiveSession:
    """一端收文件的完整状态机，主机和从机共用。

    差别只在构造时传进来的 send 函数：主机传给某个从机的
    Peer.send，从机传自己的 FramedConnection.send。
    """

    def __init__(self, send_fn, label: str = "") -> None:
        self.rx = FileReceiver()
        self._send = send_fn
        self.label = label
        self._fail: tuple[str, str] | None = None

    def handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == P.MSG_FILE_REQ:
            self._on_req(msg)
        elif t == P.MSG_FILE_CHUNK:
            self._on_chunk(msg)
        elif t == P.MSG_FILE_END:
            self._on_end(msg)

    def close(self) -> None:
        """连接断了，把半截临时文件清掉。"""
        self.rx.abort()
        self._fail = None

    # -- 内部 ---------------------------------------------------------------
    def _on_req(self, msg: dict) -> None:
        fid = str(msg.get("id") or "")
        name = str(msg.get("name") or "")
        size = int(msg.get("size") or 0)
        try:
            target = self.rx.begin(fid, name, size)
        except Exception as exc:
            # 不在这里回执 —— 发送方只认 END 时的 ACK，
            # 这里只记下失败原因，块全部丢弃，END 时再统一回一条失败。
            self._fail = (fid, str(exc))
            log.error("拒绝接收 %s：%s", name or "?", exc)
            return
        self._fail = None
        log.info("<< 开始接收 %s（%s）%s", target.name, human_size(size),
                 ("来自 " + self.label) if self.label else "")

    def _on_chunk(self, msg: dict) -> None:
        fid = str(msg.get("id") or "")
        if self._fail and self._fail[0] == fid:
            return       # 这个传输已经被拒，块直接丢掉，别把连接搞崩
        try:
            self.rx.chunk(fid, msg.get("data") or "")
        except Exception as exc:
            self._fail = (fid, str(exc))
            self.rx.abort()
            log.error("写入文件失败：%s", exc)

    def _on_end(self, msg: dict) -> None:
        fid = str(msg.get("id") or "")
        if self._fail and self._fail[0] == fid:
            err = self._fail[1]
            self._fail = None
            self._ack(fid, False, None, err)
            return
        try:
            path = self.rx.finish(fid)
        except Exception as exc:
            self._ack(fid, False, None, str(exc))
            log.error("接收失败：%s", exc)
            return
        try:
            total = path.stat().st_size
        except OSError:
            total = 0
        log.info("<< 文件已保存 %s（%s）", path, human_size(total))
        self._ack(fid, True, str(path), None)

    def _ack(self, fid: str, ok: bool, path: str | None, err: str | None) -> None:
        try:
            self._send({"t": P.MSG_FILE_ACK, "id": fid,
                        "ok": ok, "path": path, "err": err})
        except Exception as exc:
            log.warning("回执发送失败：%s", exc)


# ---- 发送端 ---------------------------------------------------------------
class FileSender:
    """把一个文件推给对端。

    run() 是阻塞的，放在独立线程里跑；对端的 ACK 由接收线程
    调 deliver_ack() 喂进来，两边靠 Event 汇合。
    """

    def __init__(self, fid: str, path: Path, send_fn,
                 on_progress=None) -> None:
        self.fid = fid
        self.path = Path(path)
        self._send = send_fn
        self._progress = on_progress
        self._ack = threading.Event()
        self._ack_result: dict | None = None
        self._lock = threading.Lock()
        self.cancelled = False

    def deliver_ack(self, ok: bool, path: str | None,
                    err: str | None) -> None:
        with self._lock:
            self._ack_result = {"ok": ok, "path": path, "err": err}
        self._ack.set()

    def run(self) -> dict:
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            raise TransferError("读不到文件：%s" % exc) from exc
        if size > MAX_FILE_SIZE:
            raise TransferError("文件太大：%s（上限 %s）"
                                % (human_size(size), human_size(MAX_FILE_SIZE)))

        self._send({"t": P.MSG_FILE_REQ, "id": self.fid,
                    "name": self.path.name, "size": size})

        sent = 0
        last_report = 0.0
        try:
            for seq, data_b64, nbytes in iter_chunks(self.path):
                if self.cancelled:
                    raise TransferError("传输已取消")
                self._send({"t": P.MSG_FILE_CHUNK, "id": self.fid,
                            "seq": seq, "data": data_b64})
                sent += nbytes
                now = time.monotonic()
                if self._progress and (now - last_report >= PROGRESS_INTERVAL
                                       or sent >= size):
                    last_report = now
                    self._progress(sent, size)
        except OSError as exc:
            raise TransferError("读文件出错：%s" % exc) from exc

        self._send({"t": P.MSG_FILE_END, "id": self.fid})

        if not self._ack.wait(ACK_TIMEOUT):
            raise TransferError("等对端确认超时（%.0f 秒）" % ACK_TIMEOUT)
        result = self._ack_result or {}
        if not result.get("ok"):
            raise TransferError(result.get("err") or "对端接收失败")
        return result

    def cancel(self) -> None:
        self.cancelled = True
