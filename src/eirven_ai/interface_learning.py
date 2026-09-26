from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from typing import Any


class InterfaceLearning:
    KEY = "interface_learning_v2"
    # Seeded from the owner's ERWI Action Recorder session (2026-08-08). These are
    # accessibility signatures, not brittle absolute coordinates. Live successes are
    # still learned into the DB and outrank these defaults through hit counts.
    BUILTIN_SEEDS = [
        {"app":"яндекс музыка", "goal":"yandex_my_wave", "name":"Моя волна", "control_type":"Hyperlink", "automation_id":"", "class_name":"NavbarDesktop_title", "hits":9},
        {"app":"яндекс музыка", "goal":"yandex_play", "name":"Воспроизведение", "control_type":"Button", "automation_id":"", "class_name":"VibePlayerControls_playButton", "hits":8},
        {"app":"яндекс музыка", "goal":"yandex_pause", "name":"Пауза", "control_type":"Button", "automation_id":"", "class_name":"VibePlayerControls_playButton_playing", "hits":8},
        {"app":"яндекс музыка", "goal":"yandex_next", "name":"Следующая песня", "control_type":"Button", "automation_id":"", "class_name":"", "hits":7},
        {"app":"яндекс музыка", "goal":"yandex_prev", "name":"Предыдущая песня", "control_type":"Button", "automation_id":"", "class_name":"", "hits":7},
        {"app":"яндекс музыка", "goal":"yandex_like", "name":"Нравится", "control_type":"Button", "automation_id":"", "class_name":"", "hits":7},
        {"app":"яндекс музыка", "goal":"yandex_dislike", "name":"Не нравится", "control_type":"Button", "automation_id":"", "class_name":"", "hits":7},
        {"app":"яндекс музыка", "goal":"yandex_search", "name":"Поиск", "control_type":"Hyperlink", "automation_id":"", "class_name":"", "hits":6},
        {"app":"яндекс музыка", "goal":"yandex_search_input", "name":"Что вы чувствуете или ищете?", "control_type":"Edit", "automation_id":"", "class_name":"", "hits":6},
        {"app":"telegram", "goal":"telegram_search", "name":"", "control_type":"Group", "automation_id":"", "class_name":"input-search-input", "hits":8},
        {"app":"telegram", "goal":"telegram_open_chat", "name":"", "control_type":"Hyperlink", "automation_id":"", "class_name":"chatlist-chat", "hits":8},
        {"app":"telegram", "goal":"telegram_compose", "name":"", "control_type":"Group", "automation_id":"", "class_name":"input-message-input", "hits":8},
        {"app":"telegram", "goal":"telegram_send", "name":"", "control_type":"Button", "automation_id":"", "class_name":"btn-send", "hits":8},
        {"app":"windows", "goal":"windows_search", "name":"Поле поиска", "control_type":"Edit", "automation_id":"SearchTextBox", "class_name":"RichEditBox", "hits":5},
    ]

    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def _norm(text: str) -> str:
        text = str(text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _load(self) -> list[dict[str, Any]]:
        raw = self.db.get_setting(self.KEY, [])
        return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    def remember(self, app: str, goal: str, element: dict[str, Any], *, success: bool = True) -> None:
        if not success:
            return
        app_n = self._norm(app)
        goal_n = self._norm(goal)
        if not app_n or not goal_n:
            return
        record = {
            "app": app_n,
            "goal": goal_n,
            "name": str(element.get("name") or "")[:160],
            "control_type": str(element.get("control_type") or "")[:80],
            "automation_id": str(element.get("automation_id") or "")[:120],
            "class_name": str(element.get("class_name") or "")[:120],
            "rect": element.get("rect") if isinstance(element.get("rect"), dict) else {},
            "at": time.time(),
            "hits": 1,
        }
        rows = self._load()
        for row in rows:
            if row.get("app") == app_n and row.get("goal") == goal_n and row.get("name") == record["name"] and row.get("control_type") == record["control_type"]:
                row.update(record)
                row["hits"] = int(row.get("hits") or 0) + 1
                break
        else:
            rows.append(record)
        rows.sort(key=lambda x: (int(x.get("hits") or 0), float(x.get("at") or 0)), reverse=True)
        self.db.set_setting(self.KEY, rows[:300])

    def candidates(self, app: str, goal: str, limit: int = 5) -> list[dict[str, Any]]:
        app_n = self._norm(app)
        goal_n = self._norm(goal)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in self._load() + [dict(x) for x in self.BUILTIN_SEEDS]:
            row_app = self._norm(row.get("app") or "")
            if row_app and app_n and row_app not in app_n and app_n not in row_app:
                continue
            score = SequenceMatcher(None, goal_n, self._norm(row.get("goal") or "")).ratio()
            if score < 0.48:
                continue
            score += min(0.22, int(row.get("hits") or 0) * 0.02)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in scored[: max(1, min(limit, 20))]]

    def clear(self) -> None:
        self.db.set_setting(self.KEY, [])
