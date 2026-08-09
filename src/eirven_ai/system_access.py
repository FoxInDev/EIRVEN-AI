from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any


def is_admin() -> bool:
    if os.name != "nt":
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def interactive_desktop_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "platform": os.name,
        "admin": is_admin(),
        "interactive": True,
        "desktop": "",
        "session": os.environ.get("SESSIONNAME", ""),
    }
    if os.name == "nt":
        status["desktop"] = os.environ.get("USERPROFILE", "")
        status["interactive"] = bool(os.environ.get("SESSIONNAME") or os.environ.get("USERPROFILE"))
    return status


def access_summary(full_access: bool, desktop_enabled: bool) -> dict[str, Any]:
    runtime = interactive_desktop_status()
    runtime.update(
        {
            "requested": bool(full_access),
            "desktop_control": bool(desktop_enabled),
            "effective_full_access": bool(full_access and desktop_enabled and runtime["admin"]),
            "scope": "full" if full_access and runtime["admin"] else "standard",
        }
    )
    return runtime
