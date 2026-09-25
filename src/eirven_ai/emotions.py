# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Эмоции Эрви.

Шесть эмоций из макета: радостная, удивлённая, думает, сосредоточена, смеётся,
спит. Каждая — отдельная картинка сферы в web/emotions/<имя>.png: у эмоций разный
не только рот и глаза, но и цвет, аура, поза (спящая расплывается и лежит).

Эмоции почти целиком задаёт СОСТОЯНИЕ Эрви, а не отдельный вызов модели: каждый
лишний вызов замедлял ответы, а это была ровно та жалоба, с которой мы начали.
    пришло сообщение              → думает
    выполняет действие            → сосредоточена
    задача подтверждена           → радостная, несколько секунд
    неожиданная ошибка            → удивлённая, несколько секунд
    долго без общения             → спит; любое обращение будит
    в покое                       → радостная
Смех и удивление в ответ на СЛОВА человека приходят из управляющего решения —
оно и так читает каждое сообщение, поле «настроение» к нему добавлено бесплатно.
"""

from __future__ import annotations

import threading
import time

EMOTIONS = ("joy", "surprise", "thinking", "focused", "laugh", "sleep")
RESTING = "joy"

# Сколько держать короткую реакцию, прежде чем вернуться в покой.
REACTION_SECONDS = {"joy": 5.0, "surprise": 4.0, "laugh": 6.0}
# Через сколько бездействия Эрви засыпает.
SLEEP_AFTER_SECONDS = 600.0

# Настроение из управляющего решения → эмоция сферы.
MOOD_TO_EMOTION = {"joy": "joy", "surprise": "surprise", "laugh": "laugh"}


class EmotionState:
    """Текущая эмоция Эрви. Безопасна для вызова из разных потоков."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._emotion = RESTING
        self._until = 0.0                 # до какого момента держать реакцию; 0 — без срока
        self._busy = 0                    # сколько ходов сейчас обрабатывается
        self._last_touch = time.monotonic()
        self._pending_mood: dict[str, str] = {}

    # ------------------------------------------------------------- события
    def touch(self) -> None:
        """Человек что-то сделал — Эрви просыпается."""
        with self._lock:
            self._last_touch = time.monotonic()

    def begin_turn(self) -> None:
        """Пришло сообщение: Эрви думает."""
        with self._lock:
            self._last_touch = time.monotonic()
            self._busy += 1
            self._emotion, self._until = "thinking", 0.0

    def working(self) -> None:
        """Началось действие на компьютере: Эрви сосредоточена."""
        with self._lock:
            if self._busy:
                self._emotion, self._until = "focused", 0.0

    def note_mood(self, conversation_id: str, mood: str) -> None:
        """Запомнить реакцию на слова человека — покажется, когда ход закончится."""
        emotion = MOOD_TO_EMOTION.get(str(mood or ""))
        if emotion:
            with self._lock:
                self._pending_mood[str(conversation_id or "")] = emotion

    def end_turn(self, conversation_id: str, *, completed: bool = False, failed: bool = False) -> None:
        """Ход закончился: показать реакцию, затем вернуться в покой."""
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._last_touch = time.monotonic()
            mood = self._pending_mood.pop(str(conversation_id or ""), "")
            if failed:
                reaction = "surprise"
            elif mood:
                reaction = mood
            elif completed:
                reaction = "joy"
            else:
                reaction = ""
            if self._busy:
                return                    # идёт ещё один ход — он и определяет эмоцию
            if reaction:
                self._emotion = reaction
                self._until = time.monotonic() + REACTION_SECONDS.get(reaction, 5.0)
            else:
                self._emotion, self._until = RESTING, 0.0

    # ------------------------------------------------------------- чтение
    def current(self) -> str:
        """Что показывать прямо сейчас."""
        with self._lock:
            now = time.monotonic()
            if self._busy:
                return self._emotion
            if self._until and now >= self._until:
                self._emotion, self._until = RESTING, 0.0
            if not self._until and now - self._last_touch >= SLEEP_AFTER_SECONDS:
                return "sleep"
            return self._emotion

    def snapshot(self) -> dict[str, object]:
        return {"emotion": self.current(), "emotions": list(EMOTIONS)}
