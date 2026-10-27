#!/usr/bin/env python3
"""远程执行工具：给 Linux 从机做部署和诊断。

    python tools/rsh.py exec "uname -a"
    python tools/rsh.py put local.txt /tmp/remote.txt
    python tools/rsh.py sudo "apt-get install -y python3-evdev"
"""

from __future__ import annotations

import os
import sys

DEPS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".deps")
sys.path.insert(0, DEPS)

import paramiko  # noqa: E402


def _load_env_file() -> None:
    """从 tools/.env 读连接参数（该文件不进仓库）。

    格式每行 KEY=VALUE，环境变量优先。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

HOST = os.environ.get("KVM_HOST", "")
USER = os.environ.get("KVM_USER", "")
PASS = os.environ.get("KVM_PASS", "")


def connect() -> paramiko.SSHClient:
    if not HOST or not USER or not PASS:
        raise SystemExit(
            "缺少连接参数。请在 tools/.env 里写：\n"
            "  KVM_HOST=192.168.1.50\n"
            "  KVM_USER=youruser\n"
            "  KVM_PASS=yourpassword\n"
            "或者直接设置同名环境变量。"
        )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=10,
              look_for_keys=False, allow_agent=False)
    return c


def run(c: paramiko.SSHClient, cmd: str) -> tuple[int, str]:
    _in, out, err = c.exec_command(cmd, timeout=120)
    text = out.read().decode("utf-8", "replace")
    etext = err.read().decode("utf-8", "replace")
    code = out.channel.recv_exit_status()
    return code, text + (("\n[stderr] " + etext) if etext.strip() else "")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    action = sys.argv[1]
    c = connect()
    try:
        if action == "exec":
            code, text = run(c, sys.argv[2])
            print(text)
            return code
        if action == "sudo":
            code, text = run(
                c, "echo '%s' | sudo -S -p '' %s" % (PASS, sys.argv[2]))
            print(text)
            return code
        if action == "execpy":
            import base64
            with open(sys.argv[2], "rb") as fh:
                blob = base64.b64encode(fh.read()).decode("ascii")
            remote_cmd = (
                "cd %s && echo %s | base64 -d | python3 - "
                % (sys.argv[3] if len(sys.argv) > 3 else "~/mouse-kvm", blob)
            )
            code, text = run(c, remote_cmd)
            print(text)
            return code
        if action == "putx":
            # 先 SFTP 落到 /tmp，再 cp 到目标位置（直接 SFTP 到目标偶发 ENOENT）
            tmp = "/tmp/_rsh_%d" % os.getpid()
            sftp = c.open_sftp()
            try:
                sftp.put(sys.argv[2], tmp)
            finally:
                sftp.close()
            code, text = run(c, "mkdir -p %s && cp %s %s && chmod +x %s && ls -l %s"
                             % (os.path.dirname(sys.argv[3]) or ".",
                                tmp, sys.argv[3], sys.argv[3], sys.argv[3]))
            print(text)
            return code
        if action == "put":
            sftp = c.open_sftp()
            sftp.put(sys.argv[2], sys.argv[3])
            print("uploaded %s -> %s" % (sys.argv[2], sys.argv[3]))
            sftp.close()
            return 0
        if action == "get":
            sftp = c.open_sftp()
            sftp.get(sys.argv[2], sys.argv[3])
            print("downloaded %s -> %s" % (sys.argv[2], sys.argv[3]))
            sftp.close()
            return 0
        print("unknown action: %s" % action)
        return 2
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
