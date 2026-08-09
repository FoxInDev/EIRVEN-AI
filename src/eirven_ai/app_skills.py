from __future__ import annotations

import re
import time
from typing import Any

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

    def inspect_vscode(self, question: str = "Что за ошибка сейчас в VS Code и как её исправить?") -> dict[str, Any]:
        opened = self.open("VS Code")
        if not opened.get("ok"):
            return opened
        time.sleep(.2)
        return {"ok": True, "skill": "vscode", "answer": self.operator.observe(question), "verified": True}
