#!/usr/bin/env python3
"""mouse-kvm 命令行入口。

    # 有键鼠的那台（主机）
    python kvm.py host --name win-box --peer ubuntu=right

    # 另一台（从机）
    python kvm.py client --name ubuntu --host 192.168.1.20

    # 推文件给对端（两端都要在运行）
    python kvm.py send 报告.pdf
    python kvm.py send a.zip --to ubuntu

    # 看谁在线
    python kvm.py status

    # 单纯验证输入权限/钩子是否正常
    python kvm.py probe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time

from mouse_kvm import __version__, control
from mouse_kvm.backend import create_backend
from mouse_kvm.client import Client
from mouse_kvm.filetransfer import human_size
from mouse_kvm.host import SIDES, Host
from mouse_kvm.keys import key_name

DEFAULT_PORT = 24800


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_peers(items: list[str] | None) -> dict[str, str]:
    peers: dict[str, str] = {}
    for raw in items or []:
        if "=" not in raw:
            raise SystemExit("--peer 格式应为 name=side，例如 ubuntu=right")
        name, side = raw.split("=", 1)
        name, side = name.strip(), side.strip().lower()
        if not name:
            raise SystemExit("--peer 的名字不能为空")
        if side not in SIDES:
            raise SystemExit("--peer 的 side 必须是 %s 之一" % "/".join(SIDES))
        peers[name] = side
    return peers


def cmd_host(args) -> int:
    backend = create_backend()
    host = Host(
        name=args.name,
        port=args.port,
        peer_sides=parse_peers(args.peer),
        backend=backend,
        token=args.token,
        bind=args.bind,
        edge_threshold=args.edge,
        enable_clip=not args.no_clip,
        ctl_port=args.ctl_port,
    )
    _install_signals(host.shutdown)
    host.run()
    return 0


def cmd_client(args) -> int:
    # 从机只负责注入，不装钩子也不读输入设备 —— 它可能一个外设都没有
    backend = create_backend(capture=False)
    client = Client(
        name=args.name,
        host=args.host,
        port=args.port,
        backend=backend,
        token=args.token,
        enable_clip=not args.no_clip,
        ctl_port=args.ctl_port,
    )

    def stop(*_):
        client.running = False

    _install_signals(stop)
    client.run()
    return 0


def cmd_send(args) -> int:
    """把本地文件推给对端。需要本机 KVM 进程正在运行。"""
    files = []
    for raw in args.files:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(path):
            print("跳过（不是文件）：%s" % raw, file=sys.stderr)
            continue
        files.append(path)
    if not files:
        print("没有可发送的文件", file=sys.stderr)
        return 1

    failed = 0
    for idx, path in enumerate(files, 1):
        if len(files) > 1:
            print("[%d/%d] %s" % (idx, len(files), os.path.basename(path)))
        try:
            _push_one(args, path)
        except Exception as exc:   # noqa: BLE001
            print("  失败：%s" % exc, file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _push_one(args, path: str) -> None:
    try:
        sock = control.connect(args.ctl_port, args.ctl_host)
    except OSError as exc:
        raise RuntimeError(
            "连不上本机的控制通道 %s:%d（%s）。"
            "先确认 kvm.py 正在运行；如果改过 --port，"
            "send 这边要同步用 --ctl-port 指定。"
            % (args.ctl_host, args.ctl_port, exc)) from exc

    reader = control.LineReader(sock)
    size = os.path.getsize(path)
    req = {"cmd": "send", "path": path}
    if args.to:
        req["to"] = args.to
    try:
        control.send_request(sock, req)
        while True:
            msg = reader.readline()
            if msg is None:
                raise RuntimeError("对端断开了控制连接")
            t = msg.get("t")
            if t == "start":
                print("  -> %s  %s" % (msg.get("to") or "?",
                                       human_size(int(msg.get("size") or size))))
            elif t == "progress":
                sent = int(msg.get("sent") or 0)
                pct = (sent * 100.0 / size) if size else 100.0
                sys.stdout.write("\r  %5.1f%%  %s / %s"
                                 % (pct, human_size(sent), human_size(size)))
                sys.stdout.flush()
            elif t == "done":
                if size:
                    sys.stdout.write("\r")
                    sys.stdout.flush()
                if msg.get("ok"):
                    print("  已送达 %s：%s" % (msg.get("to") or "?",
                                              msg.get("path") or ""))
                    return
                raise RuntimeError(msg.get("err") or "对端接收失败")
            else:
                continue
    finally:
        try:
            sock.close()
        except OSError:
            pass


def cmd_status(args) -> int:
    """看一眼对端在线情况。"""
    try:
        sock = control.connect(args.ctl_port, args.ctl_host)
    except OSError as exc:
        print("连不上控制通道 %s:%d：%s" % (args.ctl_host, args.ctl_port, exc),
              file=sys.stderr)
        return 1
    try:
        control.send_request(sock, {"cmd": "status"})
        msg = control.LineReader(sock).readline()
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not msg or not msg.get("ok"):
        print("查询失败：%s" % (msg or {}).get("err", "无响应"), file=sys.stderr)
        return 1
    if "peers" in msg:
        print("主机：%s" % msg.get("host"))
        peers = msg.get("peers") or []
        if not peers:
            print("  没有从机连接")
        for p in peers:
            print("  %-16s 位置=%-6s 屏幕=%dx%d  %s"
                  % (p.get("name"), p.get("side"),
                     p.get("screen", [0, 0])[0], p.get("screen", [0, 0])[1],
                     "在线" if p.get("alive") else "已断开"))
    else:
        print("从机：%s" % msg.get("client"))
        print("  主机：%s  连接=%s  被接管=%s"
              % (msg.get("host"),
                 "是" if msg.get("connected") else "否",
                 "是" if msg.get("active") else "否"))
    return 0


def cmd_probe(args) -> int:
    """只监听不拦截，用来验证权限和钩子。"""
    backend = create_backend()
    count = 0
    deadline = time.monotonic() + args.seconds if args.seconds else None

    def on_event(ev):
        nonlocal count
        count += 1
        kind = ev[0]
        if kind == "move":
            print("移动      x=%-5d y=%-5d" % (ev[1], ev[2]))
        elif kind == "rel":
            print("相对移动  dx=%-4d dy=%-4d" % (ev[1], ev[2]))
        elif kind == "button":
            print("鼠标键    btn=%d %s" % (ev[1], "按下" if ev[2] else "抬起"))
        elif kind == "scroll":
            print("滚轮      dx=%d dy=%d" % (ev[1], ev[2]))
        elif kind == "key":
            print("键盘      %-12s %s" % (key_name(ev[1]),
                                        "按下" if ev[2] else "抬起"))

    backend.sink = on_event
    print("平台后端: %s  屏幕: %dx%d" % (backend.name, *backend.screen_size()))
    print("开始监听（不拦截本机输入）。按 Ctrl+C 结束。\n")
    backend.start()
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
    print("\n共收到 %d 个事件。" % count)
    if count == 0:
        print("一个事件都没收到 —— 检查权限（Linux 需要 input 组或 root）。")
        return 1
    return 0


def _install_signals(handler) -> None:
    def wrapper(signum, frame):
        handler()
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, wrapper)
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kvm.py",
        description="跨平台软件 KVM：一套鼠标键盘控制多台 Windows / Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    p.add_argument("--version", action="version", version="mouse-kvm " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("host", help="在连着键鼠的那台电脑上运行")
    h.add_argument("--name", default="host", help="本机名称（仅用于显示）")
    h.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    h.add_argument("--bind", default="0.0.0.0", help="监听地址")
    h.add_argument("--peer", action="append", metavar="NAME=SIDE",
                   help="从机名与方位，如 ubuntu=right；可重复指定")
    h.add_argument("--token", default="", help="连接口令，两台电脑需一致")
    h.add_argument("--edge", type=int, default=3,
                   help="触发切换所需的外推次数，越小越灵敏（默认 3）")
    h.add_argument("--no-clip", action="store_true", help="不同步剪贴板")
    h.add_argument("--ctl-port", type=int, default=None,
                   help="本地控制通道端口，供 kvm.py send 使用（默认 端口+1）")
    h.set_defaults(func=cmd_host)

    c = sub.add_parser("client", help="在被控电脑上运行")
    c.add_argument("--name", required=True, help="从机名，须与主机的 --peer 对应")
    c.add_argument("--host", required=True, help="主机 IP")
    c.add_argument("--port", type=int, default=DEFAULT_PORT, help="主机端口")
    c.add_argument("--token", default="", help="连接口令，两台电脑需一致")
    c.add_argument("--no-clip", action="store_true", help="不同步剪贴板")
    c.add_argument("--ctl-port", type=int, default=None,
                   help="本地控制通道端口，供 kvm.py send 使用（默认 端口+1）")
    c.set_defaults(func=cmd_client)

    s = sub.add_parser("send", help="把文件推给对端（需本机 KVM 正在运行）")
    s.add_argument("files", nargs="+", metavar="文件")
    s.add_argument("--to", default=None,
                   help="目标从机名；只有一台从机时可省略")
    s.add_argument("--ctl-port", type=int, default=DEFAULT_PORT + 1,
                   help="本机控制通道端口（默认 %d）" % (DEFAULT_PORT + 1))
    s.add_argument("--ctl-host", default=control.CTL_HOST,
                   help="控制通道地址（默认 127.0.0.1）")
    s.set_defaults(func=cmd_send)

    st = sub.add_parser("status", help="查看对端在线情况")
    st.add_argument("--ctl-port", type=int, default=DEFAULT_PORT + 1,
                    help="本机控制通道端口（默认 %d）" % (DEFAULT_PORT + 1))
    st.add_argument("--ctl-host", default=control.CTL_HOST,
                    help="控制通道地址（默认 127.0.0.1）")
    st.set_defaults(func=cmd_status)

    pr = sub.add_parser("probe", help="只监听不拦截，验证权限与钩子")
    pr.add_argument("--seconds", type=float, default=0,
                    help="运行几秒后自动结束，0 表示一直跑")
    pr.set_defaults(func=cmd_probe)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 0
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.exception("运行出错: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
