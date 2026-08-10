from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import webbrowser
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .browser import BrowserAutomation

from .system_browser import open_url as open_system_url, open_search as open_system_search


class ApplicationError(RuntimeError):
    pass


@dataclass(slots=True)
class InstalledApplication:
    name: str
    app_id: str


class ApplicationService:
    """Launches applications only after an explicit user request.

    On Windows it reads Start-menu application identifiers and launches them through
    explorer.exe. This avoids hard-coded executable paths and works with Store apps.
    """

    def __init__(self, browser: BrowserAutomation, cache_path: Path | None = None):
        self.browser = browser
        self.cache_path = cache_path
        self._cache: list[InstalledApplication] | None = None
        if cache_path and cache_path.is_file():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._cache = [
                        InstalledApplication(str(item.get("name") or ""), str(item.get("app_id") or ""))
                        for item in raw if isinstance(item, dict) and item.get("name") and item.get("app_id")
                    ]
            except Exception:
                self._cache = None

    def list_installed(self, refresh: bool = False) -> list[dict[str, str]]:
        if self._cache is not None and not refresh:
            return [{"name": item.name, "app_id": item.app_id} for item in self._cache]
        items: list[InstalledApplication] = []
        if os.name == "nt":
            command = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    shell=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parsed = json.loads(result.stdout)
                    rows = parsed if isinstance(parsed, list) else [parsed]
                    items = [
                        InstalledApplication(str(row.get("Name") or ""), str(row.get("AppID") or ""))
                        for row in rows
                        if row.get("Name") and row.get("AppID")
                    ]
            except Exception:
                items = []
        else:
            for executable in ("firefox", "google-chrome", "chromium", "code", "telegram-desktop"):
                path = shutil.which(executable)
                if path:
                    items.append(InstalledApplication(executable, path))
        self._cache = sorted(items, key=lambda item: item.name.lower())
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(
                    json.dumps([{"name": item.name, "app_id": item.app_id} for item in self._cache], ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return [{"name": item.name, "app_id": item.app_id} for item in self._cache]

    @staticmethod
    def _canonical_query(query: str) -> str:
        clean = re.sub(r"\s+", " ", query.strip().casefold()).strip(" .,!?")
        aliases = {
            "telegram": ("телеграм", "телеграмм", "телега", "телегра", "тг", "telegram"),
            "visual studio code": ("vscode", "vs code", "вс код", "ви эс код", "visual studio code"),
            "discord": ("дискорд", "дискор", "discord"),
            "spotify": ("спотифай", "споти", "spotify"),
            "steam": ("стим", "steam"),
            "google chrome": ("гугл хром", "хром", "chrome", "google chrome"),
            "microsoft edge": ("эдж", "edge", "microsoft edge"),
            "whatsapp": ("ватсап", "вацап", "whatsapp"),
        }
        for canonical, variants in aliases.items():
            for variant in variants:
                # Voice ASR often drops the last one or two phonemes: "телегра" should
                # still be a deterministic Telegram launch, not an LLM problem.
                score = SequenceMatcher(None, clean, variant).ratio()
                if clean == variant or (len(clean) >= 4 and (variant.startswith(clean) or clean.startswith(variant))) or score >= 0.82:
                    return canonical
        return clean

    def resolve(self, query: str) -> InstalledApplication:
        clean = self._canonical_query(query)
        if not clean:
            raise ApplicationError("Не указано приложение")
        apps = [InstalledApplication(**item) for item in self.list_installed()]
        ranked: list[tuple[float, InstalledApplication]] = []
        # Generic fuzzy resolution. Natural-language normalization belongs to the local
        # tool-calling model; this layer only maps its requested app name to Windows.
        query_tokens = {token for token in re.split(r"[^\w]+", clean) if len(token) > 1}
        for app in apps:
            name = app.name.lower().strip()
            name_tokens = {token for token in re.split(r"[^\w]+", name) if len(token) > 1}
            ratio = SequenceMatcher(None, clean, name).ratio()
            overlap = len(query_tokens & name_tokens) / max(1, len(query_tokens | name_tokens))
            contains = 1.0 if clean in name or name in clean else 0.0
            score = ratio * 55 + overlap * 35 + contains * 25
            if name == clean:
                score += 100
            if score >= 32:
                ranked.append((score, app))
        if not ranked:
            raise ApplicationError(f"Приложение «{query}» не найдено в меню Пуск")
        ranked.sort(key=lambda pair: (-pair[0], pair[1].name.lower()))
        return ranked[0][1]

    def strong_matches(self, query: str) -> list[dict[str, str]]:
        """Return only unambiguous Start-menu name matches, without fuzzy launching.

        This is deliberately stricter than :meth:`resolve`: it is used to decide what a
        bare phrase such as ``открой Microsoft`` means.  A fuzzy best guess is suitable
        only after the owner explicitly chose an application, never for arbitration.
        """
        clean = self._canonical_query(query)
        key = re.sub(r"[^a-zа-я0-9]+", " ", clean.casefold().replace("ё", "е")).strip()
        if not key:
            return []
        matches: list[dict[str, str]] = []
        for row in self.list_installed():
            name = str(row.get("name") or "").strip()
            name_key = re.sub(r"[^a-zа-я0-9]+", " ", name.casefold().replace("ё", "е")).strip()
            if name_key == key:
                matches.append({"name": name, "app_id": str(row.get("app_id") or "")})
        return matches

    def launch(self, query: str) -> dict[str, Any]:
        canonical = self._canonical_query(query)
        try:
            app = self.resolve(canonical)
        except ApplicationError:
            # USB/Store/app updates can make the persisted Start-menu index stale.
            self.list_installed(refresh=True)
            try:
                app = self.resolve(canonical)
            except ApplicationError:
                # Telegram is a core voice scenario and classic desktop installs do not
                # always appear in Get-StartApps immediately. Try its normal user paths.
                if os.name == "nt" and canonical == "telegram":
                    env = os.environ
                    candidates = [
                        Path(env.get("APPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
                        Path(env.get("LOCALAPPDATA", "")) / "Telegram Desktop" / "Telegram.exe",
                        Path(env.get("LOCALAPPDATA", "")) / "Programs" / "Telegram Desktop" / "Telegram.exe",
                    ]
                    target = next((path for path in candidates if str(path) and path.is_file()), None)
                    if target:
                        subprocess.Popen([str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return {"name": "Telegram", "app_id": str(target), "platform": platform.system(), "fallback": "common_path"}
                raise
        if os.name == "nt":
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app.app_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen([app.app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"name": app.name, "app_id": app.app_id, "platform": platform.system()}


    @staticmethod
    def _process_name_candidates(query: str) -> set[str]:
        clean = ApplicationService._canonical_query(query)
        variants = {clean, clean.replace(" ", ""), clean.replace(" ", "-")}
        known = {
            "telegram": {"telegram", "telegram.exe"},
            "visual studio code": {"code", "code.exe", "visual studio code"},
            "discord": {"discord", "discord.exe"},
            "spotify": {"spotify", "spotify.exe"},
            "steam": {"steam", "steam.exe"},
            "google chrome": {"chrome", "chrome.exe"},
            "microsoft edge": {"msedge", "msedge.exe"},
            "whatsapp": {"whatsapp", "whatsapp.exe"},
        }
        variants |= known.get(clean, set())
        return {v.casefold() for v in variants if v}

    def reinstall(self, query: str) -> dict[str, Any]:
        """Reinstall one unambiguously named Windows app through winget and verify it."""
        if os.name != "nt":
            raise ApplicationError("Переустановка приложений реализована для Windows")
        winget = shutil.which("winget")
        if not winget:
            raise ApplicationError("winget не найден. Нужен App Installer из Microsoft Store")
        try:
            app = self.resolve(query)
            name = app.name
        except Exception:
            name = str(query or "").strip()
        if not name:
            raise ApplicationError("Не указано приложение")

        def run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [winget, *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, shell=False,
            )

        probe = run(["list", "--name", name, "--exact", "--accept-source-agreements", "--disable-interactivity"], 90)
        probe_text = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
        if probe.returncode != 0 or name.casefold() not in probe_text.casefold():
            raise ApplicationError(
                f"winget не нашёл однозначный установленный пакет «{name}». Ничего не удалено. {probe_text[-500:]}"
            )
        try:
            self.close(name)
        except Exception:
            pass
        uninstall = run(["uninstall", "--name", name, "--exact", "--silent", "--disable-interactivity"], 900)
        uninstall_text = ((uninstall.stdout or "") + "\n" + (uninstall.stderr or "")).strip()
        if uninstall.returncode != 0:
            raise ApplicationError(f"Не удалось удалить «{name}» перед переустановкой: {uninstall_text[-700:]}")
        install = run([
            "install", "--name", name, "--exact", "--silent", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity",
        ], 1200)
        install_text = ((install.stdout or "") + "\n" + (install.stderr or "")).strip()
        if install.returncode != 0:
            raise ApplicationError(f"Удаление прошло, но повторная установка «{name}» не завершилась: {install_text[-900:]}")
        verify = run(["list", "--name", name, "--exact", "--accept-source-agreements", "--disable-interactivity"], 90)
        verify_text = ((verify.stdout or "") + "\n" + (verify.stderr or "")).strip()
        verified = verify.returncode == 0 and name.casefold() in verify_text.casefold()
        self.list_installed(refresh=True)
        return {
            "name": name, "reinstalled": True, "verified": verified,
            "uninstall_output": uninstall_text[-1200:], "install_output": install_text[-1200:],
        }

    def close(self, query: str) -> dict[str, Any]:
        """Close a named user application by process metadata, without an LLM."""
        try:
            import psutil  # type: ignore
        except ImportError as exc:
            raise ApplicationError("psutil не установлен") from exc
        wanted = self._process_name_candidates(query)
        if not wanted:
            raise ApplicationError("Не указано приложение")
        matched = []
        current_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "username"]):
            try:
                if int(proc.info.get("pid") or 0) == current_pid:
                    continue
                name = str(proc.info.get("name") or "").casefold()
                exe = Path(str(proc.info.get("exe") or "")).stem.casefold() if proc.info.get("exe") else ""
                cmd = " ".join(proc.info.get("cmdline") or []).casefold()
                hay = f"{name} {exe} {cmd}"
                scores = [
                    1.0 if token in {name, exe} else
                    (0.92 if len(token) >= 4 and token in hay else SequenceMatcher(None, token, name or exe).ratio())
                    for token in wanted
                ]
                if max(scores, default=0.0) < 0.78:
                    continue
                proc.terminate()
                matched.append({"pid": proc.pid, "name": proc.info.get("name")})
            except Exception:
                continue
        if not matched:
            raise ApplicationError(f"Запущенное приложение «{query}» не найдено")
        return {"closed": matched[:30], "count": len(matched)}

    def close_browsers(self) -> dict[str, Any]:
        """Close ordinary browser processes without touching EIRVEN's hidden spatial browser."""
        try:
            import psutil  # type: ignore
        except ImportError as exc:
            raise ApplicationError("psutil не установлен") from exc
        names = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe", "vivaldi.exe", "browser.exe", "samsunginternet.exe"}
        try:
            default_exe = Path(str(self.default_browser_info().get("executable") or "")).name.casefold()
            if default_exe.endswith(".exe"):
                names.add(default_exe)
        except Exception:
            pass
        closed = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = str(proc.info.get("name") or "").casefold()
                cmd = " ".join(proc.info.get("cmdline") or []).casefold()
                if name not in names:
                    continue
                # Spatial Chromium is started by Playwright with a dedicated profile and
                # is part of EIRVEN's camera surface, not the owner's visible browser.
                if "eirven" in cmd and "playwright" in cmd:
                    continue
                proc.terminate()
                closed.append({"pid": proc.pid, "name": proc.info.get("name")})
            except Exception:
                continue
        if not closed:
            raise ApplicationError("Открытый пользовательский браузер не найден")
        return {"closed": closed[:40], "count": len(closed)}

    def close_user_apps(self) -> dict[str, Any]:
        """Close ordinary interactive apps while preserving Windows/EIRVEN processes.

        A literal 'kill every process' would terminate Windows services and can lose data;
        this method intentionally targets processes owning visible top-level windows.
        """
        if os.name != "nt":
            raise ApplicationError("Закрытие всех приложений реализовано для Windows")
        try:
            import ctypes
            import psutil  # type: ignore
        except Exception as exc:
            raise ApplicationError(f"Не удалось получить процессы Windows: {exc}") from exc
        user32 = ctypes.windll.user32
        visible_pids: set[int] = set()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        @WNDENUMPROC
        def enum(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    visible_pids.add(int(pid.value))
            except Exception:
                pass
            return True
        user32.EnumWindows(enum, 0)
        protected = {
            "explorer.exe", "dwm.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
            "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
            "python.exe", "pythonw.exe", "eirven-ai.exe", "ollama.exe",
        }
        closed = []
        for pid in sorted(visible_pids):
            try:
                proc = psutil.Process(pid)
                name = str(proc.name() or "").casefold()
                if name in protected:
                    continue
                proc.terminate()
                closed.append({"pid": pid, "name": proc.name()})
            except Exception:
                continue
        return {"closed": closed[:80], "count": len(closed), "protected_system_processes": True}


    @staticmethod
    def default_browser_info() -> dict[str, str]:
        """Resolve the Windows HTTPS UserChoice without assuming a browser brand."""
        if os.name != "nt":
            return {"name": "system_default", "progid": "", "executable": ""}
        progid = ""; command = ""; executable = ""
        try:
            import winreg
            key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                progid = str(winreg.QueryValueEx(handle, "ProgId")[0] or "")
            if progid:
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as handle:
                        command = str(winreg.QueryValueEx(handle, "")[0] or "")
                except Exception:
                    pass
        except Exception:
            pass
        if command:
            m = re.match(r'\s*"([^"]+\.exe)"|\s*([^\s]+\.exe)', command, re.I)
            if m:
                executable = str(m.group(1) or m.group(2) or "")
        blob = f"{progid} {executable}".casefold()
        known = (
            ("samsung", "Samsung Internet"), ("firefox", "Firefox"),
            ("chrome", "Google Chrome"), ("msedge", "Microsoft Edge"), ("edge", "Microsoft Edge"),
            ("opera", "Opera"), ("yandex", "Яндекс Браузер"), ("brave", "Brave"),
            ("vivaldi", "Vivaldi"), ("waterfox", "Waterfox"), ("librewolf", "LibreWolf"),
        )
        name = next((label for needle, label in known if needle in blob), "")
        if not name and executable:
            name = Path(executable).stem.replace("_", " ").strip()
        if not name:
            name = progid or "system_default"
        return {"name": name, "progid": progid, "executable": executable, "command": command}

    @staticmethod
    def default_browser_name() -> str:
        return str(ApplicationService.default_browser_info().get("name") or "system_default")

    def web_fallback(self, query: str) -> dict[str, str]:
        """Find and open the likely official web app, not just a search-results page."""
        clean = str(query or "").strip()
        if not clean:
            raise ApplicationError("Не указано приложение")
        search = f"{clean} официальный сайт web app"
        try:
            result = self.browser.search_first_site(search, open_visible=True)
            return {"url": str(result.get("url") or ""), "query": search, "fallback": "direct_site"}
        except Exception:
            # Google "I'm Feeling Lucky" redirects to the first result instead of
            # leaving the owner on a search-results page.
            url = f"https://www.google.com/search?btnI=1&q={quote_plus(search)}"
            open_system_url(url)
            return {"url": url, "query": search, "fallback": "first_result_redirect", "browser": "system_default"}

    @staticmethod
    def open_windows_search(query: str = "") -> dict[str, str]:
        """Open native Windows Search; optionally type a query. Never falls back to web."""
        if os.name != "nt":
            raise ApplicationError("Поиск приложений доступен только в Windows")
        try:
            import pyautogui
            pyautogui.hotkey("win", "s")
            if query.strip():
                import time
                time.sleep(0.25)
                try:
                    import pyperclip
                    pyperclip.copy(query)
                    pyautogui.hotkey("ctrl", "v")
                except Exception:
                    pyautogui.write(query, interval=0.01)
            return {"query": query, "opened": "windows_search"}
        except Exception as exc:
            raise ApplicationError(f"Не удалось открыть поиск Windows: {exc}") from exc

    @staticmethod
    def open_file_search(query: str) -> dict[str, str]:
        """Open indexed Explorer search when a named file/folder was not found locally."""
        clean = str(query or "").strip()
        if not clean:
            raise ApplicationError("Не указано имя для поиска")
        if os.name != "nt":
            raise ApplicationError("Системный поиск файлов доступен только в Windows")
        uri = f"search-ms:query={quote_plus(clean)}"
        subprocess.Popen(["explorer.exe", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"query": clean, "uri": uri, "opened": "explorer_search"}

    @staticmethod
    def open_folder(path: str | Path) -> dict[str, str]:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise ApplicationError(f"Путь не существует: {target}")
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"path": str(target)}

    def search_legal_movie(self, title: str, free_only: bool = False) -> dict[str, Any]:
        suffix = " где смотреть легально бесплатно" if free_only else " где смотреть легально"
        query = f"{title}{suffix}"
        result = self.browser.search(query)
        return {"title": title, "query": query, "browser": result}

    @staticmethod
    def open_default_search(query: str) -> dict[str, str]:
        url = open_system_search(query)
        return {"url": url, "query": query, "browser": "system_default"}
