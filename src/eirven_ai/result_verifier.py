# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class ResultVerifier:
    """Deny-by-default postcondition verifier.

    Older builds treated a missing ok field as success. r50 accepts success only when
    the relevant state can be observed again.
    """

    def __init__(self, services: Any):
        self.services = services

    @staticmethod
    def _inner(result: dict[str, Any] | None) -> dict[str, Any]:
        value = result or {}
        inner = value.get("result")
        return inner if isinstance(inner, dict) else value

    def application_visible(self, target: str) -> bool:
        text = str(target or "").casefold().replace("ё", "е")
        aliases = [item for item in text.split() if len(item) >= 3]
        if not aliases:
            return False
        try:
            result = self.services.tools.execute("window_list", {"max_windows": 80})
            rows = result.get("result") or [] if result.get("ok") else []
            return any(
                any(alias in str(row.get("title") or "").casefold().replace("ё", "е") for alias in aliases)
                for row in rows
            )
        except Exception:
            return False

    @staticmethod
    def file_matches(result: dict[str, Any] | None) -> bool:
        inner = ResultVerifier._inner(result)
        raw_path = str(inner.get("absolute_path") or inner.get("path") or "").strip()
        if not raw_path:
            return False
        try:
            path = Path(raw_path)
            if not path.is_file():
                return False
            expected_size = inner.get("bytes")
            if expected_size is not None and path.stat().st_size != int(expected_size):
                return False
            expected_hash = str(inner.get("sha256") or "").strip().casefold()
            if expected_hash and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                return False
            return bool(inner.get("readback_verified") or expected_hash)
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def explicit_verified(result: dict[str, Any] | None) -> bool:
        value = result or {}
        inner = ResultVerifier._inner(value)
        return bool(value.get("verified") is True or inner.get("verified") is True)

    def verify(self, kind: str, target: str, result: dict[str, Any] | None = None) -> bool:
        normalized = str(kind or "").casefold()
        if normalized in {"file_write", "write_file", "system_write_file"}:
            return self.file_matches(result)
        if normalized in {"open", "launch_application", "app_skill_open", "open_app"}:
            return self.explicit_verified(result) or self.application_visible(target)
        if normalized in {
            "send", "telegram_send", "media_next", "media_pause", "media_play", "media_stop",
            "calendar_create", "browser_open", "process_start", "ui_click", "theme", "wifi",
            "airplane", "close", "spatial_render",
        }:
            return self.explicit_verified(result)
        return False
