from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from .trace import log_event


class ProactiveObserver:
    """Always-on, low-cost desktop awareness for useful interventions.

    EIRVEN observes foreground ownership continuously and samples Windows UI Automation
    text when context makes it useful. It deliberately does *not* archive a stream of
    screenshots: pixels are requested by the desktop/vision agent only when a task needs
    them. This keeps the assistant responsive while still noticing media overuse, hung
    applications and common visible error states.
    """

    MEDIA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("youtube", re.compile(r"youtube|ютуб|youtu\.be", re.I)),
        ("twitch", re.compile(r"twitch", re.I)),
        ("vk_video", re.compile(r"vk\s*video|вк\s*видео", re.I)),
        ("kinopoisk", re.compile(r"кинопоиск|kinopoisk", re.I)),
        ("netflix", re.compile(r"netflix", re.I)),
        ("ivi", re.compile(r"\bivi\b", re.I)),
        ("okko", re.compile(r"\bokko\b", re.I)),
        ("vlc", re.compile(r"\bvlc\b", re.I)),
        ("mpc", re.compile(r"mpc-hc|media player classic", re.I)),
        ("generic_video", re.compile(r"\b(video|видео|film|movie|сериал)\b", re.I)),
    )
    CODE_TITLE = re.compile(r"visual studio code|vs\s*code|vscode", re.I)
    HUNG_TITLE = re.compile(r"не отвечает|not responding", re.I)
    ERROR_TEXT = re.compile(
        r"\b(?:traceback|exception|error|failed|failure|fatal|ошибк\w*|сбой|не удалось|tests? failed|problems?\s+[1-9]\d*)\b",
        re.I,
    )
    SENSITIVE_CONTEXT = re.compile(
        r"\b(?:password|парол\w*|2fa|одноразов\w*\s+код|cvv|cvc|номер\s+карт|"
        r"internet\s*bank|онлайн\s*банк|оплат\w*|checkout|private\s+key|seed\s+phrase|"
        r"api[_ -]?key|access[_ -]?token|паспорт\w*|госуслуг\w*)\b",
        re.I,
    )

    def __init__(self, db: Any, voice_daemon_provider, tools: Any, services_provider=None):
        self.db = db
        self.voice_daemon_provider = voice_daemon_provider
        self.tools = tools
        self.services_provider = services_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._media_key = ""
        self._media_since = 0.0
        self._last_intervention = 0.0
        self._last_trace_bucket = -1
        self._last_window_signature = ""
        self._last_context_scan = 0.0
        self._last_suggestion = 0.0
        self._hung_handle = 0
        self._hung_since = 0.0
        self._last_hung_offer = 0.0
        self._code_since = 0.0
        self._last_code_offer = 0.0
        self._window_since = 0.0
        self._last_comment_signature = ""
        self._comment_cooldown_until = 0.0

    def _trace(self, event: str, **payload: Any) -> None:
        try:
            log_event(self.tools.settings.root_dir, event, **payload)
        except Exception:
            pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-proactive")
        self._thread.start()
        self._trace("PROACTIVE_START")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        self._thread = None

    @staticmethod
    def _foreground() -> dict[str, Any]:
        if os.name != "nt":
            return {"handle": 0, "title": "", "class_name": ""}
        try:
            import ctypes
            hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            if not hwnd:
                return {"handle": 0, "title": "", "class_name": ""}
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, cls, 256)
            return {"handle": hwnd, "title": buf.value.strip(), "class_name": cls.value.strip()}
        except Exception:
            return {"handle": 0, "title": "", "class_name": ""}

    @staticmethod
    def _is_hung(hwnd: int) -> bool:
        if os.name != "nt" or not hwnd:
            return False
        try:
            import ctypes
            return bool(ctypes.windll.user32.IsHungAppWindow(int(hwnd)))
        except Exception:
            return False

    @classmethod
    def _media_kind(cls, title: str) -> str:
        for key, pattern in cls.MEDIA_PATTERNS:
            if pattern.search(title or ""):
                return key
        return ""

    def _threshold_seconds(self) -> int:
        try:
            minutes = int(self.db.get_setting("proactive_media_minutes", 75) or 75)
        except Exception:
            minutes = 75
        return max(1, min(240, minutes)) * 60

    def _pause_media(self) -> bool:
        try:
            services = self.services_provider() if callable(self.services_provider) else None
            workflow = getattr(services, "universal_workflow", None)
            ensure = getattr(workflow, "ensure_media_goal", None)
            if callable(ensure):
                state = ensure("Поставь текущее видео на паузу", allow_implicit=True)
                if isinstance(state, dict) and state.get("verified"):
                    return True
        except Exception:
            pass
        result = self.tools.execute("media_control", {"action": "play_pause"})
        if result.get("ok"):
            return True
        try:
            import pyautogui  # type: ignore
            pyautogui.press("playpause")
            return True
        except Exception:
            return False

    def _say(self, text: str, emotion: str = "natural") -> None:
        clean = re.sub(r"\s+", " ", str(text or "")).strip()[:190]
        if not clean:
            return
        try:
            self.db.set_setting(
                "proactive_last_comment",
                {"text": clean, "emotion": emotion, "expires_at": time.time() + 9.0},
            )
        except Exception:
            pass
        daemon = self.voice_daemon_provider()
        if daemon is None:
            return
        try:
            daemon.say(clean, emotion=emotion)
        except Exception as exc:
            self._trace("PROACTIVE_SPEAK_ERROR", error=str(exc)[:300])

    def _contextual_comment(self, title: str, blob: str, signature: str, now: float) -> bool:
        """Generate a rare, grounded one-sentence intervention from visible UIA text.

        No screenshot or visible-text history is stored.  Sensitive surfaces are rejected
        before the local model sees them, and silence is the default decision.
        """

        if now < self._comment_cooldown_until or now - self._window_since < 7.0:
            return False
        if not blob or len(blob) < 24 or self.SENSITIVE_CONTEXT.search(f"{title}\n{blob}"):
            return False
        try:
            services = self.services_provider() if callable(self.services_provider) else None
            if services is None:
                return False
            cognition = getattr(services, "cognition", None)
            if cognition is not None:
                allowed, reason = cognition.proactivity_allowed(title, blob)
                if not allowed:
                    self._trace("PROACTIVE_PRIVACY_SUPPRESS", reason=reason, title=title[:120])
                    self._comment_cooldown_until = now + 90.0
                    return False
            daemon = self.voice_daemon_provider()
            voice_status = daemon.status() if daemon is not None else {}
            if bool(voice_status.get("speaking")) or str(voice_status.get("state") or "") in {"hearing", "recognizing", "thinking"}:
                return False
            runtime = getattr(services, "runtime", None)
            runtime_status = runtime.status() if runtime is not None else {}
            if bool(runtime_status.get("cancellable")):
                return False
            gateway = getattr(services, "gateway", None)
            settings = getattr(services, "settings", None)
            if gateway is None or settings is None:
                return False
            style = getattr(services, "style", None)
            style_prompt = style.get().prompt() if style is not None else ""
            schema = {
                "type": "object",
                "properties": {
                    "speak": {"type": "boolean"},
                    "text": {"type": "string"},
                    "emotion": {"type": "string", "enum": [
                        "natural", "amused", "empathetic", "curious", "concerned", "warm", "calm"
                    ]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["speak", "text", "emotion", "confidence", "reason"],
            }
            prompt = (
                "Ты проактивная живая Эйрвен. Реши, стоит ли СЕЙЧАС самой произнести одну короткую реплику по реально видимому контексту. "
                "По умолчанию speak=false. speak=true только если замечание конкретное и полезное: виден более лёгкий путь, явная ошибка/зависание, "
                "уместная мягкая шутка, риск, или человеку явно нужна поддержка. Не пересказывай экран, не комментируй каждое действие, не оценивай и не оскорбляй людей, "
                "не выдумывай скрытое и не давай команд без причины. Текст — максимум одно естественное предложение, без префиксов и канцелярита.\n"
                f"СТИЛЬ:\n{style_prompt[:1200]}\nОКНО: {title[:180]}\nВИДИМЫЙ UI:\n{blob[:5200]}"
            )
            data = gateway.json(
                [{"role": "user", "content": prompt}],
                model=str(settings.fast_model), temperature=0.25, schema=schema,
                num_ctx=1300, num_predict=120, keep_alive="45s", timeout_seconds=4.2,
            )
            confidence = float(data.get("confidence") or 0) if isinstance(data, dict) else 0.0
            text = re.sub(r"\s+", " ", str((data or {}).get("text") or "")).strip()[:190]
            emotion = str((data or {}).get("emotion") or "natural")
            if not bool((data or {}).get("speak")) or confidence < 0.74 or not text:
                # A quiet decision still gets a shorter cooldown so the model is not polled
                # on every five-second UI sample.
                self._comment_cooldown_until = now + 45.0
                return False
            self._last_comment_signature = signature
            jitter = 30 + (sum(ord(ch) for ch in signature) % 91)
            self._comment_cooldown_until = now + max(90, int(self.db.get_setting("proactive_comment_seconds", 150) or 150)) + jitter
            self._trace("PROACTIVE_CONTEXT_COMMENT", title=title[:180], emotion=emotion, confidence=round(confidence, 3), reason=str(data.get("reason") or "")[:240])
            self._say(text, emotion)
            try:
                if cognition is not None:
                    cognition.update_mood(emotion, min(1.0, confidence))
            except Exception:
                pass
            return True
        except Exception as exc:
            self._comment_cooldown_until = now + 60.0
            self._trace("PROACTIVE_CONTEXT_MODEL_ERROR", error=str(exc)[:300])
            return False

    @staticmethod
    def _friendly_app_name(title: str) -> str:
        low = str(title or "").casefold()
        if "telegram" in low or "телеграм" in low:
            return "Telegram"
        if "visual studio code" in low or "vscode" in low:
            return "VS Code"
        if "яндекс музыка" in low or "yandex music" in low:
            return "Яндекс Музыка"
        return str(title or "приложение").split(" - ")[-1][:80]

    def _sample_visible_context(self, info: dict[str, Any], now: float) -> None:
        if now - self._last_context_scan < 5.0:
            return
        self._last_context_scan = now
        if not bool(self.db.get_setting("desktop_comments_enabled", True)):
            return
        title = str(info.get("title") or "")
        handle = int(info.get("handle") or 0)
        if not title or not handle:
            return
        try:
            services = self.services_provider() if callable(self.services_provider) else None
            cognition = getattr(services, "cognition", None) if services is not None else None
            allowed, reason = cognition.proactivity_allowed(title, "") if cognition is not None else (True, "")
            if not allowed:
                self._trace("PROACTIVE_PRIVACY_SUPPRESS", reason=reason, title=title[:120])
                return
            suggestion = cognition.next_skill_suggestion() if cognition is not None else None
            if suggestion and now >= self._comment_cooldown_until:
                goal = re.sub(r"\s+", " ", str(suggestion.get("goal") or "эту задачу")).strip()[:110]
                self._comment_cooldown_until = now + 180.0
                self._say(f"Мы уже несколько раз делали «{goal}». Хочешь, сохраню это как навык?", "curious")
                return
        except Exception:
            pass
        signature = f"{handle}|{title}"
        if signature != self._last_comment_signature and signature != self._last_window_signature:
            self._window_since = now

        # A hung GUI is a concrete reason to interrupt. Never restart it silently: the
        # owner may have unsaved state, so EIRVEN offers the reversible action first.
        hung = self.HUNG_TITLE.search(title) is not None or self._is_hung(handle)
        if hung:
            if self._hung_handle != handle:
                self._hung_handle, self._hung_since = handle, now
            elif now - self._hung_since >= 8.0 and now - self._last_hung_offer >= 180.0:
                self._last_hung_offer = now
                app = self._friendly_app_name(title)
                self._trace("PROACTIVE_HUNG_OFFER", app=app, title=title[:180], handle=handle)
                self._say(f"Похоже, {app} завис. Хочешь, аккуратно перезапущу?", "concerned")
            return
        self._hung_handle, self._hung_since = 0, 0.0

        try:
            rows = self.tools.execute("window_elements", {"title_contains": title, "handle": handle, "max_elements": 240})
            elements = list(rows.get("result") or []) if rows.get("ok") else []
            visible_names = []
            for row in elements:
                if not isinstance(row, dict) or not row.get("visible", True):
                    continue
                name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
                if 2 <= len(name) <= 500:
                    visible_names.append(name)
            blob = "\n".join(dict.fromkeys(visible_names))[:16000]
        except Exception:
            blob = ""

        # In VS Code, UIA exposes Problems/terminal/status text cheaply. If an obvious
        # failure appears, offer the same full workspace repair lane used by "где баг?".
        if self.CODE_TITLE.search(title):
            if not self._code_since:
                self._code_since = now
            if blob and self.ERROR_TEXT.search(blob) and now - self._last_code_offer >= 480.0:
                self._last_code_offer = now
                self._trace("PROACTIVE_CODE_ERROR_OFFER", title=title[:180])
                self._say("Вижу признаки ошибки в VS Code. Могу сама найти баг, исправить и прогнать проверку.", "concerned")
                return
            # A low-frequency spontaneous productivity suggestion while coding.
            if now - self._code_since >= 25 * 60 and now - self._last_code_offer >= 25 * 60:
                self._last_code_offer = now
                self._say("Ты давно в проекте. Если хочешь, могу сама прогнать тесты и проверить, нет ли скрытых ошибок.", "warm")
                return
        else:
            self._code_since = 0.0
        self._contextual_comment(title, blob, signature, now)

    def _run(self) -> None:
        # One-second foreground sampling is cheap enough to feel immediate without
        # recording screenshots or generating background model traffic.
        while not self._stop.wait(1.0):
            if not bool(self.db.get_setting("proactive_enabled", True)):
                if self._media_key:
                    self._trace("PROACTIVE_RESET", reason="disabled", media=self._media_key)
                self._media_key = ""
                self._media_since = 0.0
                continue

            info = self._foreground()
            title = str(info.get("title") or "")
            signature = f"{info.get('handle', 0)}|{title}|{info.get('class_name', '')}"
            if signature != self._last_window_signature:
                self._last_window_signature = signature
                self._window_since = time.monotonic()
                self._trace("PROACTIVE_FOREGROUND", title=title[:180], handle=int(info.get("handle") or 0), class_name=str(info.get("class_name") or "")[:120])

            now = time.monotonic()
            self._sample_visible_context(info, now)
            media_key = self._media_kind(title)

            if not media_key:
                if self._media_key:
                    self._trace("PROACTIVE_RESET", reason="left_media", media=self._media_key, title=title[:180])
                self._media_key = ""
                self._media_since = 0.0
                self._last_trace_bucket = -1
                continue

            if media_key != self._media_key:
                self._media_key = media_key
                self._media_since = now
                self._last_trace_bucket = -1
                self._trace("PROACTIVE_MEDIA_BEGIN", media=media_key, title=title[:180], threshold_seconds=self._threshold_seconds())
                continue

            elapsed = max(0.0, now - self._media_since)
            threshold = self._threshold_seconds()
            bucket = int(elapsed) // 15
            if bucket != self._last_trace_bucket:
                self._last_trace_bucket = bucket
                self._trace("PROACTIVE_MEDIA_TICK", media=media_key, elapsed_seconds=round(elapsed, 1), threshold_seconds=threshold, title=title[:180])

            if elapsed < threshold:
                continue
            if self._last_intervention and self._last_intervention >= self._media_since:
                continue

            paused = self._pause_media()
            self._last_intervention = now
            self._trace("PROACTIVE_INTERVENTION", media=media_key, elapsed_seconds=round(elapsed, 1), threshold_seconds=threshold, pause_sent=paused, title=title[:180])
            self._say("Ты уже дошёл до лимита отдыха. Видео поставила на паузу. Продолжить отдых или возвращаемся к работе?", "warm")
