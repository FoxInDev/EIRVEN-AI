from __future__ import annotations

import time
from typing import Any, Callable


class OfflineCache:
    def __init__(self, db: Any):
        self.db = db

    @staticmethod
    def _key(namespace: str, key: str) -> str:
        safe = "".join(ch for ch in str(key).casefold() if ch.isalnum() or ch in "._:- ")[:120]
        return f"offline_cache:{namespace}:{safe}"

    def put(self, namespace: str, key: str, value: Any) -> None:
        self.db.set_setting(self._key(namespace, key), {"at": time.time(), "value": value})

    def get(self, namespace: str, key: str, max_age: float | None = None) -> tuple[Any, float] | tuple[None, None]:
        raw = self.db.get_setting(self._key(namespace, key), None)
        if not isinstance(raw, dict) or "value" not in raw:
            return None, None
        at = float(raw.get("at") or 0)
        age = max(0.0, time.time() - at)
        if max_age is not None and age > max_age:
            return None, None
        return raw.get("value"), age

    def fetch(self, namespace: str, key: str, producer: Callable[[], Any], *, fresh_seconds: float, stale_seconds: float) -> tuple[Any, bool, float]:
        value, age = self.get(namespace, key, fresh_seconds)
        if value is not None:
            return value, True, float(age or 0)
        try:
            value = producer()
            self.put(namespace, key, value)
            return value, False, 0.0
        except Exception:
            stale, stale_age = self.get(namespace, key, stale_seconds)
            if stale is not None:
                return stale, True, float(stale_age or 0)
            raise
