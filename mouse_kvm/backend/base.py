"""输入后端抽象接口。

两个平台各自实现：Windows 用 Win32 低级钩子 + SendInput，
Linux 用 evdev 读取 + uinput 注入。

事件统一为元组，第一项是类型字符串：
    ("move",   x, y)        绝对屏幕坐标
    ("button", btn, down)   btn: 1=左 2=中 3=右 4/5=侧键
    ("scroll", dx, dy)      滚动格数
    ("key",    code, down)  code 为 evdev 键码

钩子回调必须立即返回，绝不能在里面做网络 IO。
"""

from __future__ import annotations

import abc
from typing import Callable

Event = tuple
EventSink = Callable[[Event], None]


class Backend(abc.ABC):
    """输入后端基类。"""

    name = "base"
    platform = "?"
    supports_abs = True       # 是否支持绝对坐标注入
    supports_suppress = True  # 是否支持运行时吞掉本地事件
    can_move_cursor = True    # 是否能读出/摆布本机光标位置

    def __init__(self, capture: bool = True) -> None:
        self.capture = capture
        self.sink: EventSink = lambda ev: None
        self._suppress = False

    # -- 生命周期 -----------------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """开始捕获输入。"""

    @abc.abstractmethod
    def stop(self) -> None:
        """停止捕获，释放设备。"""

    # -- 屏幕 / 光标 --------------------------------------------------------
    @abc.abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """主显示器尺寸（像素）。"""

    @abc.abstractmethod
    def cursor_pos(self) -> tuple[int, int]:
        """当前光标绝对坐标。"""

    @abc.abstractmethod
    def set_cursor_pos(self, x: int, y: int) -> None:
        """把光标移动到绝对坐标。"""

    def set_anchor(self, x: int, y: int) -> None:
        """远程模式下把光标钳制在这个位置，避免它在本机乱跑。

        只有能直接摆布光标的平台（Windows）需要实现。
        """

    # -- 压制 ---------------------------------------------------------------
    @property
    def suppress(self) -> bool:
        return self._suppress

    def set_suppress(self, on: bool) -> None:
        """切换是否吞掉本地事件（进入/退出远程模式时调用）。"""
        self._suppress = bool(on)

    # -- 注入 ---------------------------------------------------------------
    @abc.abstractmethod
    def inject_move_abs(self, x: int, y: int, screen: tuple[int, int]) -> None:
        """注入绝对鼠标移动，screen 为目标屏幕尺寸。"""

    @abc.abstractmethod
    def inject_move_rel(self, dx: int, dy: int) -> None:
        """注入相对鼠标移动。"""

    @abc.abstractmethod
    def inject_button(self, btn: int, down: bool) -> None:
        """注入鼠标按键，btn 见模块文档。"""

    @abc.abstractmethod
    def inject_scroll(self, dx: int, dy: int) -> None:
        """注入滚动。"""

    @abc.abstractmethod
    def inject_key(self, code: int, down: bool) -> None:
        """注入键盘事件，code 为 evdev 键码。"""

    # -- 辅助 ---------------------------------------------------------------
    def release_all(self) -> None:
        """释放所有可能按下的键/鼠标键，避免切换后卡键。"""
        for btn in (1, 2, 3, 4, 5):
            try:
                self.inject_button(btn, False)
            except Exception:
                pass


def create_backend(capture: bool = True) -> Backend:
    """按当前平台创建后端。

    capture=False 用于从机：只需要往系统里注入事件，
    不装钩子、不读输入设备。这样从机就算一个外设都没有也能跑。
    """
    import sys

    if sys.platform == "win32":
        from .win32 import Win32Backend
        return Win32Backend(capture=capture)
    if sys.platform.startswith("linux"):
        from .linux import LinuxBackend
        return LinuxBackend(capture=capture)
    raise RuntimeError(
        "不支持的平台: %s（本软件只支持 Windows 和 Linux）" % sys.platform
    )
