from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.RLock()

_SECRET_KEYS = {
    "authorization", "password", "passwd", "token", "access_token",
    "refresh_token", "mobile_access_token", "api_hash", "otp", "sms_code",
    "cvv", "pin", "secret",
}


def _redact(value: Any, key: str = "") -> Any:
    """Remove credentials and sensitive payloads before they reach disk."""
    normalized = str(key or "").casefold()
    if normalized in _SECRET_KEYS or any(marker in normalized for marker in ("password", "token", "secret", "api_hash", "otp", "cvv")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        # Pairing codes are twenty uppercase alphanumerics, optionally grouped by dashes.
        import re
        text = re.sub(r"(?<![A-Z0-9])(?:[A-Z0-9]{5}-){3}[A-Z0-9]{5}(?![A-Z0-9])", "[PAIRING-CODE]", value)
        text = re.sub(r"(?i)(authorization\s*[:=]\s*)(\S+)", r"\1[REDACTED]", text)
        return text[:12000]
    return value


def log_event(root: str | Path, event: str, **payload: Any) -> None:
    """Append one compact UTF-8 trace line to loggg2.txt.

    This is intentionally independent of Python logging so the owner can send one file
    containing voice, routing, tool and camera decisions in chronological order.
    """
    try:
        path = Path(root).resolve() / "loggg2.txt"
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mono": round(time.monotonic(), 3),
            "event": str(event),
            **_redact(payload),
        }
        text = json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the debug file useful over 24/7 sessions without unbounded growth.
            if path.exists() and path.stat().st_size > 12_000_000:
                backup = path.with_suffix(".prev.txt")
                try:
                    backup.unlink(missing_ok=True)
                    path.replace(backup)
                except Exception:
                    pass
            with path.open("a", encoding="utf-8") as out:
                out.write(text)
    except Exception:
        pass
