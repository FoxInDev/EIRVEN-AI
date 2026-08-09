from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(slots=True)
class CommandIntent:
    action: str
    target: str
    confidence: float
    mixed: bool = False
    raw: str = ""


_ACTIONS: dict[str, tuple[str, ...]] = {
    "open": ("открой", "открыть", "запусти", "запустить", "зайди", "зайти", "откройте", "запускай"),
    "close": ("закрой", "закрыть", "заверши", "завершить", "убей", "останови", "остановить"),
    "enable": ("включи", "включить", "вруби", "активируй", "запусти"),
    "disable": ("выключи", "выключить", "отключи", "отключить", "деактивируй"),
    "analyze": ("оцени", "оценить", "посмотри", "посмотреть", "проанализируй", "проанализировать", "глянь", "изучи", "разбери", "проверь"),
    "show": ("покажи", "выведи", "размести", "добавь", "прикрепи", "поставь", "вытащи"),
    "repair": ("почини", "починить", "восстанови", "восстановить", "переустанови", "переустановить", "переустановите"),
    "send": ("отправь", "отправить", "пошли", "пошлите", "напиши", "написать", "скинь", "скинуть"),
    "find": ("найди", "найти", "поищи", "поискать", "погугли", "загугли"),
    "answer": ("ответь", "ответить", "прими", "принять"),
    "click": ("нажми", "нажать", "кликни", "кликнуть", "ткни", "тапни"),
    "move": ("перемести", "переместить", "передвинь", "передвинуть", "сдвинь", "сдвинуть", "перенеси", "перенести"),
    "remember": ("запомни", "запомнить", "сохрани", "сохранить"),
    "ask": ("попроси", "попросить", "скажи", "скажи-ка"),
}

_FILLERS = {
    "пожалуйста", "плис", "плиз", "мне", "для", "меня", "прям", "сейчас", "давай", "ну", "ка", "же",
    "приложение", "приложуху", "программу", "можешь", "можно", "бы", "быстро",
    "умничка", "умница", "так", "слушай", "эй", "короче", "вообще", "ладно", "окей", "ок", "эрви", "эйрвен", "эрвен",
}


def _norm(text: str) -> str:
    text = str(text or "").casefold().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9$%+./:_-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _verb_match(token: str, variant: str) -> float:
    if token == variant:
        return 1.0
    if len(token) >= 4 and (variant.startswith(token) or token.startswith(variant)):
        return 0.92
    ratio = SequenceMatcher(None, token, variant).ratio()
    return ratio if ratio >= 0.84 else 0.0


def _action_for_token(token: str) -> tuple[str, float] | None:
    best: tuple[str, float] | None = None
    for action, variants in _ACTIONS.items():
        score = max((_verb_match(token, v) for v in variants), default=0.0)
        if score and (best is None or score > best[1]):
            best = (action, score)
    return best


def _clean_target(tokens: list[str]) -> str:
    out: list[str] = []
    for token in tokens:
        edge = token.strip(".,!?;:—-\"'«»")
        if not edge:
            continue
        if edge in _FILLERS:
            continue
        out.append(edge)
    target = " ".join(out).strip(" .,!?-—")
    target = re.sub(r"^(?:(?:умничка|умница|так|слушай|эй|короче|ладно|окей|ок|ну|давай)\s+)+", "", target, flags=re.I).strip()
    return target


def detect_commands(text: str) -> list[CommandIntent]:
    """Return every imperative clause in order, without swallowing the next command.

    The previous parser found only the first verb and used the *rest of the utterance* as
    its target. That turned `открой МЭШ, посмотри ДЗ` into an application literally named
    `мэш посмотри дз`. Here each action owns only the words until the next action verb.
    """
    clean = _norm(text)
    if not clean:
        return []
    tokens = clean.split()
    positions: list[tuple[int, str, float]] = []
    for i, token in enumerate(tokens):
        match = _action_for_token(token)
        if match:
            action, score = match
            positions.append((i, action, score))
    if not positions:
        return []

    intents: list[CommandIntent] = []
    for idx, (pos, action, score) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(tokens)
        # Prefer the post-verb span. A short pre-verb object is useful for inverted speech
        # like `телеграм открой`, but never carry previous conversational sentences into it.
        post = tokens[pos + 1:end]
        target = _clean_target(post)
        if not target and pos > 0:
            start = positions[idx - 1][0] + 1 if idx else max(0, pos - 3)
            target = _clean_target(tokens[start:pos])
        # conjunctions/punctuation become tokens after normalization; trim common bridges
        target = re.sub(r"^(?:и|а|потом|затем|после\s+этого)\s+", "", target, flags=re.I).strip()
        target = re.sub(r"\s+(?:и|а|потом|затем)$", "", target, flags=re.I).strip()
        confidence = score if target else score * 0.72
        raw = " ".join(tokens[pos:end])
        intents.append(CommandIntent(action=action, target=target, confidence=round(confidence, 3), raw=raw))

    # Mark only genuinely conversational mixtures, not multi-action workflows. If one
    # imperative is followed by a conversational question, keep that tail out of the
    # action target: `открой Telegram и как ты?` must target only Telegram.
    question_markers = ("?" in text) or bool(re.search(r"\b(?:как|почему|зачем|что|когда|где|кто|какой|какая|какие)\b", clean))
    if len(intents) == 1 and question_markers and len(tokens) >= 4 and intents[0].action in {"open", "close", "enable", "disable", "show", "click", "move"}:
        intents[0].mixed = True
        intents[0].target = re.split(
            r"\s+(?:и|а)\s+(?=(?:как|почему|зачем|что|когда|где|кто|какой|какая|какие)\b)",
            intents[0].target, maxsplit=1, flags=re.I,
        )[0].strip()
    return intents


def detect_command(text: str) -> CommandIntent | None:
    intents = detect_commands(text)
    return intents[0] if intents else None
