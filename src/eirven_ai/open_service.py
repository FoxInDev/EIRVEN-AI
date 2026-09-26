"""Контур открытия приложений и сервисов.

Без шаблонов и регулярных выражений: что именно человек просит открыть, решает
модель. Перечислить все названия невозможно — «мэш», «мах», «MAX», «вк видео»,
«ютуб», «яндекс музыка» и тысячи других пишутся как угодно, а завтра появится
что-то новое. Список слов здесь всегда будет отставать от жизни.

Выполнение остаётся на существующих проверенных механизмах: сначала пробуем
установленное приложение, затем — официальный веб-адрес. Контур только решает,
что открывать, и проверяет, что получилось.
"""

from __future__ import annotations

import json
from typing import Any

from .trace import log_event



OPEN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "open": {"type": "boolean"},
        "target": {"type": "string"},
        "kind": {"type": "string", "enum": ["app", "website", "folder", "file", "unknown"]},
        "spoken": {"type": "string"},
        "purpose": {"type": "string"},
    },
    "required": ["open"],
}

class OpenService:
    """Понимает просьбу открыть и доводит её до результата."""

    def __init__(self, settings: Any, gateway: Any, tools: Any) -> None:
        self.settings = settings
        self.gateway = gateway
        self.tools = tools

    # ------------------------------------------------------------- понимание
    def understand(self, query: str) -> dict[str, Any]:
        """Просят ли что-то открыть, и что именно.

        Модель возвращает каноническое имя: из «открой мэш» получается «MAX»,
        из «врубай ютубчик» — «YouTube». Дальше с этим именем работают обычные
        механизмы поиска приложения и адреса.
        """
        system = (
            "Ты определяешь, просит ли владелец что-то открыть на компьютере.\n"
            "Открыть можно приложение, сайт, сервис, папку или файл.\n"
            "Верни ТОЛЬКО JSON без пояснений:\n"
            '{"open": true|false, '
            '"target": "каноническое название на латинице или как принято в бренде", '
            '"kind": "app|website|folder|file|unknown", '
            '"spoken": "как это назвал человек", '
            '"purpose": "что он хочет там сделать, если сказал"}\n'
            "Примеры соответствий: «мэш», «мах», «макс» → MAX; «ютуб», «ютубчик» → YouTube; "
            "«вк» → VK; «телега», «тг» → Telegram; «яндекс музыка» → Yandex Music; "
            "«почта» → почтовый клиент; «загрузки» → папка Downloads.\n"
            "open=false, если просьбы открыть нет: вопрос, разговор, другая команда.\n"
            "Не придумывай цель, если человек её не назвал — оставь purpose пустым.\n\n"
            "Примеры:\n"
            "«открой мэш» → open=true, target=MAX, kind=app\n"
            "«врубай ютубчик» → open=true, target=YouTube, kind=website\n"
            "«запусти телегу» → open=true, target=Telegram, kind=app\n"
            "«покажи загрузки» → open=true, target=Downloads, kind=folder\n"
            "«включи яндекс музыку» → open=true, target=Yandex Music, kind=app\n"
            "«зайди в вк» → open=true, target=VK, kind=website\n"
            "«что такое мэш» → open=false (это вопрос, а не просьба открыть)\n"
            "«закрой браузер» → open=false (это закрытие, не открытие)\n"
            "«сделай потише» → open=false"
        )
        try:
            message = self.gateway.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Владелец сказал: {query}"}],
                model=self.settings.fast_model,
                temperature=0.1, think=False,
                response_format=OPEN_SCHEMA,
                num_ctx=self.settings.chat_num_ctx, num_predict=200,
            )
            raw = str(message.get("content") or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start:end + 1])
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log_event(self.settings.root_dir, "OPEN_UNDERSTAND_FAILED", error=str(exc)[:200])
        return {"open": False}

    # -------------------------------------------------------------- действие
    def perform(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Открыть то, что решила модель, и проверить результат.

        Порядок попыток важен: установленное приложение удобнее сайта, а сайт
        лучше, чем ничего. Каждый шаг проверяется, поэтому «открыла» говорится
        только когда что-то действительно открылось.
        """
        target = str(decision.get("target") or "").strip()
        spoken = str(decision.get("spoken") or target).strip()
        purpose = str(decision.get("purpose") or "").strip()
        kind = str(decision.get("kind") or "unknown")
        if not target:
            return {"ok": False, "message": "Не поняла, что именно открыть."}

        attempts: list[tuple[str, dict[str, Any]]] = []

        # Папка или файл — отдельный путь, приложение здесь ни при чём.
        if kind in {"folder", "file"}:
            try:
                result = self.tools.execute("system_open_path", {"path": target})
                attempts.append(("system_open_path", result))
                if isinstance(result, dict) and result.get("ok"):
                    return {"ok": True, "message": f"Открыла {spoken}.", "via": "path"}
            except Exception as exc:
                attempts.append(("system_open_path", {"ok": False, "error": str(exc)[:200]}))

        # Основной путь: единый резолвер, он сам выбирает между приложением и
        # веб-версией и проверяет появившееся окно.
        try:
            result = self.tools.execute("open_service", {"service": target, "purpose": purpose})
            attempts.append(("open_service", result))
            if isinstance(result, dict) and result.get("ok"):
                inner = result.get("result") if isinstance(result.get("result"), dict) else {}
                where = str(inner.get("surface") or inner.get("mode") or "").strip()
                tail = f" ({where})" if where else ""
                return {"ok": True, "message": f"Открыла {spoken}{tail}.", "via": "service"}
        except Exception as exc:
            attempts.append(("open_service", {"ok": False, "error": str(exc)[:200]}))

        # Запасной вариант: установленное приложение по имени.
        try:
            result = self.tools.execute("launch_application", {"query": target})
            attempts.append(("launch_application", result))
            if isinstance(result, dict) and result.get("ok"):
                return {"ok": True, "message": f"Открыла {spoken}.", "via": "application"}
        except Exception as exc:
            attempts.append(("launch_application", {"ok": False, "error": str(exc)[:200]}))

        log_event(self.settings.root_dir, "OPEN_FAILED", target=target[:80],
                  tried=[name for name, _ in attempts])
        return {
            "ok": False,
            "message": f"Не смогла открыть {spoken}.",
            "attempts": attempts,
        }
