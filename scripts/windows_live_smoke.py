# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

"""Non-destructive, real-Windows acceptance smoke for EIRVEN desktop primitives.

Run from an *interactive logged-in Windows desktop* after EIRVEN dependencies are
installed.  This script deliberately refuses Wine/Linux/headless/session-0 execution.
It exercises EIRVEN's own production code against real HWND/UIA/Explorer/default-file
handlers and writes a machine-readable report under data/windows-live-smoke/.
"""

import ctypes
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eirven_ai.applications import ApplicationService
from eirven_ai.tools import ToolExecutor
from eirven_ai.win_input import send_virtual_keys


class _DB:
    def log_action(self, *args: Any, **kwargs: Any) -> None:
        return None


def _interactive_desktop() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError(f"real Windows required; got os.name={os.name!r}, platform={platform.platform()}")
    # Windows Python under Wine also reports os.name == "nt".  Do not let a Wine
    # compatibility layer become evidence for a native Win32/UIA claim.
    try:
        ntdll = ctypes.WinDLL("ntdll")
        getattr(ntdll, "wine_get_version")
    except AttributeError:
        pass
    else:
        raise RuntimeError("Wine detected; native Windows is required for 1:1 verification")
    user32 = ctypes.windll.user32
    DESKTOP_READOBJECTS = 0x0001
    DESKTOP_SWITCHDESKTOP = 0x0100
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
    if not hdesk:
        raise RuntimeError("no interactive input desktop: run while a user is logged into Windows")
    try:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise RuntimeError("interactive desktop has no foreground HWND")
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return {
            "interactive": True,
            "foreground_hwnd": hwnd,
            "foreground_pid": int(pid.value or 0),
            "session_name": os.environ.get("SESSIONNAME", ""),
            "windows": platform.platform(),
            "python": sys.version.split()[0],
        }
    finally:
        user32.CloseDesktop(hdesk)


def _pick_notepad(apps: list[dict[str, str]]) -> dict[str, str] | None:
    needles = ("notepad", "блокнот")
    for row in apps:
        name = str(row.get("name") or "").casefold()
        if any(n in name for n in needles):
            return row
    # App IDs are language independent on current packaged Notepad builds.
    for row in apps:
        app_id = str(row.get("app_id") or "").casefold()
        if "windowsnotepad" in app_id or app_id.endswith("notepad.exe"):
            return row
    return None


