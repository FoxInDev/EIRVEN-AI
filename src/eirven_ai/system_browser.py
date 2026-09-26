from __future__ import annotations

import os
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .win_input import send_virtual_keys


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
        if os.name == 'nt':
            if not send_virtual_keys(('ctrl', 'v')):
                raise RuntimeError('Ctrl+V injection failed')
        else:
            pyautogui.hotkey('ctrl','v')
    except Exception:
        pyautogui.write(str(text), interval=.01)


def press(*keys: str) -> None:
    import pyautogui  # type: ignore
    if os.name == 'nt':
        if not send_virtual_keys(keys):
            raise RuntimeError(f"Неподдерживаемая клавиша Windows: {' + '.join(keys)}")
    elif len(keys)==1: pyautogui.press(keys[0])
    else: pyautogui.hotkey(*keys)



def _focus_existing_app_window(title: str = "Эрви") -> bool:
    """Если окно Эрви уже открыто — вывести его вперёд и вернуть True.

    Раньше каждый клик по сфере или повторный запуск ярлыка открывали НОВОЕ окно,
    и копии множились. Окно Эрви узнаём точно: класс окон Edge и заголовок ровно
    «Эрви». У обычной вкладки браузера к заголовку дописано название браузера,
    так что её с окном Эрви не спутать.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list[int] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, text, length + 1)
            if text.value.strip() == title:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(proc_type(visit), 0)
        if not found:
            return False
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE — развернуть из панели задач
        user32.ShowWindow(hwnd, 5)              # SW_SHOW
        if not user32.SetForegroundWindow(hwnd):
            # Windows иногда не даёт фоновому процессу перехватить фокус —
            # тогда просим вывести окно вперёд штатным запасным способом.
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
        return True
    except Exception:
        return False

def open_app_window(url: str) -> bool:
    """Открыть интерфейс Эрви отдельным окном-приложением, а не вкладкой браузера.

    Раньше интерфейс открывался в браузере по умолчанию — новой вкладкой с адресом
    127.0.0.1 в строке. Это выглядело как «сайт на локалке», а не как программа.
    Теперь он открывается в окне без вкладок, адресной строки и кнопок браузера —
    как самостоятельное приложение. Интерфейс тот же, без единого изменения.

    Используется режим приложения Microsoft Edge: он есть на любой Windows 10/11
    и не требует ставить ничего нового — а значит, не добавляет риска к установке.
    Если Edge не найден, открываем как раньше, в браузере по умолчанию, чтобы
    интерфейс открылся в любом случае.

    Только для окна самой Эрви. Внешние сайты — музыка, Telegram, ссылки —
    по-прежнему идут через open_url в обычный браузер.
    """
    target = str(url or "").strip()
    if not target:
        return False
    # Окно уже открыто — просто вывести его вперёд, а не открывать копию.
    if _focus_existing_app_window():
        return True
    if os.name == "nt":
        import subprocess
        candidates = [
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        edge = next((c for c in candidates if c and os.path.isfile(c)), None)
        if edge:
            # Своя папка профиля: окно Эрви не смешивается с вкладками и историей
            # обычного браузера человека и открывается отдельным приложением.
            profile = os.path.join(os.environ.get("LOCALAPPDATA", "."), "EIRVEN AI", "app-window")
            try:
                os.makedirs(profile, exist_ok=True)
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(
                    [edge, f"--app={target}", f"--user-data-dir={profile}",
                     "--no-first-run", "--no-default-browser-check",
                     "--window-size=1280,860"],
                    creationflags=flags, close_fds=True,
                )
                return True
            except Exception:
                pass
    # Запасной путь — прежний: браузер по умолчанию.
    return open_url(target)
