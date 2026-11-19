"""Keyboard and mouse input via the Windows SendInput API.

Replaces pyautogui, which pulled in GPL-licensed modules (pymsgbox, mouseinfo) --
not acceptable in a product that is sold closed-source. This module uses only the
Windows API through ctypes, and it can also type text pyautogui could not
(Hindi, accented letters, emoji) because characters are sent as Unicode.

Same function names as the subset of pyautogui LocalPilot used: write / typewrite,
press, hotkey, click, doubleClick, rightClick, moveTo, dragTo, scroll, screenshot,
plus FAILSAFE (move the mouse into the top-left corner to abort).
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

FAILSAFE = True


class FailSafeException(Exception):
    """Raised when the mouse is in the top-left corner and FAILSAFE is on."""


user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x20, 0x40
MOUSEEVENTF_WHEEL = 0x800
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

# key names the model / code use -> virtual-key codes
_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "del": 0x2E, "win": 0x5B, "winleft": 0x5B, "windows": 0x5B, "command": 0x5B,
    "apps": 0x5D, "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "playpause": 0xB3,
}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}


def _send(*inputs: INPUT):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _failsafe():
    if FAILSAFE:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        if pt.x <= 0 and pt.y <= 0:
            raise FailSafeException("mouse moved to the top-left corner (failsafe)")


def _key_input(vk: int, up: bool) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags))


def _unicode_input(ch: str, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags))


def key_code(name: str) -> int | None:
    """'ctrl' -> 0x11, 'a' -> 0x41, ',' -> VK_OEM_COMMA ... None if unknown."""
    n = (name or "").strip().lower()
    if n in _VK:
        return _VK[n]
    if len(n) == 1:
        if "a" <= n <= "z" or "0" <= n <= "9":
            return ord(n.upper())
        r = user32.VkKeyScanW(n)
        return None if r == -1 else r & 0xFF
    return None


# ---------------------------------------------------------------- keyboard
def press(key: str, presses: int = 1, interval: float = 0.0):
    _failsafe()
    vk = key_code(key)
    if vk is None:
        raise ValueError(f"unknown key: {key!r}")
    for _ in range(presses):
        _send(_key_input(vk, False), _key_input(vk, True))
        if interval:
            time.sleep(interval)


def hotkey(*keys: str, interval: float = 0.02):
    """hotkey('ctrl', 'l') -- press in order, release in reverse."""
    _failsafe()
    vks = []
    for k in keys:
        vk = key_code(k)
        if vk is None:
            raise ValueError(f"unknown key: {k!r}")
        vks.append(vk)
    for vk in vks:
        _send(_key_input(vk, False)); time.sleep(interval)
    for vk in reversed(vks):
        _send(_key_input(vk, True)); time.sleep(interval)


def write(text: str, interval: float = 0.0):
    """Type text exactly as given, any language. '\\n' presses Enter, '\\t' presses Tab."""
    _failsafe()
    if any(ord(c) > 0xFFFF for c in text):
        interval = max(interval, 0.06)   # measured: faster than this, apps garble text after emoji
    for ch in text:
        if ch == "\n":
            _send(_key_input(0x0D, False), _key_input(0x0D, True))
        elif ch == "\t":
            _send(_key_input(0x09, False), _key_input(0x09, True))
        elif ch == "\r":
            continue
        else:
            raw = ch.encode("utf-16-le")              # emoji etc. = 2 UTF-16 units
            units = [chr(int.from_bytes(raw[i:i + 2], "little")) for i in range(0, len(raw), 2)]
            # one SendInput call: all key-downs, then all key-ups (a surrogate pair must
            # arrive together, or the app garbles the NEXT character)
            _send(*[_unicode_input(u, False) for u in units], *[_unicode_input(u, True) for u in units])
            if len(units) > 1:
                time.sleep(0.12)      # let the app finish composing the emoji before more keys
        if interval:
            time.sleep(interval)


typewrite = write


# ---------------------------------------------------------------- mouse
def moveTo(x: int, y: int, duration: float = 0.0):
    _failsafe()
    if duration and duration > 0:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        steps = max(2, int(duration / 0.01))
        for i in range(1, steps + 1):
            user32.SetCursorPos(int(pt.x + (x - pt.x) * i / steps), int(pt.y + (y - pt.y) * i / steps))
            time.sleep(duration / steps)
    user32.SetCursorPos(int(x), int(y))


def _button(button: str):
    return {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}[button]


def _mouse(flags: int, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=data & 0xFFFFFFFF,
                                                  dwFlags=flags))


def click(x: int | None = None, y: int | None = None, clicks: int = 1, button: str = "left",
          interval: float = 0.05):
    if x is not None and y is not None:
        moveTo(x, y)
    _failsafe()
    down, up = _button(button)
    for i in range(clicks):
        _send(_mouse(down), _mouse(up))
        if i + 1 < clicks:
            time.sleep(interval)


def doubleClick(x=None, y=None):
    click(x, y, clicks=2)


def rightClick(x=None, y=None):
    click(x, y, button="right")


def dragTo(x: int, y: int, duration: float = 0.3, button: str = "left"):
    _failsafe()
    down, up = _button(button)
    _send(_mouse(down))
    try:
        moveTo(x, y, duration)
    finally:
        _send(_mouse(up))


def scroll(amount: int, x: int | None = None, y: int | None = None):
    """Positive = up, negative = down; same wheel units pyautogui used on Windows."""
    if x is not None and y is not None:
        moveTo(x, y)
    _failsafe()
    _send(_mouse(MOUSEEVENTF_WHEEL, int(amount)))


def screenshot():
    from PIL import ImageGrab
    return ImageGrab.grab()
