from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


class AgentCognition:
    """Privacy-bounded persistent learning, mood, checkpoints and learned skills.

    The store intentionally keeps *routes*, not message bodies, passwords, screenshots or
    clipboard contents.  It gives the desktop loop useful experience across restarts while
    keeping personal content out of the strategy journal.
    """

    EXPERIENCE_KEY = "agent_experience_v1"
    SKILLS_KEY = "learned_skills_v1"
    UNDO_KEY = "undo_stack_v1"
    OBSERVATION_KEY = "screen_state_history_v1"
    AUTH_KEY = "auth_checkpoint_v1"
    MOOD_KEY = "affective_continuity_v1"

    _SENSITIVE = re.compile(
        r"\b(?:password|парол\w*|2fa|captcha|cvv|cvc|seed\s+phrase|private\s+key|"
        r"api[_ -]?key|access[_ -]?token|одноразов\w*\s+код|номер\s+карт)\b",
        re.I,
    )
    _PRIVATE_SURFACE = re.compile(
        r"\b(?:password|парол\w*|2fa|captcha|cvv|cvc|банк|bank|checkout|оплат\w*|"
        r"демонстрац\w*\s+экрана|screen\s*share|presenting|трансляц\w*|"
        r"zoom\s+meeting|teams\s+meeting|google\s+meet|discord\s+call|звонок)\b",
        re.I,
    )

    def __init__(self, db: Any, data_dir: Path):
        self.db = db
        self.data_dir = Path(data_dir).resolve()
        self.undo_dir = (self.data_dir / "undo").resolve()
        self.undo_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _norm(value: Any) -> str:
        text = str(value or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _redact_goal(cls, goal: str) -> str:
        value = re.sub(r"[«\"'].*?[»\"']", "<текст>", str(goal or ""), flags=re.S)
        value = re.sub(r"https?://\S+", "<ссылка>", value, flags=re.I)
        value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "<адрес>", value, flags=re.I)
        value = re.sub(r"\b(?:\+?\d[\d ()-]{7,}\d)\b", "<номер>", value)
        value = re.sub(
            r"((?:напиши|отправь|введи|вбей|набери)\w*\s+[^:,.]{1,80}[:,-]\s*).+$",
            r"\1<текст>", value, flags=re.I | re.S,
        )
        if cls._SENSITIVE.search(value):
            return "<чувствительная задача>"
        return re.sub(r"\s+", " ", value).strip()[:360]

    @classmethod
    def goal_key(cls, goal: str, surface: str = "") -> str:
        safe = cls._norm(cls._redact_goal(goal))
        app = cls.surface_key(surface)
        return hashlib.sha1(f"{safe}|{app}".encode("utf-8")).hexdigest()[:20]

    @classmethod
    def surface_key(cls, surface: str) -> str:
        low = cls._norm(surface)
        for key, marks in (
            ("telegram", ("telegram", "телеграм")),
            ("yandex_music", ("яндекс музыка", "yandex music")),
            ("youtube", ("youtube", "ютуб")),
            ("vscode", ("visual studio code", "vscode", "vs code")),
            ("browser", ("browser", "браузер", "chrome", "edge", "firefox", "opera", "samsung")),
            ("explorer", ("explorer", "проводник")),
        ):
            if any(mark in low for mark in marks):
                return key
        return "desktop" if low else "unknown"

    def _get_rows(self, key: str, limit: int) -> list[dict[str, Any]]:
        raw = self.db.get_setting(key, [])
        rows = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
        return rows[-max(1, limit):]

    @staticmethod
    def _safe_steps(steps: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for step in (steps or [])[-12:]:
            if not isinstance(step, dict):
                continue
            row: dict[str, str] = {}
            for key in ("action", "reason", "target", "commit_kind", "evidence"):
                value = str(step.get(key) or "").strip()
                if value:
                    # Text entered by the owner is never a strategy feature.
                    row[key] = re.sub(r"[«\"'].*?[»\"']", "<текст>", value)[:180]
            if row:
                output.append(row)
        return output

    def record_outcome(
        self, goal: str, surface: str, *, strategy: int = 0, ok: bool,
        verified: bool = False, error: str = "", steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        template = self._redact_goal(goal)
        key = self.goal_key(goal, surface)
        rows = self._get_rows(self.EXPERIENCE_KEY, 240)
        row = next((item for item in rows if item.get("key") == key), None)
        if row is None:
            row = {
                "key": key, "goal": template, "surface": self.surface_key(surface),
                "successes": 0, "failures": 0, "strategies": {}, "created_at": time.time(),
            }
            rows.append(row)
        row["updated_at"] = time.time()
        strategies = dict(row.get("strategies") or {})
        strategy_key = str(max(0, int(strategy)))
        strategy_row = dict(strategies.get(strategy_key) or {"successes": 0, "failures": 0})
        if ok and verified:
            row["successes"] = int(row.get("successes") or 0) + 1
            strategy_row["successes"] = int(strategy_row.get("successes") or 0) + 1
            row["last_success_steps"] = self._safe_steps(steps)
            row["last_error"] = ""
        else:
            row["failures"] = int(row.get("failures") or 0) + 1
            strategy_row["failures"] = int(strategy_row.get("failures") or 0) + 1
            row["last_error"] = re.sub(r"[«\"'].*?[»\"']", "<текст>", str(error or ""))[:240]
        strategies[strategy_key] = strategy_row
        row["strategies"] = strategies
        rows.sort(key=lambda item: float(item.get("updated_at") or 0))
        self.db.set_setting(self.EXPERIENCE_KEY, rows[-240:])
        suggestion = bool(int(row.get("successes") or 0) >= 3 and not row.get("saved_as_skill"))
        return {"key": key, "skill_suggestion": suggestion, "successes": int(row.get("successes") or 0)}

    def guidance(self, goal: str, surface: str) -> str:
        key = self.goal_key(goal, surface)
        row = next((item for item in reversed(self._get_rows(self.EXPERIENCE_KEY, 240)) if item.get("key") == key), None)
        if not row:
            return "Ранее проверенного маршрута для этой поверхности нет."
        strategies = dict(row.get("strategies") or {})
        ranked = sorted(
            strategies.items(),
            key=lambda item: (int((item[1] or {}).get("successes") or 0), -int((item[1] or {}).get("failures") or 0)),
            reverse=True,
        )
        best = ranked[0][0] if ranked and int((ranked[0][1] or {}).get("successes") or 0) else ""
        failed = [name for name, stats in ranked if int((stats or {}).get("failures") or 0) > int((stats or {}).get("successes") or 0)]
        parts = []
        if best:
            parts.append(f"Раньше срабатывало поколение стратегии {best}; используй его признаки, только если они снова видимы.")
        if failed:
            parts.append("Не повторяй без нового основания неудачные поколения: " + ", ".join(failed[:4]) + ".")
        if row.get("last_error"):
            parts.append("Последняя ошибка маршрута: " + str(row.get("last_error"))[:180])
        return " ".join(parts) or "Есть история, но подтверждённого маршрута пока нет."

    def record_observation(self, *, title: str, handle: int | None, fingerprint: str, browser: bool) -> None:
        rows = self._get_rows(self.OBSERVATION_KEY, 39)
        item = {
            "at": time.time(), "surface": self.surface_key(title), "handle": int(handle or 0),
            "fingerprint": str(fingerprint or "")[:24], "browser": bool(browser),
        }
        if rows and rows[-1].get("fingerprint") == item["fingerprint"] and rows[-1].get("handle") == item["handle"]:
            rows[-1]["at"] = item["at"]
        else:
            rows.append(item)
        self.db.set_setting(self.OBSERVATION_KEY, rows[-40:])

    def capture_file(self, target: Path, *, label: str = "Изменение файла") -> str:
        path = Path(target).expanduser().resolve()
        if path.exists() and not path.is_file():
            raise ValueError("Откат поддерживает только отдельные файлы")
        if path.parent == path or str(path) in {"/", "\\"}:
            raise ValueError("Слишком широкий путь для контрольной точки")
        checkpoint_id = uuid.uuid4().hex
        existed = path.is_file()
        backup = self.undo_dir / f"{checkpoint_id}.bak"
        if existed:
            if path.stat().st_size > 24_000_000:
                return ""
            shutil.copy2(path, backup)
        rows = self._get_rows(self.UNDO_KEY, 39)
        rows.append({
            "id": checkpoint_id, "target": str(path), "backup": str(backup) if existed else "",
            "existed": existed, "label": str(label or "Изменение файла")[:160],
            "at": time.time(), "undone": False,
        })
        self.db.set_setting(self.UNDO_KEY, rows[-40:])
        return checkpoint_id

    def undo_last(self) -> dict[str, Any]:
        rows = self._get_rows(self.UNDO_KEY, 40)
        index = next((i for i in range(len(rows) - 1, -1, -1) if not rows[i].get("undone")), None)
        if index is None:
            return {"ok": False, "error": "Нет обратимого изменения для отката"}
        item = rows[index]
        target = Path(str(item.get("target") or "")).expanduser().resolve()
        backup_raw = str(item.get("backup") or "")
        if not target.name or target.parent == target:
            return {"ok": False, "error": "Контрольная точка повреждена"}
        if bool(item.get("existed")):
            backup = Path(backup_raw).resolve()
            if self.undo_dir != backup.parent or not backup.is_file():
                return {"ok": False, "error": "Резервная копия контрольной точки не найдена"}
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.is_file():
            target.unlink()
        item["undone"] = True
        item["undone_at"] = time.time()
        rows[index] = item
        self.db.set_setting(self.UNDO_KEY, rows[-40:])
        return {"ok": True, "path": str(target), "label": str(item.get("label") or "Изменение файла")}

    def save_last_as_skill(self, name: str = "") -> dict[str, Any]:
        experience = next(
            (row for row in reversed(self._get_rows(self.EXPERIENCE_KEY, 240)) if row.get("last_success_steps")),
            None,
        )
        if not experience:
            return {"ok": False, "error": "Нет подтверждённого успешного маршрута"}
        title = re.sub(r"\s+", " ", str(name or experience.get("goal") or "Новый навык")).strip()[:90]
        skill_id = hashlib.sha1(f"{title}|{experience.get('key')}".encode("utf-8")).hexdigest()[:16]
        skills = self._get_rows(self.SKILLS_KEY, 79)
        payload = {
            "id": skill_id, "name": title, "goal_key": experience.get("key"),
            "surface": experience.get("surface"), "steps": list(experience.get("last_success_steps") or []),
            "updated_at": time.time(), "enabled": True,
        }
        old = next((i for i, row in enumerate(skills) if row.get("id") == skill_id), None)
        if old is None:
            skills.append(payload)
        else:
            skills[old] = payload
        experience["saved_as_skill"] = skill_id
        all_experience = self._get_rows(self.EXPERIENCE_KEY, 240)
        for i, row in enumerate(all_experience):
            if row.get("key") == experience.get("key"):
                all_experience[i] = experience
        self.db.set_setting(self.EXPERIENCE_KEY, all_experience[-240:])
        self.db.set_setting(self.SKILLS_KEY, skills[-80:])
        return {"ok": True, "id": skill_id, "name": title, "steps": len(payload["steps"])}

    def skills(self) -> list[dict[str, Any]]:
        return self._get_rows(self.SKILLS_KEY, 80)

    def next_skill_suggestion(self, cooldown_seconds: float = 86_400.0) -> dict[str, Any] | None:
        rows = self._get_rows(self.EXPERIENCE_KEY, 240)
        now = time.time()
        candidates = [
            row for row in rows
            if int(row.get("successes") or 0) >= 3
            and not row.get("saved_as_skill")
            and now - float(row.get("suggested_at") or 0.0) >= max(300.0, float(cooldown_seconds))
        ]
        if not candidates:
            return None
        candidate = max(candidates, key=lambda row: (int(row.get("successes") or 0), float(row.get("updated_at") or 0.0)))
        candidate["suggested_at"] = now
        for index, row in enumerate(rows):
            if row.get("key") == candidate.get("key"):
                rows[index] = candidate
                break
        self.db.set_setting(self.EXPERIENCE_KEY, rows[-240:])
        return {"goal": str(candidate.get("goal") or "эта задача"), "successes": int(candidate.get("successes") or 0)}

    def save_auth_checkpoint(self, *, goal: str, surface: str, conversation_id: str = "") -> None:
        self.db.set_setting(self.AUTH_KEY, {
            "goal": self._redact_goal(goal), "surface": self.surface_key(surface),
            "conversation_id": str(conversation_id or "")[:80], "at": time.time(),
            "contains_credentials": False,
        })

    def clear_auth_checkpoint(self) -> None:
        self.db.set_setting(self.AUTH_KEY, {})

    def proactivity_allowed(self, title: str, visible_text: str = "") -> tuple[bool, str]:
        blob = f"{title}\n{visible_text}"
        if self._PRIVATE_SURFACE.search(blob):
            return False, "private_or_live_surface"
        return True, ""

    def update_mood(self, emotion: str, confidence: float = 0.65) -> dict[str, Any]:
        emotion = str(emotion or "natural")
        confidence = max(0.0, min(float(confidence), 1.0))
        previous = self.db.get_setting(self.MOOD_KEY, {})
        previous = dict(previous) if isinstance(previous, dict) else {}
        now = time.time()
        age = max(0.0, now - float(previous.get("at") or 0.0))
        retained = max(0.0, min(1.0, 1.0 - age / 900.0))
        old_strength = float(previous.get("strength") or 0.0) * retained
        if emotion in {"natural", "calm"} and old_strength > confidence:
            current = str(previous.get("emotion") or "natural")
            strength = old_strength * .86
        else:
            current = emotion
            strength = max(confidence, old_strength * .55)
        row = {"emotion": current, "strength": round(strength, 3), "at": now}
        self.db.set_setting(self.MOOD_KEY, row)
        return row

    def mood(self) -> dict[str, Any]:
        row = self.db.get_setting(self.MOOD_KEY, {})
        row = dict(row) if isinstance(row, dict) else {}
        age = max(0.0, time.time() - float(row.get("at") or 0.0))
        strength = float(row.get("strength") or 0.0) * max(0.0, 1.0 - age / 900.0)
        return {"emotion": str(row.get("emotion") or "natural"), "strength": round(strength, 3), "age": round(age, 1)}

    def status(self) -> dict[str, Any]:
        return {
            "experiences": len(self._get_rows(self.EXPERIENCE_KEY, 240)),
            "skills": len(self.skills()),
            "undo_available": any(not row.get("undone") for row in self._get_rows(self.UNDO_KEY, 40)),
            "auth_waiting": bool(self.db.get_setting(self.AUTH_KEY, {})),
            "mood": self.mood(),
        }
