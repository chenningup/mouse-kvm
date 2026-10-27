#!/usr/bin/env python3
"""剪贴板同步自测：轮询触发、去重、防回环、大小限制、真实读写。

    python tests/test_clip.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mouse_kvm.clipboard import (MAX_TEXT_BYTES, ClipboardSync, NullClipboard,
                                 create_clipboard, digest)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


class MemoryClipboard:
    """内存版剪贴板，用来做确定性测试。"""
    name = "memory"
    available = True

    def __init__(self, text=""):
        self.text = text
        self.writes = 0

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text
        self.writes += 1
        return True


def wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    print("[1] 轮询触发与去重")
    mem = MemoryClipboard("initial")
    got: list[str] = []
    sync = ClipboardSync(on_change=got.append, clip=mem, interval=0.05)
    sync.start()
    time.sleep(0.2)
    check("启动时不推送既有内容", got == [], str(got))

    mem.text = "hello"
    check("内容变化触发回调", wait_for(lambda: got == ["hello"]), str(got))
    time.sleep(0.25)
    check("同样内容不重复触发", got == ["hello"], str(got))

    mem.text = "world"
    check("再次变化能触发", wait_for(lambda: got == ["hello", "world"]), str(got))

    print("[2] 防回环")
    before = len(got)
    sync.apply_remote("from-linux")
    check("远端内容写进了本机", mem.text == "from-linux", mem.text)
    time.sleep(0.4)
    check("写回后没有反向推送", len(got) == before, str(got[before:]))

    print("[3] 相同内容不重复写入")
    writes_before = mem.writes
    sync.apply_remote("from-linux")
    check("内容一致时跳过写入", mem.writes == writes_before,
          "%d -> %d" % (writes_before, mem.writes))

    print("[4] 超长内容")
    big = "x" * (MAX_TEXT_BYTES + 10)
    before = len(got)
    mem.text = big
    time.sleep(0.35)
    check("超长内容不推送", len(got) == before, "got %d 条" % (len(got) - before))
    check("超长内容不写入", sync.apply_remote(big) is False)

    print("[5] 停止")
    sync.stop()
    mem.text = "after-stop"
    time.sleep(0.2)
    check("停止后不再回调", "after-stop" not in got, str(got))

    print("[6] 指纹一致性")
    check("相同文本指纹相同", digest("abc") == digest("abc"))
    check("不同文本指纹不同", digest("abc") != digest("abd"))

    print("[7] 真实剪贴板（会临时改写，测完恢复）")
    real = create_clipboard()
    print("       实现: %s, 可用: %s" % (real.name, real.available))
    if real.available:
        original = real.get_text()
        try:
            probe = "mouse-kvm 剪贴板测试 %d" % int(time.time())
            ok = real.set_text(probe)
            time.sleep(0.25)
            back = real.get_text()
            check("写入并读回一致", ok and back == probe,
                  "wrote=%r got=%r" % (probe, back))
            multi = "第一行\n第二行\ttab 结尾"
            real.set_text(multi)
            time.sleep(0.25)
            check("多行与制表符保留", real.get_text() == multi,
                  repr(real.get_text()))
        finally:
            if original is not None:
                real.set_text(original)
                print("       已恢复原剪贴板内容")
    else:
        print("       跳过（当前环境没有剪贴板工具）")

    print("[8] text/uri-list 解析")
    from mouse_kvm.clipboard import parse_uri_list
    uris = ("file:///home/inper/work/usb.zip\r\n"
            "# 注释行要跳过\r\n"
            "file:///home/inper/%E4%B8%AD%E6%96%87.txt\r\n"
            "https://example.com/a.txt\r\n")
    paths = parse_uri_list(uris)
    check("只留 file:// 且去掉注释",
          paths == ["/home/inper/work/usb.zip", "/home/inper/中文.txt"], str(paths))
    check("括号内容不当文件", parse_uri_list("copy\nfile:///a/b") == ["/a/b"])
    check("空内容返回空", parse_uri_list("") == [])

    print("[9] CF_HDROP 构造")
    from mouse_kvm.clipboard import dropfiles_payload
    payload = dropfiles_payload([r"C:\a\b.txt", r"C:\c\d.txt"])
    import struct as _s
    p_files, _x, _y, fnc, fwide = _s.unpack_from("<IiiII", payload, 0)
    check("头部 pFiles 指向 20 字节处", p_files == 20, str(p_files))
    check("按宽字符写", fwide == 1)
    check("不是非客户区", fnc == 0)
    body = payload[20:].decode("utf-16-le")
    check("路径都在且双 \\0 结尾",
          body == "C:\\a\\b.txt\x00C:\\c\\d.txt\x00\x00", repr(body))

    print("[10] 真实把文件放进剪贴板")
    if sys.platform == "win32":
        from mouse_kvm.clipboard import WinClipboard
        tmpdir = tempfile.mkdtemp(prefix="kvm-clip-")
        probe_file = os.path.join(tmpdir, "kvm-clip-test.txt")
        with open(probe_file, "w", encoding="utf-8") as fh:
            fh.write("hello\n")
        cb = WinClipboard()
        prev_text = cb.get_text()
        try:
            prev_files = cb.get_file_uris()
            check("写入 CF_HDROP 成功", cb.set_files([probe_file]))
            got = cb.get_file_uris()
            check("读回来是同一个文件",
                  [os.path.normcase(p) for p in got]
                  == [os.path.normcase(probe_file)], str(got))
            time.sleep(0.2)
            check("文本格式已让位（Explorer 要的是文件）",
                  cb.get_text() in (None, ""), repr(cb.get_text()))
        finally:
            if prev_files:
                cb.set_files(prev_files)
            elif prev_text is not None:
                cb.set_text(prev_text)
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print("       跳过（只在 Windows 上测）")

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
