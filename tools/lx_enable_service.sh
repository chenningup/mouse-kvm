#!/bin/bash
# 在 Linux 从机上启用 mouse-kvm 常驻服务（开机自启 + 崩溃自动重拉）
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

# [k] 技巧：避免 pkill 把执行这条命令的 shell 自己也匹配上
pkill -f "[k]vm.py client" 2>/dev/null
sleep 1

systemctl --user daemon-reload
systemctl --user enable --now mouse-kvm-client.service
sleep 3

echo "--- active ---"
systemctl --user is-active mouse-kvm-client.service
echo "--- enabled ---"
systemctl --user is-enabled mouse-kvm-client.service
echo "--- log ---"
tail -6 /tmp/kvm-client.log
