from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.RLock()


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
            **payload,
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
