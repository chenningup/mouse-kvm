#!/usr/bin/env python3
"""文件传输自测：文件名清洗、分块拼回、大小校验、以及完整的收发往返。

    python tests/test_filetransfer.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mouse_kvm import filetransfer as F
from mouse_kvm import protocol as P

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
    tmp = Path(tempfile.mkdtemp(prefix="kvm-xfer-"))
    real_inbox = F.inbox_dir
    F.inbox_dir = lambda: tmp          # 测试不往真实下载目录写东西
    try:
        print("[1] 文件名清洗")
        check("剥掉路径穿越", F.safe_name("../../etc/passwd") == "passwd",
              F.safe_name("../../etc/passwd"))
        check("Windows 反斜杠路径", F.safe_name(r"C:\Users\x\a.txt") == "a.txt",
              F.safe_name(r"C:\Users\x\a.txt"))
        check("非法字符替换", F.safe_name('a<b>c:d.txt') == "a_b_c_d.txt",
              F.safe_name('a<b>c:d.txt'))
        check("保留中文和空格", F.safe_name("我的 文件.txt") == "我的 文件.txt")
        check("空名字兜底", F.safe_name("") == "unnamed")
        check("Windows 保留名加前缀", F.safe_name("CON.txt") == "_CON.txt",
              F.safe_name("CON.txt"))

        print("[2] 大小显示")
        check("1024 -> 1.0 KB", F.human_size(1024) == "1.0 KB", F.human_size(1024))
        check("0 -> 0 B", F.human_size(0) == "0 B", F.human_size(0))

        print("[3] 重名不覆盖")
        (tmp / "a.txt").write_text("x")
        alt = F.unique_path(tmp, "a.txt")
        check("重名加 -1", alt.name == "a-1.txt", alt.name)
        alt.write_text("y")
        check("再加 -2", F.unique_path(tmp, "a.txt").name == "a-2.txt")

        print("[4] 分块与拼回")
        src = tmp / "src.bin"
        payload = bytes(range(256)) * 3000          # ~768KB，跨 3 个块
        src.write_bytes(payload)
        chunks = list(F.iter_chunks(src))
        check("分成多块", len(chunks) >= 3, "%d 块" % len(chunks))
        check("每块都带字节数", all(c[2] == F.CHUNK_SIZE for c in chunks[:-1]))
        check("总字节数一致", sum(c[2] for c in chunks) == len(payload))

        rx = F.FileReceiver()
        target = rx.begin("fid1", "back.bin", len(payload))
        for seq, data, _n in chunks:
            rx.chunk("fid1", data)
        out = rx.finish("fid1")
        check("落盘内容一致", out.read_bytes() == payload)
        check("临时文件已清掉", not target.with_suffix(target.suffix + ".part").exists())
        check("拼回后状态复位", rx.fid is None and rx.received == 0)

        print("[5] 大小不符要拒绝")
        rx2 = F.FileReceiver()
        t2 = rx2.begin("fid2", "bad.bin", 10)
        rx2.chunk("fid2", "YWJj")            # 只给了 3 字节
        try:
            rx2.finish("fid2")
            check("大小不符抛异常", False, "没有抛")
        except ValueError:
            check("大小不符抛异常", True)
        check("半截文件被删掉", not t2.with_suffix(t2.suffix + ".part").exists())
        check("最终文件没留下", not t2.exists())

        print("[6] 超大文件直接拒收")
        rx3 = F.FileReceiver()
        try:
            rx3.begin("fid3", "huge.bin", F.MAX_FILE_SIZE + 1)
            check("超限抛异常", False, "没有抛")
        except ValueError:
            check("超限抛异常", True)

        print("[7] 完整收发往返（内存链路）")
        ok = _roundtrip(tmp / "roundtrip.bin", 300 * 1024)
        check("小文件往返成功", ok is not None and ok[0], str(ok))
        ok2 = _roundtrip(tmp / "big.bin", F.CHUNK_SIZE * 2 + 4321)
        check("跨块文件往返成功", ok2 is not None and ok2[0], str(ok2))

        print("[8] 对端拒收时发送方要收到错误")
        ok3 = _roundtrip(tmp / "toobig.bin", 1024, force_oversize=True)
        check("发送方拿到失败原因",
              ok3 is not None and not ok3[0] and "过大" in (ok3[1] or ""),
              str(ok3))

        print("[9] 未知块不会打崩连接")
        sess = F.ReceiveSession(lambda obj: None)
        sess.handle({"t": P.MSG_FILE_CHUNK, "id": "nope", "seq": 0, "data": "YWJj"})
        check("孤块被吞掉不抛异常", True)
    finally:
        F.inbox_dir = real_inbox
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


def _roundtrip(src: Path, size: int, force_oversize: bool = False):
    """发送方和接收方直连，返回 (成功?, 路径或错误, 进度回调次数)。"""
    src.write_bytes(b"\xa5" * size)

    sender_box: dict = {}
    progress = []

    def rx_send(obj):
        # 接收方回执 -> 喂给发送方
        if obj.get("t") == P.MSG_FILE_ACK:
            fid = obj.get("id")
            sender = sender_box.get(fid)
            if sender is not None:
                sender.deliver_ack(bool(obj.get("ok")), obj.get("path"),
                                   obj.get("err"))

    rx = F.ReceiveSession(rx_send)

    def tx_send(obj):
        if force_oversize and obj.get("t") == P.MSG_FILE_REQ:
            obj = dict(obj, size=F.MAX_FILE_SIZE + 1)
        rx.handle(obj)

    result: dict = {}

    def run():
        try:
            fid = F.new_transfer_id()
            sender = F.FileSender(fid, src, tx_send,
                                  lambda s, t: progress.append(s))
            sender_box[fid] = sender
            result["ok"] = sender.run()
        except Exception as exc:   # noqa: BLE001
            result["err"] = str(exc)

    th = threading.Thread(target=run)
    th.start()
    th.join(120)

    if not result.get("ok"):
        return False, result.get("err") or "无结果", len(progress)
    final = Path(result["ok"]["path"])
    if force_oversize:
        return True, str(final), len(progress)
    if final.read_bytes() != src.read_bytes():
        return False, "内容不一致", len(progress)
    return True, str(final), len(progress)


if __name__ == "__main__":
    sys.exit(main())
