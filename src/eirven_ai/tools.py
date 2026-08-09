from __future__ import annotations

import json
import ctypes
import fnmatch
import string
import webbrowser
from urllib.parse import quote_plus
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ImageGrab

from .browser import BrowserAutomation
from .applications import ApplicationService
from .config import Settings
from .database import Database
from .system_access import access_summary
from .trace import log_event
from .system_browser import open_url as open_system_url, open_search as open_system_search


class ToolError(RuntimeError):
    pass


class PathGuard:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str | Path = ".") -> Path:
        value = Path(relative)
        target = value.resolve() if value.is_absolute() else (self.root / value).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"Доступ разрешён только внутри {self.root}")
        return target


class ToolExecutor:
    SAFE_EXECUTABLES = {
        "python",
        "python3",
        "py",
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "git",
        "pip",
        "pip3",
        "uv",
    }
    SHELL_TOKENS = {";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r"}

    def __init__(
        self,
        settings: Settings,
        db: Database,
        browser: BrowserAutomation | None = None,
        applications: ApplicationService | None = None,
    ):
        self.settings = settings
        self.db = db
        self.browser = browser
        self.applications = applications
        self.guard = PathGuard(settings.workspace_dir)
        # Emergency stop remains global, but normal chat/task cancellation is scoped
        # to the current worker thread. A cancelled screenshot must never poison the
        # next unrelated command.
        self.stop_event = threading.Event()
        self._scope = threading.local()
        self._process_lock = threading.RLock()
        self._active_processes: dict[int, subprocess.Popen[str]] = {}
        self.runtime_control = None

    _VOICE_GUARDED_SIDE_EFFECTS = {
        "click", "window_click", "window_type", "type_text", "press_key", "hotkey",
        "media_control", "system_volume", "process_terminate", "browser_click_text", "browser_fill", "browser_press",
        "browser_upload", "launch_application", "open_default_url", "powershell",
    }

    def _wait_voice_precommit(self, name: str) -> None:
        """Pause a side effect if the owner started speaking during the active task.

        VAD itself is not a cancellation decision.  It only closes the commit gate.
        ASR/policy will release the gate for harmless chatter, or set the task stop token
        for an actual cancel/new command.  This makes `Эрви, отмена` useful *before* the
        final Enter/click instead of acknowledging it after the irreversible action.
        """
        if name not in self._VOICE_GUARDED_SIDE_EFFECTS:
            return
        runtime = getattr(self, "runtime_control", None)
        if runtime is None or not runtime.voice_hold_active():
            return
        started = time.monotonic()
        while runtime.voice_hold_active():
            if self._stop_requested():
                raise ToolError("Остановлено пользователем до выполнения действия")
            # A broken ASR worker must fail closed rather than commit a click after an
            # indefinitely unresolved interruption.
            if time.monotonic() - started >= 4.5:
                raise ToolError("Действие не выполнено: голосовая команда ещё не разрешена")
            time.sleep(0.025)
        if self._stop_requested():
            raise ToolError("Остановлено пользователем до выполнения действия")

    def _scoped_stop_event(self) -> threading.Event | None:
        return getattr(self._scope, "stop_event", None)

    def _stop_requested(self) -> bool:
        scoped = self._scoped_stop_event()
        return self.stop_event.is_set() or bool(scoped and scoped.is_set())

    def task_scope(self, stop_event: threading.Event | None = None):
        executor = self
        class _Scope:
            def __enter__(self_inner):
                self_inner.previous = getattr(executor._scope, "stop_event", None)
                executor._scope.stop_event = stop_event
                return executor
            def __exit__(self_inner, *_args):
                if self_inner.previous is None:
                    try:
                        del executor._scope.stop_event
                    except AttributeError:
                        pass
                else:
                    executor._scope.stop_event = self_inner.previous
        return _Scope()

    def reset_stop(self) -> None:
        # Only clears the explicit emergency stop. Per-task cancellation tokens are
        # owned by TaskManager/ChatJobManager and are never reset globally.
        self.stop_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        with self._process_lock:
            processes = list(self._active_processes.values())
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass

    def _log(
        self,
        name: str,
        args: dict[str, Any],
        result: Any,
        risk: str,
        success: bool,
        *,
        elapsed_ms: int | None = None,
    ) -> None:
        self.db.log_action(name, args, result, risk, success)
        extra = {"elapsed_ms": int(elapsed_ms)} if elapsed_ms is not None else {}
        log_event(
            self.settings.root_dir,
            "TOOL",
            name=name,
            args=args,
            result=result,
            risk=risk,
            success=success,
            **extra,
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._stop_requested():
            return {"ok": False, "error": "Остановлено пользователем"}
        method = getattr(self, f"tool_{name}", None)
        if method is None:
            return {"ok": False, "error": f"Неизвестный инструмент: {name}"}
        risk = {
            "list_files": "low",
            "read_file": "low",
            "write_file": "medium",
            "make_directory": "low",
            "run_command": "medium",
            "screenshot": "low",
            "desktop_state": "low",
            "access_status": "low",
            "browser_open": "low",
            "browser_search": "low",
            "browser_snapshot": "low",
            "browser_screenshot": "low",
            "crypto_price": "low",
            "browser_click_text": "medium",
            "browser_fill": "high",
            "browser_press": "high",
            "browser_upload": "high",
            "system_volume": "medium",
            "click": "high",
            "type_text": "high",
            "window_list": "low",
            "window_elements": "low",
            "window_focus": "medium",
            "window_click": "high",
            "window_type": "high",
            "launch_application": "medium",
            "close_application": "medium",
            "close_browsers": "medium",
            "close_user_apps": "high",
            "set_dark_theme": "medium",
            "toggle_quick_setting": "high",
            "process_list": "low",
            "process_terminate": "high",
            "system_find": "low",
            "system_open_named": "medium",
            "system_open_path": "medium",
            "system_list_files": "low",
            "system_read_file": "low",
            "system_write_file": "high",
            "powershell": "high",
            "system_diagnostics": "low",
            "git_publish": "high",
            "open_default_url": "medium",
            "default_search": "low",
            "web_search": "low",
            "wait": "low",
            "window_wait": "low",
            "command_available": "low",
            "foreground_window": "low",
            "media_control": "medium",
        }.get(name, "medium")
        started = time.monotonic()
        try:
            self._wait_voice_precommit(name)
            result = method(**arguments)
            payload = {"ok": True, "result": result}
            self._log(name, arguments, payload, risk, True, elapsed_ms=round((time.monotonic() - started) * 1000))
            return payload
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
            self._log(name, arguments, payload, risk, False, elapsed_ms=round((time.monotonic() - started) * 1000))
            return payload

    def _user_path(self, path: str | Path = ".", *, must_exist: bool = False) -> Path:
        value = Path(path).expanduser()
        target = value.resolve() if value.is_absolute() else (Path.home() / value).resolve()
        # Full computer control is enabled for the owner, but autonomous file writes
        # must not silently target Windows/Program Files. User projects on any drive
        # are allowed; system directories are read-only through these generic tools.
        protected = []
        if os.name == "nt":
            for env_name in ("WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)"):
                raw = os.environ.get(env_name)
                if raw:
                    try: protected.append(Path(raw).resolve())
                    except Exception: pass
        if not self.settings.full_access and any(target == root or root in target.parents for root in protected):
            raise ToolError(f"Системный каталог защищён без режима полного доступа: {target}")
        if must_exist and not target.exists():
            raise ToolError(f"Путь не существует: {target}")
        return target

    def tool_wait(self, seconds: float = 0.7) -> dict[str, Any]:
        """Short cancellable wait for UI/network transitions."""
        delay = max(0.05, min(float(seconds or 0.7), 8.0))
        started = time.monotonic()
        while time.monotonic() - started < delay:
            if self._stop_requested():
                raise ToolError("Ожидание остановлено пользователем")
            time.sleep(min(0.08, delay))
        return {"seconds": round(time.monotonic() - started, 2)}

    def tool_command_available(self, command: str) -> dict[str, Any]:
        name = Path(str(command or "").strip()).name
        if not name:
            raise ToolError("Не указана команда")
        found = shutil.which(name)
        return {"command": name, "available": bool(found), "path": found or ""}

    def tool_foreground_window(self) -> dict[str, Any]:
        """Return the actual foreground Windows window without enumerating processes."""
        title = self._foreground_window_title()
        payload: dict[str, Any] = {"title": title or ""}
        if os.name != "nt" or not title:
            return payload
        try:
            import win32gui
            import win32process
            hwnd = win32gui.GetForegroundWindow()
            rect = win32gui.GetWindowRect(hwnd) if hwnd else None
            payload["handle"] = int(hwnd or 0) or None
            payload["class_name"] = win32gui.GetClassName(hwnd) if hwnd else ""
            if hwnd:
                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
                payload["pid"] = int(pid)
            if rect:
                payload["rectangle"] = [int(x) for x in rect]
        except Exception:
            pass
        return payload

    def tool_media_control(self, action: str = "play_pause") -> dict[str, Any]:
        """Send a standard Windows media key to the active media session.

        This is an OS primitive, not an application recipe: it works with any player that
        registers for Windows media keys. The agent must still verify the resulting UI/state.
        """
        action = str(action or "play_pause").strip().lower().replace("-", "_")
        aliases = {
            "play": "play_pause", "pause": "play_pause", "toggle": "play_pause",
            "playpause": "play_pause", "nexttrack": "next", "prev": "previous",
            "previoustrack": "previous",
        }
        action = aliases.get(action, action)
        vk = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}.get(action)
        if vk is None:
            raise ToolError("media_control: action должен быть play_pause|next|previous|stop")
        if os.name != "nt":
            raise ToolError("Медиа-клавиши доступны только в Windows")
        try:
            # keybd_event is intentionally used here for broad Windows 10 compatibility.
            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            raise ToolError(f"Не удалось отправить медиа-клавишу: {exc}") from exc
        return {"action": action, "sent": True}

    def tool_system_volume(self, action: str = "down", steps: int = 2) -> dict[str, Any]:
        """Adjust Windows master volume through the standard global volume keys.

        This is deliberately an OS primitive rather than an app-specific slider. ``steps``
        is bounded so a vague command such as "сделай потише" cannot accidentally mute
        the machine by issuing an unbounded sequence.
        """
        action = str(action or "down").strip().lower().replace("-", "_")
        aliases = {
            "lower": "down", "quieter": "down", "decrease": "down",
            "raise": "up", "louder": "up", "increase": "up",
            "toggle_mute": "mute",
        }
        action = aliases.get(action, action)
        vk = {"mute": 0xAD, "down": 0xAE, "up": 0xAF}.get(action)
        if vk is None:
            raise ToolError("system_volume: action должен быть down|up|mute")
        if os.name != "nt":
            raise ToolError("Системная громкость доступна только в Windows")
        count = 1 if action == "mute" else max(1, min(int(steps or 2), 10))
        try:
            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            for _ in range(count):
                user32.keybd_event(vk, 0, 0, 0)
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                if count > 1:
                    time.sleep(0.035)
        except Exception as exc:
            raise ToolError(f"Не удалось изменить системную громкость: {exc}") from exc
        return {"action": action, "steps": count, "sent": True, "scope": "system"}

    def tool_window_wait(
        self, title_contains: str = "", element_text: str = "", automation_id: str = "", timeout: float = 8.0
    ) -> dict[str, Any]:
        """Wait until a window/element becomes available without burning LLM turns."""
        deadline = time.monotonic() + max(0.2, min(float(timeout or 8.0), 20.0))
        last_error = ""
        while time.monotonic() < deadline:
            if self._stop_requested():
                raise ToolError("Ожидание окна остановлено пользователем")
            try:
                win = self._find_window(title_contains) if title_contains else None
                if win is not None and not element_text and not automation_id:
                    return {"ready": True, "window": win.window_text()}
                if win is not None:
                    for item in win.descendants()[:500]:
                        try:
                            info = item.element_info
                            name = str(info.name or "")
                            aid = str(info.automation_id or "")
                            if (element_text and element_text.casefold() in name.casefold()) or (automation_id and automation_id == aid):
                                return {"ready": True, "window": win.window_text(), "element": name, "automation_id": aid}
                        except Exception:
                            continue
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.18)
        return {"ready": False, "error": last_error or "Элемент не появился за отведённое время"}

    def tool_explorer_current_folder(self) -> dict[str, Any]:
        """Return the folder shown by the most relevant File Explorer window.

        r19 captures this before a cross-app mission leaves Explorer so later artifact
        steps do not have to guess what "из открытой папки" meant.
        """
        if os.name != "nt":
            raise ToolError("Текущая папка Проводника доступна только в Windows")
        script = r"""
$ErrorActionPreference = 'Stop'
$fg = 0
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EirvenWin32 { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); }
"@ -ErrorAction SilentlyContinue
try { $fg = [int64][EirvenWin32]::GetForegroundWindow() } catch {}
$shell = New-Object -ComObject Shell.Application
$rows = @()
foreach ($w in @($shell.Windows())) {
  try {
    $path = [string]$w.Document.Folder.Self.Path
    if ([string]::IsNullOrWhiteSpace($path)) { continue }
    $rows += [pscustomobject]@{ hwnd=[int64]$w.HWND; path=$path; name=[string]$w.Name }
  } catch {}
}
$pick = $rows | Where-Object { $_.hwnd -eq $fg } | Select-Object -First 1
if (-not $pick) { $pick = $rows | Select-Object -First 1 }
if ($pick) { $pick | ConvertTo-Json -Compress }
"""
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise ToolError((completed.stderr or completed.stdout or "Explorer COM failed").strip())
            raw = (completed.stdout or "").strip().splitlines()
            if not raw:
                raise ToolError("Открытая папка Проводника не найдена")
            data = json.loads(raw[-1])
            path = str(data.get("path") or "").strip()
            if not path or not Path(path).is_dir():
                raise ToolError("Путь открытого Проводника не подтверждён")
            return {"path": path, "hwnd": int(data.get("hwnd") or 0), "name": str(data.get("name") or "Explorer"), "verified": True}
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Не удалось определить открытую папку Проводника: {exc}") from exc

    def tool_process_list(self, name_contains: str = "", limit: int = 200) -> list[dict[str, Any]]:
        try:
            import psutil
        except ImportError as exc:
            raise ToolError("psutil не установлен") from exc
        needle = name_contains.strip().lower()
        rows = []
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "status"]):
            try:
                info = proc.info
                hay = " ".join([str(info.get("name") or ""), str(info.get("exe") or ""), " ".join(info.get("cmdline") or [])]).lower()
                if needle and needle not in hay:
                    continue
                rows.append({
                    "pid": info.get("pid"), "name": info.get("name"), "exe": info.get("exe"),
                    "cmdline": (info.get("cmdline") or [])[:12], "status": info.get("status"),
                })
                if len(rows) >= max(1, min(limit, 500)):
                    break
            except Exception:
                continue
        return rows

    def tool_process_terminate(
        self, name_contains: str = "", all_matches: bool = True, protect_eirven: bool = True
    ) -> dict[str, Any]:
        """Terminate matching external processes and verify they are gone.

        EIRVEN is itself Python-based. By default its current process, ancestors and any
        Python process whose command line points at this EIRVEN root are protected so a
        voice command can finish and report the result. Self-shutdown remains a separate
        explicit product action.
        """
        needle = str(name_contains or "").strip().casefold()
        if not needle:
            raise ToolError("process_terminate: укажите имя процесса")
        try:
            import psutil
        except Exception as exc:
            raise ToolError(f"psutil недоступен: {exc}") from exc

        current_pid = os.getpid()
        protected: set[int] = {current_pid}
        if protect_eirven:
            try:
                proc = psutil.Process(current_pid)
                protected.update(p.pid for p in proc.parents())
            except Exception:
                pass
        root_key = str(getattr(self.settings, "root_dir", "") or "").casefold().replace("/", "\\")
        candidates: list[Any] = []
        protected_rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                name = str(proc.info.get("name") or "")
                exe = str(proc.info.get("exe") or "")
                cmd = " ".join(str(x) for x in (proc.info.get("cmdline") or []))
                blob = f"{name} {Path(exe).name if exe else ''}".casefold()
                if needle not in blob:
                    continue
                eirven_owned = bool(protect_eirven and (pid in protected or (root_key and root_key in cmd.casefold().replace("/", "\\")) or "eirven_ai" in cmd.casefold()))
                row = {"pid": pid, "name": name, "exe": exe, "cmdline": cmd[:900]}
                if eirven_owned:
                    protected_rows.append(row)
                    protected.add(pid)
                    continue
                candidates.append(proc)
                if not all_matches:
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        terminated: list[int] = []
        killed: list[int] = []
        errors: list[str] = []
        for proc in candidates:
            try:
                proc.terminate()
                terminated.append(proc.pid)
            except Exception as exc:
                errors.append(f"{getattr(proc, 'pid', '?')}: {exc}")
        if candidates:
            try:
                _gone, alive = psutil.wait_procs(candidates, timeout=1.6)
            except Exception:
                alive = candidates
            for proc in alive:
                try:
                    proc.kill(); killed.append(proc.pid)
                except Exception as exc:
                    errors.append(f"kill {getattr(proc, 'pid', '?')}: {exc}")
            if alive:
                try: psutil.wait_procs(alive, timeout=.8)
                except Exception: pass

        remaining: list[int] = []
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                if pid in protected:
                    continue
                name = str(proc.info.get("name") or "")
                exe = str(proc.info.get("exe") or "")
                blob = f"{name} {Path(exe).name if exe else ''}".casefold()
                if needle in blob:
                    remaining.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        verified = not remaining
        return {
            "name_contains": needle, "matched_count": len(candidates),
            "terminated_count": len(set(terminated + killed)), "terminated": terminated,
            "killed": killed, "protected_count": len(protected_rows),
            "protected": protected_rows[:20], "remaining": remaining[:30],
            "verified": verified, "errors": errors[:20],
        }

    def tool_system_find(self, name: str, root: str = "", max_results: int = 80, max_depth: int = 7) -> list[str]:
        pattern = name.strip() or "*"
        # Search likely human work locations first. This is generic filesystem
        # prioritisation, not a command/app rule, and makes "find folder X" effectively
        # instant even when the profile contains a huge OneDrive/AppData tree.
        if root:
            bases = [self._user_path(root, must_exist=True)]
        else:
            home = Path.home().resolve()
            candidates = [home / "Desktop", home / "Рабочий стол", home / "Documents", home / "Downloads"]
            for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
                raw = os.environ.get(env_name)
                if raw:
                    cloud = Path(raw).expanduser().resolve()
                    candidates.extend([cloud / "Desktop", cloud / "Рабочий стол", cloud / "Documents", cloud])
            candidates.append(home)
            bases = []
            seen: set[str] = set()
            for item in candidates:
                key = str(item).lower()
                if key not in seen and item.exists():
                    seen.add(key); bases.append(item)

        results: list[str] = []
        result_seen: set[str] = set()
        limit = max(1, min(max_results, 300))
        # Exact child checks avoid a recursive scan for the overwhelmingly common case.
        for base in bases:
            direct = base / pattern
            if direct.exists():
                key = str(direct).lower()
                if key not in result_seen:
                    results.append(str(direct)); result_seen.add(key)
                    if len(results) >= limit: return results
        for base in bases:
            base_depth = len(base.parts)
            for current, dirs, files in os.walk(base):
                current_path = Path(current)
                if len(current_path.parts) - base_depth >= max(1, min(max_depth, 12)):
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d.lower() not in {".git", ".venv", "node_modules", "appdata", "$recycle.bin", "windows", "programdata"}]
                for item in list(dirs) + files:
                    if fnmatch.fnmatch(item.lower(), pattern.lower()) or pattern.lower() in item.lower():
                        found = str(current_path / item); key = found.lower()
                        if key in result_seen: continue
                        results.append(found); result_seen.add(key)
                        if len(results) >= limit: return results
        return results

    def tool_system_list_files(self, path: str, max_entries: int = 300) -> list[dict[str, Any]]:
        target = self._user_path(path, must_exist=True)
        if target.is_file():
            return [{"path": str(target), "type": "file", "size": target.stat().st_size}]
        out = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max(1, min(max_entries, 1000))]:
            out.append({"path": str(item), "name": item.name, "type": "dir" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else None})
        return out

    def tool_system_read_file(self, path: str, max_chars: int = 80_000) -> dict[str, Any]:
        target = self._user_path(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"Не файл: {target}")
        if target.stat().st_size > 10_000_000:
            raise ToolError("Файл слишком большой; сначала сузьте нужный фрагмент")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": str(target), "content": content[:max_chars], "truncated": len(content) > max_chars}

    def tool_system_write_file(self, path: str, content: str, overwrite: bool = True) -> dict[str, Any]:
        if not self.settings.enable_desktop_control:
            raise ToolError("Доступ к компьютеру отключён")
        target = self._user_path(path)
        if target.exists() and not overwrite:
            raise ToolError("Файл уже существует")
        if len(content.encode("utf-8")) > 4_000_000:
            raise ToolError("Слишком большой файл для одного шага")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    def tool_powershell(self, command: str, cwd: str = "", timeout: int = 900) -> dict[str, Any]:
        if os.name != "nt":
            raise ToolError("PowerShell-инструмент доступен только в Windows")
        if not self.settings.enable_desktop_control or not self.settings.enable_commands:
            raise ToolError("Управление компьютером отключено")
        lowered = command.lower()
        destructive = ("clear-disk", "format-volume", "diskpart", "remove-item c:\\", "rd /s c:\\", "shutdown /s", "stop-computer", "bcdedit")
        if any(token in lowered for token in destructive):
            raise ToolError("Команда затрагивает систему/диск и требует отдельного ручного выполнения владельцем")
        workdir = self._user_path(cwd or str(Path.home()), must_exist=True)
        limit = max(10, min(int(timeout), 1800))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
        thread_id = threading.get_ident()
        with self._process_lock:
            self._active_processes[thread_id] = process
        started = time.monotonic()
        try:
            while process.poll() is None:
                if self._stop_requested():
                    process.terminate(); raise ToolError("Команда остановлена пользователем")
                if time.monotonic() - started > limit:
                    process.kill(); raise ToolError(f"PowerShell превысил лимит {limit} сек.")
                time.sleep(.12)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            with self._process_lock:
                self._active_processes.pop(thread_id, None)
        return {"cwd": str(workdir), "returncode": process.returncode, "stdout": stdout[-40000:], "stderr": stderr[-40000:], "duration_seconds": round(time.monotonic()-started,2)}

    def tool_system_diagnostics(self, recent_minutes: int = 90) -> dict[str, Any]:
        """Collect a broad Windows health snapshot without changing the machine."""
        if os.name != "nt":
            return {"access": self.tool_access_status(), "processes": self.tool_process_list(limit=80)}
        minutes = max(10, min(int(recent_minutes), 1440))
        script = f"""
$ErrorActionPreference='SilentlyContinue'
$result = [ordered]@{{}}
$result.Computer = Get-ComputerInfo | Select-Object WindowsProductName,WindowsVersion,OsBuildNumber,OsLastBootUpTime
$result.Disks = Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | Select-Object DeviceID,Size,FreeSpace
$result.Network = Get-NetAdapter | Select-Object Name,Status,LinkSpeed,InterfaceDescription
$result.IP = Get-NetIPConfiguration | Select-Object InterfaceAlias,IPv4Address,IPv4DefaultGateway,DNSServer
$result.Services = Get-Service | Where-Object {{$_.Status -eq 'Stopped' -and $_.StartType -eq 'Automatic'}} | Select-Object -First 40 Name,DisplayName,Status,StartType
$since=(Get-Date).AddMinutes(-{minutes})
$result.Errors = Get-WinEvent -FilterHashtable @{{LogName='System'; Level=1,2; StartTime=$since}} -MaxEvents 40 | Select-Object TimeCreated,Id,ProviderName,LevelDisplayName,Message
$result | ConvertTo-Json -Depth 7 -Compress
"""
        output = self.tool_powershell(script, cwd=str(Path.home()), timeout=75)
        parsed: Any = output.get("stdout", "")
        try:
            parsed = json.loads(str(parsed))
        except Exception:
            pass
        return {"access": self.tool_access_status(), "snapshot": parsed, "stderr": output.get("stderr", "")}

    def tool_git_publish(self, path: str, remote: str, message: str = "update") -> dict[str, Any]:
        repo = self._user_path(path, must_exist=True)
        if not repo.is_dir():
            raise ToolError("Путь репозитория должен быть папкой")
        if not (remote.startswith("git@") or remote.startswith("https://")):
            raise ToolError("Поддерживается Git SSH/HTTPS remote")
        commands = [
            ["git", "init"], ["git", "add", "-A"],
            ["git", "commit", "-m", message[:160]],
        ]
        results = []
        for parts in commands:
            completed = subprocess.run(parts, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            results.append({"command": parts, "returncode": completed.returncode, "stdout": completed.stdout[-12000:], "stderr": completed.stderr[-12000:]})
            # commit can legitimately return 1 when there is nothing to commit.
            if completed.returncode != 0 and not (parts[1] == "commit" and "nothing to commit" in (completed.stdout+completed.stderr).lower()):
                return {"ok": False, "stage": parts[1], "results": results}
        current = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if current.returncode == 0:
            subprocess.run(["git", "remote", "set-url", "origin", remote], cwd=repo, capture_output=True, timeout=30)
        else:
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=repo, capture_output=True, timeout=30)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True, timeout=30)
        push = subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        results.append({"command": ["git", "push", "-u", "origin", "main"], "returncode": push.returncode, "stdout": push.stdout[-12000:], "stderr": push.stderr[-12000:]})
        return {"ok": push.returncode == 0, "path": str(repo), "remote": remote, "results": results}

    def tool_open_default_url(self, url: str) -> dict[str, str]:
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ToolError("Разрешены только http/https ссылки")
        if not open_system_url(url):
            raise ToolError("Не удалось открыть браузер по умолчанию")
        return {"url": url, "browser": "system_default"}

    def tool_default_search(self, query: str) -> dict[str, str]:
        url = open_system_search(query)
        return {"url": url, "query": query, "browser": "system_default"}

    def tool_web_search(self, query: str, max_results: int = 5, timelimit: str = "") -> dict[str, Any]:
        """Search the public web without a paid API key.

        This is a generic current-information tool. The model decides when it is
        needed; there are no weather/news/site-specific rules in EIRVEN.
        """
        text = str(query or "").strip()
        if not text:
            raise ToolError("Пустой поисковый запрос")
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise ToolError("Модуль быстрого веб-поиска не установлен") from exc
        limit = max(1, min(int(max_results or 5), 8))
        window = str(timelimit or "").strip().lower()
        if window not in {"", "d", "w", "m", "y"}:
            window = ""
        # DDGS performs its own backend failover. A short timeout is deliberate:
        # interactive questions must never hang behind a slow search engine.
        try:
            results = DDGS(timeout=5).text(
                text, region="ru-ru", safesearch="moderate",
                timelimit=window or None, max_results=limit, backend="auto"
            )
        except Exception as exc:
            raise ToolError(f"Быстрый веб-поиск временно недоступен: {exc}") from exc
        cleaned=[]
        for item in list(results or [])[:limit]:
            if not isinstance(item, dict):
                continue
            cleaned.append({
                "title": str(item.get("title") or "")[:300],
                "url": str(item.get("href") or item.get("url") or "")[:1200],
                "snippet": str(item.get("body") or item.get("snippet") or "")[:1800],
            })
        return {"query": text, "results": cleaned, "count": len(cleaned)}

    def tool_list_files(self, path: str = ".", max_entries: int = 300) -> list[dict[str, Any]]:
        target = self.guard.resolve(path)
        if not target.exists():
            raise ToolError(f"Путь не существует: {path}")
        if target.is_file():
            return [{"path": str(target.relative_to(self.guard.root)), "type": "file", "size": target.stat().st_size}]
        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max_entries]:
            entries.append(
                {
                    "path": str(item.relative_to(self.guard.root)),
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
        return entries

    def tool_read_file(self, path: str, max_chars: int = 50_000) -> dict[str, Any]:
        target = self.guard.resolve(path)
        if not target.is_file():
            raise ToolError(f"Файл не найден: {path}")
        if target.stat().st_size > 5_000_000:
            raise ToolError("Файл слишком большой для чтения агентом")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(target.relative_to(self.guard.root)),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }

    def tool_write_file(self, path: str, content: str, overwrite: bool = True) -> dict[str, Any]:
        target = self.guard.resolve(path)
        if target.exists() and not overwrite:
            raise ToolError("Файл уже существует")
        encoded = content.encode("utf-8")
        if len(encoded) > 2_000_000:
            raise ToolError("Лимит одного файла — 2 МБ")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(target.relative_to(self.guard.root)), "bytes": len(encoded)}

    def tool_make_directory(self, path: str) -> dict[str, Any]:
        target = self.guard.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": str(target.relative_to(self.guard.root))}

    def _split_command(self, command: str) -> list[str]:
        if any(token in command for token in self.SHELL_TOKENS):
            raise ToolError("Shell-конвейеры, перенаправления и составные команды запрещены")
        parts = shlex.split(command, posix=os.name != "nt")
        if not parts:
            raise ToolError("Пустая команда")
        executable = Path(parts[0]).name.lower().removesuffix(".exe")
        if executable not in self.SAFE_EXECUTABLES:
            raise ToolError(f"Команда {executable!r} не входит в белый список")
        if executable in {"python", "python3", "py"} and "-c" in parts:
            raise ToolError("python -c запрещён; создайте файл внутри workspace и запустите его")
        if executable == "git" and any(flag in parts for flag in ("push", "clean", "reset", "checkout")):
            raise ToolError("Опасная Git-команда запрещена в автономном режиме")
        return parts

    def tool_run_command(self, command: str, cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
        if not self.settings.enable_commands:
            raise ToolError("Запуск команд отключён в .env")
        parts = self._split_command(command)
        workdir = self.guard.resolve(cwd)
        if not workdir.is_dir():
            raise ToolError(f"Рабочая папка не существует: {cwd}")
        limit = min(timeout or self.settings.command_timeout, 1800)
        process = subprocess.Popen(
            parts,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        thread_id = threading.get_ident()
        with self._process_lock:
            self._active_processes[thread_id] = process
        started = time.monotonic()
        try:
            while process.poll() is None:
                if self._stop_requested():
                    process.terminate()
                    raise ToolError("Команда остановлена пользователем")
                if time.monotonic() - started > limit:
                    process.kill()
                    raise ToolError(f"Команда превысила лимит {limit} сек.")
                time.sleep(0.15)
            if self._stop_requested():
                raise ToolError("Команда остановлена пользователем")
            stdout, stderr = process.communicate(timeout=5)
        finally:
            with self._process_lock:
                self._active_processes.pop(thread_id, None)
        return {
            "command": parts,
            "cwd": str(workdir.relative_to(self.guard.root)),
            "returncode": process.returncode,
            "stdout": stdout[-30_000:],
            "stderr": stderr[-30_000:],
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    def tool_launch_application(self, application: str) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис запуска приложений не настроен")
        return self.applications.launch(application)

    def tool_close_application(self, application: str) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис приложений не настроен")
        return self.applications.close(application)

    def tool_close_browsers(self) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис приложений не настроен")
        return self.applications.close_browsers()

    def tool_close_user_apps(self) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис приложений не настроен")
        return self.applications.close_user_apps()

    def tool_set_dark_theme(self, enabled: bool = True) -> dict[str, Any]:
        if os.name != "nt":
            raise ToolError("Системная тема поддерживается только в Windows")
        value = 0 if bool(enabled) else 1
        command = (
            "$p='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize'; "
            f"Set-ItemProperty -Path $p -Name AppsUseLightTheme -Type DWord -Value {value}; "
            f"Set-ItemProperty -Path $p -Name SystemUsesLightTheme -Type DWord -Value {value}; "
            "Write-Output 'ok'"
        )
        result = self.tool_powershell(command, cwd=str(self.settings.root_dir), timeout=20)
        return {"dark": bool(enabled), "powershell": result}

    def tool_toggle_quick_setting(self, name: str, enabled: bool) -> dict[str, Any]:
        """Best-effort Windows Quick Settings toggle using the visible UI."""
        if os.name != "nt":
            raise ToolError("Быстрые настройки доступны только в Windows")
        pyautogui = self._desktop()
        pyautogui.hotkey("win", "a")
        time.sleep(0.55)
        desktop = self._uia_desktop()
        wanted = str(name or "").casefold().replace("ё", "е")
        aliases = {
            "airplane": ("режим в самолете", "режим полета", "airplane mode"),
            "wifi": ("wi-fi", "wifi", "вай фай"),
            "bluetooth": ("bluetooth", "блютуз"),
        }
        terms = aliases.get(wanted, (wanted,))
        found = None
        for window in desktop.windows():
            try:
                for ctrl in window.descendants(control_type="Button"):
                    text = (ctrl.window_text() or "").casefold().replace("ё", "е")
                    if text and any(term in text for term in terms):
                        found = ctrl
                        break
                if found is not None:
                    break
            except Exception:
                continue
        if found is None:
            pyautogui.press("esc")
            raise ToolError(f"Переключатель не найден в быстрых настройках: {name}")
        current = None
        try:
            current = int(found.get_toggle_state())
        except Exception:
            try:
                current = int(found.iface_toggle.CurrentToggleState)
            except Exception:
                current = None
        desired = 1 if bool(enabled) else 0
        if current is None or current != desired:
            found.click_input()
            time.sleep(0.3)
        pyautogui.press("esc")
        return {"name": name, "enabled": bool(enabled), "previous": current}

    def tool_system_open_named(self, name: str, location: str = "") -> dict[str, Any]:
        """Find and open a user file/folder in one generic operation.

        This is intentionally semantic filesystem plumbing, not a catalogue of commands:
        it works for any name and merely ranks the normal ``system_find`` results.
        """
        needle = str(name or "").strip()
        if not needle:
            raise ToolError("Не указано имя файла или папки")
        results = self.tool_system_find(needle, max_results=50, max_depth=6)
        if not results:
            raise ToolError(f"Не найдено: {needle}")
        location_norm = str(location or "").strip().casefold()
        aliases = {
            "desktop": ("desktop", "рабочий стол"),
            "рабочий стол": ("desktop", "рабочий стол"),
            "documents": ("documents", "документы"),
            "downloads": ("downloads", "загрузки"),
        }
        location_tokens = aliases.get(location_norm, (location_norm,) if location_norm else ())
        needle_cf = needle.casefold()
        def score(raw: str) -> tuple[int, int, int]:
            path = Path(raw)
            exact = 0 if path.name.casefold() == needle_cf else 1
            loc = 0 if not location_tokens or any(token and token in raw.casefold() for token in location_tokens) else 1
            return (loc, exact, len(path.parts))
        chosen = min(results, key=score)
        opened = self.tool_system_open_path(chosen)
        opened["matched_name"] = needle
        opened["candidates"] = len(results)
        return opened

    def tool_system_open_path(self, path: str) -> dict[str, Any]:
        """Open any existing user file/folder with the OS default handler."""
        target = self._user_path(path, must_exist=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"path": str(target), "type": "directory" if target.is_dir() else "file"}

    def tool_access_status(self) -> dict[str, Any]:
        """Return the effective local Windows access level available to EIRVEN."""
        return access_summary(self.settings.full_access, self.settings.enable_desktop_control)

    @staticmethod
    def _foreground_window_title() -> str:
        if os.name != "nt":
            return ""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:
            return ""

    def tool_screenshot(self) -> dict[str, Any]:
        """Capture the real Windows desktop across all monitors, not the browser viewport."""
        if not self.settings.enable_desktop_control:
            raise ToolError("Доступ к рабочему столу отключён")
        folder = self.settings.data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"desktop-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
        backend = ""
        width = height = 0
        bounds: dict[str, int] = {"left": 0, "top": 0, "width": 0, "height": 0}
        try:
            import mss
            from PIL import Image
            with mss.mss() as capture:
                monitor = dict(capture.monitors[0])
                shot = capture.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                image.save(target)
                width, height = image.size
                bounds = {key: int(monitor.get(key, 0)) for key in ("left", "top", "width", "height")}
                backend = "mss"
        except Exception as mss_error:
            try:
                image = ImageGrab.grab(all_screens=True)
                image.save(target)
                width, height = image.size
                bounds["width"], bounds["height"] = width, height
                backend = "imagegrab"
            except Exception:
                try:
                    import pyautogui
                except ImportError as exc:
                    raise ToolError(f"Не удалось снять реальный рабочий стол: {mss_error}") from exc
                pyautogui.FAILSAFE = True
                image = pyautogui.screenshot(str(target))
                width, height = image.size
                bounds["width"], bounds["height"] = width, height
                backend = "pyautogui"
        cursor = {}
        try:
            import pyautogui
            pos = pyautogui.position()
            cursor = {"x": int(pos.x), "y": int(pos.y)}
        except Exception:
            pass
        return {
            "path": str(target),
            "width": int(width),
            "height": int(height),
            "backend": backend,
            "virtual_desktop": bounds,
            "foreground_window": self._foreground_window_title(),
            "cursor": cursor,
            "source": "real_desktop",
        }

    def tool_desktop_state(self) -> dict[str, Any]:
        """One-shot real-desktop observation with screenshot, active window and access state."""
        return {
            "screen": self.tool_screenshot(),
            "access": self.tool_access_status(),
        }

    def _browser(self) -> BrowserAutomation:
        if not self.browser:
            raise ToolError("Браузерный модуль не настроен")
        return self.browser

    def tool_browser_open(self, url: str) -> dict[str, Any]:
        return self._browser().open(url)

    def tool_browser_search(self, query: str) -> dict[str, Any]:
        return self._browser().search(query)

    def tool_browser_snapshot(self, max_chars: int = 30_000) -> dict[str, Any]:
        return self._browser().snapshot(max_chars=max_chars)

    def tool_browser_click_text(self, text: str, exact: bool = False) -> dict[str, Any]:
        return self._browser().click_text(text, exact=exact)

    def tool_browser_fill(self, selector_or_label: str, value: str) -> dict[str, Any]:
        return self._browser().fill(selector_or_label, value)

    def tool_browser_press(self, key: str) -> dict[str, Any]:
        return self._browser().press(key)

    def tool_browser_upload(self, path: str, selector: str = "input[type=file]") -> dict[str, Any]:
        return self._browser().upload_file(path, selector=selector)

    def tool_browser_screenshot(self) -> dict[str, Any]:
        return self._browser().screenshot()

    def tool_crypto_price(self, symbol: str = "bitcoin", currency: str = "usd") -> dict[str, Any]:
        return self._browser().crypto_price(symbol=symbol, currency=currency)

    def _desktop(self):
        if not self.settings.enable_desktop_control:
            raise ToolError("Управление мышью/клавиатурой отключено в настройках")
        try:
            import pyautogui
        except ImportError as exc:
            raise ToolError("Запустите scripts/repair_windows.ps1") from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.2
        return pyautogui

    def tool_click(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        pyautogui = self._desktop()
        pyautogui.click(x=int(x), y=int(y), button=button)
        return {"x": int(x), "y": int(y), "button": button}

    def tool_mouse_move(self, x: int, y: int, duration: float = 0.2) -> dict[str, Any]:
        pyautogui = self._desktop()
        duration = max(0.0, min(float(duration), 2.0))
        pyautogui.moveTo(int(x), int(y), duration=duration)
        return {"x": int(x), "y": int(y), "duration": duration}

    def tool_mouse_drag(self, x: int, y: int, duration: float = 0.4, button: str = "left") -> dict[str, Any]:
        pyautogui = self._desktop()
        duration = max(0.05, min(float(duration), 3.0))
        if button not in {"left", "right", "middle"}:
            raise ToolError("Неизвестная кнопка мыши")
        pyautogui.dragTo(int(x), int(y), duration=duration, button=button)
        return {"x": int(x), "y": int(y), "duration": duration, "button": button}

    def tool_scroll(self, amount: int) -> dict[str, Any]:
        pyautogui = self._desktop()
        amount = max(-50, min(int(amount), 50))
        pyautogui.scroll(amount)
        return {"amount": amount}

    def tool_press_key(self, key: str) -> dict[str, Any]:
        pyautogui = self._desktop()
        key = str(key).strip().lower()
        if not key or len(key) > 30:
            raise ToolError("Некорректная клавиша")
        pyautogui.press(key)
        return {"key": key}

    def tool_hotkey(self, keys: list[str]) -> dict[str, Any]:
        pyautogui = self._desktop()
        safe = [str(k).strip().lower() for k in keys if str(k).strip()]
        if not 2 <= len(safe) <= 5:
            raise ToolError("Горячая клавиша должна содержать 2–5 клавиш")
        pyautogui.hotkey(*safe)
        return {"keys": safe}

    def tool_type_text(self, text: str, interval: float = 0.02) -> dict[str, Any]:
        pyautogui = self._desktop()
        if len(text) > 4000:
            raise ToolError("Слишком длинный ввод")
        pyautogui.write(text, interval=max(0.0, min(interval, 0.2)))
        return {"chars": len(text)}

    def _uia_desktop(self):
        if not self.settings.enable_desktop_control:
            raise ToolError("Управление приложениями отключено в настройках")
        if os.name != "nt":
            raise ToolError("Структурное управление окнами сейчас доступно только в Windows")
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise ToolError("Компонент Windows UI Automation не установлен") from exc
        return Desktop(backend="uia")

    def _find_window(self, title_contains: str = "", handle: int | None = None):
        desktop = self._uia_desktop()
        if handle:
            return desktop.window(handle=int(handle))
        needle = title_contains.strip().lower()
        if not needle:
            raise ToolError("Укажите часть заголовка окна или handle")
        for window in desktop.windows():
            try:
                if needle in (window.window_text() or "").lower():
                    return window
            except Exception:
                continue
        raise ToolError(f"Окно не найдено: {title_contains}")

    def tool_window_list(self, max_windows: int = 80) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for window in self._uia_desktop().windows()[: max(1, min(max_windows, 200))]:
            try:
                title = (window.window_text() or "").strip()
                if not title:
                    continue
                rect = window.rectangle()
                output.append(
                    {
                        "title": title,
                        "handle": int(window.handle),
                        "class_name": window.element_info.class_name,
                        "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
                    }
                )
            except Exception:
                continue
        return output

    def tool_window_focus(self, title_contains: str = "", handle: int | None = None) -> dict[str, Any]:
        window = self._find_window(title_contains, handle)
        window.set_focus()
        return {"title": window.window_text(), "handle": int(window.handle)}

    def tool_window_elements(
        self,
        title_contains: str = "",
        handle: int | None = None,
        max_elements: int = 250,
    ) -> list[dict[str, Any]]:
        window = self._find_window(title_contains, handle)
        output: list[dict[str, Any]] = []
        for item in window.descendants()[: max(1, min(max_elements, 500))]:
            try:
                info = item.element_info
                name = (info.name or "").strip()
                automation_id = str(info.automation_id or "")
                control_type = str(info.control_type or "")
                class_name = str(getattr(info, "class_name", "") or "")
                focused = bool(getattr(info, "has_keyboard_focus", False))
                focusable = bool(getattr(info, "is_keyboard_focusable", False))
                # Browser contenteditable controls can be unnamed Groups/Documents.
                # Keep them when their role/class/focusability carries interaction evidence;
                # otherwise the resolver never gets a chance to discover the real composer.
                inputish = control_type.casefold() in {"edit", "combobox", "group", "document"}
                if not name and not automation_id and not class_name and not (inputish and (focused or focusable)):
                    continue
                rect = item.rectangle()
                value = ""
                if control_type.casefold() in {"edit", "combobox"} or (inputish and (focused or focusable)):
                    try:
                        value = str(item.iface_value.CurrentValue or "")
                    except Exception:
                        try:
                            value = str(item.window_text() or "")
                        except Exception:
                            value = ""
                output.append(
                    {
                        "name": name,
                        "control_type": control_type,
                        "automation_id": automation_id,
                        "class_name": class_name,
                        "enabled": bool(item.is_enabled()),
                        "visible": bool(item.is_visible()),
                        "focused": focused,
                        "focusable": focusable,
                        "value": value,
                        "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
                    }
                )
            except Exception:
                continue
        return output

    def _find_control(
        self,
        title_contains: str,
        element_text: str = "",
        control_type: str = "",
        automation_id: str = "",
        handle: int | None = None,
    ):
        window = self._find_window(title_contains, handle)
        criteria: dict[str, Any] = {}
        if element_text:
            criteria["title"] = element_text
        if control_type:
            criteria["control_type"] = control_type
        if automation_id:
            criteria["auto_id"] = automation_id
        if not criteria:
            raise ToolError("Укажите текст, тип или automation_id элемента")
        control = window.child_window(**criteria).wrapper_object()
        return window, control

    def tool_window_click(
        self,
        title_contains: str,
        element_text: str = "",
        control_type: str = "",
        automation_id: str = "",
        handle: int | None = None,
    ) -> dict[str, Any]:
        window, control = self._find_control(
            title_contains, element_text, control_type, automation_id, handle
        )
        window.set_focus()
        try:
            control.invoke()
        except Exception:
            control.click_input()
        return {"window": window.window_text(), "element": control.window_text()}

    def tool_window_type(
        self,
        title_contains: str,
        text: str,
        element_text: str = "",
        control_type: str = "Edit",
        automation_id: str = "",
        handle: int | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        if len(text) > 10_000:
            raise ToolError("Слишком длинный ввод")
        window, control = self._find_control(
            title_contains, element_text, control_type, automation_id, handle
        )
        window.set_focus()
        control.set_focus()
        if replace:
            try:
                control.set_edit_text(text)
            except Exception:
                control.type_keys("^a{BACKSPACE}", set_foreground=True)
                control.type_keys(text, with_spaces=True, set_foreground=True)
        else:
            control.type_keys(text, with_spaces=True, set_foreground=True)
        return {"window": window.window_text(), "element": control.window_text(), "chars": len(text)}

    def descriptions(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {"name": "list_files", "description": "Показать файлы/папки внутри workspace", "arguments": {"path": "строка, по умолчанию ."}},
            {"name": "read_file", "description": "Прочитать текстовый файл внутри workspace", "arguments": {"path": "строка"}},
            {"name": "write_file", "description": "Создать или изменить текстовый файл внутри workspace", "arguments": {"path": "строка", "content": "полное содержимое"}},
            {"name": "make_directory", "description": "Создать папку внутри workspace", "arguments": {"path": "строка"}},
            {"name": "run_command", "description": "Запустить одну разрешённую команду без shell внутри workspace", "arguments": {"command": "строка", "cwd": "строка"}},
            {"name": "screenshot", "description": "Сделать снимок ВСЕГО реального рабочего стола Windows со всех мониторов, не страницы браузера", "arguments": {}},
            {"name": "desktop_state", "description": "Получить состояние реального рабочего стола: полный скриншот, активное окно, курсор и уровень доступа", "arguments": {}},
            {"name": "access_status", "description": "Проверить, запущен ли EIRVEN с полным административным доступом к компьютеру", "arguments": {}},
            {"name": "launch_application", "description": "Запустить установленное приложение по названию после явной просьбы владельца", "arguments": {"application": "название приложения"}},
            {"name": "close_application", "description": "Закрыть запущенное пользовательское приложение по названию", "arguments": {"application": "название приложения"}},
            {"name": "close_user_apps", "description": "Закрыть видимые пользовательские приложения, сохранив Windows и EIRVEN", "arguments": {}},
            {"name": "set_dark_theme", "description": "Включить или выключить темную системную тему Windows", "arguments": {"enabled": "true/false"}},
            {"name": "toggle_quick_setting", "description": "Переключить видимую быструю настройку Windows, например airplane/wifi/bluetooth", "arguments": {"name": "airplane|wifi|bluetooth", "enabled": "true/false"}},
            {"name": "process_list", "description": "Посмотреть реальные процессы Windows; можно фильтровать по названию", "arguments": {"name_contains": "необязательно"}},
            {"name": "explorer_current_folder", "description": "Получить подтверждённый путь папки, которая сейчас открыта в Проводнике Windows", "arguments": {}},
            {"name": "process_terminate", "description": "Завершить совпадающие внешние процессы и затем проверить, что они действительно закрыты; процессы EIRVEN по умолчанию защищены", "arguments": {"name_contains": "имя процесса", "all_matches": "true/false", "protect_eirven": "true/false"}},
            {"name": "system_find", "description": "Найти файл или папку на компьютере владельца по имени; по умолчанию ищет в профиле пользователя", "arguments": {"name": "часть имени", "root": "необязательный путь"}},
            {"name": "system_open_named", "description": "Найти и сразу открыть любой пользовательский файл или папку по имени за один шаг; location можно указать как desktop/рабочий стол/documents/downloads", "arguments": {"name": "имя файла или папки", "location": "необязательная подсказка места"}},
            {"name": "system_open_path", "description": "Открыть существующий пользовательский файл или папку стандартным приложением ОС", "arguments": {"path": "полный путь"}},
            {"name": "system_list_files", "description": "Показать содержимое произвольной пользовательской папки", "arguments": {"path": "полный путь"}},
            {"name": "system_read_file", "description": "Прочитать текстовый файл проекта вне workspace", "arguments": {"path": "полный путь"}},
            {"name": "system_write_file", "description": "Изменить текстовый файл в пользовательской области, когда это прямо нужно задаче", "arguments": {"path": "полный путь", "content": "содержимое"}},
            {"name": "powershell", "description": "Универсальный PowerShell с правами текущего EIRVEN. Используй для файлов, Git/Docker/SSH, служб, реестра, темы Windows, Wi-Fi, VPN, сети, устройств и системных настроек, когда нет более структурного инструмента. Критически разрушительные операции запрещены без отдельного подтверждения.", "arguments": {"command": "PowerShell", "cwd": "полный путь"}},
            {"name": "system_diagnostics", "description": "Собрать реальную диагностику Windows: диски, сеть, службы и недавние системные ошибки; ничего не меняет", "arguments": {"recent_minutes": "10..1440"}},
            {"name": "git_publish", "description": "Закоммитить текущие изменения проекта и отправить в указанный Git-репозиторий. При ошибке авторизации попроси владельца войти/добавить ключ и затем wait_user", "arguments": {"path": "папка проекта", "remote": "git@... или https://...", "message": "commit message"}},
            {"name": "open_default_url", "description": "Открыть ссылку в браузере пользователя по умолчанию", "arguments": {"url": "http/https"}},
            {"name": "default_search", "description": "Открыть поиск в браузере пользователя по умолчанию", "arguments": {"query": "строка"}},
            {"name": "web_search", "description": "Быстро получить актуальные результаты публичного веб-поиска без платного API. Используй для погоды, новостей, цен, свежей документации и любых фактов, которые могли измениться.", "arguments": {"query": "поисковый запрос", "max_results": "1..8", "timelimit": "необязательно: d|w|m|y"}},
            {"name": "wait", "description": "Коротко подождать загрузку/разблокировку интерфейса и затем снова проверить состояние", "arguments": {"seconds": "0.05..8"}},
            {"name": "command_available", "description": "Проверить, установлена ли системная команда/утилита (например git) и узнать путь", "arguments": {"command": "имя команды"}},
            {"name": "foreground_window", "description": "Получить только настоящее активное окно Windows без перечисления процессов", "arguments": {}},
            {"name": "media_control", "description": "Отправить стандартную системную медиа-команду любому активному плееру Windows; после неё обязательно проверь состояние интерфейса", "arguments": {"action": "play_pause|next|previous|stop"}},
            {"name": "system_volume", "description": "Изменить системную громкость Windows стандартными глобальными клавишами", "arguments": {"action": "down|up|mute", "steps": "1..10"}},
        ]
        if self.settings.enable_browser:
            # r14 agents never get a second hidden browser profile. Visible web work
            # goes through open_default_url/default_search + the real Windows UI.
            tools.append({"name": "crypto_price", "description": "Получить текущую цену криптовалюты из публичного источника", "arguments": {"symbol": "bitcoin|btc|ethereum", "currency": "usd|eur|rub"}})
        if self.settings.enable_desktop_control:
            tools.extend(
                [
                    {"name": "window_list", "description": "Получить открытые окна Windows со структурными идентификаторами", "arguments": {}},
                    {"name": "window_elements", "description": "Прочитать кнопки, поля и другие элементы окна через Windows UI Automation", "arguments": {"title_contains": "часть заголовка", "handle": "необязательный handle"}},
                    {"name": "window_wait", "description": "Подождать появления окна или элемента после загрузки/перехода, не расходуя LLM-циклы", "arguments": {"title_contains": "часть заголовка", "element_text": "необязательный текст элемента", "automation_id": "необязательный id", "timeout": "0.2..20 сек."}},
                    {"name": "window_focus", "description": "Перевести фокус на окно", "arguments": {"title_contains": "часть заголовка"}},
                    {"name": "window_click", "description": "Нажать структурный элемент окна по тексту или automation_id", "arguments": {"title_contains": "окно", "element_text": "текст", "control_type": "Button", "automation_id": "необязательно"}},
                    {"name": "window_type", "description": "Ввести текст в структурное поле окна", "arguments": {"title_contains": "окно", "element_text": "подпись/текст", "text": "строка", "replace": "bool"}},
                    {"name": "mouse_move", "description": "Плавно переместить курсор к координатам, чтобы владелец видел, на что указывает EIRVEN", "arguments": {"x": "целое", "y": "целое", "duration": "0..2 сек."}},
                    {"name": "mouse_drag", "description": "Перетащить мышью элемент или ползунок к координатам", "arguments": {"x": "целое", "y": "целое", "duration": "0..3 сек.", "button": "left|right|middle"}},
                    {"name": "scroll", "description": "Прокрутить активное окно вверх или вниз", "arguments": {"amount": "-50..50; положительное вверх"}},
                    {"name": "press_key", "description": "Нажать одну клавишу в активном окне", "arguments": {"key": "enter|escape|tab|f5 и т.п."}},
                    {"name": "hotkey", "description": "Нажать сочетание клавиш в активном окне", "arguments": {"keys": "массив вроде [ctrl,l]"}},
                    {"name": "click", "description": "Резервный клик по координатам всего экрана", "arguments": {"x": "целое", "y": "целое", "button": "left|right"}},
                    {"name": "type_text", "description": "Резервный ввод текста в активное окно", "arguments": {"text": "строка", "interval": "число"}},
                ]
            )
        return tools


# Native Ollama/OpenAI-compatible function schemas. Kept outside ToolExecutor's
# internal prose format so the same executor can be used by both tests and agents.
def _native_tool_schema(executor: ToolExecutor) -> list[dict[str, Any]]:
    type_map = {
        "x": "integer", "y": "integer", "amount": "integer", "handle": "integer",
        "timeout": "integer", "recent_minutes": "integer", "max_results": "integer", "max_depth": "integer",
        "max_entries": "integer", "max_windows": "integer", "max_elements": "integer", "steps": "integer",
        "interval": "number", "duration": "number", "seconds": "number", "exact": "boolean",
        "replace": "boolean", "overwrite": "boolean", "all_matches": "boolean", "protect_eirven": "boolean", "keys": "array",
    }
    required_by_tool = {
        "read_file": {"path"}, "write_file": {"path", "content"},
        "make_directory": {"path"}, "run_command": {"command"},
        "launch_application": {"application"}, "close_application": {"application"}, "close_user_apps": set(), "set_dark_theme": set(), "toggle_quick_setting": {"name", "enabled"}, "system_find": {"name"},
        "system_open_named": {"name"}, "system_open_path": {"path"}, "system_list_files": {"path"},
        "system_read_file": {"path"}, "system_write_file": {"path", "content"},
        "powershell": {"command"}, "git_publish": {"path", "remote"},
        "open_default_url": {"url"}, "default_search": {"query"}, "web_search": {"query"},
        "command_available": {"command"}, "media_control": {"action"}, "system_volume": {"action"}, "process_terminate": {"name_contains"},
        "browser_open": {"url"}, "browser_search": {"query"},
        "browser_click_text": {"text"}, "browser_fill": {"selector_or_label", "value"},
        "browser_press": {"key"}, "browser_upload": {"path"}, "click": {"x", "y"},
        "mouse_move": {"x", "y"}, "mouse_drag": {"x", "y"},
        "scroll": {"amount"}, "press_key": {"key"}, "hotkey": {"keys"},
        "type_text": {"text"}, "window_focus": {"title_contains"},
        "window_click": {"title_contains"}, "window_type": {"title_contains", "text"},
    }
    schemas: list[dict[str, Any]] = []
    for item in executor.descriptions():
        name = str(item.get("name") or "")
        arguments = dict(item.get("arguments") or {})
        properties: dict[str, Any] = {}
        for key, hint in arguments.items():
            json_type = type_map.get(key, "string")
            prop: dict[str, Any] = {"type": json_type, "description": str(hint)}
            if json_type == "array":
                prop["items"] = {"type": "string"}
            properties[key] = prop
        params: dict[str, Any] = {"type": "object", "properties": properties}
        required = sorted(required_by_tool.get(name, set()) & set(properties))
        if required:
            params["required"] = required
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(item.get("description") or name),
                "parameters": params,
            },
        })
    return schemas

ToolExecutor.native_descriptions = _native_tool_schema  # type: ignore[attr-defined]
