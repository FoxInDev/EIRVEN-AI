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
        "yandex_eda": "https://eda.yandex.ru/",
        "vkusvill": "https://vkusvill.ru/",
        "telegram": "https://web.telegram.org/",
        "discord": "https://discord.com/app",
    }

    def __init__(self, services: Any, operator: Any):
        self.services = services
        self.operator = operator

    @staticmethod
    def canonical(text: str) -> str:
        s = str(text or "").casefold().replace("ё", "е")
        # Canonical capability ids are typed values at the router/engine boundary.  A
        # canonicalizer must therefore be idempotent: canonical(canonical(x)) == x.
        # r64 violated this for ``yandex_music`` and sent the already-normalized id to
        # the fuzzy Start-menu launcher, which opened Yandex Browser instead of Music.
        stable_ids = {
            "telegram", "yandex_music", "yandex_eda", "vkusvill", "youtube", "spotify", "discord",
            "vscode", "explorer", "windows_settings", "browser",
        }
        if s in stable_ids:
            return s
        if re.search(r"\b(?:яндекс[.\s]*ед[ауые]|yandex\s*(?:eda|eats))\b", s): return "yandex_eda"
        if re.search(r"\b(?:вкусвилл[а]?|vkusvill)\b", s): return "vkusvill"
        if re.search(r"\b(?:телеграм\w*|телегр\w*|телега\w*|telegram|тг)\b", s): return "telegram"
        if re.search(r"\b(?:яндекс\s*музык\w*|яндекс\w*\s+музык\w*|yandex\s*music|моя\s*волна)\b", s): return "yandex_music"
        if re.search(r"\b(?:ютуб|youtube)\b", s): return "youtube"
        if re.search(r"\b(?:спотифай|spotify)\b", s): return "spotify"
        if re.search(r"\b(?:дискорд|discord)\b", s): return "discord"
        if re.search(r"\b(?:vscode|vs\s*code|visual\s*studio\s*code|вс\s*код)\b", s): return "vscode"
        if re.search(r"\b(?:проводник|explorer|файловый\s*менеджер)\b", s): return "explorer"
        if re.search(r"\b(?:windows\s*settings|параметры\s*windows|настройки\s*windows)\b", s): return "windows_settings"
        if re.search(r"\b(?:браузер|browser)\b", s): return "browser"
        return ""

    @staticmethod
    def browser_open_target(query: str) -> str:
        """Recover an explicit single browser-open command even if admission says unknown."""
        match = re.fullmatch(
            r"\s*(?:эрви[,\s]+)?(?:открой|открыть|запусти|запустить)\s+"
            r"(?:(?:в|через)\s+браузер(?:е)?\s+)?(.+?)\s*[.!]?", query, re.I,
        )
        if not match:
            return ""
        target = match.group(1).strip()
        if re.search(r"\b(?:и|затем|потом|после|чтобы)\b|[;\n]", target, re.I):
            return ""
        in_browser = bool(re.search(r"\b(?:в|через)\s+браузер", query, re.I))
        if in_browser or AppSkills.canonical(target) in {"yandex_eda", "vkusvill"}:
            return target
        return ""

    def _log(self, event: str, **data: Any) -> None:
        try: log_event(self.services.settings.root_dir, event, **data)
        except Exception: pass

    def _desktop_permission_error(self) -> dict[str, Any] | None:
        """Fail closed before an adapter bypasses the typed tool executor.

        Most app skills eventually call ``ToolExecutor``, but a few recovery paths use
        ``open_url`` or the visible operator directly.  The user's desktop toggle must
        govern those paths too; otherwise disabling computer control only hides part of
        the capability while Telegram/browser recovery can still touch the desktop.
        """
        settings = getattr(self.services, "settings", None)
        if bool(getattr(settings, "enable_desktop_control", True)):
            return None
        return {
            "ok": False,
            "completed": False,
            "verified": False,
            "error": "Управление компьютером отключено в настройках",
            "permission_required": "desktop_control",
        }

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

    def open(self, target: str, purpose: str = "") -> dict[str, Any]:
        permission_error = self._desktop_permission_error()
        if permission_error is not None:
            return permission_error
        target = str(target or "").strip()
        purpose = str(purpose or "").strip()
        # Preserve the exact owner-provided provider while using the typed category only
        # for disambiguation.  Thus «яндекс» in a music slot resolves differently from
        # an unrelated request to open «яндекс», without adding a chat-router catalogue.
        skill = self.canonical(target)
        if not skill and purpose:
            skill = self.canonical(f"{target} {purpose}")
        if skill in {"yandex_eda", "vkusvill"}:
            url = self.URLS[skill]
            opened = bool(open_url(url))
            aliases = ["Яндекс Еда", "Яндекс.Еда", "eda.yandex"] if skill == "yandex_eda" else ["ВкусВилл", "vkusvill.ru"]
            window = self.operator.wait_window(aliases, 8.0) if opened else None
            return {"ok": opened, "skill": skill, "result": {"url": url},
                    "verified": bool(window), "window": window or {}}
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
        if skill == "yandex_music":
            # Yandex Music is a web service.  Searching Start-menu applications for the
            # same words can fuzzy-match "Яндекс Браузер" and was the source of the
            # reported wrong launch.  Use the canonical URL and verify the page surface.
            try:
                existing = self.operator.yandex_surface(timeout=.45, focus=True)
            except Exception:
                existing = None
            if existing:
                return {"ok": True, "skill": skill, "verified": True, "reused_tab": True, "window": existing}
            opened = bool(open_url(self.URLS[skill]))
            try:
                window = self.operator.yandex_surface(timeout=8.0, focus=False)
            except Exception:
                window = None
            return {
                "ok": opened, "skill": skill, "fallback": "canonical_web",
                "verified": bool(window), "window": window or {}, "url": self.URLS[skill],
                "error": "Яндекс Музыка не подтвердилась в браузере" if opened and not window else "",
            }
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
        app_names = {"telegram":"Telegram", "discord":"Discord", "spotify":"Spotify", "vscode":"Visual Studio Code"}
        if skill in app_names:
            result = self.services.tools.execute("launch_application", {"application": app_names[skill]})
            if result.get("ok"):
                window = self.operator.wait_window([app_names[skill], skill], 2.0)
                return {
                    "ok": True, "skill": skill, "result": result,
                    "verified": bool(window), "window": window or {},
                }
            if skill in self.URLS:
                open_url(self.URLS[skill])
                window = self.operator.wait_window([app_names[skill], skill], 2.5)
                return {"ok": True, "skill": skill, "fallback": "web", "verified": bool(window), "window": window}
        if skill in {"youtube", "spotify"}:
            open_url(self.URLS[skill])
            aliases = [skill.capitalize(), self.services.applications.default_browser_name(), "Samsung Internet"]
            window = self.operator.wait_window(aliases, 2.5)
            return {"ok": True, "skill": skill, "verified": bool(window), "window": window}

        # The generic resolver must not fuzzy-launch an unrelated Start-menu row.
        # ``ApplicationService.resolve`` is intentionally permissive for an explicit
        # low-level launch call, but an arbitrary service name here is a user-facing
        # open request: only an exact live catalog match is safe.  If there is no such
        # match, continue directly to the official web resolver instead of spending a
        # long retry cycle (or accidentally opening an installer/package manager).
        application_service = getattr(self.services, "applications", None)
        strong_matches: list[dict[str, Any]] = []
        try:
            if application_service is not None:
                strong_matches = list(application_service.strong_matches(target) or [])
        except Exception:
            strong_matches = []
        if len(strong_matches) == 1:
            app_name = str(strong_matches[0].get("name") or target).strip()
            result = self.services.tools.execute("launch_application", {"application": app_name})
            if result.get("ok"):
                window = self.operator.wait_window([app_name, target], 3.0)
                return {
                    "ok": True, "skill": skill or "generic", "result": result,
                    "verified": bool(window), "window": window or {},
                }

        # Unknown services are not rejected merely because they are absent from a fixed
        # catalogue.  Resolve against the owner's installed apps first, then open the
        # likely official web app in the owner's default browser.  This is an adapter
        # capability used by the reactive engine, not a front-door scenario router.
        fallback: dict[str, Any] = {}
        try:
            fallback = dict(self.services.applications.web_fallback(target) or {})
            window = self.operator.wait_window(
                [str(fallback.get("title") or target), self.services.applications.default_browser_name()],
                5.0,
            )
            return {
                "ok": bool(fallback.get("url")), "skill": skill or "generic_web",
                "fallback": "official_web_resolution", "result": fallback,
                "verified": bool(window), "window": window or {},
            }
        except Exception as exc:
            return {
                "ok": False, "skill": skill or "generic", "result": fallback,
                "verified": False, "error": str(exc),
            }

    def play_music(self, request: str = "") -> dict[str, Any]:
        """Compatibility adapter over the provider-neutral verified media state lane."""
        permission_error = self._desktop_permission_error()
        if permission_error is not None:
            return permission_error
        workflow = getattr(self.services, "universal_workflow", None)
        ensure = getattr(workflow, "ensure_media_goal", None)
        # An explicitly named provider must be opened/bound before transport control;
        # otherwise a request like "Включи яндекс музыку" can toggle an unrelated
        # foreground session.  Extract the provider from the owner's wording without a
        # catalogue: any free-form token left after the media purpose is removed is a
        # valid candidate (SoundCloud, Deezer, future services, ...).
        raw_request = str(request or "Включи музыку").strip()
        # A bare media command is an actionable request, not a request for a brand
        # picker.  The owner previously told EIRVEN that Yandex Music is the default;
        # keep that preference durable and use it only when no provider was spoken in
        # this turn.  A named provider still wins and remains completely free-form.
        explicit_provider = False
        service_hint = re.sub(
            r"^\s*(?:включи|запусти|играй|воспроизведи)\w*\s*", "", raw_request,
            flags=re.I,
        )
        service_hint = re.sub(r"\b(?:музык\w*|трек\w*|песн\w*)\b", " ", service_hint, flags=re.I)
        service_hint = re.sub(r"^\s*(?:в|на)\s+", "", service_hint, flags=re.I)
        service_hint = re.sub(r"\s+", " ", service_hint).strip(" .,!?;:")
        if re.fullmatch(r"(?:мо[её]м|текущw*|открытw*)\s+плеер\w*", service_hint, re.I):
            service_hint = ""
        explicit_provider = bool(service_hint)
        bare_media = not explicit_provider
        if bare_media:
            provider = "yandex_music"
            try:
                stored = self.services.db.get_setting("music_provider", "yandex_music")
                candidate = self.canonical(str(stored or ""))
                if candidate in {"yandex_music", "youtube", "spotify", "telegram", "discord"}:
                    provider = candidate
            except Exception:
                pass
            service_hint = provider
        # Minimal compatibility doubles (and offline callers) may expose only the
        # provider-neutral ensure_media_goal contract.  In the live service the
        # yandex_wave capability is present, so the default opens the browser; in a
        # narrow adapter test we should not turn a bare command into an attribute error
        # before the generic lane gets a chance to handle it.
        can_open_default = callable(getattr(getattr(self, "operator", None), "yandex_wave", None))
        if service_hint and (explicit_provider or not bare_media or can_open_default):
            try:
                opened = dict(self.open(service_hint, "музыка") or {})
            except Exception as exc:
                opened = {"ok": False, "verified": False, "error": str(exc)}
            if not opened.get("ok") or not opened.get("verified"):
                return {
                    "ok": False, "completed": False, "verified": False,
                    "error": str(opened.get("error") or f"Не удалось открыть сервис «{service_hint}»"),
                    "service": service_hint, "open_result": opened,
                }
        # Yandex's own player has a grounded, one-click "Моя волна" path.  Use it for
        # the default/bare command instead of sending a global media key to whichever
        # tab happens to own the current session.  If the page is not ready or the
        # preference is another provider, fall through to the provider-neutral lane.
        if bare_media and service_hint == "yandex_music" and self.operator is not None:
            try:
                wave = dict(self.operator.yandex_wave() or {})
                if wave.get("verified") or wave.get("playing"):
                    return {"skill": "yandex_music", "service": "yandex_music", **wave,
                            "ok": True, "completed": True, "verified": True}
            except Exception as exc:
                self._log("Yandex_WAVE_FALLBACK", error=str(exc)[:300])
        if callable(ensure):
            try:
                result = ensure(raw_request, allow_implicit=True)
            except Exception as exc:
                result = {"ok": False, "completed": False, "verified": False, "error": str(exc)}
            if isinstance(result, dict):
                return {"skill": "current_player", **result}
        # A provider is already selected above (Yandex by durable default, or the
        # owner's free-form name).  Never show a dead brand picker for an executable
        # command; report the concrete surface that failed so the model can recover in
        # the browser or ask one useful question for an actually ambiguous request.
        return {
            "ok": False, "completed": False, "verified": False, "needs_user": True,
            "error": f"Не удалось подтвердить воспроизведение в «{service_hint or 'текущем плеере'}».",
            "choices": [], "allow_free_text": True,
        }

    def control_music(self, request: str) -> dict[str, Any]:
        """Control the existing player without requiring it to be foreground first."""
        permission_error = self._desktop_permission_error()
        if permission_error is not None:
            return permission_error
        try:
            result = dict(self.operator.yandex_player_control(request) or {})
            return {"skill": "yandex_music", **result}
        except Exception as exc:
            self._log("SKILL_RECOVERY", skill="yandex_music_control", error=str(exc))

        clean = str(request or "").casefold().replace("ё", "е")
        if re.search(r"\b(?:лайк|дизлайк|не\s+нравится|нравится|перемот|промот|повтор|зацикл|перемеша|случайн)\w*", clean):
            return {
                "ok": False, "completed": False, "verified": False, "needs_user": True,
                "error": "Не вижу открытого плеера с такими кнопками. Открой нужный музыкальный сервис и повтори команду.",
            }
        action = "next" if re.search(r"\bследующ", clean) else (
            "previous" if re.search(r"\bпредыдущ", clean) else (
                "stop" if re.search(r"\b(?:останов|выключ)", clean) else "play_pause"
            )
        )
        sent = self.services.tools.execute("media_control", {"action": action})
        return {
            "ok": bool(sent.get("ok")), "completed": bool(sent.get("ok")),
            "verified": False, "skill": "current_player", "action": action, "result": sent,
            "error": "Команда отправлена, но состояние плеера не удалось прочитать" if sent.get("ok") else str(sent.get("error") or "Плеер не ответил"),
        }

    def send_telegram(
        self, recipient: str, text: str, *, expected_surface: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        permission_error = self._desktop_permission_error()
        if permission_error is not None:
            return permission_error
        try:
            return {
                "ok": True, "skill": "telegram",
                **self.operator.telegram_send(
                    recipient, text, expected_surface=dict(expected_surface or {}),
                ),
            }
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
        permission_error = self._desktop_permission_error()
        if permission_error is not None:
            return permission_error
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
