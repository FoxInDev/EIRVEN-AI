from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database, utc_now
from .llm import ModelGateway
from .style import StyleStore


class RelationshipStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, name: str, context: str) -> None:
        if not name.strip():
            return
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO relationships(name, context, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET context=excluded.context, updated_at=excluded.updated_at
                """,
                (name.strip(), context.strip(), utc_now()),
            )

    def get(self, name: str) -> str:
        if not name.strip():
            return ""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT context FROM relationships WHERE name = ?", (name.strip(),)
            ).fetchone()
        return row["context"] if row else ""


class SocialMirror:
    def __init__(
        self,
        settings: Settings,
        gateway: ModelGateway,
        style: StyleStore,
        relationships: RelationshipStore,
    ):
        self.settings = settings
        self.gateway = gateway
        self.style = style
        self.relationships = relationships

    @staticmethod
    def _image(path: str | None) -> str | None:
        if not path:
            return None
        data = Path(path).read_bytes()
        return base64.b64encode(data).decode("ascii")

    def analyze(
        self,
        dialogue: str,
        goal: str,
        person_name: str = "",
        person_context: str = "",
        image_path: str | None = None,
        model: str | None = None,
    ) -> str:
        saved = self.relationships.get(person_name)
        combined_context = "\n".join(part for part in (saved, person_context.strip()) if part)
        if person_name and person_context.strip():
            self.relationships.save(person_name, person_context)
        prompt = f"""
Ты анализируешь личную переписку для владельца приложения. Твоя задача — помочь ему
выразить собственные мысли естественно, а не манипулировать человеком.

Стиль владельца:
{self.style.get().prompt()}

Собеседник: {person_name or 'не указан'}
Контекст отношений:
{combined_context or 'нет данных'}

Цель владельца:
{goal or 'понять ситуацию и ответить естественно'}

Текст переписки:
{dialogue or 'Текст отсутствует; используй прикреплённый скриншот.'}

Дай ответ в формате Markdown:
1. **Что происходит** — краткий разбор тона и динамики, с пометкой где это лишь гипотеза.
2. **Чего лучше не делать** — 2–4 конкретных ошибки.
3. **Лучший ответ в стиле владельца** — один основной готовый вариант.
4. **Альтернативы** — уверенная, флиртующая, ироничная и примирительная версии.
5. **Эффект и риск** — по одной строке на каждый вариант.

Не утверждай, что знаешь скрытые мысли собеседника. Не предлагай давление, шантаж,
преследование, обход отказа или ложную личность. Избегай шаблонных фраз ИИ.
""".strip()
        user_message: dict[str, Any] = {"role": "user", "content": prompt}
        encoded = self._image(image_path)
        selected_model = model or self.settings.model
        if encoded:
            user_message["images"] = [encoded]
            selected_model = model or self.settings.vision_model
        message = self.gateway.chat(
            [
                {"role": "system", "content": "Отвечай по-русски, прямо и естественно."},
                user_message,
            ],
            model=selected_model,
            temperature=0.65,
        )
        return message.get("content", "Модель не вернула ответ.")
