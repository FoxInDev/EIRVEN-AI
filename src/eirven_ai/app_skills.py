from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .system_browser import open_url
from .trace import log_event


class AppSkills:
    """Fast adapters for common apps; unknown apps fall back to the visible desktop operator."""

    URLS = {
        "youtube": "https://www.youtube.com/",
        "spotify": "https://open.spotify.com/",
        "yandex_music": "https://music.yandex.ru/",
        "telegram": "https://web.telegram.org/",
        "discord": "https://discord.com/app",
    }

    def __init__(self, services: Any, operator: Any):
        self.services = services
        self.operator = operator

    @staticmethod
    def canonical(text: str) -> str:
        s = str(text or "").casefold().replace("ё", "е")
        if re.search(r"\b(?:телеграм\w*|телегр\w*|телега\w*|telegram|тг)\b", s): return "telegram"
        if re.search(r"\b(?:яндекс\s*музык|yandex\s*music|моя\s*волна)\b", s): return "yandex_music"
        if re.search(r"\b(?:ютуб|youtube)\b", s): return "youtube"
        if re.search(r"\b(?:спотифай|spotify)\b", s): return "spotify"
        if re.search(r"\b(?:дискорд|discord)\b", s): return "discord"
        if re.search(r"\b(?:vscode|vs\s*code|visual\s*studio\s*code|вс\s*код)\b", s): return "vscode"
        if re.search(r"\b(?:проводник|explorer|файловый\s*менеджер)\b", s): return "explorer"
        if re.search(r"\b(?:windows\s*settings|параметры\s*windows|настройки\s*windows)\b", s): return "windows_settings"
        if re.search(r"\b(?:браузер|browser)\b", s): return "browser"
        return ""

    def _log(self, event: str, **data: Any) -> None:
        try: log_event(self.services.settings.root_dir, event, **data)
        except Exception: pass

    def _focus_existing_browser_tab(self, aliases: list[str]) -> dict[str, Any] | None:
        """Reuse an already-open web-app tab before launching/opening another one.

        Windows exposes a Chromium/Samsung browser as one top-level window, so window_list
        cannot see background tab titles.  TabItem accessibility can.  Reusing the tab is
        both faster and safer because it preserves the owner's authenticated session.
        """
        wanted=[str(x or "").casefold().replace("ё","е") for x in aliases if str(x or "").strip()]
        try:
            listing=self.services.tools.execute("window_list",{"max_windows":50})
            windows=list(listing.get("result") or []) if listing.get("ok") else []
            for win in windows:
                title=str(win.get("title") or ""); cls=str(win.get("class_name") or "").casefold()
                if not any(m in cls or m in title.casefold() for m in ("chrome_widgetwin","browser","samsung","chrome","edge","firefox","opera","yandex")):
                    continue
                handle=int(win.get("handle") or 0) or None
                rows=self.services.tools.execute("window_elements",{"title_contains":title,"max_elements":420,"handle":handle})
                for el in list(rows.get("result") or []) if rows.get("ok") else []:
                    if str(el.get("control_type") or "").casefold() != "tabitem" or not el.get("visible",True):
                        continue
                    name=str(el.get("name") or ""); key=name.casefold().replace("ё","е")
                    if not any(alias in key for alias in wanted):
                        continue
                    if handle: self.services.tools.execute("window_focus",{"handle":handle})
                    if self.operator.click_element(title,el,goal="reuse_existing_web_app_tab"):
                        time.sleep(.18)
                        fg=self.services.tools.execute("foreground_window",{})
                        current=dict(fg.get("result") or {}) if fg.get("ok") else {}
                        self._log("APP_TAB_REUSED",aliases=aliases,tab=name,title=str(current.get("title") or ""))
                        return {"title":str(current.get("title") or name),"handle":int(current.get("handle") or handle or 0),"tab":name}
        except Exception as exc:
            self._log("APP_TAB_REUSE_ERROR",aliases=aliases,error=str(exc)[:300])
        return None

    def open(self, target: str) -> dict[str, Any]:
        skill = self.canonical(target)
        web_aliases={
            "telegram":["Telegram","web.telegram","Телеграм"],
            "yandex_music":["Яндекс Музыка","Yandex Music","music.yandex"],
            "youtube":["YouTube","Ютуб"],
            "spotify":["Spotify"],
            "discord":["Discord"],
        }
        if skill in web_aliases:
            existing=self._focus_existing_browser_tab(web_aliases[skill])
            if existing:
                return {"ok":True,"skill":skill,"verified":True,"reused_tab":True,"window":existing}
        if skill == "browser":
            open_url("https://www.google.com/")
            default_name = str(self.services.applications.default_browser_name() or "")
            window = self.operator.wait_window([default_name, "Browser", "Браузер", "Firefox", "Opera", "Yandex", "Яндекс", "Samsung"], 2.0)
            return {"ok": True, "skill": skill, "verified": bool(window), "window": window}
        if skill == "windows_settings":
            if __import__("os").name == "nt":
                __import__("os").startfile("ms-settings:")  # type: ignore[attr-defined]
                return {"ok": True, "skill": skill, "verified": True}
        if skill == "explorer":
            result = self.services.tools.execute("system_open_path", {"path": str(__import__("pathlib").Path.home())})
            return {"ok": bool(result.get("ok")), "skill": skill, "result": result, "verified": bool(result.get("ok"))}
        app_names = {"telegram":"Telegram", "discord":"Discord", "spotify":"Spotify", "vscode":"Visual Studio Code", "yandex_music":"Яндекс Музыка"}
        if skill in app_names:
            result = self.services.tools.execute("launch_application", {"application": app_names[skill]})
            if result.get("ok"):
                window = self.operator.wait_window([app_names[skill], skill], 2.0)
                return {"ok": True, "skill": skill, "result": result, "verified": bool(window)}
            if skill in self.URLS:
                open_url(self.URLS[skill])
                window = self.operator.wait_window([app_names[skill], skill], 2.5)
                return {"ok": True, "skill": skill, "fallback": "web", "verified": bool(window), "window": window}
        if skill in {"youtube", "spotify"}:
            open_url(self.URLS[skill])
            aliases = [skill.capitalize(), self.services.applications.default_browser_name(), "Samsung Internet"]
            window = self.operator.wait_window(aliases, 2.5)
            return {"ok": True, "skill": skill, "verified": bool(window), "window": window}
        result = self.services.tools.execute("launch_application", {"application": target})
        return {"ok": bool(result.get("ok")), "skill": skill or "generic", "result": result, "verified": bool(result.get("ok"))}

    def play_music(self) -> dict[str, Any]:
        try:
            result = self.operator.yandex_wave()
            return {"ok": True, "skill": "yandex_music", **result}
        except Exception as exc:
            self._log("SKILL_RECOVERY", skill="yandex_music", error=str(exc))
            # yandex_wave already opens the site at most once. Do not create another tab
            # merely because Play verification failed.
            window=self.operator.wait_window(["Яндекс Музыка","Yandex Music","music.yandex"],.25)
            if window:
                recovery="kept_current_yandex_screen"
            else:
                open_url(self.URLS["yandex_music"]); recovery="opened_default_browser"
            return {"ok": False, "skill": "yandex_music", "error": str(exc), "recovery": recovery}

    def send_telegram(self, recipient: str, text: str) -> dict[str, Any]:
        try:
            return {"ok": True, "skill": "telegram", **self.operator.telegram_send(recipient, text)}
        except Exception as exc:
            self._log("SKILL_RECOVERY", skill="telegram", recipient=recipient, error=str(exc))
            # Never create a second Telegram tab if the owner's real session is already
            # visible. Opening a page is only a last recovery when Telegram is absent.
            window=self.operator.wait_window(["Telegram","web.telegram","Телеграм"],.25)
            if not window:
                open_url(self.URLS["telegram"])
                recovery="telegram_web_default_browser"
            else:
                recovery="kept_current_telegram_screen"
            return {"ok": False, "skill": "telegram", "error": str(exc), "recovery": recovery}

    def answer_discord_call(self) -> dict[str, Any]:
        try:
            return {"ok": True, "skill": "discord", **self.operator.answer_discord_call()}
        except Exception as exc:
            return {"ok": False, "skill": "discord", "error": str(exc)}

    @staticmethod
    def _path_from_vscode_uri(value: object) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            if raw.startswith("file:"):
                parsed = urlparse(raw)
                path_text = unquote(parsed.path or "")
                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path_text):
                    path_text = path_text[1:]
                path = Path(path_text)
            else:
                path = Path(raw)
            path = path.expanduser()
            return path.resolve() if path.exists() else None
        except Exception:
            return None

    @classmethod
    def _json_paths(cls, payload: object) -> list[Path]:
        found: list[Path] = []
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    key_n = str(key or "").casefold()
                    if key_n in {"folder", "folderuri", "workspace", "workspaceuri", "configpath"}:
                        path = cls._path_from_vscode_uri(value)
                        if path is not None:
                            if path.is_file() and path.suffix.casefold() == ".code-workspace":
                                # The workspace file's parent is a safe, useful project root
                                # when its inner folders are not available in this metadata.
                                found.append(path.parent)
                            elif path.is_dir():
                                found.append(path)
                    stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
        return found

    def vscode_workspace(self) -> Path | None:
        """Resolve the project currently owned by VS Code without guessing a new folder."""
        candidates: list[tuple[float, Path]] = []
        seen: set[str] = set()

        def add(path: Path | None, score: float) -> None:
            if path is None:
                return
            try:
                path = path.resolve()
                if not path.is_dir():
                    return
                key = str(path).casefold()
                if key in seen:
                    return
                seen.add(key)
                candidates.append((score, path))
            except Exception:
                return

        # Command lines are the strongest evidence when Code was launched with a folder.
        try:
            result = self.services.tools.execute("process_list", {"name_contains": "Code", "limit": 80})
            rows = list(result.get("result") or []) if result.get("ok") else []
            for row in rows:
                for raw in list((row or {}).get("cmdline") or [])[1:]:
                    value = str(raw or "").strip('"')
                    if not value or value.startswith("-"):
                        continue
                    add(self._path_from_vscode_uri(value), 120.0)
        except Exception:
            pass

        appdata = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None
        if appdata:
            user_dir = appdata / "Code" / "User"
            storage = user_dir / "workspaceStorage"
            if storage.is_dir():
                try:
                    for meta in storage.glob("*/workspace.json"):
                        try:
                            payload = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
                        except Exception:
                            continue
                        age_bonus = min(30.0, max(0.0, 30.0 - (time.time() - meta.stat().st_mtime) / 3600.0))
                        for path in self._json_paths(payload):
                            add(path, 80.0 + age_bonus)
                except Exception:
                    pass
            global_state = user_dir / "globalStorage" / "storage.json"
            if global_state.is_file():
                try:
                    payload = json.loads(global_state.read_text(encoding="utf-8", errors="replace"))
                    for path in self._json_paths(payload):
                        add(path, 65.0)
                except Exception:
                    pass

        # Prefer a folder whose name is visible in the active VS Code title.
        title = ""
        try:
            fg = self.services.tools.execute("foreground_window", {})
            title = str((fg.get("result") or {}).get("title") or "") if fg.get("ok") else ""
        except Exception:
            pass
        title_n = title.casefold()
        rescored = []
        for score, path in candidates:
            if path.name.casefold() and path.name.casefold() in title_n:
                score += 45.0
            rescored.append((score, path))
        if not rescored:
            return None
        rescored.sort(key=lambda item: item[0], reverse=True)
        return rescored[0][1]

    def inspect_vscode(self, question: str = "Что за ошибка сейчас в VS Code и как её исправить?") -> dict[str, Any]:
        try:
            fg = self.services.tools.execute("foreground_window", {})
            title = str((fg.get("result") or {}).get("title") or "") if fg.get("ok") else ""
        except Exception:
            title = ""
        if not re.search(r"(?:visual studio code|vs code|vscode|\bcode\b)", title, re.I):
            opened = self.open("VS Code")
            if not opened.get("ok"):
                return opened
            time.sleep(.2)
        visible = ""
        workflow = getattr(self.services, "universal_workflow", None)
        if workflow is not None:
            try:
                visible = str(workflow.extract_visible_text(question) or "")
            except Exception:
                visible = ""
        if not visible:
            visible = str(self.operator.observe(question) or "")
        return {"ok": True, "skill": "vscode", "answer": visible, "verified": bool(visible)}

    def repair_vscode(self, question: str = "Найди баг в текущем проекте VS Code и исправь его") -> dict[str, Any]:
        """Inspect the real active workspace, edit the minimum files, run checks, verify."""
        workspace = self.vscode_workspace()
        if workspace is None:
            return {
                "ok": False, "skill": "vscode", "verified": False,
                "error": "Не удалось однозначно определить открытую папку проекта VS Code. Открой папку проекта в VS Code и повтори.",
            }
        visible = ""
        workflow = getattr(self.services, "universal_workflow", None)
        if workflow is not None:
            try:
                visible = str(workflow.extract_visible_text(question) or "")[:5000]
            except Exception:
                pass
        agent = getattr(self.services, "agent", None)
        router = getattr(self.services, "router", None)
        if agent is None or router is None:
            return {"ok": False, "skill": "vscode", "verified": False, "workspace": str(workspace), "error": "Кодовый агент недоступен"}
        prompt = (
            "Ты ремонтируешь УЖЕ ОТКРЫТЫЙ проект владельца в VS Code. Не создавай новый проект и не меняй файлы вне указанной папки. "
            "Сначала просмотри реальные файлы и конфигурацию, затем воспроизведи ошибку подходящей командой тестов/линтера/сборки, "
            "найди корневую причину, внеси минимальное исправление и ОБЯЗАТЕЛЬНО повтори проверку. "
            "Можно устанавливать только явно недостающие проектные зависимости через штатный менеджер проекта; не отключай защиту Windows и не удаляй пользовательские данные. "
            "Если проблема требует секрета, логина, CAPTCHA/UAC или неоднозначного продуктового решения — остановись и попроси одно конкретное действие. "
            f"\n\nПапка проекта: {workspace}\nЗапрос владельца: {question}"
        )
        if visible:
            prompt += f"\n\nЧто видно в текущем VS Code (может содержать ошибку/терминал):\n{visible}"
        allowed = {
            "system_list_files", "system_read_file", "system_write_file", "system_find",
            "powershell", "command_available", "process_list", "foreground_window",
        }
        try:
            report = agent.run(
                prompt,
                model=router.agent_model(question),
                max_steps=min(getattr(self.services.settings, "max_agent_steps", 16), 18),
                allowed_tools=allowed,
                require_tool_action=True,
                require_side_effect=False,
                require_verification=True,
            )
        except Exception as exc:
            return {"ok": False, "skill": "vscode", "verified": False, "workspace": str(workspace), "error": str(exc)}
        return {"ok": True, "skill": "vscode", "verified": True, "workspace": str(workspace), "answer": str(report or "Исправление завершено")}
