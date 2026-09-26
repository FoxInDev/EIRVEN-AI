"""Приглушение звука других программ, пока человек говорит с Эрви.

Когда громко играют музыка или видео, микрофон записывает твой голос вперемешку с
ними, и распознаватель слышит «Эрви» искажённым — поэтому она тебя «не слышала».
Голосовые помощники решают это одинаково: как только микрофон слышит, что человек
начал говорить при играющем медиа, звук других программ ненадолго приглушается,
голос становится в записи главным, а потом громкость возвращается.

Правила, которых модуль держится жёстко:
  — громкость СВОЕГО процесса (голос Эрви) не трогается;
  — возвращается ровно та громкость, что была; а если человек за это время сам
    поменял громкость программы, его решение не перебивается;
  — музыка не может остаться тихой: громкость возвращается, как только Эрви
    закончила слушать и отвечать, и в любом случае не позже чем через 20 секунд;
  — все обращения к звуковой системе Windows идут в одном собственном потоке,
    как и чтение громкости в голосе: неверная работа с COM из разных потоков
    приводила к падениям процесса;
  — любая неудача просто выключает приглушение и никогда не задевает голос.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Callable

DUCK_FACTOR = 0.25          # до скольких процентов приглушать
RELEASE_QUIET_SECONDS = 0.9  # сколько секунд тишины после ответа — и вернуть громкость
MAX_DUCK_SECONDS = 20.0      # дольше — никогда, что бы ни случилось


class AudioDucker:
    def __init__(self, can_release: Callable[[], bool], log: Callable[..., None] | None = None) -> None:
        self._can_release = can_release
        self._log = log or (lambda *a, **k: None)
        self._requests: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._disabled = os.name != "nt"
        # Сами регуляторы громкости, взятые при приглушении, и (была, стала).
        # Сеансы заново не ищем: номера сдвигаются, когда программы открывают
        # или закрывают звук, и громкость могла бы не вернуться.
        self._ducked: list[tuple[Any, float, float]] = []
        self._ducked_at = 0.0
        self._last_request = 0.0
        self._quiet_since = 0.0

    # ------------------------------------------------------------ снаружи
    def request(self) -> None:
        """Человек начал говорить при играющем медиа — приглушить. Не блокирует."""
        if self._disabled:
            return
        self._last_request = time.monotonic()
        self._ensure_thread()
        self._requests.put("duck")

    def ducked(self) -> bool:
        return bool(self._ducked)

    # ------------------------------------------------------------ поток
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="eirven-duck")
            self._thread.start()

    def _loop(self) -> None:
        com = None
        try:
            import comtypes  # type: ignore
            comtypes.CoInitialize()
            com = comtypes
        except Exception as exc:
            self._disabled = True
            self._log("VOICE_DUCK_UNAVAILABLE", error=str(exc)[:160])
            return
        try:
            while True:
                try:
                    cmd = self._requests.get(timeout=0.2)
                except queue.Empty:
                    cmd = ""
                if cmd == "duck" and not self._ducked:
                    self._duck()
                if self._ducked:
                    self._maybe_release()
        finally:
            if self._ducked:
                self._restore()
            try:
                com.CoUninitialize()
            except Exception:
                pass

    def _sessions(self) -> list[Any]:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        return list(AudioUtilities.GetAllSessions())

    def _duck(self) -> None:
        own = os.getpid()
        changed = 0
        try:
            sessions = self._sessions()
            for s in sessions:
                pid = int(getattr(s, "ProcessId", 0) or 0)
                if pid in (0, own):            # системные звуки и свой голос не трогаем
                    continue
                vol = getattr(s, "SimpleAudioVolume", None)
                if vol is None:
                    continue
                before = float(vol.GetMasterVolume())
                if before <= 0.05:
                    continue
                after = max(0.02, before * DUCK_FACTOR)
                vol.SetMasterVolume(after, None)
                self._ducked.append((vol, before, after))
                changed += 1
            sessions = None
        except Exception as exc:
            self._log("VOICE_DUCK_FAILED", error=str(exc)[:160])
            self._restore()
            return
        if changed:
            self._ducked_at = time.monotonic()
            self._quiet_since = 0.0
            self._log("VOICE_DUCKED", sessions=changed)

    def _maybe_release(self) -> None:
        now = time.monotonic()
        if now - self._ducked_at >= MAX_DUCK_SECONDS:
            self._restore(reason="предел 20 с")
            return
        try:
            quiet = bool(self._can_release()) and now - self._last_request > 1.0
        except Exception:
            quiet = True
        if not quiet:
            self._quiet_since = 0.0
            return
        if not self._quiet_since:
            self._quiet_since = now
        elif now - self._quiet_since >= RELEASE_QUIET_SECONDS:
            self._restore(reason="разговор закончен")

    def _restore(self, reason: str = "") -> None:
        restored = 0
        try:
            for vol, before, after in self._ducked:
                try:
                    current = float(vol.GetMasterVolume())
                    # Человек сам поменял громкость, пока мы её приглушали, — не перебиваем.
                    if abs(current - after) <= 0.03:
                        vol.SetMasterVolume(before, None)
                        restored += 1
                except Exception:
                    continue                      # программа закрыла звук — вернуть нечего
        finally:
            self._ducked = []                     # регуляторы освобождаются в этом же потоке
            self._ducked_at = 0.0
            self._quiet_since = 0.0
        self._log("VOICE_UNDUCKED", sessions=restored, reason=reason)
