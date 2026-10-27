#!/usr/bin/env python3
"""mouse-kvm 命令行入口。

    # 有键鼠的那台（主机）
    python kvm.py host --name win-box --peer ubuntu=right

    # 另一台（从机）
    python kvm.py client --name ubuntu --host 192.168.1.20

    # 单纯验证输入权限/钩子是否正常
    python kvm.py probe
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from mouse_kvm import __version__
from mouse_kvm.backend import create_backend
from mouse_kvm.client import Client
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
    )

    def stop(*_):
        client.running = False

    _install_signals(stop)
    client.run()
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
    h.set_defaults(func=cmd_host)

    c = sub.add_parser("client", help="在被控电脑上运行")
    c.add_argument("--name", required=True, help="从机名，须与主机的 --peer 对应")
    c.add_argument("--host", required=True, help="主机 IP")
    c.add_argument("--port", type=int, default=DEFAULT_PORT, help="主机端口")
    c.add_argument("--token", default="", help="连接口令，两台电脑需一致")
    c.add_argument("--no-clip", action="store_true", help="不同步剪贴板")
    c.set_defaults(func=cmd_client)

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
