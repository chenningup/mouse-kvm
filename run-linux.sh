#!/usr/bin/env bash
# 在这台机器上运行 —— 前提是已经配好 evdev / uinput 权限（见 README）。
#
# 用法：
#   1. 改下面两行参数
#   2. ./run-linux.sh
set -e
cd "$(dirname "$0")"

# ---------------- 改这里 ----------------
MODE="client"              # client = 被控端；改成 host 就变成主机
NAME="ubuntu"              # 从机名，必须和主机的 --peer 一致
HOST_IP="192.168.1.20"     # 主机的 IP
SIDE="right"               # 仅当 MODE=host 时有效：从机摆在哪个方向
# ---------------------------------------

if [ "$MODE" = "host" ]; then
    exec python3 kvm.py host --name "$NAME" --peer "slave=$SIDE"
else
    exec python3 kvm.py client --name "$NAME" --host "$HOST_IP"
fi
