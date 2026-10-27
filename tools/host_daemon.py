#!/usr/bin/env python3
"""后台启动 Windows 主机，脱离终端（关掉窗口也不会停）。

    python tools/host_daemon.py start --peer linux=right
    python tools/host_daemon.py status
    python tools/host_daemon.py stop

日志写在项目根目录的 host.log。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIDFILE = os.path.join(ROOT, "host.pid")
LOGFILE = os.path.join(ROOT, "host.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_ulong)]


def is_alive(pid: int) -> bool:
    # 只有 SYNCHRONIZE 权限的话 GetExitCodeProcess 会失败，
    # 必须带上 PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_pid() -> int | None:
    try:
        with open(PIDFILE, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def start(argv: list[str]) -> int:
    pid = read_pid()
    if pid and is_alive(pid):
        print("主机已经在运行，PID=%d" % pid)
        print("日志：%s" % LOGFILE)
        return 0

    cmd = [sys.executable, os.path.join(ROOT, "kvm.py"), "host"] + argv
    log = open(LOGFILE, "a", encoding="utf-8")
    log.write("\n===== 启动 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log.flush()
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                       | CREATE_NO_WINDOW),
        close_fds=True,
    )
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))

    time.sleep(1.2)
    if is_alive(proc.pid):
        print("主机已启动，PID=%d" % proc.pid)
        print("日志：%s" % LOGFILE)
        return 0
    print("启动失败，看看日志：%s" % LOGFILE)
    return 1


def stop() -> int:
    pid = read_pid()
    if not pid or not is_alive(pid):
        print("主机没有在运行")
        return 0
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, 0, pid)
    if not handle:
        print("无法结束进程 PID=%d（可能权限不足）" % pid)
        return 1
    kernel32.TerminateProcess(handle, 0)
    kernel32.CloseHandle(handle)
    time.sleep(0.5)
    print("已停止 PID=%d" % pid)
    return 0


def status() -> int:
    pid = read_pid()
    if pid and is_alive(pid):
        print("运行中，PID=%d" % pid)
    else:
        print("未运行")
    if os.path.exists(LOGFILE):
        print("--- 最近日志 ---")
        with open(LOGFILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip() for ln in fh.readlines()[-8:]]
        print("\n".join(lines))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    action = sys.argv[1]
    if action == "start":
        return start(sys.argv[2:])
    if action == "stop":
        return stop()
    if action == "status":
        return status()
    print("未知动作：%s" % action)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
