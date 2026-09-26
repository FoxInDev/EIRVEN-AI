from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import ModelGateway
from .tasks import TaskCancelled, TaskContext
from .tools import ToolExecutor


class GamePilotError(RuntimeError):
    pass


GAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "keys": {"type": "array", "items": {"type": "string"}},
        "duration": {"type": "number"},
        "mouse_dx": {"type": "integer"},
        "mouse_dy": {"type": "integer"},
        "click": {"type": "boolean"},
        "done": {"type": "boolean"},
    },
    "required": ["summary", "keys", "duration", "mouse_dx", "mouse_dy", "click", "done"],
}


class GamePilot:
    KEY_WHITELIST = {"w", "a", "s", "d", "space", "shift", "ctrl", "e", "q", "1", "2", "3", "4", "5", "6", "7", "8", "9", "esc"}

    def __init__(self, settings: Settings, gateway: ModelGateway, tools: ToolExecutor):
        self.settings = settings
        self.gateway = gateway
        self.tools = tools

    def run(self, context: TaskContext, goal: str, window_title: str = "Minecraft", max_minutes: int = 15) -> dict[str, Any]:
        if not getattr(self.settings, "enable_game_control", False):
            raise GamePilotError("Игровое управление выключено. Включите его в настройках после теста аварийной остановки.")
        if not self.settings.enable_desktop_control:
            raise GamePilotError("Сначала разрешите управление компьютером")
        try:
            import pyautogui
        except ImportError as exc:
            raise GamePilotError("PyAutoGUI не установлен") from exc
        pyautogui.FAILSAFE = True
        deadline = time.monotonic() + max(1, min(max_minutes, 120)) * 60
        history: list[dict[str, Any]] = []
        steps = 0
        context.set_total(max(1, min(max_minutes, 120)) * 10)
        while time.monotonic() < deadline:
            context.check_cancelled()
            steps += 1
            # Never send controls unless the intended game is the active window.
            try:
                active = pyautogui.getActiveWindow()
                active_title = (active.title if active else "") or ""
            except Exception:
                active_title = ""
            if window_title.lower() not in active_title.lower():
                context.update("Жду, пока окно Minecraft станет активным", completed_steps=steps - 1)
                time.sleep(1.0)
                continue
            shot = self.tools.tool_screenshot()
            path = Path(shot["path"])
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            prompt = (
                "Ты управляешь Minecraft как обычный игрок. Цель: " + goal + "\n"
                "Оцени только текущий кадр. Выбери короткое действие максимум на 2 секунды. "
                "Не открывай чат, не вводи команды, не используй моды и не взаимодействуй с другими аккаунтами. "
                "keys могут содержать только w,a,s,d,space,shift,ctrl,e,q,1..9,esc. "
                "mouse_dx/mouse_dy ограничь диапазоном -300..300. done=true только если цель визуально достигнута.\n"
                f"Последние наблюдения: {json.dumps(history[-5:], ensure_ascii=False)}"
            )
            message = self.gateway.chat(
                [
                    {"role": "system", "content": "Ты осторожный визуальный игровой агент."},
                    {"role": "user", "content": prompt, "images": [encoded]},
                ],
                model=self.settings.vision_model,
                temperature=0.1,
                think=False,
                num_ctx=4096,
                num_predict=500,
                response_format=GAME_SCHEMA,
            )
            try:
                decision = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                decision = {}
            keys = [str(key).lower() for key in decision.get("keys", []) if str(key).lower() in self.KEY_WHITELIST]
            duration = max(0.05, min(float(decision.get("duration") or 0.2), 2.0))
            dx = max(-300, min(int(decision.get("mouse_dx") or 0), 300))
            dy = max(-300, min(int(decision.get("mouse_dy") or 0), 300))
            if dx or dy:
                pyautogui.moveRel(dx, dy, duration=0.15)
            if decision.get("click"):
                pyautogui.click()
            held = []
            try:
                for key in keys:
                    pyautogui.keyDown(key)
                    held.append(key)
                time.sleep(duration)
            finally:
                for key in reversed(held):
                    pyautogui.keyUp(key)
            summary = str(decision.get("summary") or "Выполнен игровой шаг")[:300]
            history.append({"step": steps, "summary": summary, "keys": keys, "duration": duration})
            context.update(summary, completed_steps=steps, progress=min(0.98, steps / context.total_steps))
            if decision.get("done"):
                return {"goal": goal, "steps": steps, "status": "done", "history": history[-30:]}
        return {"goal": goal, "steps": steps, "status": "time_limit", "history": history[-30:]}
