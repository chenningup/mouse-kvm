#!/usr/bin/env python3
"""把 mouse-kvm 部署到 Linux 从机。

    python tools/deploy.py
"""

from __future__ import annotations

import os
import posixpath
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".deps"))

import paramiko  # noqa: E402


def _load_env_file() -> None:
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
REMOTE = os.environ.get("KVM_REMOTE", "/home/%s/mouse-kvm" % (USER or "user"))

FILES = [
    "kvm.py",
    "mouse_kvm/__init__.py",
    "mouse_kvm/protocol.py",
    "mouse_kvm/keys.py",
    "mouse_kvm/host.py",
    "mouse_kvm/client.py",
    "mouse_kvm/clipboard.py",
    "mouse_kvm/backend/__init__.py",
    "mouse_kvm/backend/base.py",
    "mouse_kvm/backend/linux.py",
    "mouse_kvm/backend/win32.py",
]

DIRS = ["", "mouse_kvm", "mouse_kvm/backend"]


def ensure_dir(sftp, path: str) -> None:
    try:
        sftp.stat(path)
    except IOError:
        sftp.mkdir(path)


def main() -> int:
    if not HOST or not USER or not PASS:
        raise SystemExit(
            "缺少连接参数。请在 tools/.env 里写 KVM_HOST / KVM_USER / KVM_PASS，"
            "或者设置同名环境变量。"
        )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=10,
              look_for_keys=False, allow_agent=False)
    sftp = c.open_sftp()
    try:
        # 远程是 Linux，必须用 posixpath；os.path.join 在 Windows 上会拼出反斜杠
        ensure_dir(sftp, REMOTE)
        for sub in DIRS:
            if sub:
                ensure_dir(sftp, posixpath.join(REMOTE, sub))
        for rel in FILES:
            local = os.path.join(ROOT, rel.replace("/", os.sep))
            remote = posixpath.join(REMOTE, rel)
            sftp.put(local, remote)
            print("  put %-32s -> %s" % (rel, remote))
    finally:
        sftp.close()
        c.close()
    print("部署完成：%s" % REMOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
