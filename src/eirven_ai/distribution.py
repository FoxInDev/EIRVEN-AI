from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


_ALLOWED_SOURCES = {"website", "website-standard", "website-express", "unknown"}
_ALLOWED_CHANNELS = {"standard", "express"}
_TICKET_RE = re.compile(r"(?:^|[-_.])EXP[-_.]([A-Za-z0-9_-]{12,160})(?:[-_.]|$)", re.I)


def _installer_ticket() -> str:
    """Recover a one-time Express ticket from the recent installer filename.

    The unified installer extracts a clean launcher name, so the first installed run can
    no longer see the browser download name through ``sys.executable``. Only conventional
    user download folders and recent EIRVEN executables are inspected. The server still
    redeems the ticket once and remains the authority for Express access.
    """
    candidates: list[Path] = []
    for raw in (sys.argv[0] if sys.argv else "", sys.executable):
        if raw:
            candidates.append(Path(raw))
    home = Path.home()
    for folder in (home / "Downloads", home / "Загрузки", home / "Desktop", home / "Рабочий стол"):
        try:
            candidates.extend(folder.glob("EIRVEN-EXP-*.exe"))
        except Exception:
            continue
    now = __import__("time").time()
    recent: list[tuple[float, Path]] = []
    for path in candidates:
        try:
            stamp = path.stat().st_mtime
        except Exception:
            stamp = now
        if now - stamp <= 7 * 24 * 60 * 60:
            recent.append((stamp, path))
    for _, path in sorted(recent, key=lambda item: item[0], reverse=True):
        match = _TICKET_RE.search(path.name)
        if match:
            return match.group(1)
    return ""


class DistributionState:
    """Persist where this installation came from without trusting the client for VIP access.

    ``claimed_channel`` is only UX metadata. The update/download server decides the real
    delivery channel from a server-issued bearer entitlement. A patched open-source client
    can repaint its badge, but it cannot mint a valid Express token.
    """

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir).resolve()
        self.path = self.root_dir / "data" / "distribution.json"
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _clean_source(value: str) -> str:
        source = str(value or "").strip().casefold()
        return source if source in _ALLOWED_SOURCES else "unknown"

    @staticmethod
    def _clean_channel(value: str) -> str:
        channel = str(value or "").strip().casefold()
        return channel if channel in _ALLOWED_CHANNELS else "standard"

    def _default(self) -> dict[str, Any]:
        executable = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0] or "")
        name = executable.name
        source = "website-standard"
        claimed = "standard"
        ticket = ""
        match = _TICKET_RE.search(name)
        recovered = _installer_ticket()
        if match:
            source = "website-express"
            claimed = "express"
            ticket = match.group(1)
        elif recovered:
            source = "website-express"
            claimed = "express"
            ticket = recovered
        elif "STD" in name.upper() or "STANDARD" in name.upper():
            source = "website-standard"
        return {
            "format": "EIRVEN_DISTRIBUTION_V1",
            "install_id": uuid.uuid4().hex,
            "source": source,
            "claimed_channel": claimed,
            "entitlement_token": "",
            "claim_ticket": ticket,
            "token_fingerprint": "",
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            data = self._default()
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data.update(raw)
                except Exception:
                    pass
            install_id = re.sub(r"[^a-fA-F0-9]", "", str(data.get("install_id") or ""))[:64]
            if len(install_id) < 24:
                install_id = uuid.uuid4().hex
            token = str(data.get("entitlement_token") or "").strip()[:1024]
            data.update({
                "format": "EIRVEN_DISTRIBUTION_V1",
                "install_id": install_id,
                "source": self._clean_source(str(data.get("source") or "unknown")),
                "claimed_channel": self._clean_channel(str(data.get("claimed_channel") or "standard")),
                "entitlement_token": token,
                "claim_ticket": re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("claim_ticket") or ""))[:160],
                "token_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else "",
            })
            self._write(data)
            return dict(data)

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def capture_environment(self) -> dict[str, Any]:
        """Accept installer-provided metadata; never ship owner secrets in source/APK."""
        with self._lock:
            data = self.load()
            source = os.getenv("EIRVEN_INSTALL_SOURCE", "").strip()
            channel = os.getenv("EIRVEN_DELIVERY_CHANNEL", "").strip()
            token = os.getenv("EIRVEN_ENTITLEMENT_TOKEN", "").strip()
            ticket = os.getenv("EIRVEN_CLAIM_TICKET", "").strip()
            if source:
                data["source"] = self._clean_source(source)
            if channel:
                data["claimed_channel"] = self._clean_channel(channel)
            if token:
                data["entitlement_token"] = token[:1024]
            if ticket:
                data["claim_ticket"] = re.sub(r"[^A-Za-z0-9_-]", "", ticket)[:160]
            token = str(data.get("entitlement_token") or "")
            data["token_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""
            self._write(data)
            return dict(data)

    def save_entitlement(self, token: str, *, source: str = "website-express") -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            raise ValueError("Пустой Express-токен")
        with self._lock:
            data = self.load()
            data["entitlement_token"] = token[:1024]
            data["token_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()[:16]
            data["claimed_channel"] = "express"
            data["source"] = self._clean_source(source)
            data["claim_ticket"] = ""
            self._write(data)
            return dict(data)

    def clear_entitlement(self) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            data["entitlement_token"] = ""
            data["token_fingerprint"] = ""
            data["claimed_channel"] = "standard"
            self._write(data)
            return dict(data)

    def public(self) -> dict[str, Any]:
        data = self.load()
        return {
            "install_id": data["install_id"],
            "source": data["source"],
            "claimed_channel": data["claimed_channel"],
            "has_entitlement": bool(data.get("entitlement_token")),
            "token_fingerprint": data.get("token_fingerprint", ""),
            "claim_pending": bool(data.get("claim_ticket")),
        }

    def auth_headers(self) -> dict[str, str]:
        data = self.load()
        headers = {
            "X-EIRVEN-Install-ID": str(data["install_id"]),
            "X-EIRVEN-Install-Source": str(data["source"]),
        }
        token = str(data.get("entitlement_token") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
