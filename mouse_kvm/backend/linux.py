"""Linux 输入后端：evdev 读取 + uinput 注入。

不用 X11 API 的原因：
  - Wayland 下 XTest / XQueryPointer 全部失效，evdev 依然可用。
  - 需要独占输入时用 EVIOCGRAB，比 X 的 grab 干净。

权限要求（二选一）：
  - 用 root 运行；
  - 或把用户加入 input 组，并写好 udev 规则（见 README）。
"""

from __future__ import annotations

import os
import selectors
import subprocess
import threading

try:
    import evdev
    from evdev import AbsInfo, InputDevice, UInput
    from evdev import ecodes as e
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Linux 端需要 python-evdev： pip install evdev"
    ) from exc

from .base import Backend

VIRTUAL_PREFIX = "mouse-kvm"
ABS_MAX = 32767

# 鼠标按键 -> evdev 码
BTN_MAP = {
    1: e.BTN_LEFT,
    2: e.BTN_MIDDLE,
    3: e.BTN_RIGHT,
    4: e.BTN_SIDE,
    5: e.BTN_EXTRA,
}
BTN_REVERSE = {v: k for k, v in BTN_MAP.items()}

KEY_CODES = sorted({
    code for name, code in e.ecodes.items()
    if name.startswith("KEY_") and isinstance(code, int) and 0 < code < 256
})


def detect_screen_size() -> tuple[int, int]:
    """猜主显示器分辨率。X11 下问 xrandr，其余读 DRM 模式表。"""
    display = os.environ.get("DISPLAY")
    if display:
        try:
            out = subprocess.run(["xrandr", "--current"], capture_output=True,
                                 text=True, timeout=2).stdout
            for line in out.splitlines():
                if "current" in line:
                    token = line.split("current")[1].split(",")[0].strip()
                    if "x" in token:
                        w, h = token.split("x")
                        return int(w), int(h.split()[0])
        except Exception:
            pass

    base = "/sys/class/drm"
    try:
        for entry in sorted(os.listdir(base)):
            if not entry.startswith("card") or "-" not in entry:
                continue
            path = os.path.join(base, entry, "modes")
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                first = fh.readline().strip()
            if "x" in first:
                w, h = first.split("x")
                return int(w), int(h)
    except Exception:
        pass

    return (1920, 1080)


