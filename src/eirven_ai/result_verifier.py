from __future__ import annotations

from typing import Any


class ResultVerifier:
    def __init__(self, services: Any):
        self.services=services

    def application_visible(self, target: str) -> bool:
        text=str(target or "").casefold().replace("ё","е")
        try:
            result=self.services.tools.execute("window_list",{"max_windows":80})
            rows=result.get("result") or [] if result.get("ok") else []
            aliases=[x for x in text.split() if len(x)>=3]
            return any(any(a in str(r.get("title") or "").casefold().replace("ё","е") for a in aliases) for r in rows)
        except Exception:
            return False

    def verify(self, kind: str, target: str, result: dict[str,Any] | None=None) -> bool:
        if kind in {"open","launch_application","app_skill_open"}:
            return bool((result or {}).get("verified")) or self.application_visible(target)
        if kind in {"send","telegram_send"}:
            return bool((result or {}).get("verified"))
        if kind in {"theme","wifi","airplane","close","spatial_render"}:
            return bool((result or {}).get("ok",True))
        return bool((result or {}).get("ok",True))