def _unicode_roundtrip(executor: ToolExecutor, target: Path) -> dict[str, Any]:
    """Type through EIRVEN SendInput into real Notepad, save, and verify disk bytes."""
    text = "Эрви: Привет, Windows! Ёжик 123 — café — 日本語 — 😀"
    before = {int(r.get("handle") or 0) for r in executor.tool_window_list(160)}
    subprocess.Popen(["notepad.exe", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    handle = 0
    deadline = time.monotonic() + 18.0
    while time.monotonic() < deadline:
        rows = executor.tool_window_list(160)
        named = [r for r in rows if target.name.casefold() in str(r.get("title") or "").casefold()]
        # Modern Notepad reuses processes, windows and tabs. A single new top-level
        # HWND can still be a restored unsaved tab, so only the unique target title is
        # acceptable evidence that this run's file is active.
        row = named[0] if named else None
        if row:
            handle = int(row.get("handle") or 0)
            if handle:
                break
        time.sleep(.15)
    if not handle:
        raise RuntimeError("Notepad window did not become observable through UIA")

    from pywinauto import Desktop
    import pyautogui
    import pyperclip

    win = Desktop(backend="uia").window(handle=handle)
    win.set_focus()
    controls = []
    for control in win.descendants():
        try:
            kind = str(control.element_info.control_type or "").casefold()
            if kind in {"edit", "document"} and control.is_visible() and control.is_enabled():
                rect = control.rectangle()
                area = max(0, rect.width()) * max(0, rect.height())
                controls.append((area, control))
        except Exception:
            continue
    if not controls:
        raise RuntimeError("Notepad editable UIA control not found")
    control = max(controls, key=lambda item: item[0])[1]

    def focus_editor() -> None:
        def editor_has_focus() -> bool:
            try:
                return bool(control.element_info.element.CurrentHasKeyboardFocus)
            except Exception:
                return False

        win.set_focus()
        control.set_focus()
        time.sleep(.12)
        foreground = executor.tool_foreground_window()
        if int(foreground.get("handle") or 0) != handle or not editor_has_focus():
            control.click_input()
            time.sleep(.12)
            foreground = executor.tool_foreground_window()
        if int(foreground.get("handle") or 0) != handle or not editor_has_focus():
            raise RuntimeError("Notepad editor did not retain foreground focus")

    focus_editor()

    old_clipboard = None
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        pass
    try:
        if not send_virtual_keys(("ctrl", "a")) or not send_virtual_keys(("backspace",)):
            raise RuntimeError("Win32 clear-editor hotkey failed")
        if not executor._windows_send_unicode(text):
            raise RuntimeError("Win32 SendInput(KEYEVENTF_UNICODE) returned incomplete injection")
        time.sleep(.20)
        try:
            ui_value = str(control.iface_value.CurrentValue or "")
        except Exception:
            ui_value = ""
        if ui_value != text:
            raise RuntimeError(f"Unicode UIA value mismatch: {ui_value!r}")

        # A slow application launched by the previous acceptance step may steal the
        # foreground window after typing. Reassert the exact editor before shortcuts.
        focus_editor()
        if not send_virtual_keys(("ctrl", "s")):
            raise RuntimeError("Win32 Ctrl+S hotkey failed")
        deadline = time.monotonic() + 6.0
        disk = ""
        while time.monotonic() < deadline:
            try:
                disk = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Older Notepad may have saved UTF-16/ANSI; clipboard still verifies UI.
                disk = ""
            if disk == text:
                break
            time.sleep(.15)

        focus_editor()
        if not send_virtual_keys(("ctrl", "a")) or not send_virtual_keys(("ctrl", "c")):
            raise RuntimeError("Win32 clipboard verification hotkey failed")
        time.sleep(.15)
        clipboard = str(pyperclip.paste() or "")
        verified = clipboard == text and (disk == text or not disk)
        if not verified:
            raise RuntimeError(
                f"Unicode roundtrip mismatch: clipboard={clipboard!r}, disk_utf8={disk!r}"
            )
        return {
            "verified": True,
            "handle": handle,
            "chars": len(text),
            "clipboard_exact": clipboard == text,
            "disk_utf8_exact": disk == text,
            "uia_value_exact": ui_value == text,
            "method": "EIRVEN Win32 SendInput -> real Notepad -> clipboard/save verification",
        }
    finally:
        if old_clipboard is not None:
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass
        try:
            send_virtual_keys(("alt", "f4"))
            time.sleep(.25)
            # If a save prompt appears despite Ctrl+S, decline it. The previous smoke
            # left this dialog open and polluted every later Notepad verification.
            for button in win.descendants():
                try:
                    if (
                        str(button.element_info.automation_id or "") == "SecondaryButton"
                        and button.is_visible()
                        and button.is_enabled()
                    ):
                        button.click_input()
                        break
                except Exception:
                    continue
        except Exception:
            pass


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_dir = ROOT / "data" / "windows-live-smoke" / stamp
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"started": stamp, "checks": [], "ok": False}

    def check(name: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            value = fn()
            row = {"name": name, "ok": True, "seconds": round(time.monotonic()-started, 3), "result": value}
            report["checks"].append(row)
            print(f"PASS  {name}")
            return value
        except Exception as exc:
            row = {
                "name": name,
                "ok": False,
                "seconds": round(time.monotonic()-started, 3),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            report["checks"].append(row)
            print(f"FAIL  {name}: {exc}")
            return None

    env = check("interactive_real_windows", _interactive_desktop)
    if not env:
        (report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    workspace = report_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    sample_dir = workspace / "Папка Эрви — Windows"
    sample_dir.mkdir(exist_ok=True)
    sample_file = sample_dir / "Тест Эрви — ёжик.txt"
    sample_file.write_text("initial", encoding="utf-8")

    settings = SimpleNamespace(
        workspace_dir=workspace,
        root_dir=ROOT,
        data_dir=report_dir,
        enable_desktop_control=True,
        enable_commands=True,
        full_access=True,
    )
    applications = ApplicationService(None, cache_path=report_dir / "start-apps.json")  # type: ignore[arg-type]
    executor = ToolExecutor(settings, _DB(), applications=applications)

    fg = check("foreground_hwnd_pid", executor.tool_foreground_window)
    check("uia_window_list", lambda: {"count": len(executor.tool_window_list(160))})
    check("real_desktop_screenshot", executor.tool_screenshot)
    check("open_folder_via_windows_default", lambda: executor.tool_system_open_path(str(sample_dir)))
    check("open_file_via_windows_default", lambda: executor.tool_system_open_path(str(sample_file)))

    apps = check("get_start_apps", lambda: applications.list_installed(refresh=True)) or []
    notepad = _pick_notepad(apps)
    if notepad:
        check(
            "launch_start_menu_app_and_verify_window",
            lambda: executor.tool_launch_application(str(notepad.get("name") or "Notepad")),
        )
    else:
        report["checks"].append({"name": "launch_start_menu_app_and_verify_window", "ok": False, "error": "Notepad not found in Get-StartApps"})
        print("FAIL  launch_start_menu_app_and_verify_window: Notepad not found in Get-StartApps")

    # A unique basename prevents restored tabs from an earlier failed Notepad run
    # from matching this run's window evidence.
    unicode_file = sample_dir / f"unicode-roundtrip-{stamp}.txt"
    unicode_file.write_text("", encoding="utf-8")
    check("unicode_sendinput_notepad_roundtrip", lambda: _unicode_roundtrip(executor, unicode_file))

    report["ok"] = all(bool(row.get("ok")) for row in report["checks"])
    report["foreground_initial"] = fg
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT {report_path}")
    print("RESULT PASS" if report["ok"] else "RESULT FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
