"""跨平台键码映射。

内部统一使用 Linux evdev 键码（linux/input-event-codes.h），
Windows 侧做 VK <-> evdev 双向转换。

这样 Linux 主机 -> Windows 从机、Windows 主机 -> Linux 从机
两个方向都能用同一套协议。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Windows Virtual-Key -> evdev
# ---------------------------------------------------------------------------
VK_TO_EVDEV: dict[int, int] = {
    0x08: 14,    # BACKSPACE
    0x09: 15,    # TAB
    0x0D: 28,    # RETURN
    0x10: 42,    # SHIFT (通用)
    0x11: 29,    # CONTROL (通用)
    0x12: 56,    # MENU / ALT (通用)
    0x13: 119,   # PAUSE
    0x14: 58,    # CAPITAL
    0x1B: 1,     # ESCAPE
    0x20: 57,    # SPACE
    0x21: 104,   # PRIOR / PageUp
    0x22: 109,   # NEXT / PageDown
    0x23: 107,   # END
    0x24: 102,   # HOME
    0x25: 105,   # LEFT
    0x26: 103,   # UP
    0x27: 106,   # RIGHT
    0x28: 108,   # DOWN
    0x2C: 99,    # SNAPSHOT / PrintScreen
    0x2D: 110,   # INSERT
    0x2E: 111,   # DELETE
    0x5B: 125,   # LWIN
    0x5C: 126,   # RWIN
    0x5D: 127,   # APPS / Menu
    0x5F: 142,   # SLEEP
    0x6A: 55,    # MULTIPLY
    0x6B: 78,    # ADD
    0x6C: 121,   # SEPARATOR
    0x6D: 74,    # SUBTRACT
    0x6E: 83,    # DECIMAL
    0x6F: 98,    # DIVIDE
    0x90: 69,    # NUMLOCK
    0x91: 70,    # SCROLL
    0xA0: 42,    # LSHIFT
    0xA1: 54,    # RSHIFT
    0xA2: 29,    # LCONTROL
    0xA3: 97,    # RCONTROL
    0xA4: 56,    # LMENU
    0xA5: 100,   # RMENU
    0xA6: 158,   # BROWSER_BACK
    0xA7: 159,   # BROWSER_FORWARD
    0xA8: 173,   # BROWSER_REFRESH
    0xA9: 172,   # BROWSER_STOP
    0xAA: 217,   # BROWSER_SEARCH
    0xAB: 155,   # BROWSER_FAVORITES
    0xAC: 156,   # BROWSER_HOME
    0xAD: 113,   # VOLUME_MUTE
    0xAE: 114,   # VOLUME_DOWN
    0xAF: 115,   # VOLUME_UP
    0xB0: 163,   # MEDIA_NEXT_TRACK
    0xB1: 165,   # MEDIA_PREV_TRACK
    0xB2: 166,   # MEDIA_STOP
    0xB3: 164,   # MEDIA_PLAY_PAUSE
    0xBA: 39,    # OEM_1  ;:
    0xBB: 13,    # OEM_PLUS =+
    0xBC: 51,    # OEM_COMMA ,
    0xBD: 12,    # OEM_MINUS -
    0xBE: 52,    # OEM_PERIOD .
    0xBF: 53,    # OEM_2  /?
    0xC0: 41,    # OEM_3  `~
    0xDB: 26,    # OEM_4  [{
    0xDC: 43,    # OEM_5  \|
    0xDD: 27,    # OEM_6  ]}
    0xDE: 40,    # OEM_7  '"
    0xE2: 86,    # OEM_102 额外 <> 键
}

# 字母 A-Z
for _i in range(26):
    VK_TO_EVDEV[0x41 + _i] = [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
                              50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45,
                              21, 44][_i]

# 主键盘数字 0-9
for _i in range(10):
    VK_TO_EVDEV[0x30 + _i] = [11, 2, 3, 4, 5, 6, 7, 8, 9, 10][_i]

# 小键盘 0-9
for _i in range(10):
    VK_TO_EVDEV[0x60 + _i] = [82, 79, 80, 81, 75, 76, 77, 71, 72, 73][_i]

# F1-F12
for _i in range(12):
    VK_TO_EVDEV[0x70 + _i] = 59 + _i if _i < 10 else (87 if _i == 10 else 88)

# F13-F24
for _i in range(12):
    VK_TO_EVDEV[0x7C + _i] = 183 + _i


# ---------------------------------------------------------------------------
# evdev -> Windows Virtual-Key
# 反向表：同一个 evdev 码对应多个 VK 时，优先选左右明确的那个
# ---------------------------------------------------------------------------
EVDEV_TO_VK: dict[int, int] = {}
for _vk, _code in VK_TO_EVDEV.items():
    if _code not in EVDEV_TO_VK:
        EVDEV_TO_VK[_code] = _vk
# 用区分左右的 VK 覆盖通用 VK，SendInput 时更准确
EVDEV_TO_VK.update({
    42: 0xA0,    # LSHIFT
    54: 0xA1,    # RSHIFT
    29: 0xA2,    # LCONTROL
    97: 0xA3,    # RCONTROL
    56: 0xA4,    # LMENU
    100: 0xA5,   # RMENU
})

# 需要 KEYEVENTF_EXTENDEDKEY 标志的 VK（右侧 Ctrl/Alt、方向键、小键盘等）
EXTENDED_VKS = frozenset({
    0xA3, 0xA5,             # RCONTROL, RMENU
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PageUp/Down End Home 方向键
    0x2D, 0x2E,             # INSERT, DELETE
    0x5B, 0x5C,             # LWIN, RWIN
    0x5D,                   # APPS
    0x6F,                   # NUMPAD DIVIDE
    0x90,                   # NUMLOCK
    0x2C,                   # PRINTSCREEN
    0xAD, 0xAE, 0xAF,       # 音量键
    0xB0, 0xB1, 0xB2, 0xB3,  # 媒体键
})


# ---------------------------------------------------------------------------
# 可读名称（只用于日志/自检，不参与映射）
# ---------------------------------------------------------------------------
KEY_NAMES: dict[int, str] = {
    1: "Esc", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8",
    10: "9", 11: "0", 12: "-", 13: "=", 14: "Backspace", 15: "Tab",
    16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I",
    24: "O", 25: "P", 26: "[", 27: "]", 28: "Enter", 29: "LCtrl",
    30: "A", 31: "S", 32: "D", 33: "F", 34: "G", 35: "H", 36: "J", 37: "K",
    38: "L", 39: ";", 40: "'", 41: "`", 42: "LShift", 43: "\\",
    44: "Z", 45: "X", 46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
    51: ",", 52: ".", 53: "/", 54: "RShift", 55: "KP*", 56: "LAlt", 57: "Space",
    58: "CapsLock", 59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5",
    64: "F6", 65: "F7", 66: "F8", 67: "F9", 68: "F10", 69: "NumLock",
    70: "ScrollLock", 71: "KP7", 72: "KP8", 73: "KP9", 74: "KP-", 75: "KP4",
    76: "KP5", 77: "KP6", 78: "KP+", 79: "KP1", 80: "KP2", 81: "KP3",
    82: "KP0", 83: "KP.", 86: "OEM102", 87: "F11", 88: "F12",
    96: "KPEnter", 97: "RCtrl", 98: "KP/", 99: "PrintScreen", 100: "RAlt",
    102: "Home", 103: "Up", 104: "PageUp", 105: "Left", 106: "Right",
    107: "End", 108: "Down", 109: "PageDown", 110: "Insert", 111: "Delete",
    113: "Mute", 114: "VolDown", 115: "VolUp", 119: "Pause",
    121: "KP,", 125: "LWin", 126: "RWin", 127: "Menu",
    142: "Sleep", 155: "BrowserFav", 156: "BrowserHome",
    158: "BrowserBack", 159: "BrowserForward", 163: "NextTrack",
    164: "PlayPause", 165: "PrevTrack", 166: "Stop", 172: "BrowserStop",
    173: "BrowserRefresh", 217: "BrowserSearch",
}


def key_name(code: int) -> str:
    name = KEY_NAMES.get(code)
    if name:
        return name
    if 183 <= code <= 194:
        return "F%d" % (13 + code - 183)
    return "code%d" % code
