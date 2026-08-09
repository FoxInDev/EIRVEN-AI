from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import LLMError, ModelGateway
from .style import StyleStore
from .tasks import TaskNeedsUser
from .tools import ToolExecutor


class LocalAgent:
    """Native tool-calling computer agent.

    The model never emits an intermediate JSON plan that another personality interprets.
    It sees the real tool catalogue, calls tools, receives observations, and continues in
    the same conversation until it has a factual final result.
    """

    def __init__(
        self,
        settings: Settings,
        gateway: ModelGateway,
        tools: ToolExecutor,
        style: StyleStore,
    ):
        self.settings = settings
        self.gateway = gateway
        self.tools = tools
        self.style = style

    def stop(self) -> None:
        # Emergency stop only. Normal task cancellation is passed as a scoped token.
        self.tools.stop()

    def _describe_screenshot(self, path: str, question: str = "Опиши экран для следующего действия") -> str:
        try:
            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            message = self.gateway.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты компьютерное зрение локального агента. Анализируй только видимое. "
                            "Укажи окна, текст, элементы интерфейса и приблизительные координаты "
                            "полезных элементов относительно всего изображения. Не говори, что не "
                            "имеешь доступа к экрану: изображение уже приложено."
                        ),
                    },
                    {"role": "user", "content": question, "images": [encoded]},
                ],
                model=self.settings.vision_model,
                temperature=0.1,
                think=False,
                num_ctx=min(self.settings.chat_num_ctx, 4096),
                num_predict=180,
                timeout_seconds=9,
            )
            return str(message.get("content") or "").strip()
        except Exception as exc:
            return f"Vision-анализ не удался: {exc}"

    @staticmethod
    def _tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        return name, dict(arguments) if isinstance(arguments, dict) else {}

    @staticmethod
    def _needs_user(tool_name: str, result: dict[str, Any]) -> str | None:
        if result.get("ok"):
            inner = result.get("result") or {}
            if isinstance(inner, dict) and (inner.get("ok") is False or int(inner.get("returncode", 0) or 0) != 0):
                text = json.dumps(inner, ensure_ascii=False).lower()
            else:
                return None
        else:
            text = str(result.get("error") or "").lower()
        if tool_name in {"git_publish", "powershell", "browser_fill", "window_type"} and any(
            marker in text
            for marker in (
                "auth", "authentication", "login", "sign in", "permission denied",
                "publickey", "credential", "captcha", "uac", "access denied",
                "авториз", "войд", "доступ запрещ",
            )
        ):
            return "Нужно вручную завершить авторизацию/подтверждение в открытом окне. После этого напиши «готово»."
        return None

    @staticmethod
    def _compact_result(result: dict[str, Any], limit: int = 12000) -> str:
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        return str((schema.get("function") or {}).get("name") or schema.get("name") or "")

    @staticmethod
    def _screen_only(task: str) -> bool:
        q = task.casefold()
        return any(token in q for token in (
            "текущем экране", "на экране", "это окно", "текущее окно", "здесь напис",
            "нажми", "кликни", "кнопк", "поле", "прокрут", "скролл", "видишь экран",
        ))

    def run(
        self,
        task: str,
        model: str | None = None,
        max_steps: int | None = None,
        external_stop_event: threading.Event | None = None,
        allowed_tools: set[str] | None = None,
        auto_vision: bool = False,
        require_tool_action: bool = False,
        require_side_effect: bool = False,
        require_verification: bool = False,
        num_gpu: int | None = None,
    ) -> str:
        task = task.strip()
        if not task:
            return "Пустая задача."
        steps = max(1, min(max_steps or self.settings.max_agent_steps, 32))
        tool_schemas = self.tools.native_descriptions()  # type: ignore[attr-defined]
        if allowed_tools is not None:
            tool_schemas = [s for s in tool_schemas if self._schema_name(s) in allowed_tools]
        screen_only = self._screen_only(task)
        if screen_only:
            allowed = {
                "screenshot", "desktop_state", "window_list", "window_elements",
                "window_focus", "window_click", "window_type", "mouse_move", "mouse_drag",
                "scroll", "press_key", "hotkey", "click", "type_text", "launch_application",
                "foreground_window", "media_control",
            }
            tool_schemas = [s for s in tool_schemas if self._schema_name(s) in allowed]
        selected = model or self.settings.fast_model or self.settings.model
        code_heavy = any(word in task.lower() for word in ("код", "файл", "проект", "исправ", "рефактор", "тест"))
        decision_tokens = 260 if code_heavy else 120
        # Interactive desktop work must remain bounded even for code. Long work is split
        # into multiple tool turns rather than one 120-second model call.
        decision_timeout = 9.0 if code_heavy else 6.5
        system = (
            "Ты EIRVEN — один локальный ИИ и одновременно исполнитель на компьютере владельца. "
            "Инструменты ниже — твои собственные руки, глаза, терминал и доступ к файлам. "
            "Если пользователь просит действие, не объясняй, что ты текстовая модель, и не проси "
            "его сделать то, что доступно инструментами: вызови инструмент. Не сочиняй результат. "
            "Для незнакомого приложения сначала попробуй launch_application; для неизвестного пути — "
            "system_find; для существующего пути — system_open_path. Для сайта используй open_default_url, "
            "если URL очевиден, иначе default_search. Для уже открытого интерфейса предпочитай window_list/"
            "window_elements перед координатным кликом. Для визуальной задачи screenshot — только снимок; "
            "если vision не подключён, опирайся на UI Automation/структурные данные, а не выдумывай содержимое пикселей. PowerShell используй как универсальный способ "
            "системной работы, когда структурного инструмента нет. При полном доступе PowerShell может управлять службами, сетью, Wi-Fi/VPN, реестром, темой и настройками Windows. Для поиска сбоев используй system_diagnostics. Автокликеры, локальные утилиты, Git, Docker "
            "и обычная автоматизация разрешены. Если для явной задачи не хватает безопасной зависимости вроде Git, разрешено поставить её "
            "через официальный Windows package manager и продолжить. Если нужен логин/CAPTCHA/UAC/2FA/пароль, открой нужное место, "
            "остановись и попроси владельца выполнить только этот ручной шаг. Не обходи защиту. Действуй кратчайшим практическим путём и проверяй результат. "
            "После изменяющего действия ОБЯЗАТЕЛЬНО снова наблюдай состояние подходящим read-only инструментом и только затем сообщай об успехе. "
            "Не перечисляй все процессы/файлы без причины: сначала используй самый узкий инструмент, который проверяет текущую гипотезу. "
            "Если интерфейс ещё грузится, подожди и посмотри снова вместо преждевременного отказа."
        )
        if screen_only:
            system += (
                " ВАЖНО: задача относится к уже открытому текущему экрану. Не открывай новый браузер, "
                "поиск, сайт, проект или файл. Смотри screenshot/window_elements и действуй только в "
                "текущем foreground-интерфейсе."
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        transcript: list[str] = []
        used_tool = False
        used_side_effect = False
        verified_after_effect = False
        side_effect_tools = {
            "write_file", "make_directory", "system_write_file", "system_open_path", "system_open_named",
            "powershell", "git_publish", "open_default_url", "default_search", "launch_application", "close_application",
            "close_browsers", "close_user_apps", "set_dark_theme", "toggle_quick_setting", "window_focus", "window_click",
            "window_type", "mouse_move", "mouse_drag", "scroll", "press_key", "hotkey", "click", "type_text", "media_control",
        }
        observation_tools = {
            "desktop_state", "access_status", "foreground_window", "window_list", "window_elements", "window_wait", "process_list",
            "system_find", "system_list_files", "system_read_file", "system_diagnostics", "command_available", "web_search",
        }

        with self.tools.task_scope(external_stop_event):
            for step in range(1, steps + 1):
                if external_stop_event and external_stop_event.is_set():
                    return "Остановлено пользователем."
                try:
                    response = self.gateway.chat(
                        messages,
                        model=selected,
                        temperature=0.05,
                        tools=tool_schemas,
                        think=False,
                        num_ctx=min(self.settings.task_num_ctx, 2304),
                        num_predict=decision_tokens,
                        keep_alive="45s",
                        timeout_seconds=decision_timeout,
                        num_gpu=num_gpu,
                    )
                except LLMError as exc:
                    if transcript:
                        return "\n".join(transcript + [f"Модель остановилась: {exc}"])
                    return f"Не удалось выполнить задачу: {exc}"

                tool_calls = list(response.get("tool_calls") or [])
                content = str(response.get("content") or "").strip()
                assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                messages.append(assistant_message)

                if not tool_calls:
                    if require_tool_action and not used_tool:
                        return "Не удалось выполнить задачу: модель не выбрала ни одного реального действия на компьютере."
                    if require_side_effect and not used_side_effect:
                        return "Не удалось выполнить задачу: модель только посмотрела состояние, но не выполнила требуемое изменение."
                    if require_verification and used_side_effect and not verified_after_effect and step < steps:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Изменяющее действие уже было выполнено, но результат ещё не проверен. "
                                "Не завершай задачу. Используй read-only наблюдение (window_elements/window_list/process_list/"
                                "system_read_file/command_available/window_wait и т.п.) и подтверди фактическое состояние."
                            ),
                        })
                        continue
                    if require_verification and used_side_effect and not verified_after_effect:
                        return "Не удалось надёжно подтвердить результат после выполненного действия."
                    return content or ("\n".join(transcript) if transcript else "Задача завершена без текстового результата.")

                for call in tool_calls:
                    name, arguments = self._tool_call(call)
                    if not name:
                        continue
                    result = self.tools.execute(name, arguments)
                    used_tool = True
                    if name in side_effect_tools:
                        used_side_effect = True
                        verified_after_effect = False
                    elif used_side_effect and name in observation_tools and result.get("ok"):
                        verified_after_effect = True
                    if auto_vision and name == "screenshot" and result.get("ok"):
                        path = str((result.get("result") or {}).get("path") or "")
                        if path:
                            result.setdefault("result", {})["vision_analysis"] = self._describe_screenshot(path, task)
                    prompt = self._needs_user(name, result)
                    if prompt:
                        raise TaskNeedsUser(prompt)
                    transcript.append(f"{name}: {self._compact_result(result, 1800)}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": name,
                            "content": self._compact_result(result, 5000),
                        }
                    )

        return "Достигнут лимит шагов. Последние действия:\n" + "\n".join(transcript[-6:])
