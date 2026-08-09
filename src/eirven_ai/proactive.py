from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from .trace import log_event


class ProactiveObserver:
    """Low-cost local activity observer.

    The configured value is literal minutes. If the owner selects 1 minute, intervention
    is allowed after one uninterrupted minute — there is no hidden 20-minute floor.

    Only inexpensive foreground metadata is sampled. The observer never records screen
    pixels. For media it tracks a *media family* (YouTube/Twitch/etc.) rather than an exact
    title so a changing video title does not reset the timer.
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

    def __init__(self, db: Any, voice_daemon_provider, tools: Any):
        self.db = db
        self.voice_daemon_provider = voice_daemon_provider
        self.tools = tools
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._media_key = ""
        self._media_since = 0.0
        self._last_intervention = 0.0
        self._last_trace_bucket = -1

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
    def _foreground_title() -> str:
        if os.name != "nt":
            return ""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value.strip()
        except Exception:
            return ""

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
        result = self.tools.execute("media_control", {"action": "play_pause"})
        if result.get("ok"):
            return True
        try:
            import pyautogui  # type: ignore
            pyautogui.press("playpause")
            return True
        except Exception:
            return False

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            if not bool(self.db.get_setting("proactive_enabled", True)):
                if self._media_key:
                    self._trace("PROACTIVE_RESET", reason="disabled", media=self._media_key)
                self._media_key = ""
                self._media_since = 0.0
                continue

            title = self._foreground_title()
            media_key = self._media_kind(title)
            now = time.monotonic()

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
                self._trace(
                    "PROACTIVE_MEDIA_BEGIN",
                    media=media_key,
                    title=title[:180],
                    threshold_seconds=self._threshold_seconds(),
                )
                continue

            elapsed = max(0.0, now - self._media_since)
            threshold = self._threshold_seconds()
            bucket = int(elapsed) // 15
            if bucket != self._last_trace_bucket:
                self._last_trace_bucket = bucket
                self._trace(
                    "PROACTIVE_MEDIA_TICK",
                    media=media_key,
                    elapsed_seconds=round(elapsed, 1),
                    threshold_seconds=threshold,
                    title=title[:180],
                )

            if elapsed < threshold:
                continue
            if self._last_intervention and self._last_intervention >= self._media_since:
                continue

            paused = self._pause_media()
            self._last_intervention = now
            self._trace(
                "PROACTIVE_INTERVENTION",
                media=media_key,
                elapsed_seconds=round(elapsed, 1),
                threshold_seconds=threshold,
                pause_sent=paused,
                title=title[:180],
            )
            daemon = self.voice_daemon_provider()
            if daemon is not None:
                try:
                    daemon.say(
                        "Ты уже дошёл до лимита отдыха. Видео поставила на паузу. "
                        "Продолжить отдых или возвращаемся к работе?"
                    )
                except Exception as exc:
                    self._trace("PROACTIVE_SPEAK_ERROR", error=str(exc)[:300])
