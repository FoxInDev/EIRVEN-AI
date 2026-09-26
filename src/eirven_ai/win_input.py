from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Iterable


_NAMED_KEYS = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "winleft": 0x5B,
    "super": 0x5B,
    "winright": 0x5C,
    "apps": 0x5D,
    "browserback": 0xA6,
    "browserforward": 0xA7,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
    "stop": 0xB2,
    "playpause": 0xB3,
}

_EXTENDED_KEYS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2C,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
    0x5D,
    0xA6,
    0xA7,
    0xAD,
    0xAE,
    0xAF,
    0xB0,
    0xB1,
    0xB2,
    0xB3,
}


def virtual_key_code(key: str) -> int | None:
    clean = str(key or "").strip().casefold()
    if clean in _NAMED_KEYS:
        return _NAMED_KEYS[clean]
    if len(clean) == 1 and "a" <= clean <= "z":
        return ord(clean.upper())
    if len(clean) == 1 and "0" <= clean <= "9":
        return ord(clean)
    if clean.startswith("f") and clean[1:].isdigit():
        number = int(clean[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return None


def send_virtual_keys(keys: Iterable[str], interval: float = 0.04) -> bool:
    """Send a layout-independent Windows key press or chord.

    PyAutoGUI resolves printable hotkey letters through the active keyboard layout.
    On a Russian layout that can silently turn Ctrl+A/C/S/V into no-ops. Virtual-key
    codes for letters, digits and named control keys are stable across layouts.
    """
    if os.name != "nt":
        return False
    codes = [virtual_key_code(key) for key in keys]
    if not codes or any(code is None for code in codes):
        return False

    pause = max(0.01, min(float(interval), 0.15))
    keybd_event = ctypes.windll.user32.keybd_event
    keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.WPARAM]
    keybd_event.restype = None
    pressed: list[int] = []
    try:
        for code in codes:
            assert code is not None
            flags = 0x0001 if code in _EXTENDED_KEYS else 0
            keybd_event(code, 0, flags, 0)
            pressed.append(code)
            time.sleep(pause)
        for code in reversed(pressed):
            flags = 0x0002 | (0x0001 if code in _EXTENDED_KEYS else 0)
            keybd_event(code, 0, flags, 0)
            time.sleep(pause)
        pressed.clear()
        return True
    except Exception:
        return False
    finally:
        for code in reversed(pressed):
            try:
                flags = 0x0002 | (0x0001 if code in _EXTENDED_KEYS else 0)
                keybd_event(code, 0, flags, 0)
            except Exception:
                pass
