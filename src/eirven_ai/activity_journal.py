from __future__ import annotations

import json
from typing import Any


_LABELS = {
    "system_batch_rename": "Переименование файлов",
    "system_write_file": "Запись файла",
    "write_file": "Запись файла",
    "launch_application": "Запуск приложения",
    "window_click": "Действие в интерфейсе",
    "window_elements": "Чтение элементов окна",
    "window_list": "Поиск открытых окон",
    "desktop_state": "Проверка текущего экрана",
    "window_type": "Ввод текста",
    "type_text": "Ввод текста",
    "media_control": "Управление музыкой",
    "powershell": "Системная команда",
    "process_terminate": "Завершение процесса",
    "mail_review": "Проверка почты",
    "mail_send": "Отправка письма",
}

_SENSITIVE = ("password", "token", "secret", "api_hash", "authorization", "content", "body")


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _scrub(value: Any, key: str = "") -> Any:
    low = key.casefold()
    if any(item in low for item in _SENSITIVE):
        return "[скрыто]"
    if isinstance(value, dict):
        return {str(k): _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(x) for x in value[:30]]
    if isinstance(value, str):
        return value[:1000]
    return value


def friendly_action(row: dict[str, Any], *, details: bool = False) -> dict[str, Any]:
    tool = str(row.get("tool") or "")
    args = _decode(row.get("arguments"))
    result = _decode(row.get("result"))
    args = args if isinstance(args, dict) else {}
    result = result if isinstance(result, dict) else {"value": result}
    inner = result.get("result") if isinstance(result.get("result"), dict) else result
    success = bool(row.get("success"))
    verified = bool(result.get("verified") is True or inner.get("verified") is True)
    status = "verified" if success and verified else ("done" if success else "failed")
    label = _LABELS.get(tool, tool.replace("_", " ").strip().capitalize() or "Действие")
    summary = "Выполнено"
    reason = ""
    if tool == "system_batch_rename":
        total = int(inner.get("matched", 0) or 0)
        renamed = int(inner.get("renamed", 0) or 0)
        summary = f"Переименовано {renamed}/{total} файлов"
        reason = str(inner.get("verification") or inner.get("error") or "")
    elif tool == "mail_review":
        summary = f"Проверено непрочитанных: {int(inner.get('unread', 0) or 0)}"
        reason = str(inner.get('verification') or inner.get('error') or '')
    elif tool == "mail_send":
        summary = "Письмо принято SMTP" if success else "Письмо не отправлено"
        reason = str(inner.get('note') or inner.get('error') or '')
    elif tool in {"write_file", "system_write_file"}:
        summary = "Файл записан" if success else "Файл не записан"
        reason = "Повторное чтение совпало" if verified else str(result.get("error") or inner.get("error") or "")
    elif not success:
        summary = "Не выполнено"
        reason = str(result.get("error") or inner.get("error") or "Неизвестная ошибка")
        if "управление приложениями отключено" in reason.casefold():
            reason = "В момент попытки было выключено «Разрешить управление компьютером». Сейчас разрешение можно проверить в «Настройки → Безопасность»."
    elif verified:
        summary = "Выполнено и перепроверено"
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "tool": tool,
        "title": label,
        "status": status,
        "risk": str(row.get("risk") or ""),
        "summary": summary,
        "reason": reason[:700],
        "verified": verified,
        "details": {"arguments": _scrub(args), "result": _scrub(result)} if details else {},
    }
