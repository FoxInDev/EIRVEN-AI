from __future__ import annotations

import json
import re
import ctypes
import fnmatch
import hashlib
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
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import ImageGrab

from .browser import BrowserAutomation
from .applications import ApplicationService
from .config import Settings
from .database import Database
from .system_access import access_summary
from .risk_policy import RiskPolicy
from .trace import log_event
from .system_browser import open_url as open_system_url, open_search as open_system_search, foreground_window
from .win_input import send_virtual_keys


class ToolError(RuntimeError):
    pass


class PathGuard:
    def __init__(self, root: Path, extra_roots: list[Path] | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = tuple(dict.fromkeys([self.root, *[Path(item).resolve() for item in (extra_roots or [])]]))

    def display(self, target: Path) -> str:
        target = target.resolve()
        for base in self.allowed_roots:
            if target == base:
                return "." if base == self.root else str(base)
            if base in target.parents:
                return str(target.relative_to(base))
        return str(target)

    def resolve(self, relative: str | Path = ".") -> Path:
        value = Path(relative)
        target = value.resolve() if value.is_absolute() else (self.root / value).resolve()
        if not any(target == base or base in target.parents for base in self.allowed_roots):
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
        # Bound by services.py after MailService is constructed. Keeping this dependency
        # optional lets ToolExecutor stay usable in isolated tests.
        self.mail_service = None
        self.phone_sync_service = None
        self.service_opener = None
        desktop_roots = []
        for candidate in (
            Path(os.environ.get("OneDrive", "")) / "Desktop" if os.environ.get("OneDrive") else None,
            Path(os.environ.get("USERPROFILE", "")) / "Desktop" if os.environ.get("USERPROFILE") else None,
            Path.home() / "Desktop",
            Path.home() / "Рабочий стол",
        ):
            if candidate and candidate.is_dir():
                desktop_roots.append(candidate)
        self.guard = PathGuard(settings.workspace_dir, desktop_roots)
        # Emergency stop remains global, but normal chat/task cancellation is scoped
        # to the current worker thread. A cancelled screenshot must never poison the
        # next unrelated command.
        self.stop_event = threading.Event()
        self._scope = threading.local()
        self._stop_lock = threading.RLock()
        self._stop_generation = 0
        self._process_lock = threading.RLock()
        self._active_processes: dict[int, subprocess.Popen[str]] = {}
        self.runtime_control = None
        self.desktop_lock = None
        self.cognition = None
        self._last_observer_log = 0.0
        self._uia_probe_lock = threading.RLock()
        self._uia_probe_thread: threading.Thread | None = None

    _DESKTOP_SERIALIZED = {
        "screenshot", "desktop_state", "foreground_window", "window_list", "window_elements",
        "window_focus", "window_click", "window_type", "window_wait", "click", "type_text",
        "mouse_move", "mouse_drag", "scroll", "press_key", "hotkey", "launch_application",
        "close_application", "close_browsers", "close_user_apps", "open_default_url",
        "default_search", "media_control", "system_volume", "system_brightness", "set_dark_theme",
        "toggle_quick_setting", "explorer_current_folder", "explorer_selected_files",
        "open_service",
    }

    _VOICE_GUARDED_SIDE_EFFECTS = {
        "click", "window_click", "window_type", "type_text", "press_key", "hotkey",
        "media_control", "system_volume", "system_brightness", "system_power", "process_terminate", "browser_click_text", "browser_fill", "browser_press",
        "browser_upload", "launch_application", "open_default_url", "powershell",
        "open_service",
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
        expected = getattr(self._scope, "stop_generation", None)
        with self._stop_lock:
            invalidated = expected is not None and int(expected) != self._stop_generation
        return self.stop_event.is_set() or invalidated or bool(scoped and scoped.is_set())

    def task_scope(self, stop_event: threading.Event | None = None):
        executor = self
        class _Scope:
            def __enter__(self_inner):
                self_inner.previous = getattr(executor._scope, "stop_event", None)
                self_inner.previous_generation = getattr(executor._scope, "stop_generation", None)
                self_inner.previous_task_scoped = getattr(executor._scope, "task_scoped", False)
                executor._scope.stop_event = stop_event
                with executor._stop_lock:
                    executor._scope.stop_generation = executor._stop_generation
                executor._scope.task_scoped = True
                return executor
            def __exit__(self_inner, *_args):
                if self_inner.previous is None:
                    try:
                        del executor._scope.stop_event
                    except AttributeError:
                        pass
                else:
                    executor._scope.stop_event = self_inner.previous
                if self_inner.previous_generation is None:
                    try: del executor._scope.stop_generation
                    except AttributeError: pass
                else:
                    executor._scope.stop_generation = self_inner.previous_generation
                executor._scope.task_scoped = self_inner.previous_task_scoped
        return _Scope()

    def reset_stop(self) -> None:
        # Only clears the explicit emergency stop. Per-task cancellation tokens are
        # owned by TaskManager/ChatJobManager and are never reset globally.
        self.stop_event.clear()

    def stop(self) -> None:
        with self._stop_lock:
            # The monotonic generation permanently invalidates already-running tool loops,
            # even after reset_stop reopens the lane for the owner's next command.
            self._stop_generation += 1
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
        # UI Automation trees contain private on-screen text and previously displaced
        # real user actions from the bounded journal.  Persist a structural summary and
        # sample background observer scans; task-scoped observations remain traceable.
        logged_result = result
        if name == "window_elements":
            now = time.monotonic()
            scoped = bool(getattr(self._scope, "task_scoped", False))
            if not scoped and now - self._last_observer_log < 60.0:
                return
            self._last_observer_log = now
            rows: list[dict[str, Any]] = []
            if isinstance(result, dict) and isinstance(result.get("result"), list):
                rows = [row for row in result["result"] if isinstance(row, dict)]
            logged_result = {
                "ok": bool(result.get("ok")) if isinstance(result, dict) else bool(success),
                "element_count": len(rows),
                "surface_handles": sorted({
                    int(row.get("surface_handle") or 0)
                    for row in rows if row.get("surface_handle")
                })[:4],
            }
        self.db.log_action(name, args, logged_result, risk, success)
        extra = {"elapsed_ms": int(elapsed_ms)} if elapsed_ms is not None else {}
        log_event(
            self.settings.root_dir,
            "TOOL",
            name=name,
            args=args,
            result=logged_result,
            risk=risk,
            success=success,
            **extra,
        )

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not bool(getattr(self._scope, "task_scoped", False)):
            with self._stop_lock:
                self._scope.stop_generation = self._stop_generation
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
            "system_brightness": "medium",
            "system_power": "high",
            "click": "high",
            "type_text": "high",
            "window_list": "low",
            "window_elements": "low",
            "window_focus": "medium",
            "window_click": "high",
            "window_type": "high",
            "application_list": "low",
            "open_service": "medium",
            "launch_application": "medium",
            "mail_status": "low",
            "mail_review": "medium",
            "mail_drafts": "low",
            "mail_stage_draft": "medium",
            "organizer_note_create": "medium",
            "organizer_notes_list": "low",
            "organizer_event_create": "medium",
            "organizer_day_plan": "low",
            "reinstall_application": "high",
            "close_application": "medium",
            "close_browsers": "medium",
            "close_user_apps": "high",
            "set_dark_theme": "medium",
            "toggle_quick_setting": "high",
            "process_list": "low",
            "explorer_current_folder": "low",
            "explorer_selected_files": "low",
            "process_terminate": "high",
            "system_find": "low",
            "system_open_named": "medium",
            "system_open_path": "medium",
            "system_list_files": "low",
            "system_read_file": "low",
            "system_write_file": "high",
            "system_batch_rename": "high",
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
        if (
            RiskPolicy.requires_desktop_control(name, arguments)
            and not bool(getattr(self.settings, "enable_desktop_control", True))
        ):
            payload = {
                "ok": False,
                "error": "Управление компьютером отключено в настройках",
                "permission_required": "desktop_control",
                "completed": False,
                "verified": False,
            }
            self._log(
                name, arguments, payload, risk, False,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
            return payload
        try:
            runtime = getattr(self, "runtime_control", None)
            if runtime is not None:
                try:
                    runtime.step(
                        f"{name}: выполняю" if risk != "low" else f"{name}: наблюдаю",
                        tool=name, risk=risk,
                    )
                except Exception:
                    pass
            self._wait_voice_precommit(name)
            desktop_lock = getattr(self, "desktop_lock", None)
            if desktop_lock is not None and name in self._DESKTOP_SERIALIZED:
                with desktop_lock:
                    if self._stop_requested():
                        raise ToolError("Остановлено пользователем до действия на рабочем столе")
                    result = method(**arguments)
            else:
                result = method(**arguments)
            inner_failed = bool(
                isinstance(result, dict)
                and (
                    result.get("ok") is False
                    or ("ready" in result and result.get("ready") is False)
                    # A tool that explicitly exposes a postcondition contract must not
                    # be promoted to success when that postcondition is false. This is
                    # the central invariant behind “выполнила -> проверила -> отчиталась”.
                    or ("verified" in result and result.get("verified") is False)
                    or int(result.get("returncode", 0) or 0) != 0
                )
            )
            payload = {"ok": not inner_failed, "result": result}
            if isinstance(result, dict):
                # Preserve the tri-state contract at the executor boundary. A control
                # may have changed external state even when its postcondition could not
                # be verified; callers must know that before deciding whether retry is safe.
                for field in ("attempted", "executed", "completed", "verified"):
                    if field in result:
                        payload[field] = bool(result.get(field))
            if inner_failed:
                payload["error"] = str(result.get("error") or "Инструмент не подтвердил постусловие")
            self._log(name, arguments, payload, risk, not inner_failed, elapsed_ms=round((time.monotonic() - started) * 1000))
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

    def tool_reinstall_application(self, application: str) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис приложений недоступен")
        return dict(self.applications.reinstall(application) or {})

    def tool_foreground_window(self) -> dict[str, Any]:
        """Return the actual foreground Windows HWND/process using Win32 directly.

        pywin32 is useful elsewhere, but launch verification must not lose the PID just
        because that optional wrapper failed to import.  ctypes keeps this primitive
        available on every supported Windows Python install.
        """
        title = self._foreground_window_title()
        payload: dict[str, Any] = {"title": title or ""}
        if os.name != "nt":
            return payload
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow() or 0)
            if not hwnd:
                return payload
            payload["handle"] = hwnd
            app_user_model_id = self._window_app_user_model_id(hwnd)
            if app_user_model_id:
                payload["app_user_model_id"] = app_user_model_id

            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            if pid.value:
                payload["pid"] = int(pid.value)

            class_buf = ctypes.create_unicode_buffer(256)
            if user32.GetClassNameW(wintypes.HWND(hwnd), class_buf, len(class_buf)):
                payload["class_name"] = class_buf.value

            rect = wintypes.RECT()
            if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                payload["rectangle"] = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
        except Exception:
            # The title still provides useful evidence if a locked-down desktop denies
            # one of the richer Win32 queries.
            pass
        return payload

    @staticmethod
    def _window_app_user_model_id(hwnd: int) -> str:
        """Read System.AppUserModel.ID from a real Windows HWND property store.

        This is the shell identity Windows uses to associate windows with applications
        on the taskbar.  For packaged/hosted apps it can be stronger launch evidence
        than a process name because several apps may run behind generic host processes.
        """
        if os.name != "nt" or not int(hwnd or 0):
            return ""
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

            class _PROPVARIANT_VALUE(ctypes.Union):
                _fields_ = [
                    ("pwszVal", ctypes.c_wchar_p),
                    ("uhVal", ctypes.c_ulonglong),
                    ("raw", ctypes.c_ubyte * 16),
                ]

            class PROPVARIANT(ctypes.Structure):
                _anonymous_ = ("value",)
                _fields_ = [
                    ("vt", wintypes.WORD),
                    ("wReserved1", wintypes.WORD),
                    ("wReserved2", wintypes.WORD),
                    ("wReserved3", wintypes.WORD),
                    ("value", _PROPVARIANT_VALUE),
                ]

            ole32 = ctypes.OleDLL("ole32")
            shell32 = ctypes.OleDLL("shell32")
            clsid_from_string = ole32.CLSIDFromString
            clsid_from_string.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
            clsid_from_string.restype = ctypes.c_long

            iid = GUID()
            appid_fmtid = GUID()
            if clsid_from_string("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}", ctypes.byref(iid)) < 0:
                return ""
            if clsid_from_string("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", ctypes.byref(appid_fmtid)) < 0:
                return ""

            store = ctypes.c_void_p()
            getter = shell32.SHGetPropertyStoreForWindow
            getter.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
            getter.restype = ctypes.c_long
            hr = int(getter(wintypes.HWND(int(hwnd)), ctypes.byref(iid), ctypes.byref(store)))
            if hr < 0 or not store.value:
                return ""

            # IPropertyStore vtable: IUnknown(3), GetCount, GetAt, GetValue, SetValue, Commit.
            vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_value = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)
            )(vtable[5])
            release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
            prop = PROPERTYKEY(appid_fmtid, 5)
            value = PROPVARIANT()
            try:
                hr = int(get_value(store, ctypes.byref(prop), ctypes.byref(value)))
                # VT_LPWSTR == 31. Other variants are deliberately ignored.
                if hr >= 0 and int(value.vt) == 31 and value.pwszVal:
                    return str(value.pwszVal).strip()
                return ""
            finally:
                try:
                    ole32.PropVariantClear(ctypes.byref(value))
                except Exception:
                    pass
                try:
                    release(store)
                except Exception:
                    pass
        except Exception:
            return ""

    @staticmethod
    def _foreground_app_id_evidence(
        foreground: dict[str, Any], expected_app_id: str
    ) -> dict[str, Any]:
        actual = str(foreground.get("app_user_model_id") or "").strip()
        expected = str(expected_app_id or "").strip()
        if not actual or not expected:
            return {"verified": False, "reason": "app_user_model_id_unavailable"}
        if actual.casefold() == expected.casefold():
            return {
                "verified": True,
                "reason": "foreground_app_user_model_id_match",
                "app_user_model_id": actual,
                "window": foreground,
            }
        return {
            "verified": False,
            "reason": "foreground_app_user_model_id_mismatch",
            "app_user_model_id": actual,
            "expected_app_id": expected,
        }

    @staticmethod
    def _foreground_process_evidence(
        foreground: dict[str, Any], requested: str, resolved: str
    ) -> dict[str, Any]:
        """Correlate an existing foreground window with the requested desktop app.

        Windows commonly reuses an already-running app window.  Telegram may foreground
        a chat titled with a person's name, so caption matching alone can be a false
        negative.  PID -> executable matching is stronger evidence and works whether the
        HWND is new, reused, or was already foreground before the launch request.
        """
        pid = int(foreground.get("pid") or 0)
        if not pid:
            return {"verified": False, "reason": "foreground_pid_unavailable"}
        try:
            import psutil
            proc = psutil.Process(pid)
            process_name = str(proc.name() or "").casefold()
            try:
                executable = Path(str(proc.exe() or "")).name.casefold()
            except Exception:
                executable = ""
        except Exception as exc:
            return {"verified": False, "reason": f"foreground_process_unavailable: {exc}"}

        def key(value: str) -> str:
            value = Path(str(value or "")).name.casefold()
            if value.endswith(".exe"):
                value = value[:-4]
            return re.sub(r"[^a-zа-я0-9]+", "", value.replace("ё", "е"))

        candidates: set[str] = set()
        for query in (requested, resolved):
            try:
                candidates.update(ApplicationService._process_name_candidates(str(query or "")))
            except Exception:
                if query:
                    candidates.add(str(query))
        process_keys = {key(process_name), key(executable)} - {""}
        candidate_keys = {key(item) for item in candidates} - {""}
        matched = sorted(process_keys & candidate_keys)
        if matched:
            return {
                "verified": True,
                "reason": "foreground_process_match",
                "pid": pid,
                "process_name": process_name or executable,
                "match": matched[0],
                "window": foreground,
            }
        return {
            "verified": False,
            "reason": "foreground_process_mismatch",
            "pid": pid,
            "process_name": process_name or executable,
            "candidate_keys": sorted(candidate_keys)[:12],
        }

    @staticmethod
    def _media_search_forms(value: str) -> set[str]:
        """Return generic Cyrillic/Latin forms for live media-session matching.

        This deliberately contains no service catalogue.  A newly released player can
        be selected from its AUMID, window title or GSMTC metadata without an EIRVEN
        update.  The transliteration only bridges names such as ``Яндекс``/``Yandex``.
        """
        clean = re.sub(r"[^a-zа-яё0-9]+", " ", str(value or "").casefold()).strip()
        if not clean:
            return set()
        translit = {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
            "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
            "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
            "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
            "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "yu", "я": "ya",
        }
        latin = "".join(translit.get(char, char) for char in clean)
        compact = clean.replace(" ", "")
        latin_compact = latin.replace(" ", "")
        return {item for item in (clean, compact, latin, latin_compact) if item}

    @staticmethod
    def _media_snapshot_session_id(row: dict[str, Any]) -> str:
        stable_seed = json.dumps(
            {
                "source": str(row.get("source") or "unknown").casefold(),
                "title": str(row.get("title") or "").casefold(),
                "artist": str(row.get("artist") or "").casefold(),
                "album": str(row.get("album") or "").casefold(),
                "subtitle": str(row.get("subtitle") or "").casefold(),
                "duration_seconds": row.get("duration_seconds"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(stable_seed.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _select_media_snapshot(
        cls,
        sessions: list[dict[str, Any]],
        *,
        target: str = "",
        session_id: str = "",
        windows: list[dict[str, Any]] | None = None,
    ) -> tuple[int | None, str, list[dict[str, Any]]]:
        """Select one session with evidence, or fail closed when browser tabs collide."""
        if not sessions:
            return None, "no_sessions", []
        requested_id = str(session_id or "").strip().casefold()
        if requested_id:
            exact = [i for i, row in enumerate(sessions) if str(row.get("session_id") or "").casefold() == requested_id]
            if len(exact) == 1:
                return exact[0], "explicit_session_id", []
            if len(exact) > 1:
                return None, "ambiguous_session_id", []
            return None, "session_id_not_found", []

        target_forms = cls._media_search_forms(target)
        if not target_forms:
            if len(sessions) == 1:
                return 0, "only_windows_media_session", []
            current = [i for i, row in enumerate(sessions) if bool(row.get("is_current"))]
            if len(current) == 1:
                return current[0], "windows_current_session", []
            return None, "no_current_session", []

        target_tokens = {
            token for form in target_forms for token in form.split() if len(token) > 1
        }
        window_rows = list(windows or [])
        ranked: list[dict[str, Any]] = []
        for index, row in enumerate(sessions):
            direct_values = [
                str(row.get(key) or "")
                for key in ("source", "title", "artist", "album", "subtitle")
            ]
            candidate_forms = set().union(*(cls._media_search_forms(value) for value in direct_values))
            candidate_tokens = {
                token for form in candidate_forms for token in form.split() if len(token) > 1
            }
            ratio = max(
                (SequenceMatcher(None, wanted, actual).ratio() for wanted in target_forms for actual in candidate_forms),
                default=0.0,
            )
            contains = any(
                wanted in actual or actual in wanted
                for wanted in target_forms for actual in candidate_forms
                if len(wanted) >= 3 and len(actual) >= 3
            )
            overlap = len(target_tokens & candidate_tokens) / max(1, len(target_tokens | candidate_tokens))
            direct_score = (0.72 if contains else 0.0) + overlap * 0.55 + ratio * 0.30

            source_forms = cls._media_search_forms(str(row.get("source") or ""))
            window_score = 0.0
            matched_window = ""
            for window in window_rows:
                title_forms = cls._media_search_forms(str(window.get("title") or ""))
                process_forms = cls._media_search_forms(str(window.get("process") or ""))
                app_forms = cls._media_search_forms(str(window.get("app_user_model_id") or ""))
                title_match = any(
                    wanted in actual or actual in wanted
                    for wanted in target_forms for actual in title_forms
                    if len(wanted) >= 3 and len(actual) >= 3
                )
                source_match = bool(source_forms & (process_forms | app_forms)) or any(
                    a in b or b in a for a in source_forms for b in (process_forms | app_forms)
                    if len(a) >= 4 and len(b) >= 4
                )
                if title_match and source_match:
                    score = 0.78 + (0.08 if window.get("foreground") else 0.0)
                    if score > window_score:
                        window_score = score
                        matched_window = str(window.get("title") or "")
            score = direct_score + window_score + (0.04 if row.get("is_current") else 0.0)
            ranked.append({
                "index": index,
                "score": round(score, 6),
                "direct_score": round(direct_score, 6),
                "window_score": round(window_score, 6),
                "matched_window": matched_window,
                "session_id": row.get("session_id"),
                "source": row.get("source"),
            })
        ranked.sort(key=lambda item: (-float(item["score"]), int(item["index"])))
        best = ranked[0]
        if float(best["score"]) < 0.42:
            return None, "target_not_matched", ranked
        source_name = str(best.get("source") or "").casefold()
        browser_source = any(mark in source_name for mark in (
            "chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi", "chromium",
        ))
        # A Win32 caption identifies a browser window, not the GSMTC tab/session inside
        # that process.  Even when only one browser session currently exists, using a
        # differently titled window as identity evidence can control an unrelated tab.
        # Browser targets therefore require direct live session metadata or an explicit
        # session id obtained from the same snapshot; otherwise the UIA path must bind the
        # exact HWND instead.
        if browser_source and float(best["direct_score"]) < 0.42:
            return None, "browser_window_not_session_identity", ranked
        if len(ranked) > 1:
            second = ranked[1]
            # Browser tabs commonly expose the same browser AUMID. A matching window
            # title cannot identify which of two same-source GSMTC sessions belongs to
            # that tab, so choosing either would again control a random video.
            same_source = str(best.get("source") or "").casefold() == str(second.get("source") or "").casefold()
            close = float(best["score"]) - float(second["score"]) < 0.12
            no_direct_identity = float(best["direct_score"]) < 0.42
            if same_source and close and no_direct_identity:
                return None, "ambiguous_same_application_sessions", ranked
            if close and float(second["score"]) >= 0.52:
                return None, "ambiguous_target", ranked
        reason = "session_metadata_match" if float(best["direct_score"]) >= 0.42 else "named_window_match"
        return int(best["index"]), reason, ranked

    @staticmethod
    def _media_window_evidence() -> list[dict[str, Any]]:
        """Enumerate visible Win32 captions cheaply, without walking UIA trees."""
        if os.name != "nt":
            return []
        rows: list[dict[str, Any]] = []
        try:
            from ctypes import wintypes
            import psutil
            user32 = ctypes.windll.user32
            foreground = int(user32.GetForegroundWindow() or 0)
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def visit(hwnd, _lparam):  # noqa: ANN001
                try:
                    handle = int(hwnd or 0)
                    if not handle or not user32.IsWindowVisible(hwnd):
                        return True
                    length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                    if length <= 0:
                        return True
                    buffer = ctypes.create_unicode_buffer(min(length + 1, 1024))
                    user32.GetWindowTextW(hwnd, buffer, len(buffer))
                    title = str(buffer.value or "").strip()
                    if not title:
                        return True
                    pid = wintypes.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    process = ""
                    if pid.value:
                        try:
                            process = str(psutil.Process(int(pid.value)).name() or "")
                        except Exception:
                            pass
                    rows.append({
                        "handle": handle,
                        "title": title[:500],
                        "process": process,
                        "app_user_model_id": ToolExecutor._window_app_user_model_id(handle),
                        "foreground": handle == foreground,
                    })
                except Exception:
                    pass
                return True

            callback = callback_type(visit)
            user32.EnumWindows(callback, 0)
        except Exception:
            return []
        return rows[:160]

    def tool_media_control(
        self,
        action: str = "status",
        target: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Inspect or address one Windows GSMTC session and verify that same session.

        Mutating actions never use a global media key and never fall back to the first
        session.  A named service is resolved from live AUMID/metadata/window evidence;
        ambiguous same-browser sessions fail closed so a YouTube tab cannot be toggled
        while the owner asked for another player.
        """
        action = str(action or "status").strip().lower().replace("-", "_")
        aliases = {
            "resume": "play", "playing": "play", "paused": "pause", "toggle": "play_pause",
            "playpause": "play_pause", "nexttrack": "next", "prev": "previous",
            "previoustrack": "previous", "sessions": "list", "inspect": "status",
        }
        action = aliases.get(action, action)
        allowed = {"status", "list", "play", "pause", "play_pause", "next", "previous", "stop"}
        if action not in allowed:
            raise ToolError("media_control: action должен быть status|list|play|pause|play_pause|next|previous|stop")
        if os.name != "nt":
            raise ToolError("Windows media sessions доступны только в Windows")

        windows = self._media_window_evidence() if target else []
        operation_state: dict[str, Any] = {
            "attempted": False,
            "executed": False,
            "selection": "",
            "session_id": str(session_id or ""),
        }

        async def operate() -> dict[str, Any]:
            import asyncio
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

            manager = await Manager.request_async()
            native_sessions = list(manager.get_sessions())
            current = manager.get_current_session()

            async def read_one(session: Any, index: int, is_current: bool) -> dict[str, Any]:
                state = "unknown"
                controls: dict[str, bool] = {}
                try:
                    playback = session.get_playback_info()
                    state = {
                        0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                        4: "playing", 5: "paused",
                    }.get(int(playback.playback_status), str(int(playback.playback_status)))
                    native_controls = playback.controls
                    controls = {
                        "play": bool(native_controls.is_play_enabled),
                        "pause": bool(native_controls.is_pause_enabled),
                        "toggle": bool(native_controls.is_play_pause_toggle_enabled),
                        "next": bool(native_controls.is_next_enabled),
                        "previous": bool(native_controls.is_previous_enabled),
                        "stop": bool(native_controls.is_stop_enabled),
                    }
                except Exception:
                    pass
                media: Any = None
                try:
                    media = await session.try_get_media_properties_async()
                except Exception:
                    media = None
                timeline: Any = None
                try:
                    timeline = session.get_timeline_properties()
                except Exception:
                    timeline = None
                def seconds(value: Any) -> float | None:
                    try:
                        return round(float(value.total_seconds()), 3)
                    except Exception:
                        return None
                return {
                    "index": index,
                    "available": True,
                    "is_current": bool(is_current),
                    "state": state,
                    "source": str(getattr(session, "source_app_user_model_id", "") or ""),
                    "title": str(getattr(media, "title", "") or ""),
                    "artist": str(getattr(media, "artist", "") or ""),
                    "album": str(getattr(media, "album_title", "") or ""),
                    "subtitle": str(getattr(media, "subtitle", "") or ""),
                    "track_number": int(getattr(media, "track_number", 0) or 0),
                    "position_seconds": seconds(getattr(timeline, "position", None)),
                    "duration_seconds": seconds(getattr(timeline, "end_time", None)),
                    "controls": controls,
                }

            raw: list[dict[str, Any]] = []
            for index, native in enumerate(native_sessions):
                is_current = native is current
                if not is_current and current is not None:
                    try:
                        is_current = bool(native == current)
                    except Exception:
                        pass
                raw.append(await read_one(native, index, is_current))
            for row in raw:
                # Order is not identity: WinRT may enumerate two browser sessions in a
                # different order between list and action.  Hash the observable session
                # fingerprint instead.  Truly indistinguishable duplicates intentionally
                # receive the same id, making an explicit selection ambiguous/fail-closed
                # rather than pretending occurrence #1 is stable.
                row["session_id"] = self._media_snapshot_session_id(row)

            selected_index, selection_reason, ranking = self._select_media_snapshot(
                raw, target=target, session_id=session_id, windows=windows,
            )
            public_sessions = [{key: value for key, value in row.items() if key != "index"} for row in raw]
            if action == "list":
                return {
                    "action": action, "available": bool(raw), "sessions": public_sessions,
                    "count": len(raw), "verified": True,
                }
            if selected_index is None:
                if action == "status" and not (target or session_id):
                    return {
                        "action": action, "available": bool(raw), "selected": None,
                        "sessions": public_sessions, "count": len(raw), "selection": selection_reason,
                        "verified": True,
                    }
                return {
                    "action": action, "target": target, "available": bool(raw),
                    "sessions": public_sessions, "selection": selection_reason,
                    "ranking": ranking[:6], "executed": False, "completed": False,
                    "verified": False,
                    "error": "Не выбрала медиасессию: совпадение отсутствует или неоднозначно",
                }

            native = native_sessions[selected_index]
            before = raw[selected_index]
            before_public = {key: value for key, value in before.items() if key != "index"}
            operation_state["selection"] = selection_reason
            operation_state["session_id"] = str(before.get("session_id") or "")
            if action == "status":
                return {
                    "action": action, "available": True, "selected": before_public,
                    "sessions": public_sessions, "count": len(raw), "selection": selection_reason,
                    "verified": True,
                }

            if action == "play" and before.get("state") == "playing":
                return {"action": action, "target": target, "sent": False, "executed": False, "completed": True, "verified": True, "selection": selection_reason, "before": before_public, "after": before_public}
            if action == "pause" and before.get("state") in {"paused", "stopped"}:
                return {"action": action, "target": target, "sent": False, "executed": False, "completed": True, "verified": True, "selection": selection_reason, "before": before_public, "after": before_public}
            if action == "stop" and before.get("state") == "stopped":
                return {"action": action, "target": target, "sent": False, "executed": False, "completed": True, "verified": True, "selection": selection_reason, "before": before_public, "after": before_public}

            effective_action = action
            if action == "play_pause":
                effective_action = "pause" if before.get("state") == "playing" else "play"
            controls = dict(before.get("controls") or {})
            control_key = effective_action
            use_native_toggle = bool(action == "play_pause" and controls.get("toggle", False))
            if not use_native_toggle and not controls.get(control_key, False):
                return {
                    "action": action, "effective_action": effective_action, "target": target,
                    "selection": selection_reason, "before": before_public,
                    "executed": False, "completed": False, "verified": False,
                    "error": f"Выбранная сессия не разрешает действие {effective_action}",
                }
            method = (
                native.try_toggle_play_pause_async
                if use_native_toggle else {
                    "play": native.try_play_async,
                    "pause": native.try_pause_async,
                    "next": native.try_skip_next_async,
                    "previous": native.try_skip_previous_async,
                    "stop": native.try_stop_async,
                }[effective_action]
            )
            operation_state["attempted"] = True
            accepted = bool(await method())
            if not accepted:
                return {
                    "action": action, "effective_action": effective_action, "target": target,
                    "selection": selection_reason, "before": before_public, "sent": True,
                    "attempted": True, "executed": False, "completed": False, "verified": False,
                    "error": "Windows media session отклонила адресную команду",
                }
            operation_state["executed"] = True

            def track_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
                return tuple(row.get(key) for key in ("title", "artist", "album", "subtitle", "track_number"))

            after = await read_one(native, selected_index, bool(before.get("is_current")))
            deadline = time.monotonic() + 2.4
            verified = False
            while True:
                state = str(after.get("state") or "")
                if effective_action == "play":
                    verified = state == "playing"
                elif effective_action == "pause":
                    verified = state in {"paused", "stopped"}
                elif effective_action == "stop":
                    verified = state == "stopped"
                else:
                    before_track = track_fingerprint(before)
                    after_track = track_fingerprint(after)
                    metadata_changed = bool(any(before_track) and any(after_track) and before_track != after_track)
                    before_position = before.get("position_seconds")
                    after_position = after.get("position_seconds")
                    position_reset = bool(
                        isinstance(before_position, (int, float))
                        and isinstance(after_position, (int, float))
                        and float(before_position) >= 2.0
                        and float(after_position) + 1.0 < float(before_position)
                    )
                    verified = metadata_changed or position_reset
                if verified or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.12)
                after = await read_one(native, selected_index, bool(before.get("is_current")))
            after["session_id"] = before.get("session_id")
            after_public = {key: value for key, value in after.items() if key != "index"}
            return {
                "action": action, "effective_action": effective_action, "target": target,
                "session_id": before.get("session_id"), "selection": selection_reason,
                "sent": True, "attempted": True, "executed": True,
                "completed": bool(verified), "verified": bool(verified),
                "before": before_public, "after": after_public,
                "error": "Адресная команда принята, но изменение этой же сессии не подтвердилось" if not verified else "",
            }

        try:
            import asyncio
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(operate())
            holder: dict[str, Any] = {}
            failure: list[BaseException] = []
            def runner() -> None:
                try:
                    holder["result"] = asyncio.run(operate())
                except BaseException as exc:  # pragma: no cover - defensive event-loop bridge
                    failure.append(exc)
            thread = threading.Thread(target=runner, daemon=True, name="eirven-media-gsmtc")
            thread.start(); thread.join(timeout=8.0)
            if thread.is_alive():
                return {
                    "action": action,
                    "target": target,
                    "session_id": operation_state.get("session_id") or session_id,
                    "selection": "timeout_uncertain",
                    "selection_before_timeout": operation_state.get("selection") or "",
                    "attempted": bool(operation_state.get("attempted")),
                    "executed": bool(operation_state.get("executed")),
                    "completed": False,
                    "verified": False,
                    "error": (
                        "Windows media session не завершила проверку за 8 секунд; "
                        "команду автоматически не повторять"
                    ),
                }
            if failure:
                raise failure[0]
            return dict(holder.get("result") or {})
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Не удалось прочитать Windows media sessions: {exc}") from exc

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

    def tool_system_brightness(self, level: int) -> dict[str, Any]:
        """Set and read back the current Windows monitor brightness."""
        if os.name != "nt":
            raise ToolError("Системная яркость доступна только в Windows")
        target = max(0, min(int(level), 100))
        script = (
            "$ErrorActionPreference='Stop'; "
            f"$target={target}; "
            "$items=@(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods); "
            "if(-not $items){throw 'Windows не предоставила управление яркостью монитора'}; "
            "$items | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness -Arguments @{Brightness=$target;Timeout=0} | Out-Null }; "
            "$state=@(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -ExpandProperty CurrentBrightness); "
            "[pscustomobject]@{requested=$target;current=([int]($state | Select-Object -First 1))} | ConvertTo-Json -Compress"
        )
        result = self.tool_powershell(script, cwd=str(self.settings.root_dir), timeout=20)
        inner = result if isinstance(result, dict) else {}
        raw = str(inner.get("stdout") or "").strip()
        observed = None
        try:
            observed = json.loads(raw.splitlines()[-1]).get("current") if raw else None
        except (ValueError, TypeError, json.JSONDecodeError):
            observed = None
        verified = bool(int(inner.get("returncode", 1) or 1) == 0 and observed == target)
        return {"requested": target, "observed": observed, "verified": verified, "scope": "system"}

    def tool_system_power(self, action: str = "shutdown", delay_seconds: int = 15) -> dict[str, Any]:
        """Schedule/cancel Windows shutdown after ChatService's explicit confirmation.

        This primitive is intentionally absent from the model-facing tool catalogue: only
        the deterministic confirmation state machine may invoke it.
        """
        if os.name != "nt":
            raise ToolError("Управление питанием доступно только в Windows")
        if not self.settings.enable_desktop_control or not self.settings.enable_commands:
            raise ToolError("Управление компьютером отключено")
        normalized = str(action or "shutdown").strip().casefold()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if normalized in {"cancel", "abort", "отмена"}:
            completed = subprocess.run(
                ["shutdown.exe", "/a"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=8, creationflags=flags,
            )
            if completed.returncode != 0:
                raise ToolError((completed.stderr or completed.stdout or "Нет запланированного выключения").strip())
            return {"action": "cancel", "cancelled": True, "verified": True}
        if normalized != "shutdown":
            raise ToolError("system_power: action должен быть shutdown|cancel")
        delay = max(10, min(int(delay_seconds or 15), 300))
        completed = subprocess.run(
            ["shutdown.exe", "/s", "/t", str(delay), "/c", "EIRVEN: подтверждённое выключение компьютера"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=flags,
        )
        if completed.returncode != 0:
            raise ToolError((completed.stderr or completed.stdout or "Windows не приняла команду выключения").strip())
        return {"action": "shutdown", "scheduled": True, "delay_seconds": delay, "verified": True}

    def tool_window_wait(
        self, title_contains: str = "", element_text: str = "", automation_id: str = "",
        handle: int | None = None, timeout: float = 8.0
    ) -> dict[str, Any]:
        """Wait until a window/element becomes available without burning LLM turns."""
        deadline = time.monotonic() + max(0.2, min(float(timeout or 8.0), 45.0))
        last_error = ""
        while time.monotonic() < deadline:
            if self._stop_requested():
                raise ToolError("Ожидание окна остановлено пользователем")
            try:
                win = self._find_window(title_contains, handle) if (title_contains or handle) else None
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

    def tool_explorer_selected_files(self) -> dict[str, Any]:
        """Return an unambiguous File Explorer selection.

        A typed/voice command often moves focus away from Explorer before this tool is
        called.  Prefer the foreground Explorer when there is one; otherwise return the
        selections from every Explorer window instead of silently taking the first stale
        window.  MissionEngine will ask the owner when that produces more than one file.
        """
        if os.name != "nt":
            raise ToolError("Выделение Проводника доступно только в Windows")
        script = r"""
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EirvenSelectionWin32 { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); }
"@ -ErrorAction SilentlyContinue
$fg = 0
try { $fg = [int64][EirvenSelectionWin32]::GetForegroundWindow() } catch {}
$shell = New-Object -ComObject Shell.Application
$windows = @($shell.Windows())
$picks = @()
foreach ($w in $windows) {
  try {
    if ([int64]$w.HWND -eq $fg -and $w.Document.Folder) { $picks = @($w); break }
  } catch {}
}
if ($picks.Count -eq 0) {
  foreach ($w in $windows) {
    try { if ($w.Document.SelectedItems().Count -gt 0) { $picks += $w } } catch {}
  }
}
$items = @()
$sourceHwnds = @()
foreach ($pick in $picks) {
  try {
    $pickHwnd = [int64]$pick.HWND
    $sourceHwnds += $pickHwnd
    $selected = $pick.Document.SelectedItems()
    for ($i = 0; $i -lt $selected.Count; $i++) {
      try {
        $item = $selected.Item($i)
        $path = [string]$item.Path
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        $isFolder = [bool]$item.IsFolder
        $size = 0
        if (-not $isFolder -and [System.IO.File]::Exists($path)) { $size = [int64](Get-Item -LiteralPath $path).Length }
        $items += [pscustomobject]@{ path=$path; name=[string]$item.Name; is_folder=$isFolder; size=$size; source_hwnd=$pickHwnd }
      } catch {}
    }
  } catch {}
}
$hwnd = 0
if ($sourceHwnds.Count -eq 1) {
  $hwnd = [int64]$sourceHwnds[0]
}
[pscustomobject]@{ hwnd=$hwnd; source_hwnds=@($sourceHwnds); files=@($items); verified=$true } | ConvertTo-Json -Depth 4 -Compress
"""
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise ToolError((completed.stderr or completed.stdout or "Explorer selection COM failed").strip())
            lines = (completed.stdout or "").strip().splitlines()
            data = json.loads(lines[-1]) if lines else {"files": [], "hwnd": 0, "verified": True}
            rows = data.get("files") or []
            if isinstance(rows, dict):
                rows = [rows]
            files = []
            for row in rows if isinstance(rows, list) else []:
                path = str((row or {}).get("path") or "").strip()
                if not path:
                    continue
                files.append({
                    "path": path, "name": str((row or {}).get("name") or Path(path).name),
                    "is_folder": bool((row or {}).get("is_folder")),
                    "size": int((row or {}).get("size") or 0),
                    "source_hwnd": int((row or {}).get("source_hwnd") or 0),
                })
            source_hwnds = data.get("source_hwnds") or []
            if isinstance(source_hwnds, (int, str)):
                source_hwnds = [source_hwnds]
            return {
                "hwnd": int(data.get("hwnd") or 0), "files": files, "verified": True,
                "source_hwnds": [int(value) for value in source_hwnds if str(value).strip()],
            }
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Не удалось прочитать выделенные файлы Проводника: {exc}") from exc

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
        cognition = getattr(self, "cognition", None)
        if cognition is not None:
            try:
                cognition.capture_file(target, label=f"Изменение {target.name}")
            except Exception:
                # An unavailable checkpoint must not corrupt the requested write. The
                # write itself remains guarded and its action log is still preserved.
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        encoded = content.encode("utf-8")
        readback = target.read_bytes()
        verified = bool(target.is_file() and readback == encoded)
        return {
            "path": str(target),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(readback).hexdigest(),
            "readback_verified": verified,
            "verified": verified,
        }

    def tool_system_batch_rename(
        self,
        directory: str,
        template: str = "file_{n}",
        glob: str = "*",
        start: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Rename a batch deterministically and verify every postcondition.

        ``template`` may contain {n}, {stem}, {ext}. Extension is preserved when the
        template does not contain {ext}. A two-phase temporary rename prevents collisions.
        """
        if not self.settings.enable_desktop_control:
            raise ToolError("Доступ к компьютеру отключён")
        root = self._user_path(directory, must_exist=True)
        if not root.is_dir():
            raise ToolError(f"Не папка: {root}")
        pattern = str(glob or "*").strip() or "*"
        files = [p for p in sorted(root.glob(pattern), key=lambda x: x.name.casefold()) if p.is_file()]
        if not files:
            return {"matched": 0, "renamed": 0, "verified": True, "verification": "Подходящих файлов нет", "mapping": []}
        if len(files) > 5000:
            raise ToolError("За один шаг можно переименовать не больше 5000 файлов")
        number = int(start or 1)
        mapping: list[tuple[Path, Path]] = []
        target_keys: set[str] = set()
        for offset, source in enumerate(files):
            n = number + offset
            try:
                rendered = str(template or "file_{n}").format(n=n, stem=source.stem, ext=source.suffix.lstrip("."))
            except Exception as exc:
                raise ToolError(f"Ошибка шаблона имени: {exc}") from exc
            rendered = rendered.strip().rstrip(". ")
            if not rendered or any(ch in rendered for ch in '<>:"/\\|?*'):
                raise ToolError(f"Недопустимое имя: {rendered!r}")
            if "{ext}" not in str(template) and source.suffix and not rendered.casefold().endswith(source.suffix.casefold()):
                rendered += source.suffix
            target = source.with_name(rendered)
            key = str(target).casefold()
            if key in target_keys:
                raise ToolError(f"Шаблон создаёт одинаковые имена: {target.name}")
            target_keys.add(key)
            if target.exists() and target.resolve() != source.resolve() and target not in files:
                raise ToolError(f"Целевой файл уже существует: {target.name}")
            mapping.append((source, target))
        preview = [{"from": a.name, "to": b.name} for a,b in mapping[:30]]
        if dry_run:
            return {"matched": len(mapping), "renamed": 0, "verified": True, "dry_run": True, "mapping": preview, "verification": "План проверен, файловая система не изменялась"}
        # Phase 1: move all changing sources to unique temporary names.
        temp_rows: list[tuple[Path, Path, Path]] = []
        try:
            for idx, (source, target) in enumerate(mapping):
                if source.name == target.name:
                    temp_rows.append((source, source, target)); continue
                temp = source.with_name(f".eirven-rename-{os.getpid()}-{threading.get_ident()}-{idx}{source.suffix}")
                while temp.exists():
                    temp = source.with_name(f".eirven-rename-{os.getpid()}-{threading.get_ident()}-{idx}-{time.time_ns()}{source.suffix}")
                source.rename(temp)
                temp_rows.append((source, temp, target))
            for original, temp, target in temp_rows:
                if temp == original and original == target:
                    continue
                temp.rename(target)
        except Exception as exc:
            # Best-effort rollback: restore any temp/final target to its original path.
            for original, temp, target in reversed(temp_rows):
                try:
                    current = target if target.exists() else temp
                    if current.exists() and not original.exists():
                        current.rename(original)
                except Exception:
                    pass
            raise ToolError(f"Переименование остановлено и откатано насколько возможно: {exc}") from exc
        failures=[]
        for original, target in mapping:
            if not target.is_file() or (original != target and original.exists()):
                failures.append({"from": original.name, "to": target.name, "target_exists": target.exists(), "source_left": original.exists()})
        verified = not failures
        return {
            "matched": len(mapping), "renamed": len(mapping) - len(failures),
            "verified": verified, "failures": failures[:50], "mapping": preview,
            "verification": (f"Повторно проверены все {len(mapping)} целевых путей; старые имена отсутствуют" if verified else f"Не подтверждено файлов: {len(failures)}"),
        }

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
            return [{"path": self.guard.display(target), "type": "file", "size": target.stat().st_size}]
        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max_entries]:
            entries.append(
                {
                    "path": self.guard.display(item),
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
            "path": self.guard.display(target),
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
        cognition = getattr(self, "cognition", None)
        if cognition is not None:
            try:
                cognition.capture_file(target, label=f"Изменение {target.name}")
            except Exception:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        readback = target.read_bytes()
        verified = bool(target.is_file() and readback == encoded)
        return {
            "path": self.guard.display(target),
            "absolute_path": str(target),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(readback).hexdigest(),
            "readback_verified": verified,
            "verified": verified,
        }

    def tool_make_directory(self, path: str) -> dict[str, Any]:
        target = self.guard.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": self.guard.display(target)}

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
            "cwd": self.guard.display(workdir),
            "returncode": process.returncode,
            "stdout": stdout[-30_000:],
            "stderr": stderr[-30_000:],
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    def tool_application_list(self, query: str = "", limit: int = 60, refresh: bool = False) -> dict[str, Any]:
        """Return the real Start-menu application index for semantic app discovery.

        This intentionally contains no product aliases. The reasoning layer can inspect
        whatever is actually installed (including renamed/localised apps) and choose the
        best candidate instead of relying on a hard-coded list such as Telegram/Spotify.
        """
        if not self.applications:
            raise ToolError("Сервис приложений не настроен")
        rows = list(self.applications.list_installed(refresh=bool(refresh)) or [])
        q = re.sub(r"\s+", " ", str(query or "").casefold().replace("ё", "е")).strip()
        best_score = 0.0
        if q:
            q_tokens = {x for x in re.split(r"[^a-zа-я0-9]+", q) if len(x) > 1}
            ranked = []
            for row in rows:
                name = str(row.get("name") or "")
                n = re.sub(r"\s+", " ", name.casefold().replace("ё", "е")).strip()
                n_tokens = {x for x in re.split(r"[^a-zа-я0-9]+", n) if len(x) > 1}
                ratio = SequenceMatcher(None, q, n).ratio()
                overlap = len(q_tokens & n_tokens) / max(1, len(q_tokens | n_tokens))
                contains = 1.0 if (q in n or n in q) else 0.0
                ranked.append((ratio * .55 + overlap * .30 + contains * .15, row))
            ranked.sort(key=lambda item: (-item[0], str(item[1].get("name") or "").casefold()))
            if ranked:
                best_score = float(ranked[0][0])
            rows = [row for _score, row in ranked]
        return {
            "query": query,
            "count": len(rows),
            "best_score": round(best_score, 3) if q else None,
            "applications": rows[:max(1, min(int(limit), 200))],
        }

    def tool_mail_status(self) -> dict[str, Any]:
        service = getattr(self, "mail_service", None)
        if service is None:
            raise ToolError("Почтовый сервис не настроен")
        return dict(service.public_status() or {})

    def tool_mail_review(self, limit: int = 40, prepare_replies: bool = True, move_spam: bool | None = None) -> dict[str, Any]:
        """Review the owner's configured mailbox with server-side verification.

        Moving mail is intentionally limited by MailService itself to messages with a
        high-confidence spam signal; arbitrary deletion is not exposed as a model tool.
        """
        service = getattr(self, "mail_service", None)
        if service is None:
            raise ToolError("Почтовый сервис не настроен")
        if not service.configured():
            raise ToolError("Почта не подключена в настройках EIRVEN")
        return dict(service.review(limit=max(1, min(int(limit), 100)), prepare_replies=bool(prepare_replies), move_spam=move_spam) or {})

    def tool_mail_drafts(self) -> dict[str, Any]:
        service = getattr(self, "mail_service", None)
        if service is None:
            raise ToolError("Почтовый сервис не настроен")
        rows = list(service.drafts() or [])
        return {"count": len(rows), "drafts": rows}

    def tool_mail_stage_draft(self, index: int) -> dict[str, Any]:
        service = getattr(self, "mail_service", None)
        if service is None:
            raise ToolError("Почтовый сервис не настроен")
        # Staging is reversible. Actual SMTP send remains outside the autonomous tool
        # set and requires the owner's explicit confirmation in ChatService.
        return dict(service.stage_draft(int(index)) or {})

    def _phone_organizer(self) -> Any:
        service = getattr(self, "phone_sync_service", None)
        if service is None:
            raise ToolError("Личный органайзер недоступен")
        return service

    def tool_organizer_note_create(self, text: str) -> dict[str, Any]:
        service = self._phone_organizer()
        note = service.add_note(str(text or ""))
        verified = any(
            int(row.get("id") or 0) == int(note.get("id") or -1)
            and str(row.get("text") or "") == str(note.get("text") or "")
            for row in service.recent_notes(30)
        )
        return {
            "completed": True,
            "verified": verified,
            "note": note,
            "paired_devices": len(service.devices()),
        }

    def tool_organizer_notes_list(self, limit: int = 20) -> dict[str, Any]:
        rows = self._phone_organizer().recent_notes(
            max(1, min(int(limit), 100))
        )
        return {"verified": True, "count": len(rows), "notes": rows}

    def tool_organizer_day_plan(self) -> dict[str, Any]:
        value = self._phone_organizer().today()
        return {"verified": True, **value}

    def tool_organizer_event_create(
        self,
        title: str,
        starts_at: str,
        ends_at: str = "",
        reminders: list[Any] | None = None,
    ) -> dict[str, Any]:
        service = self._phone_organizer()
        try:
            start = datetime.fromisoformat(str(starts_at or ""))
            if start.tzinfo is None:
                start = start.astimezone()
            end = (
                datetime.fromisoformat(str(ends_at))
                if str(ends_at or "").strip()
                else start + timedelta(hours=1)
            )
            if end.tzinfo is None:
                end = end.astimezone()
        except (TypeError, ValueError) as exc:
            raise ToolError("Для события нужна точная дата и время в ISO-8601") from exc
        reminder_values = [120, 60, 30, 10]
        if reminders is not None:
            try:
                reminder_values = [int(value) for value in reminders]
            except (TypeError, ValueError) as exc:
                raise ToolError("Некорректные интервалы напоминаний") from exc
        event = service.add_event(
            str(title or ""), start, end, reminders=reminder_values
        )
        with self.db.connect() as conn:
            stored = conn.execute(
                "SELECT id,title,starts_at,ends_at FROM phone_events WHERE id=?",
                (str(event.get("id") or ""),),
            ).fetchone()
        verified = bool(stored and str(stored["title"]) == str(event.get("title") or ""))
        return {
            "completed": True,
            "verified": verified,
            "event": event,
            "paired_devices": len(service.devices()),
        }

    @staticmethod
    def _launch_window_evidence(
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
        before_fg: dict[str, Any],
        after_fg: dict[str, Any],
        requested: str,
        resolved: str,
    ) -> dict[str, Any]:
        """Verify an app launch from live window state, not a guessed title.

        Start-menu names, process names and actual window captions routinely differ
        (Store apps, renamed brands, documents in title bars, localized clients).  A
        fixed ``wait_window(app.name)`` therefore produces false negatives.  Evidence
        is accepted when a semantically matching window appears/activates, or when a
        single new foreground user window is created by the launch.
        """
        def norm(value: Any) -> str:
            value = str(value or "").casefold().replace("ё", "е")
            return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()

        def user_window(row: dict[str, Any]) -> bool:
            title = norm(row.get("title")); cls = norm(row.get("class_name"))
            if not title:
                return False
            if any(x in cls for x in ("shell traywnd", "progman", "workerw")):
                return False
            if any(x in title for x in ("eirven ai", "диспетчер программ")):
                return False
            return True

        before_handles = {int(r.get("handle") or 0) for r in before if int(r.get("handle") or 0)}
        fg_before = int(before_fg.get("handle") or 0)
        fg_after = int(after_fg.get("handle") or 0)
        targets = [norm(requested), norm(resolved)]
        targets = [x for x in targets if x]

        scored: list[tuple[float, dict[str, Any]]] = []
        new_rows: list[dict[str, Any]] = []
        for row in after:
            if not user_window(row):
                continue
            handle = int(row.get("handle") or 0)
            if handle and handle not in before_handles:
                new_rows.append(row)
            title = norm(row.get("title"))
            title_tokens = {x for x in title.split() if len(x) > 1}
            best = 0.0
            for target in targets:
                tt = {x for x in target.split() if len(x) > 1}
                ratio = SequenceMatcher(None, target, title).ratio() if target and title else 0.0
                overlap = len(tt & title_tokens) / max(1, len(tt | title_tokens))
                contains = 1.0 if (target in title or title in target) else 0.0
                best = max(best, ratio * .55 + overlap * .30 + contains * .25)
            scored.append((best, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_row = scored[0] if scored else (0.0, {})
        best_handle = int(best_row.get("handle") or 0)

        # Strong semantic caption evidence, either newly created or brought forward.
        if best_score >= .58 and (best_handle not in before_handles or best_handle == fg_after):
            return {
                "ready": True, "verified": True, "reason": "matching_window",
                "score": round(best_score, 3), "window": best_row,
            }
        # A single new window becoming foreground is strong causal evidence even when
        # its caption is a document/account name rather than the Start-menu product name.
        if fg_after and fg_after != fg_before:
            foreground_row = next((r for r in new_rows if int(r.get("handle") or 0) == fg_after), None)
            if foreground_row is not None and len(new_rows) == 1:
                return {
                    "ready": True, "verified": True, "reason": "single_new_foreground_window",
                    "score": round(best_score, 3), "window": foreground_row,
                }
        return {
            "ready": False, "verified": False, "reason": "no_confirmed_window_transition",
            "score": round(best_score, 3), "new_windows": new_rows[:8],
            "foreground_before": before_fg, "foreground_after": after_fg,
        }

    def tool_launch_application(self, application: str) -> dict[str, Any]:
        if not self.applications:
            raise ToolError("Сервис запуска приложений не настроен")
        before: list[dict[str, Any]] = []
        before_fg: dict[str, Any] = {}
        if os.name == "nt":
            try: before = self.tool_window_list(max_windows=120)
            except Exception: before = []
            try: before_fg = self.tool_foreground_window()
            except Exception: before_fg = {}

        result = dict(self.applications.launch(application) or {})
        title = str(result.get("name") or application).strip()
        if os.name != "nt":
            # Linux is used only by the real-GUI CI lab; production verification is the
            # Windows branch below. Do not pretend a non-Windows launch was UIA-verified.
            result["observation"] = {"ready": False, "verified": False, "reason": "windows_verifier_not_available"}
            result["verified"] = False
            return result

        deadline = time.monotonic() + 22.0
        observation: dict[str, Any] = {"ready": False, "verified": False, "reason": "timeout"}
        while time.monotonic() < deadline:
            if self._stop_requested():
                raise ToolError("Запуск приложения остановлен пользователем")
            try:
                after = self.tool_window_list(max_windows=120)
                after_fg = self.tool_foreground_window()
                observation = self._launch_window_evidence(before, after, before_fg, after_fg, application, title)
                if observation.get("verified") is not True:
                    app_id_evidence = self._foreground_app_id_evidence(after_fg, str(result.get("app_id") or ""))
                    if app_id_evidence.get("verified") is True:
                        observation = {"ready": True, **app_id_evidence}
                if observation.get("verified") is not True:
                    process_evidence = self._foreground_process_evidence(after_fg, application, title)
                    if process_evidence.get("verified") is True:
                        observation = {"ready": True, **process_evidence}
                if observation.get("verified") is True:
                    break
            except Exception as exc:
                observation = {"ready": False, "verified": False, "reason": f"observation_error: {exc}"}
            time.sleep(.18)
        result["observation"] = observation
        result["verified"] = bool(observation.get("verified") is True)
        return result

    def tool_open_service(self, service: str, purpose: str = "") -> dict[str, Any]:
        """Resolve an arbitrary named app/service through the live capability adapter.

        Known web apps may have an authoritative URL accelerator; unknown names are
        resolved against installed applications and then the likely official web app.
        The reactive engine invokes this like any other capability and verifies the
        resulting surface instead of routing the whole user utterance through AppSkills.
        """
        opener = getattr(self, "service_opener", None)
        if opener is None:
            raise ToolError("Резолвер приложений и сервисов не настроен")
        result = dict(opener.open(
            str(service or "").strip(),
            str(purpose or "").strip(),
        ) or {})
        result.setdefault("completed", bool(result.get("ok")))
        return result

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
        self._desktop()
        if not send_virtual_keys(("win", "a")):
            raise ToolError("Windows не подтвердил открытие быстрых настроек")
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
            send_virtual_keys(("esc",))
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
        send_virtual_keys(("esc",))
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
        """Open a user file/folder and verify the visible postcondition on Windows."""
        target = self._user_path(path, must_exist=True)
        kind = "directory" if target.is_dir() else "file"
        before: list[dict[str, Any]] = []
        before_fg: dict[str, Any] = {}
        if os.name == "nt":
            try: before = self.tool_window_list(max_windows=120)
            except Exception: before = []
            try: before_fg = self.tool_foreground_window()
            except Exception: before_fg = {}
            os.startfile(str(target))  # type: ignore[attr-defined]
            deadline = time.monotonic() + (14.0 if target.is_dir() else 20.0)
            observation: dict[str, Any] = {"ready": False, "verified": False, "reason": "timeout"}
            while time.monotonic() < deadline:
                if self._stop_requested():
                    raise ToolError("Открытие остановлено пользователем")
                if target.is_dir():
                    try:
                        current = self.tool_explorer_current_folder()
                        if Path(str(current.get("path") or "")).resolve() == target.resolve():
                            observation = {"ready": True, "verified": True, "reason": "explorer_path", "window": current}
                            break
                    except Exception:
                        pass
                try:
                    after = self.tool_window_list(max_windows=120)
                    after_fg = self.tool_foreground_window()
                    names = [target.name, target.stem]
                    evidence = self._launch_window_evidence(before, after, before_fg, after_fg, names[0], names[1])
                    if evidence.get("verified") is True:
                        observation = evidence
                        break
                except Exception as exc:
                    observation = {"ready": False, "verified": False, "reason": f"observation_error: {exc}"}
                time.sleep(.18)
            return {"path": str(target), "type": kind, "observation": observation, "verified": bool(observation.get("verified") is True)}
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Non-Windows execution is a real-GUI CI aid only; never label it Windows-verified.
        return {"path": str(target), "type": kind}

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

    def tool_screenshot(self, handle: int | None = None) -> dict[str, Any]:
        """Capture one leased surface or the real desktop across all monitors."""
        if not self.settings.enable_desktop_control:
            raise ToolError("Доступ к рабочему столу отключён")
        folder = self.settings.data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"desktop-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.png"
        backend = ""
        width = height = 0
        bounds: dict[str, int] = {"left": 0, "top": 0, "width": 0, "height": 0}
        requested_bounds: dict[str, int] | None = None
        if handle and os.name == "nt":
            try:
                from ctypes import wintypes
                rect = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(handle)), ctypes.byref(rect)):
                    width_px = max(1, int(rect.right - rect.left))
                    height_px = max(1, int(rect.bottom - rect.top))
                    requested_bounds = {
                        "left": int(rect.left), "top": int(rect.top),
                        "width": width_px, "height": height_px,
                    }
            except Exception:
                requested_bounds = None
        try:
            import mss
            from PIL import Image
            with mss.mss() as capture:
                monitor = dict(requested_bounds or capture.monitors[0])
                shot = capture.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                image.save(target)
                width, height = image.size
                bounds = {key: int(monitor.get(key, 0)) for key in ("left", "top", "width", "height")}
                backend = "mss"
        except Exception as mss_error:
            try:
                bbox = None
                if requested_bounds:
                    left, top = requested_bounds["left"], requested_bounds["top"]
                    bbox = (left, top, left + requested_bounds["width"], top + requested_bounds["height"])
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
                image.save(target)
                width, height = image.size
                if requested_bounds:
                    bounds = dict(requested_bounds)
                else:
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
            "surface_handle": int(handle or 0),
            "coordinate_origin": {"x": int(bounds.get("left", 0)), "y": int(bounds.get("top", 0))},
            "foreground_window": self._foreground_window_title(),
            "cursor": cursor,
            "source": "leased_window" if requested_bounds else "real_desktop",
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
        # Models and users naturally spell a hotkey as ``ctrl,l`` or ``ctrl+l``.
        # Treat it as a chord instead of failing a multi-step browser workflow.
        chord = [part.strip() for part in re.split(r"[,+]", key) if part.strip()]
        if len(chord) >= 2:
            return self.tool_hotkey(chord)
        if os.name == "nt":
            if not send_virtual_keys((key,)):
                raise ToolError(f"Неподдерживаемая клавиша Windows: {key}")
            return {"key": key, "method": "win32_virtual_key"}
        pyautogui.press(key)
        return {"key": key, "method": "pyautogui"}

    def tool_hotkey(self, keys: list[str]) -> dict[str, Any]:
        pyautogui = self._desktop()
        safe = [str(k).strip().lower() for k in keys if str(k).strip()]
        if not 2 <= len(safe) <= 5:
            raise ToolError("Горячая клавиша должна содержать 2–5 клавиш")
        if os.name == "nt":
            if not send_virtual_keys(safe):
                raise ToolError(f"Неподдерживаемая горячая клавиша Windows: {' + '.join(safe)}")
            return {"keys": safe, "method": "win32_virtual_key"}
        pyautogui.hotkey(*safe)
        return {"keys": safe, "method": "pyautogui"}

    @staticmethod
    def _windows_send_unicode(text: str) -> bool:
        """Type arbitrary Unicode into the currently focused Windows control.

        ``pyautogui.write`` only has a finite virtual-key map and can silently drop
        Cyrillic/non-Latin characters while still returning successfully.  Win32
        ``SendInput`` with ``KEYEVENTF_UNICODE`` injects UTF-16 code units directly,
        independent of the active keyboard layout and without overwriting the owner's
        clipboard.
        """
        if os.name != "nt":
            return False
        try:
            from ctypes import wintypes

            INPUT_KEYBOARD = 1
            KEYEVENTF_KEYUP = 0x0002
            KEYEVENTF_UNICODE = 0x0004
            ULONG_PTR = wintypes.WPARAM

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR),
                ]

            # INPUT's union must include its largest native member. Declaring only
            # KEYBDINPUT makes ctypes.sizeof(INPUT) 32 bytes on 64-bit Windows, while
            # user32 expects the real 40-byte INPUT layout and rejects the whole batch.
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR),
                ]

            class HARDWAREINPUT(ctypes.Structure):
                _fields_ = [
                    ("uMsg", wintypes.DWORD),
                    ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD),
                ]

            class _INPUTUNION(ctypes.Union):
                _fields_ = [
                    ("mi", MOUSEINPUT),
                    ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT),
                ]

            class INPUT(ctypes.Structure):
                _anonymous_ = ("u",)
                _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

            raw = str(text).encode("utf-16-le")
            units = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]
            if not units:
                return True
            send_input = ctypes.windll.user32.SendInput
            send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
            send_input.restype = wintypes.UINT
            # WinUI/RichEdit can acknowledge a large atomic SendInput batch yet lose
            # characters around spaces and punctuation while its text service catches
            # up. Submit one UTF-16 unit at a time with a short settling interval. This
            # also keeps surrogate pairs ordered and makes every accepted unit explicit.
            for index, unit in enumerate(units):
                array_type = INPUT * 2
                array = array_type(
                    INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0)),
                    INPUT(
                        type=INPUT_KEYBOARD,
                        ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0),
                    ),
                )
                sent = int(send_input(2, array, ctypes.sizeof(INPUT)) or 0)
                if sent != 2:
                    return False
                if index + 1 < len(units):
                    time.sleep(0.012)
            return True
        except Exception:
            return False

    @staticmethod
    def _x11_clipboard_paste(text: str) -> bool:
        """Real-GUI test fallback for POSIX/X11. Never used by Windows releases."""
        if os.name == "nt" or not os.environ.get("DISPLAY"):
            return False
        try:
            import tkinter as tk
            import pyautogui
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(str(text))
            root.update()
            pyautogui.hotkey("ctrl", "v")
            # X11 clipboard transfer is request/response: keep servicing Tk events until
            # the focused client has fetched the UTF-8 selection.
            deadline = time.monotonic() + 0.30
            while time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            root.destroy()
            return True
        except Exception:
            return False

    def tool_type_text(self, text: str, interval: float = 0.02) -> dict[str, Any]:
        pyautogui = self._desktop()
        text = str(text)
        if len(text) > 4000:
            raise ToolError("Слишком длинный ввод")
        if not text:
            return {"chars": 0, "method": "noop", "unicode_safe": True}
        # Windows release path: always use layout-independent Unicode injection.
        # This fixes silent empty input for Russian Telegram/mail/browser composers.
        if os.name == "nt":
            if not self._windows_send_unicode(text):
                raise ToolError("Windows не подтвердил Unicode-ввод через SendInput")
            return {"chars": len(text), "method": "win32_sendinput_unicode", "unicode_safe": True}
        # The Linux branch exists only so the release engine can be exercised against
        # a real Xvfb/Openbox GUI in CI. ASCII keeps the normal pyautogui path; Unicode
        # uses an actual X11 clipboard paste because pyautogui can silently drop it.
        if all(ord(ch) < 128 for ch in text):
            pyautogui.write(text, interval=max(0.0, min(interval, 0.2)))
            return {"chars": len(text), "method": "pyautogui_ascii", "unicode_safe": True}
        if self._x11_clipboard_paste(text):
            return {"chars": len(text), "method": "x11_clipboard", "unicode_safe": True}
        raise ToolError("Не удалось выполнить Unicode-ввод")

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

    def _bounded_uia_probe(self, callback: Any, label: str, timeout: float = 4.0) -> Any:
        """Bound read-only UIA provider calls so a broken window cannot freeze EIRVEN."""
        with self._uia_probe_lock:
            previous = self._uia_probe_thread
            if previous is not None and previous.is_alive():
                raise ToolError("UI Automation временно недоступна после зависшего окна; используй screenshot")
            done = threading.Event()
            box: dict[str, Any] = {}

            def worker() -> None:
                try:
                    box["value"] = callback()
                except BaseException as exc:  # isolated read-only COM probe
                    box["error"] = exc
                finally:
                    done.set()

            thread = threading.Thread(target=worker, daemon=True, name=f"eirven-uia-{label}")
            self._uia_probe_thread = thread
            thread.start()
        if not done.wait(max(0.5, min(float(timeout), 8.0))):
            raise ToolError(f"UI Automation зависла на {label}; переключаюсь на screenshot")
        if box.get("error") is not None:
            raise ToolError(f"UI Automation {label}: {box['error']}")
        return box.get("value")

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
                        "pid": int(getattr(window.element_info, "process_id", 0) or 0),
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
        time.sleep(0.08)
        verified = False
        try:
            verified = int(ctypes.windll.user32.GetForegroundWindow() or 0) == int(window.handle)
        except Exception:
            pass
        rect = window.rectangle()
        return {
            "title": window.window_text(),
            "handle": int(window.handle),
            "pid": int(getattr(window.element_info, "process_id", 0) or 0),
            "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
            "verified": verified,
        }

    def tool_window_elements(
        self,
        title_contains: str = "",
        handle: int | None = None,
        max_elements: int = 250,
    ) -> list[dict[str, Any]]:
        window = self._find_window(title_contains, handle)
        output: list[dict[str, Any]] = []
        descendants = self._bounded_uia_probe(
            lambda: list(window.descendants()), "elements", timeout=4.0
        )
        for item in list(descendants or [])[: max(1, min(max_elements, 500))]:
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
                # A flat accessibility list loses the relationship between an
                # identically named control and its card/dialog/player.  Preserve the
                # nearest semantic ancestor so the policy can distinguish, for
                # example, the main player Play button from a row of preview buttons.
                parent_name = ""
                try:
                    parent_info = getattr(info, "parent", None)
                    for _ in range(4):
                        if parent_info is None:
                            break
                        candidate = str(getattr(parent_info, "name", "") or "").strip()
                        parent_type = str(getattr(parent_info, "control_type", "") or "").casefold()
                        if candidate and parent_type not in {"window", "pane", "document"}:
                            parent_name = candidate[:240]
                            break
                        parent_info = getattr(parent_info, "parent", None)
                except Exception:
                    parent_name = ""
                output.append(
                    {
                        "stable_id": hashlib.sha256(
                            f"{int(window.handle)}|{automation_id}|{control_type}|{name}|{rect.left},{rect.top},{rect.right},{rect.bottom}".encode("utf-8", errors="replace")
                        ).hexdigest()[:20],
                        "surface_handle": int(window.handle),
                        "name": name,
                        "control_type": control_type,
                        "automation_id": automation_id,
                        "class_name": class_name,
                        "parent_name": parent_name,
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
        stable_id: str = "",
        handle: int | None = None,
    ):
        window = self._find_window(title_contains, handle)
        if not any((element_text, control_type, automation_id, stable_id)):
            raise ToolError("Укажите текст, тип или automation_id элемента")
        wanted_text = str(element_text or "").strip().casefold()
        wanted_type = str(control_type or "").strip().casefold()
        wanted_id = str(automation_id or "").strip().casefold()
        wanted_stable = str(stable_id or "").strip().casefold()
        candidates = []
        try:
            candidates = list(self._bounded_uia_probe(
                lambda: list(window.descendants()), "find-control", timeout=4.0
            ) or [])
        except Exception as exc:
            raise ToolError(f"Не удалось прочитать элементы окна: {exc}") from exc
        exact = []
        partial = []
        for item in candidates:
            try:
                info = item.element_info
                raw_name = str(getattr(info, "name", "") or item.window_text() or "").strip()
                raw_kind = str(getattr(info, "control_type", "") or "").strip()
                raw_auto_id = str(getattr(info, "automation_id", "") or "").strip()
                name, kind, auto_id = raw_name.casefold(), raw_kind.casefold(), raw_auto_id.casefold()
                rect = item.rectangle()
                item_stable = hashlib.sha256(
                    f"{int(window.handle)}|{raw_auto_id}|{raw_kind}|{raw_name}|{rect.left},{rect.top},{rect.right},{rect.bottom}".encode("utf-8", errors="replace")
                ).hexdigest()[:20]
                if wanted_stable and item_stable != wanted_stable:
                    continue
                if wanted_type and kind != wanted_type:
                    continue
                if wanted_id and auto_id != wanted_id:
                    continue
                if wanted_text and wanted_text not in name:
                    continue
                score = int(bool(wanted_text and name == wanted_text)) + int(bool(wanted_id and auto_id == wanted_id))
                row = (score, bool(item.is_visible()), bool(item.is_enabled()), item)
                (exact if score else partial).append(row)
            except Exception:
                continue
        rows = exact or partial
        if not rows:
            raise ToolError(f"Элемент окна не найден: {element_text or automation_id or control_type}")
        rows.sort(key=lambda row: (row[1], row[2], row[0]), reverse=True)
        return window, rows[0][3]

    def tool_window_click(
        self,
        title_contains: str,
        element_text: str = "",
        control_type: str = "",
        automation_id: str = "",
        stable_id: str = "",
        handle: int | None = None,
    ) -> dict[str, Any]:
        window, control = self._find_control(
            title_contains, element_text, control_type, automation_id, stable_id, handle
        )
        window.set_focus()
        before_title = str(window.window_text() or "")
        try:
            element_label = str(control.window_text() or element_text or automation_id or stable_id)
        except Exception:
            element_label = str(element_text or automation_id or stable_id)
        try:
            before_rect = control.rectangle()
            target_rect = [before_rect.left, before_rect.top, before_rect.right, before_rect.bottom]
        except Exception:
            target_rect = []
        try:
            control.invoke()
        except Exception:
            control.click_input()
        time.sleep(0.12)
        foreground = self.tool_foreground_window()
        return {
            "window": before_title,
            "surface_handle": int(window.handle),
            "element": element_label,
            "target_rectangle": target_rect,
            "completed": True,
            "foreground_after": foreground,
        }

    def tool_window_type(
        self,
        title_contains: str,
        text: str,
        element_text: str = "",
        control_type: str = "Edit",
        automation_id: str = "",
        stable_id: str = "",
        handle: int | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        if len(text) > 10_000:
            raise ToolError("Слишком длинный ввод")
        try:
            window, control = self._find_control(
                title_contains, element_text, control_type, automation_id, stable_id, handle
            )
        except ToolError:
            # Chromium's omnibox is sometimes omitted from the UIA tree or the tab
            # changed its title between planning and execution. A URL entry is still
            # unambiguous: use the current browser window, focus the omnibox and type
            # with Unicode-safe input instead of aborting the whole chain.
            address_request = bool(re.search(r"(?:адресн|address|omnibox|строк[аи]\s+поиск)", element_text, re.I))
            if not (address_request and re.match(r"^https?://", str(text).strip(), re.I)):
                raise
            foreground = foreground_window()
            foreground_handle = int(foreground.get("handle") or 0) if isinstance(foreground, dict) else 0
            window = self._find_window("", foreground_handle) if foreground_handle else self._find_window(title_contains, handle)
            window.set_focus()
            if not send_virtual_keys(("ctrl", "l")):
                raise ToolError("Не удалось сфокусировать адресную строку")
            if not self._windows_send_unicode(str(text)):
                raise ToolError("Не удалось ввести адрес")
            if not send_virtual_keys(("enter",)):
                raise ToolError("Не удалось открыть адрес")
            return {
                "window": window.window_text(), "element": "address_bar", "chars": len(text),
                "method": "browser_omnibox_fallback", "verified": True,
            }
        window.set_focus()
        control.set_focus()

        def read_value() -> str:
            try:
                return str(control.iface_value.CurrentValue or "")
            except Exception:
                try:
                    return str(control.window_text() or "")
                except Exception:
                    return ""

        method = "uia_set_edit_text"
        attempted = False
        if replace:
            try:
                control.set_edit_text(text)
                attempted = True
            except Exception:
                attempted = False
        if not replace and text:
            # Append through Unicode-safe focused input; UIA type_keys is not reliable
            # for Cyrillic and custom Chromium controls.
            attempted = self._windows_send_unicode(text)
            method = "win32_sendinput_unicode_append"

        value = read_value()
        verified = value == text if replace else (bool(text) and value.endswith(text))
        if replace and not verified:
            # Re-focus and replace idempotently. The first UIA attempt may be accepted
            # asynchronously or may be ignored by a custom control; never report success
            # solely because no exception was raised.
            try:
                if os.name == "nt":
                    if not send_virtual_keys(("ctrl", "a")):
                        raise RuntimeError("Ctrl+A injection failed")
                    if not send_virtual_keys(("backspace",)):
                        raise RuntimeError("Backspace injection failed")
                else:
                    import pyautogui
                    pyautogui.hotkey("ctrl", "a")
                    pyautogui.press("backspace")
            except Exception:
                try:
                    control.type_keys("^a{BACKSPACE}", set_foreground=True)
                except Exception:
                    pass
            method = "win32_sendinput_unicode_replace"
            attempted = self._windows_send_unicode(text)
            time.sleep(0.08)
            value = read_value()
            verified = value == text

        # Some HTML/contenteditable controls expose no ValuePattern even after valid
        # input. In that case caller-level UI evidence (type_verified) is authoritative;
        # expose 'attempted' but do not lie that the field value was verified here.
        return {
            "ok": bool(verified),
            "window": window.window_text(),
            "surface_handle": int(window.handle),
            "element": control.window_text(),
            "chars": len(text),
            "attempted": bool(attempted),
            "verified": bool(verified),
            "completed": bool(attempted),
            "value_chars": len(value),
            "method": method,
        }

    def descriptions(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {"name": "list_files", "description": "Показать файлы/папки внутри workspace или явно указанного проекта на Рабочем столе", "arguments": {"path": "строка, по умолчанию ."}},
            {"name": "read_file", "description": "Прочитать текстовый файл внутри workspace или явно указанного проекта на Рабочем столе", "arguments": {"path": "строка"}},
            {"name": "write_file", "description": "Создать или изменить текстовый файл внутри workspace или явно указанного проекта на Рабочем столе", "arguments": {"path": "строка", "content": "полное содержимое"}},
            {"name": "make_directory", "description": "Создать папку внутри workspace или явно указанного проекта на Рабочем столе", "arguments": {"path": "строка"}},
            {"name": "run_command", "description": "Запустить одну разрешённую команду без shell внутри workspace или явно указанного проекта на Рабочем столе", "arguments": {"command": "строка", "cwd": "строка"}},
            {"name": "screenshot", "description": "Сделать свежий снимок привязанного окна (по handle) либо всего реального рабочего стола. Используй, когда UI Automation не показывает нужный элемент; vision_analysis вернёт видимые элементы и координаты.", "arguments": {"handle": "необязательный handle привязанного окна"}},
            {"name": "desktop_state", "description": "Получить состояние реального рабочего стола: полный скриншот, активное окно, курсор и уровень доступа", "arguments": {}},
            {"name": "access_status", "description": "Проверить, запущен ли EIRVEN с полным административным доступом к компьютеру", "arguments": {}},
            {"name": "application_list", "description": "Получить реальный список приложений из меню Пуск. Используй, когда пользователь называет приложение разговорно, старым названием, брендом или неточно; выбери семантически подходящее из фактически установленных, не выдумывай его.", "arguments": {"query": "необязательная фраза пользователя для ранжирования", "limit": "1..200", "refresh": "true/false"}},
            {"name": "open_service", "description": "Открыть произвольно названное приложение или веб-сервис через live resolver: сначала существующая авторизованная вкладка/установленное приложение, затем вероятный официальный веб-вариант. Не ограничен фиксированным списком сервисов.", "arguments": {"service": "название сервиса или приложения словами пользователя", "purpose": "необязательная категория из исходной задачи, например музыка или видео"}},
            {"name": "mail_status", "description": "Проверить, подключена ли личная почта владельца к EIRVEN; не читает письма", "arguments": {}},
            {"name": "mail_review", "description": "Проверить непрочитанную личную почту через IMAP, вернуть отправителя/тему/фрагмент, подготовить локальные черновики и при move_spam=true перенести только высокоуверенный спам с последующей IMAP-проверкой. Ничего не отправляет.", "arguments": {"limit": "1..100", "prepare_replies": "true/false", "move_spam": "true/false или не указывать"}},
            {"name": "mail_drafts", "description": "Показать локально подготовленные почтовые черновики; ничего не отправляет", "arguments": {}},
            {"name": "mail_stage_draft", "description": "Выбрать один черновик для последующего подтверждения владельцем. Само письмо НЕ отправляет.", "arguments": {"index": "номер черновика, начиная с 1"}},
            {"name": "organizer_note_create", "description": "Записать точный продиктованный владельцем текст в локальные заметки EIRVEN и очередь нативной синхронизации телефона", "arguments": {"text": "точный текст заметки"}},
            {"name": "organizer_notes_list", "description": "Прочитать сохранённые заметки владельца", "arguments": {"limit": "1..100"}},
            {"name": "organizer_event_create", "description": "Создать событие в локальном календаре EIRVEN, очереди нативного календаря телефона и поставить напоминания; даты в ISO-8601 с часовым поясом", "arguments": {"title": "название события словами владельца", "starts_at": "ISO-8601", "ends_at": "необязательный ISO-8601", "reminders": "массив минут, обычно [120,60,30,10]"}},
            {"name": "organizer_day_plan", "description": "Прочитать события на сегодня из EIRVEN и синхронизированного нативного календаря телефона", "arguments": {}},
            {"name": "launch_application", "description": "Запустить установленное приложение по названию после явной просьбы владельца", "arguments": {"application": "название приложения"}},
            {"name": "reinstall_application", "description": "Переустановить ОДНО однозначно найденное Windows-приложение через winget после явной просьбы владельца и проверить, что пакет снова установлен. Не удаляет пользовательские файлы вручную.", "arguments": {"application": "точное название приложения"}},
            {"name": "close_application", "description": "Закрыть запущенное пользовательское приложение по названию", "arguments": {"application": "название приложения"}},
            {"name": "close_user_apps", "description": "Закрыть видимые пользовательские приложения, сохранив Windows и EIRVEN", "arguments": {}},
            {"name": "set_dark_theme", "description": "Включить или выключить темную системную тему Windows", "arguments": {"enabled": "true/false"}},
            {"name": "toggle_quick_setting", "description": "Переключить видимую быструю настройку Windows, например airplane/wifi/bluetooth", "arguments": {"name": "airplane|wifi|bluetooth", "enabled": "true/false"}},
            {"name": "process_list", "description": "Посмотреть реальные процессы Windows; можно фильтровать по названию", "arguments": {"name_contains": "необязательно"}},
            {"name": "explorer_current_folder", "description": "Получить подтверждённый путь папки, которая сейчас открыта в Проводнике Windows", "arguments": {}},
            {"name": "explorer_selected_files", "description": "Получить точные пути файлов, которые владелец сейчас выделил в Проводнике Windows", "arguments": {}},
            {"name": "process_terminate", "description": "Завершить совпадающие внешние процессы и затем проверить, что они действительно закрыты; процессы EIRVEN по умолчанию защищены", "arguments": {"name_contains": "имя процесса", "all_matches": "true/false", "protect_eirven": "true/false"}},
            {"name": "system_find", "description": "Найти файл или папку на компьютере владельца по имени; по умолчанию ищет в профиле пользователя", "arguments": {"name": "часть имени", "root": "необязательный путь"}},
            {"name": "system_open_named", "description": "Найти и сразу открыть любой пользовательский файл или папку по имени за один шаг; location можно указать как desktop/рабочий стол/documents/downloads", "arguments": {"name": "имя файла или папки", "location": "необязательная подсказка места"}},
            {"name": "system_open_path", "description": "Открыть существующий пользовательский файл или папку стандартным приложением ОС", "arguments": {"path": "полный путь"}},
            {"name": "system_list_files", "description": "Показать содержимое произвольной пользовательской папки", "arguments": {"path": "полный путь"}},
            {"name": "system_read_file", "description": "Прочитать текстовый файл проекта вне workspace", "arguments": {"path": "полный путь"}},
            {"name": "system_write_file", "description": "Изменить текстовый файл в пользовательской области, когда это прямо нужно задаче", "arguments": {"path": "полный путь", "content": "содержимое"}},
            {"name": "system_batch_rename", "description": "Массово переименовать файлы в папке безопасной двухфазной операцией и затем перепроверить каждый новый и старый путь. Для массового переименования предпочитай этот инструмент PowerShell.", "arguments": {"directory": "полный путь к папке", "template": "шаблон: {n}, {stem}, {ext}; например photo_{n}", "glob": "маска файлов, например *.jpg", "start": "первый номер", "dry_run": "только проверить план"}},
            {"name": "powershell", "description": "Универсальный PowerShell с правами текущего EIRVEN. Используй для файлов, Git/Docker/SSH, служб, реестра, темы Windows, Wi-Fi, VPN, сети, устройств и системных настроек, когда нет более структурного инструмента. Критически разрушительные операции запрещены без отдельного подтверждения.", "arguments": {"command": "PowerShell", "cwd": "полный путь"}},
            {"name": "system_diagnostics", "description": "Собрать реальную диагностику Windows: диски, сеть, службы и недавние системные ошибки; ничего не меняет", "arguments": {"recent_minutes": "10..1440"}},
            {"name": "git_publish", "description": "Закоммитить текущие изменения проекта и отправить в указанный Git-репозиторий. При ошибке авторизации попроси владельца войти/добавить ключ и затем wait_user", "arguments": {"path": "папка проекта", "remote": "git@... или https://...", "message": "commit message"}},
            {"name": "open_default_url", "description": "Открыть ссылку в браузере пользователя по умолчанию", "arguments": {"url": "http/https"}},
            {"name": "default_search", "description": "Открыть поиск в браузере пользователя по умолчанию", "arguments": {"query": "строка"}},
            {"name": "web_search", "description": "Быстро получить актуальные результаты публичного веб-поиска без платного API. Используй для погоды, новостей, цен, свежей документации и любых фактов, которые могли измениться.", "arguments": {"query": "поисковый запрос", "max_results": "1..8", "timelimit": "необязательно: d|w|m|y"}},
            {"name": "wait", "description": "Коротко подождать загрузку/разблокировку интерфейса и затем снова проверить состояние", "arguments": {"seconds": "0.05..8"}},
            {"name": "command_available", "description": "Проверить, установлена ли системная команда/утилита (например git) и узнать путь", "arguments": {"command": "имя команды"}},
            {"name": "foreground_window", "description": "Получить только настоящее активное окно Windows без перечисления процессов", "arguments": {}},
            {"name": "media_control", "description": "Показать live Windows media sessions либо адресно управлять конкретной сессией по target/session_id. Всегда указывай target, если владелец назвал сервис, приложение, окно, трек или исполнителя. Не использует глобальную медиаклавишу и отказывается от неоднозначного выбора.", "arguments": {"action": "status|list|play|pause|play_pause|next|previous|stop", "target": "необязательное название сервиса, окна, трека или исполнителя", "session_id": "необязательный id из status/list"}},
            {"name": "system_volume", "description": "Изменить системную громкость Windows стандартными глобальными клавишами", "arguments": {"action": "down|up|mute", "steps": "1..10"}},
            {"name": "system_brightness", "description": "Установить яркость монитора Windows на указанное значение и проверить фактическое значение", "arguments": {"level": "0..100"}},
            {"name": "system_power", "description": "Запланировать выключение Windows либо отменить его. Выключение всегда проходит детерминированное подтверждение перед выполнением.", "arguments": {"action": "shutdown|cancel", "delay_seconds": "10..300"}},
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
                    {"name": "window_wait", "description": "Подождать появления окна или элемента после загрузки/перехода, не расходуя LLM-циклы", "arguments": {"title_contains": "часть заголовка", "handle": "handle привязанного окна", "element_text": "необязательный текст элемента", "automation_id": "необязательный id", "timeout": "0.2..45 сек."}},
                    {"name": "window_focus", "description": "Перевести фокус на одно конкретное окно и привязать к нему задачу", "arguments": {"title_contains": "часть заголовка", "handle": "предпочтительный точный handle"}},
                    {"name": "window_click", "description": "Нажать структурный элемент только внутри привязанного окна; stable_id из последнего window_elements предпочтительнее текста", "arguments": {"title_contains": "окно", "handle": "handle привязанного окна", "stable_id": "stable_id из свежего наблюдения", "element_text": "текст", "control_type": "Button", "automation_id": "необязательно"}},
                    {"name": "window_type", "description": "Ввести текст в структурное поле только внутри привязанного окна; stable_id из последнего window_elements предпочтительнее текста", "arguments": {"title_contains": "окно", "handle": "handle привязанного окна", "stable_id": "stable_id из свежего наблюдения", "element_text": "подпись/текст", "text": "строка", "replace": "bool"}},
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
        "timeout": "integer", "delay_seconds": "integer", "recent_minutes": "integer", "max_results": "integer", "max_depth": "integer",
        "max_entries": "integer", "max_windows": "integer", "max_elements": "integer", "steps": "integer",
        "interval": "number", "duration": "number", "seconds": "number", "exact": "boolean",
        "start": "integer", "dry_run": "boolean", "limit": "integer", "refresh": "boolean", "prepare_replies": "boolean", "move_spam": "boolean", "index": "integer",
        "replace": "boolean", "overwrite": "boolean", "all_matches": "boolean", "protect_eirven": "boolean", "keys": "array", "reminders": "array",
    }
    required_by_tool = {
        "read_file": {"path"}, "write_file": {"path", "content"},
        "make_directory": {"path"}, "run_command": {"command"},
        "application_list": set(), "open_service": {"service"}, "mail_status": set(), "mail_review": set(), "mail_drafts": set(), "mail_stage_draft": {"index"},
        "organizer_note_create": {"text"}, "organizer_notes_list": set(), "organizer_event_create": {"title", "starts_at"}, "organizer_day_plan": set(),
        "launch_application": {"application"}, "reinstall_application": {"application"}, "close_application": {"application"}, "close_user_apps": set(), "set_dark_theme": set(), "toggle_quick_setting": {"name", "enabled"}, "system_find": {"name"},
        "system_open_named": {"name"}, "system_open_path": {"path"}, "system_list_files": {"path"},
        "system_read_file": {"path"}, "system_write_file": {"path", "content"},
        "system_batch_rename": {"directory", "template"},
        "powershell": {"command"}, "git_publish": {"path", "remote"},
        "open_default_url": {"url"}, "default_search": {"query"}, "web_search": {"query"},
        "command_available": {"command"}, "media_control": {"action"}, "system_volume": {"action"}, "system_brightness": {"level"}, "system_power": {"action"}, "process_terminate": {"name_contains"},
        "browser_open": {"url"}, "browser_search": {"query"},
        "browser_click_text": {"text"}, "browser_fill": {"selector_or_label", "value"},
        "browser_press": {"key"}, "browser_upload": {"path"}, "click": {"x", "y"},
        "mouse_move": {"x", "y"}, "mouse_drag": {"x", "y"},
        "scroll": {"amount"}, "press_key": {"key"}, "hotkey": {"keys"},
        "type_text": {"text"}, "window_focus": set(),
        "window_click": set(), "window_type": {"text"},
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
