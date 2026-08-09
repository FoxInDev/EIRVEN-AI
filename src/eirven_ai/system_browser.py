from __future__ import annotations

import os
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


def open_url(url: str) -> bool:
    """Open URL with the user's Windows default-browser association."""
    target = str(url or '').strip()
    if not target:
        return False
    if os.name == 'nt':
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
    return bool(webbrowser.open(target))


def open_search(query: str) -> str:
    url = f"https://www.google.com/search?q={quote_plus(str(query or '').strip())}"
    open_url(url)
    return url


def foreground_window(timeout: float = 8.0) -> Any:
    if os.name != 'nt':
        raise RuntimeError('UI Automation доступна только в Windows')
    import ctypes
    from pywinauto import Desktop  # type: ignore
    deadline=time.monotonic()+max(.5,float(timeout))
    last=0
    while time.monotonic()<deadline:
        handle=int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if handle and handle != last:
            last=handle
        if handle:
            try:
                win=Desktop(backend='uia').window(handle=handle)
                if win.exists(timeout=.2): return win
            except Exception: pass
        time.sleep(.15)
    raise RuntimeError('Не удалось получить активное окно браузера')


def paste_text(text: str) -> None:
    import pyautogui  # type: ignore
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(str(text))
        pyautogui.hotkey('ctrl','v')
    except Exception:
        pyautogui.write(str(text), interval=.01)


def press(*keys: str) -> None:
    import pyautogui  # type: ignore
    if len(keys)==1: pyautogui.press(keys[0])
    else: pyautogui.hotkey(*keys)
