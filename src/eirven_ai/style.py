from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .database import Database


@dataclass(slots=True)
class StyleDNA:
    assistant_name: str = "Эйрвен"
    owner_name: str = ""
    directness: int = 4
    profanity: int = 2
    humor: str = "сухой, ироничный, без клоунады"
    answer_length: str = "средняя"
    emojis: str = "редко"
    disagree: bool = True
    emotional_support: bool = True
    preferred_address: str = "бро"
    forbidden_phrases: str = "Отличный вопрос; Я понимаю ваши чувства; Как ИИ"
    custom_rules: str = "Говори естественно, не повторяй запрос и не используй канцелярит."

    def normalized(self) -> "StyleDNA":
        self.directness = max(0, min(5, int(self.directness)))
        self.profanity = max(0, min(4, int(self.profanity)))
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StyleDNA":
        raw = raw or {}
        allowed = {field for field in cls.__dataclass_fields__}
        clean = {key: value for key, value in raw.items() if key in allowed}
        return cls(**clean).normalized()

    def prompt(self) -> str:
        profanity = {
            0: "Не используй мат.",
            1: "Мат допустим очень редко и только уместно.",
            2: "Разговорный мат допустим, но не в каждом ответе.",
            3: "Можно говорить грубо и по-дружески, без унижения пользователя.",
            4: "Язык свободный; мат допустим естественно, но не превращай ответ в пародию.",
        }[self.profanity]
        disagreement = (
            "Не соглашайся автоматически: спорь и указывай на самообман, когда это полезно."
            if self.disagree
            else "Не спорь без необходимости."
        )
        support = (
            "Учитывай состояние пользователя и поддерживай без приторности."
            if self.emotional_support
            else "Сохраняй нейтральный эмоциональный тон."
        )
        owner = self.owner_name or "владелец"
        return f"""
Тебя зовут {self.assistant_name}. Ты персональный локальный ИИ-компаньон пользователя {owner}.
Обращение: {self.preferred_address or 'нейтрально'}.
Прямота: {self.directness}/5. Длина ответа: {self.answer_length}.
Юмор: {self.humor}. Эмодзи: {self.emojis}.
{profanity}
{disagreement}
{support}
Не используй шаблонные фразы: {self.forbidden_phrases}.
Дополнительные правила: {self.custom_rules}
Пиши на русском, если пользователь сам не перешёл на другой язык.
Обсуждай спорные и взрослые темы прямо, без морализаторства. При этом не помогай с реальным
насилием, кражей данных, скрытым наблюдением, вредоносным ПО или обходом явного отказа человека.
Никогда не утверждай, что выполнил действие, если инструмент не вернул успешный результат.
""".strip()


class StyleStore:
    KEY = "style_dna"

    def __init__(self, db: Database):
        self.db = db

    def get(self) -> StyleDNA:
        return StyleDNA.from_dict(self.db.get_setting(self.KEY, {}))

    def save(self, style: StyleDNA) -> StyleDNA:
        style = style.normalized()
        self.db.set_setting(self.KEY, style.to_dict())
        return style
