#!/bin/bash
# 在 Linux 从机上启动 client。用法: ./lx_start_client.sh <主机IP>
HOSTIP="${1:?用法: lx_start_client.sh <主机IP>}"

pkill -f "kvm.py client" 2>/dev/null
sleep 0.5
cd "$HOME/mouse-kvm" || exit 1

setsid nohup python3 kvm.py client --name linux --host "$HOSTIP" \
    > /tmp/kvm-client.log 2>&1 < /dev/null &
sleep 2
echo "--- pid ---"
pgrep -af "kvm.py client"
echo "--- log ---"
cat /tmp/kvm-client.log
