from __future__ import annotations

from typing import Any


def _choice(label: str, value: str, action: str = "message") -> dict[str, str]:
    return {"label": label, "value": value, "action": action}


def enrich_stream_event(event: dict[str, Any], original_query: str = "") -> dict[str, Any]:
    """Attach conservative button choices to clarification/failure packets.

    Explicit route.ui_choices produced by a deterministic action always wins. The
    generic rules below only cover known recoverable UX failures; they never execute an
    action without the user's tap.
    """
    value = dict(event or {})
    route = dict(value.get("route") or {})
    if route.get("ui_choices"):
        value["route"] = route
        return value

    text = str(value.get("answer") or value.get("message") or "").strip()
    low = text.casefold().replace("ё", "е")
    query = str(original_query or "").strip()

    if value.get("type") == "error" or (value.get("type") == "done" and not text):
        route.update({
            "needs_user": True,
            "ui_question": text or "Локальная модель не вернула ответ. Что сделать?",
            "ui_choices": [
                _choice("Найти в поиске", f"Найди в интернете ответ на вопрос: {query}" if query else "Найди ответ в интернете"),
                _choice("Перезапустить модель", "Перезапусти локальную модель и повтори мой предыдущий вопрос"),
            ],
        })
    elif route.get("needs_user") and any(mark in low for mark in ("не смогла найти приложение", "приложение не найдено", "не нашла приложение")):
        route.update({
            "ui_question": text,
            "ui_choices": [
                _choice("Открыть веб-версию", "Открой веб-версию"),
                _choice("Установить приложение", "Установи это приложение"),
                _choice("Попробовать ещё раз", "Попробуй найти приложение ещё раз"),
            ],
        })
    elif route.get("needs_user") and all(mark in low for mark in ("сайт", "прилож")):
        route.update({
            "ui_question": text,
            "ui_choices": [
                _choice("Сайт", "Открой сайт"),
                _choice("Приложение", "Открой приложение"),
                _choice("Трек", "Открой трек"),
            ],
        })
    elif route.get("needs_user") and (
        route.get("confirmation_fingerprint")
        or route.get("confirmation_summary")
        or route.get("confirmation_category")
        or "подтверждени" in low
    ):
        # Keep the confirmation in the visible sphere dock.  The exact payload is
        # still owned and checked by the server; these buttons only submit the same
        # affirmative/cancel control phrases as the voice/text lane.
        route.update({
            "ui_question": text,
            "ui_choices": [
                _choice("Да, продолжить", "да, продолжай", "confirm"),
                _choice("Отмена", "отмена", "cancel"),
            ],
        })
    value["route"] = route
    return value
