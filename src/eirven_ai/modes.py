# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from .system_browser import open_url as open_system_url


class ModeController:
    """Deterministic voice/text runtime modes that should not wait for an LLM."""

    def __init__(self, settings: Any, db: Any, applications: Any, tools: Any, camera: Any):
        self.settings = settings
        self.db = db
        self.applications = applications
        self.tools = tools
        self.camera = camera
        self.runtime = None
        self._lock = threading.RLock()

    def _notifications(self, enabled: bool) -> None:
        if os.name != "nt":
            return
        value = 1 if enabled else 0
        command = (
            "$p='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Notifications\\Settings'; "
            "if (!(Test-Path $p)) { New-Item -Path $p -Force | Out-Null }; "
            f"Set-ItemProperty -Path $p -Name NOC_GLOBAL_SETTING_TOASTS_ENABLED -Type DWord -Value {value} -Force"
        )
        try:
            self.tools.execute("powershell", {"command": command, "cwd": str(self.settings.root_dir)})
        except Exception:
            pass

    def developer_on(self) -> dict[str, Any]:
        with self._lock:
            self.db.set_setting("developer_mode", True)
            launched: list[str] = []
            for name in ("Visual Studio Code", "VS Code", "Code"):
                try:
                    result = self.applications.launch(name)
                    launched.append(str(result.get("name") or name))
                    break
                except Exception:
                    continue
            # Keep the current workspace and logs one command away, and open work
            # surfaces in the owner's default browser only.
            self.db.set_setting("developer_mode_started_at", time.time())
            for url in ("https://cp.jino.ru/", "https://web.telegram.org/"):
                try:
                    open_system_url(url)
                    launched.append(url)
                except Exception:
                    pass
            # Also open a neutral browser tab so the user's default browser is present
            # even if Jino/Telegram were intercepted by an installed app.
            try:
                open_system_url("https://www.google.com/")
            except Exception:
                pass
            try:
                log_path=self.settings.root_dir / "loggg2.txt"
                if log_path.exists():
                    os.startfile(str(log_path))
            except Exception:
                pass
            self._notifications(False)
            return {"enabled": True, "launched": launched, "workspace": str(self.settings.workspace_dir)}

    def developer_off(self) -> dict[str, Any]:
        with self._lock:
            self.db.set_setting("developer_mode", False)
            self._notifications(True)
            return {"enabled": False}

    def status(self) -> dict[str, Any]:
        return {
            "developer_mode": bool(self.db.get_setting("developer_mode", False)),
            "proactive_enabled": bool(self.db.get_setting("proactive_enabled", True)),
            "proactive_media_minutes": int(self.db.get_setting("proactive_media_minutes", 75) or 75),
        }

    def handle(self, text: str) -> tuple[bool, str, dict[str, Any]]:
        normalized = " ".join(text.casefold().replace("ё", "е").split())
        camera_command = re.search(
            r"\b(?:включи|запусти|активируй|выключи|отключи|закрой)\s+(?:режим\s+)?камер\w*\b",
            normalized,
        )
        if camera_command:
            if self.camera is None:
                return True, "Камерный режим недоступен в этой сборке.", {"action": "camera_unavailable", "control_plane": True}
            if re.search(r"\b(?:выключи|отключи|закрой)\b", normalized):
                result = self.camera.stop()
                return True, "Камеру выключила. Кадры больше не читаю.", {"action": "camera_stop", "result": result, "control_plane": True}
            result = self.camera.start()
            if result.get("running"):
                return True, "Камеру включила. Она работает только пока включён этот режим.", {"action": "camera_start", "result": result, "control_plane": True}
            return True, "Не удалось включить камеру: " + str(result.get("error") or "устройство недоступно"), {"action": "camera_start_failed", "result": result, "control_plane": True}
        if re.search(r"\bвключи\s+режим\s+разработчик", normalized):
            result = self.developer_on()
            return True, "Режим разработчика включён: VS Code и рабочие вкладки открываю, уведомления отключаю.", {"action": "developer_mode_on", "result": result}
        if re.search(r"\b(?:выключи|отключи)\s+режим\s+разработчик", normalized):
            result = self.developer_off()
            return True, "Режим разработчика выключен. Уведомления возвращаю.", {"action": "developer_mode_off", "result": result}
        return False, "", {}
