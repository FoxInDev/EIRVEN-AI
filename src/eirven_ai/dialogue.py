from __future__ import annotations

import re


_WAKE_WORDS = {
    "eirven", "eirwen", "эрви", "эйрви", "эйрвен", "эйрвэн", "эрвен", "ирвен",
}
_POLITE_WORDS = {
    "пожалуйста", "плиз", "прошу", "слушай", "слышишь", "ну", "эй", "ладно",
}
_RESUME_WORDS = {
    "готов", "готова", "готово", "сделал", "сделала", "сделано", "вошел", "вошла",
    "авторизовался", "авторизовалась", "авторизация", "продолжай", "продолжи",
    "возобнови", "дальше", "далее", "можно", "давай", "закончил", "закончила",
    "подтвердил", "подтвердила", "ввел", "ввела", "введен", "введена", "код",
    "я", "уже", "теперь", "все", "всё", "можешь", "можете", "вход", "выполнен",
}
_NEGATIONS = {"не", "нет", "нельзя", "неготов", "неготова"}


def normalize_phrase(text: str, *, drop_wake: bool = True) -> str:
    """Normalize a short control phrase without depending on Russian word order."""
    value = str(text or "").casefold().replace("ё", "е")
    words = re.findall(r"[a-zа-я0-9]+", value)
    if drop_wake:
        words = [word for word in words if word not in _WAKE_WORDS]
    words = [word for word in words if word not in _POLITE_WORDS]
    return " ".join(words)


def is_resume_confirmation(text: str) -> bool:
    """Accept natural checkpoint replies such as ``Эрви, я готов, продолжай``.

    A bounded vocabulary prevents an unrelated sentence containing the word ``готов``
    from resuming a desktop workflow. Repetition and wake words are harmless, while a
    negation or a question about *the assistant's* readiness is rejected.
    """
    raw = str(text or "").strip()
    clean = normalize_phrase(raw)
    words = clean.split()
    if not words or len(words) > 14 or any(word in _NEGATIONS for word in words):
        return False
    if "?" in raw and any(word in words for word in ("ты", "сама", "сам")):
        return False
    compact = set(words)
    signal = bool(
        compact.intersection({
            "готов", "готова", "готово", "сделал", "сделала", "сделано", "вошел",
            "вошла", "авторизовался", "авторизовалась", "продолжай", "продолжи",
            "возобнови", "дальше", "далее", "закончил", "закончила", "подтвердил",
            "подтвердила",
        })
        or ({"код", "ввел"} <= compact)
        or ({"код", "ввела"} <= compact)
        or ({"вход", "выполнен"} <= compact)
    )
    return signal and all(word in _RESUME_WORDS for word in words)


def is_cancel_confirmation(text: str) -> bool:
    words = set(normalize_phrase(text).split())
    return bool(words.intersection({"нет", "отмена", "отмени", "стоп", "передумал", "передумала"}))


def is_affirmative_confirmation(text: str) -> bool:
    words = normalize_phrase(text).split()
    if not words or len(words) > 10 or any(word in _NEGATIONS for word in words):
        return False
    accepted = {
        "да", "подтверждаю", "подтвердить", "выключай", "выключи", "завершай",
        "заверши", "работу", "компьютер", "пк", "ноутбук", "можно", "согласен",
        "согласна", "точно",
    }
    return bool(set(words).intersection({"да", "подтверждаю", "выключай", "завершай", "точно"})) and all(
        word in accepted for word in words
    )


def is_pc_shutdown_request(text: str) -> bool:
    clean = normalize_phrase(text)
    words = set(clean.split())
    if not words or words.intersection({"себя", "тебя", "ассистент", "агент"}):
        return False
    machine = bool(words.intersection({"компьютер", "комп", "пк", "ноутбук", "windows", "виндовс"}))
    other_object = bool(re.search(r"\b(?:звук|аудио|интернет|сеть|wifi|вайфай|экран|монитор|камер|микрофон|bluetooth|блютуз)\w*\b", clean))
    if other_object:
        return False
    shutdown = bool(
        re.search(r"\b(?:выключ|выруб|погас)\w*\b", clean)
        or re.search(r"\bзаверш\w*.{0,18}\b(?:работ|сеанс)\w*\b", clean)
        or (re.search(r"\bотключ\w*\b", clean) and not other_object)
    )
    cancel = bool(
        re.search(r"\b(?:отмен|прерв)\w*\b", clean)
        or "не надо" in clean
        or re.search(r"\bне\s+(?:выключ|выруб|отключ|заверш|погас)\w*\b", clean)
    )
    return machine and shutdown and not cancel


def is_pc_shutdown_cancel_request(text: str) -> bool:
    clean = normalize_phrase(text)
    machine = bool(re.search(r"\b(?:компьютер|комп|пк|ноутбук|windows|виндовс)\b", clean))
    other_object = bool(re.search(r"\b(?:звук|аудио|интернет|сеть|wifi|вайфай|экран|монитор|камер|микрофон|bluetooth|блютуз)\w*\b", clean))
    if other_object:
        return False
    shutdown = bool(
        re.search(r"\b(?:выключ|выруб|погас)\w*\b", clean)
        or re.search(r"\bзаверш\w*.{0,18}\b(?:работ|сеанс)\w*\b", clean)
        or (re.search(r"\bотключ\w*\b", clean) and not other_object)
    )
    cancel = bool(
        re.search(r"\b(?:отмен|прерв)\w*\b", clean)
        or "не надо" in clean
        or re.search(r"\bне\s+(?:выключ|выруб|отключ|заверш|погас)\w*\b", clean)
    )
    return machine and shutdown and cancel


def is_phone_setup_request(text: str) -> bool:
    clean = normalize_phrase(text)
    subject = bool(re.search(r"\b(?:телефон|телеграм|telegram|удаленн\w*\s+управлен)\w*\b", clean))
    action = bool(re.search(r"\b(?:настро|подключ|привяж|свяж|управл|команд)\w*\b", clean))
    return subject and action


def is_chat_pairing_request(text: str) -> bool:
    clean = normalize_phrase(text)
    chat = bool(re.search(r"\b(?:этот|сюда|текущ)\w*\b.{0,30}\bчат\w*|\bчат\w*.{0,30}\b(?:этот|сюда|текущ)\w*\b", clean))
    messaging = bool(re.search(r"\b(?:сообщен|команд)\w*\b", clean))
    bind = bool(re.search(r"\b(?:привяж|подключ|запомн|перезапиш|отправл|писать|пишу)\w*\b", clean))
    return (chat and bind) or (messaging and bind and "сюда" in clean.split())
