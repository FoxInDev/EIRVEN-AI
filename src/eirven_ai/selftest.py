from __future__ import annotations

import time
from typing import Any

from .trace import log_event


class StartupSelfTest:
    def __init__(self, services: Any):
        self.services = services

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        checks: dict[str, Any] = {}
        try: checks["ollama"] = self.services.gateway.health()
        except Exception as exc: checks["ollama"] = {"ok": False, "error": str(exc)}
        try:
            v = self.services.voice.status(); checks["voice"] = {"ok": bool(v.get("stt_ready") and v.get("tts_ready")), "detail": v}
        except Exception as exc: checks["voice"] = {"ok": False, "error": str(exc)}
        try:
            access = self.services.tools.execute("access_status", {}); checks["desktop"] = {"ok": bool(access.get("ok")), "detail": access}
        except Exception as exc: checks["desktop"] = {"ok": False, "error": str(exc)}
        try:
            caps = self.services.capabilities.refresh(force=True); checks["capabilities"] = {"ok": True, "detail": caps}
        except Exception as exc: checks["capabilities"] = {"ok": False, "error": str(exc)}
        result = {"at": time.time(), "elapsed_ms": int((time.monotonic()-started)*1000), "checks": checks,
                  "ok": all(bool(x.get("ok")) for x in checks.values() if isinstance(x, dict))}
        self.services.db.set_setting("startup_selftest", result)
        try: log_event(self.services.settings.root_dir, "SELF_TEST", **result)
        except Exception: pass
        return result
