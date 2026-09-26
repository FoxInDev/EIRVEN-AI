"""Перевод технических сбоев на человеческий язык.

Ассистент, который в ответ показывает текст исключения, читается как незаконченный
продукт — независимо от того, насколько хорошо он работает в остальном. Здесь
редкие узнаваемые причины превращаются в одну понятную фразу, а всё незнакомое
просто скрывается: лучше короткое честное «не получилось», чем обрывок трассировки.

Полный текст ошибки при этом никуда не девается — он пишется в журнал, где и нужен
для разбора.
"""

from __future__ import annotations

import re

# Узнаваемые причины. Порядок важен: более частные идут раньше общих.
_PATTERNS: tuple[tuple[str, str], ...] = (
    # Сеть
    (r"getaddrinfo|name or service not known|temporary failure in name resolution",
     "Нет доступа к интернету."),
    (r"connectionrefused|connection refused|failed to establish a new connection",
     "Сервис не отвечает — возможно, он не запущен."),
    (r"timed? ?out|timeout",
     "Слишком долго не было ответа."),
    (r"ssl|certificate verify failed",
     "Не удалось установить защищённое соединение."),
    (r"\b(401|403)\b|unauthorized|forbidden",
     "Доступ не разрешён — похоже, нужно войти заново."),
    (r"\b429\b|too many requests|rate.?limit",
     "Слишком много запросов подряд, нужна пауза."),
    (r"\b5\d\d\b|internal server error|bad gateway",
     "Сервис временно недоступен."),
    # Файлы
    (r"no such file or directory|filenotfound",
     "Файл не найден."),
    (r"permission denied|accessdenied|\[errno 13\]",
     "Нет прав на этот файл или папку."),
    (r"being used by another process|being used by|file is locked",
     "Файл занят другой программой."),
    (r"no space left|disk full",
     "На диске закончилось место."),
    # Модели и голос
    (r"ollama|model .* not found|pull model",
     "Модель не готова — возможно, ещё загружается."),
    (r"cuda|out of memory|oom",
     "Не хватило видеопамяти для этой задачи."),
    (r"portaudio|no default input|device unavailable|sounddevice",
     "Микрофон занят или недоступен."),
    (r"torch|silero|_multiarray|dll load failed",
     "Не удалось загрузить голосовой движок."),
    # Интерфейс
    (r"element not found|no such element|window not found|elementnotfound",
     "Не нашла нужный элемент на экране."),
    (r"telethon|flood|phone.?code|session",
     "Telegram отклонил запрос."),
    # Данные
    (r"json|expecting value|decode",
     "Ответ пришёл в неожиданном виде."),
    (r"database is locked|sqlite",
     "База данных занята, попробуй ещё раз."),
)

_MAX_TAIL = 0   # ничего технического в ответ не добавляем


def humanize(error: object, *, fallback: str = "") -> str:
    """Короткая понятная причина сбоя.

    fallback — что сказать, если причина не распознана. Пустая строка означает
    «промолчать о причине»: это честнее, чем показать обрывок трассировки.
    """
    text = str(error or "").strip()
    if not text:
        return fallback
    low = text.casefold()
    for pattern, message in _PATTERNS:
        if re.search(pattern, low):
            return message
    return fallback


def explain(error: object, action: str, *, hint: str = "") -> str:
    """Полная фраза о неудаче: что не вышло, почему и что делать.

    Формулировка намеренно без извинений и без слов вроде «не смогла надёжно»:
    оправдывающийся тон читается как слабость продукта, а спокойная констатация
    с понятным следующим шагом — как надёжность.
    """
    reason = humanize(error)
    parts = [action.rstrip(".") + "."]
    if reason:
        parts.append(reason)
    if hint:
        parts.append(hint)
    return " ".join(parts)