class LinuxBackend(Backend):
    name = "linux"
    platform = "linux"
    # Wayland/X11 都没有通用的"读取+设置光标"接口，
    # 主机端改用自己维护的虚拟坐标做边缘判定。
    can_move_cursor = False

    def __init__(self, use_abs: bool = True, capture: bool = True) -> None:
        super().__init__(capture)
        self.supports_abs = use_abs
        self.supports_suppress = True
        self._use_abs = use_abs

        self._devices: list[InputDevice] = []
        self._grabbed: set[str] = set()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

        self._mouse_ui: UInput | None = None
        self._kbd_ui: UInput | None = None
        self._screen = (0, 0)
        self._abs_range = (ABS_MAX, ABS_MAX)

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if self._running or not self.capture:
            return
        self._discover()
        if not self._devices:
            raise RuntimeError(
                "没有找到可读的输入设备。请用 root 运行，"
                "或把当前用户加入 input 组（见 README）。"
            )
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="evdev-read",
                                        daemon=True)
        self._thread.start()

    def _discover(self) -> None:
        keyboards, mice = [], []
        for path in evdev.list_devices():
            try:
                dev = InputDevice(path)
            except (PermissionError, OSError):
                continue
            if dev.name.startswith(VIRTUAL_PREFIX):
                dev.close()
                continue
            caps = dev.capabilities()
            keys = set(caps.get(e.EV_KEY, []))
            rels = set(caps.get(e.EV_REL, []))
            is_mouse = e.REL_X in rels and e.REL_Y in rels
            is_keyboard = e.KEY_A in keys and e.KEY_Z in keys
            if is_mouse or is_keyboard:
                (mice if is_mouse else keyboards).append(dev)
            else:
                dev.close()
        self._devices = mice + keyboards

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._ungrab_all()
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices = []
        for ui in (self._mouse_ui, self._kbd_ui):
            if ui:
                try:
                    ui.close()
                except Exception:
                    pass
        self._mouse_ui = self._kbd_ui = None

    # -- 设备独占 -----------------------------------------------------------
    def set_suppress(self, on: bool) -> None:
        self._suppress = bool(on)
        if on:
            self._grab_all()
        else:
            self._ungrab_all()

    def _grab_all(self) -> None:
        with self._lock:
            for dev in self._devices:
                if dev.path in self._grabbed:
                    continue
                try:
                    dev.grab()
                    self._grabbed.add(dev.path)
                except Exception:
                    pass

    def _ungrab_all(self) -> None:
        with self._lock:
            for dev in self._devices:
                if dev.path not in self._grabbed:
                    continue
                try:
                    dev.ungrab()
                except Exception:
                    pass
            self._grabbed.clear()

    # -- 读取循环 -----------------------------------------------------------
    def _loop(self) -> None:
        sel = selectors.DefaultSelector()
        for dev in self._devices:
            try:
                sel.register(dev.fd, selectors.EVENT_READ, dev)
            except Exception:
                pass

        while self._running:
            try:
                ready = sel.select(timeout=0.2)
            except OSError:
                break
            for key, _mask in ready:
                dev: InputDevice = key.data
                try:
                    events = dev.read()
                except BlockingIOError:
                    continue
                except OSError:
                    continue
                for ev in events:
                    self._translate(ev)

        sel.close()

    def _translate(self, ev) -> None:
        if ev.type == e.EV_REL:
            if ev.code == e.REL_X:
                self.sink(("rel", ev.value, 0))
            elif ev.code == e.REL_Y:
                self.sink(("rel", 0, ev.value))
            elif ev.code == e.REL_WHEEL:
                self.sink(("scroll", 0, ev.value))
            elif ev.code == e.REL_HWHEEL:
                self.sink(("scroll", ev.value, 0))
        elif ev.type == e.EV_KEY:
            if ev.code in BTN_REVERSE:
                self.sink(("button", BTN_REVERSE[ev.code], ev.value == 1))
            elif ev.code < 256:
                self.sink(("key", ev.code, ev.value != 0))

    # -- 屏幕 / 光标 --------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        if self._screen == (0, 0):
            self._screen = detect_screen_size()
        return self._screen

    def cursor_pos(self) -> tuple[int, int]:
        # Wayland/X11 都没有通用方案；返回虚拟位置由 host 维护
        return (0, 0)

    def set_cursor_pos(self, x: int, y: int) -> None:
        # 客户端场景下光标位置由我们注入的绝对事件决定，无需额外操作
        return None

    # -- 注入 ---------------------------------------------------------------
    def _ensure_mouse(self, screen: tuple[int, int]) -> UInput:
        if self._mouse_ui is not None:
            return self._mouse_ui
        caps = {
            e.EV_KEY: list(BTN_MAP.values()),
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
        }
        if self._use_abs:
            sw, sh = screen if screen[0] > 0 else self.screen_size()
            self._abs_range = (max(1, sw - 1), max(1, sh - 1))
            caps[e.EV_ABS] = [
                (e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
            ]
        self._mouse_ui = UInput(caps, name="%s-mouse" % VIRTUAL_PREFIX,
                                version=0x1)
        return self._mouse_ui

    def _ensure_kbd(self) -> UInput:
        if self._kbd_ui is not None:
            return self._kbd_ui
        self._kbd_ui = UInput({e.EV_KEY: KEY_CODES},
                              name="%s-keyboard" % VIRTUAL_PREFIX,
                              version=0x1)
        return self._kbd_ui

    def inject_move_abs(self, x: int, y: int, screen: tuple[int, int]) -> None:
        ui = self._ensure_mouse(screen)
        sw, sh = screen
        if self._use_abs:
            nx = 0 if sw <= 1 else int(round(x * ABS_MAX / (sw - 1)))
            ny = 0 if sh <= 1 else int(round(y * ABS_MAX / (sh - 1)))
            nx = max(0, min(ABS_MAX, nx))
            ny = max(0, min(ABS_MAX, ny))
            ui.write(e.EV_ABS, e.ABS_X, nx)
            ui.write(e.EV_ABS, e.ABS_Y, ny)
            ui.syn()
        else:
            # 相对模式：由调用方换算好增量
            self.inject_move_rel(x, y)

    def inject_move_rel(self, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        ui = self._ensure_mouse(self.screen_size())
        if dx:
            ui.write(e.EV_REL, e.REL_X, int(dx))
        if dy:
            ui.write(e.EV_REL, e.REL_Y, int(dy))
        ui.syn()

    def inject_button(self, btn: int, down: bool) -> None:
        code = BTN_MAP.get(btn)
        if code is None:
            return
        ui = self._ensure_mouse(self.screen_size())
        ui.write(e.EV_KEY, code, 1 if down else 0)
        ui.syn()

    def inject_scroll(self, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        ui = self._ensure_mouse(self.screen_size())
        if dy:
            ui.write(e.EV_REL, e.REL_WHEEL, int(dy))
        if dx:
            ui.write(e.EV_REL, e.REL_HWHEEL, int(dx))
        ui.syn()

    def inject_key(self, code: int, down: bool) -> None:
        if not code:
            return
        ui = self._ensure_kbd()
        ui.write(e.EV_KEY, int(code), 1 if down else 0)
        ui.syn()
