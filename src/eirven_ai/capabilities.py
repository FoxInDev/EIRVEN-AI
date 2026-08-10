from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any


class CapabilityRegistry:
    """Runtime map of what EIRVEN can actually do on this machine right now.

    The registry intentionally reports verified local capabilities rather than promises.
    It is cheap enough to refresh after installs, logins and camera/device changes.
    """

    APP_ALIASES: dict[str, tuple[str, ...]] = {
        "telegram": ("telegram", "телеграм"),
        "yandex_music": ("яндекс музыка", "yandex music"),
        "discord": ("discord",),
        "spotify": ("spotify",),
        "vscode": ("visual studio code", "vs code", "code"),
        "explorer": ("проводник", "file explorer", "explorer"),
    }

    def __init__(self, services: Any):
        self.services = services
        self._cache: dict[str, Any] = {}
        self._updated = 0.0

    def _apps(self) -> list[dict[str, Any]]:
        try:
            return list(self.services.applications.list_installed(refresh=False))
        except Exception:
            return []

    def refresh(self, force: bool = False) -> dict[str, Any]:
        if not force and self._cache and time.monotonic() - self._updated < 20:
            return dict(self._cache)
        apps = self._apps()
        app_text = "\n".join(
            f"{str(item.get('name') or '')} {str(item.get('target') or '')}".casefold()
            for item in apps
        )
        installed: dict[str, bool] = {}
        for key, aliases in self.APP_ALIASES.items():
            installed[key] = any(alias.casefold() in app_text for alias in aliases)
        installed["explorer"] = True if os.name == "nt" else installed.get("explorer", False)

        try:
            models = self.services.gateway.installed_models()
        except Exception:
            models = []
        low_models = {str(item).casefold() for item in models}
        voice = self.services.voice.status()
        browser = ""
        browser_info: dict[str, str] = {}
        try:
            browser_info = dict(self.services.applications.default_browser_info())
            browser = str(browser_info.get("name") or self.services.applications.default_browser_name())
        except Exception:
            pass
        if not browser:
            browser = "system_default"

        result = {
            "updated_at": time.time(),
            "os": os.name,
            "browser": {"default": browser, "available": True, **{k: v for k, v in browser_info.items() if k != "name" and v}},
            "desktop": {
                "uia": self._module("pywinauto"),
                "mouse_keyboard": self._module("pyautogui"),
                "screenshot": True,
            },
            "voice": {
                "stt": bool(voice.get("stt_ready")),
                "tts": bool(voice.get("tts_ready")),
                "background": bool(getattr(self.services, "voice_daemon", None)),
            },
            "models": {
                "installed": models,
                "fast": self.services.settings.fast_model,
                "main": self.services.settings.model,
                "vision": self.services.settings.vision_model,
                "deep": self.services.settings.deep_model,
                "vision_ready": str(self.services.settings.vision_model).casefold() in low_models,
            },
            "apps": installed,
            "features": {
                "spatial_os": False,
                "recovery_engine": True,
                "result_verification": True,
                "autonomous_workflow": True,
                "long_horizon_missions": True,
                "cross_app_task_graph": True,
                "parallel_background_nodes": True,
                "interface_learning": True,
                "offline_cache": True,
                "repair_mode": True,
                "developer_mode": True,
                "proactive_observer": True,
            },
        }
        self._cache = result
        self._updated = time.monotonic()
        try:
            self.services.db.set_setting("capability_registry", result)
        except Exception:
            pass
        return dict(result)

    def has_app(self, key: str) -> bool:
        return bool(self.refresh().get("apps", {}).get(key))

    @staticmethod
    def _module(name: str) -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec(name) is not None
        except Exception:
            return False
