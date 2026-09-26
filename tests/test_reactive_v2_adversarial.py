from __future__ import annotations

import copy
import json
import random
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from eirven_ai.agent import LocalAgent
from eirven_ai.risk_policy import RiskPolicy
from eirven_ai.tasks import TaskNeedsUser
from eirven_ai.universal_workflow import UniversalWorkflowEngine


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments or {}}}]}


def _schema(name: str, description: str = "") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        max_agent_steps=32,
        fast_model="fake",
        model="fake",
        code_model="fake",
        vision_model="fake-vision",
        task_num_ctx=4096,
        chat_num_ctx=4096,
        llm_first_token_timeout=2.0,
        keep_alive="1m",
        workspace_dir="C:/work",
    )


class ScriptedGateway:
    def __init__(self, responses: list[dict[str, Any]], events: list[Any] | None = None):
        self.responses = list(responses)
        self.events = events if events is not None else []
        self.calls: list[list[dict[str, Any]]] = []

    @staticmethod
    def model_capabilities(_model: str) -> list[str]:
        return ["tools"]

    @staticmethod
    def installed_models() -> list[str]:
        return ["fake"]

    def chat(self, messages: list[dict[str, Any]], *_args, **_kwargs) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(messages))
        self.events.append(("model", len(self.calls)))
        if self.responses:
            return copy.deepcopy(self.responses.pop(0))
        return {"content": "Цель подтверждена.", "tool_calls": []}

    def json(self, *_args, **_kwargs) -> dict[str, Any]:
        return {"achieved": True, "reason": "fake postcondition binds to goal", "missing": ""}


class ScriptedTools:
    def __init__(
        self,
        schemas: list[str],
        handlers: dict[str, Any],
        events: list[Any] | None = None,
    ):
        self.schemas = list(schemas)
        self.handlers = dict(handlers)
        self.events = events if events is not None else []
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def native_descriptions(self) -> list[dict[str, Any]]:
        descriptions = {
            "window_list": "Получить список окон",
            "window_elements": "Прочитать элементы окна",
            "foreground_window": "Получить текущее окно",
            "screenshot": "Наблюдать снимок окна",
            "service_inventory": "Получить список доступных сервисов",
        }
        return [_schema(name, descriptions.get(name, name)) for name in self.schemas]

    @staticmethod
    def task_scope(_stop=None):
        return nullcontext()

    @staticmethod
    def stop() -> None:
        return None

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = copy.deepcopy(arguments)
        self.executed.append((name, args))
        self.events.append(("tool", name, args))
        handler = self.handlers.get(name)
        if isinstance(handler, list):
            if not handler:
                raise AssertionError(f"No scripted result left for {name}")
            value = handler.pop(0)
        elif callable(handler):
            value = handler(args)
        elif handler is not None:
            value = handler
        else:
            value = {"ok": False, "error": f"unscripted tool: {name}"}
        return copy.deepcopy(value)


def _window(handle: int = 42, title: str = "Checkout", pid: int = 700) -> dict[str, Any]:
    return {"handle": handle, "title": title, "pid": pid, "rectangle": [0, 0, 1200, 900]}


def _agent(gateway: ScriptedGateway, tools: ScriptedTools) -> LocalAgent:
    return LocalAgent(_settings(), gateway, tools, SimpleNamespace())


def test_verified_tool_target_surface_outranks_ambient_chatgpt() -> None:
    """Regression for the real open_service -> ChatGPT surface theft incident."""
    target = _window(67120, "Без имени - Google Chrome", 101)
    ambient = _window(591484, "ChatGPT - Google Chrome", 202)
    gateway = ScriptedGateway([
        _call("open_service", {"service": "SoundCloud"}),
        _call("window_elements"),
    ])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements"],
        {
            "open_service": {
                "ok": True,
                "verified": True,
                "completed": True,
                "result": {"ok": True, "verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": ambient},
            "window_elements": {"ok": True, "result": [{"name": "Play", "stable_id": "play"}]},
        },
    )
    _agent(gateway, tools).run(
        "Открой SoundCloud",
        owner_goal="Открой SoundCloud",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=4,
    )
    element_calls = [args for name, args in tools.executed if name == "window_elements"]
    assert element_calls and element_calls[0]["handle"] == target["handle"]
    assert element_calls[0]["handle"] != ambient["handle"]


@pytest.mark.parametrize("service", ["SoundCloud", "Deezer", "Bandcamp"])
def test_unknown_music_service_is_not_a_fixed_catalogue_boundary(service: str) -> None:
    class CapturingAgent:
        def __init__(self):
            self.kwargs: dict[str, Any] = {}

        def run(self, prompt: str, **kwargs: Any) -> str:
            self.kwargs = {"prompt": prompt, **kwargs}
            return "Открыла сервис и прочитала поверхность."

        @staticmethod
        def last_run_outcome() -> dict[str, Any]:
            return {"goal_verified": True, "verified": True, "used_side_effect": True}

    agent = CapturingAgent()
    services = SimpleNamespace(
        tools=SimpleNamespace(execute=lambda *_args, **_kwargs: {"ok": False}),
        gateway=ScriptedGateway([]),
        desktop_operator=None,
        desktop_lock=threading.RLock(),
        agent=agent,
        settings=_settings(),
        capabilities=SimpleNamespace(snapshot=lambda: {"services": {service: "discoverable"}}),
        style=None,
    )
    engine = UniversalWorkflowEngine(services)
    engine._agent_num_gpu = lambda: None  # type: ignore[method-assign]
    result = engine._execute_goal(
        {"goal": f"Открой {service}", "original_task": f"Открой {service}", "reactive_v2": True},
        lambda _q: (False, "", {}),
    )
    assert result["ok"] is True
    assert agent.kwargs["allowed_tools"] is None
    assert service in agent.kwargs["prompt"]
    assert "open_service" in agent.kwargs["prompt"]


def test_bare_media_clarification_is_engine_owned_and_free_form() -> None:
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["media_control", "window_list", "window_elements", "open_service", "launch_application"],
        {
            "media_control": {"ok": True, "result": {"action": "list", "sessions": [], "verified": True}},
            "window_list": {"ok": True, "result": []},
        },
    )
    agent = _agent(gateway, tools)
    with pytest.raises(TaskNeedsUser) as suspended:
        agent.run(
            "Включи музыку",
            owner_goal="Включи музыку",
            require_tool_action=True,
            max_steps=2,
        )
    assert suspended.value.prompt.strip()
    outcome = agent.last_run_outcome()
    assert outcome.get("needs_user") is True
    assert outcome.get("clarification") is True
    assert outcome.get("clarification_prompt") == suspended.value.prompt
    assert outcome.get("clarification_kind") == "media_owner"
    assert outcome.get("missing_fields") == ["service_or_player"]
    assert outcome.get("allow_free_text") is True
    assert gateway.calls == []
    assert not any(name in {"open_service", "launch_application"} for name, _ in tools.executed)


def test_model_clarification_does_not_require_question_mark() -> None:
    gateway = ScriptedGateway([{"content": "Назови точку отправления.", "tool_calls": []}])
    tools = ScriptedTools(["window_list"], {})
    agent = _agent(gateway, tools)
    with pytest.raises(TaskNeedsUser, match="Назови точку отправления"):
        agent.run(
            "Узнай время в пути до Мытищ",
            owner_goal="Узнай время в пути до Мытищ",
            require_tool_action=True,
            max_steps=2,
        )
    assert agent.last_run_outcome().get("clarification") is True


def test_future_media_service_grounding_uses_owner_words_not_catalogue() -> None:
    service = "NebulaAudio7421"
    assert LocalAgent._media_service_is_grounded(f"Включи музыку в {service}", service)
    assert LocalAgent._media_service_is_grounded(
        f"Включи музыку\n\nУточняющий вопрос Эрви: В каком сервисе?\nОтвет владельца: {service}",
        service,
    )
    assert not LocalAgent._media_service_is_grounded("Включи музыку", service)
    assert not LocalAgent._media_service_is_grounded("Включи Мою волну", "Моя волна")


@pytest.mark.parametrize("service", ["SoundCloud", "Deezer", "Bandcamp", "FutureBrand7421"])
def test_explicit_media_service_is_engine_bound_and_opened_exactly_once(service: str) -> None:
    target = _window(7301, f"{service} - Browser", 915)
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": {
                "ok": True,
                "result": [{"name": "Play", "stable_id": "main-play", "surface_handle": target["handle"]}],
            },
        },
    )
    _agent(gateway, tools).run(
        f"Включи музыку в {service}",
        owner_goal=f"Включи музыку в {service}",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=2,
    )
    opener_calls = [args for name, args in tools.executed if name == "open_service"]
    assert opener_calls == [{"service": service}]
    assert tools.executed[0] == ("open_service", {"service": service})
    elements = [args for name, args in tools.executed if name == "window_elements"]
    assert len(elements) == 1
    assert elements[0]["handle"] == target["handle"]
    assert elements[0]["title_contains"] == target["title"]
    assert gateway.calls == []


def test_clarified_media_provider_keeps_exact_owner_slot_and_typed_purpose() -> None:
    target = _window(7311, "Яндекс Музыка - Browser", 916)
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": {
                "ok": True,
                "result": [{"name": "Play", "stable_id": "main-play", "surface_handle": target["handle"]}],
            },
        },
    )
    owner_goal = (
        "Включи музыку\n\nУточняющий вопрос Эрви: "
        "В каком сервисе или плеере включить музыку?\nОтвет владельца: яндекс"
    )
    _agent(gateway, tools).run(
        owner_goal,
        owner_goal=owner_goal,
        owner_slots={"service_or_player": "яндекс"},
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=2,
    )
    opener_calls = [args for name, args in tools.executed if name == "open_service"]
    assert opener_calls == [{"service": "яндекс", "purpose": "музыка"}]
    assert gateway.calls == []


def test_media_service_slot_rejects_content_and_quality_modifiers() -> None:
    assert LocalAgent._explicit_media_service_slot("Включи Мою волну") == ""
    assert LocalAgent._explicit_media_service_slot("Включи музыку в хорошем качестве") == ""
    assert LocalAgent._explicit_media_service_slot(
        "Включи музыку\nУточняющий вопрос Эрви: В каком сервисе?\nОтвет владельца: nova audio"
    ) == "nova audio"
    assert LocalAgent._explicit_media_service_slot(
        "Запусти Мою волну в Яндекс Музыке"
    ) == "Яндекс Музыке"
    assert LocalAgent._explicit_media_service_slot(
        "Запусти трек в FutureBrand7421"
    ) == "FutureBrand7421"
    assert LocalAgent._media_content_for_service(
        "Запусти Мою волну в Яндекс Музыке", "Яндекс Музыке"
    ) == "Мою волну"
    assert LocalAgent._media_content_for_service(
        "Включи музыку в SoundCloud", "SoundCloud"
    ) == ""
    assert LocalAgent._media_content_for_service(
        "Включи музыку в хорошем качестве в SoundCloud", "SoundCloud"
    ) == ""


def test_named_content_is_selected_before_transport_and_verified_by_live_metadata() -> None:
    target = _window(7366, "Яндекс Музыка - Browser", 921)
    main_play = {
        "name": "Воспроизведение", "control_type": "Button", "stable_id": "main-play",
        "surface_handle": target["handle"], "parent_name": "Плеер",
        "class_name": "PlayerControls_playButton", "rectangle": [900, 700, 980, 780],
    }
    wave = {
        "name": "Моя волна", "control_type": "Hyperlink", "stable_id": "my-wave",
        "surface_handle": target["handle"], "parent_name": "Навигация",
        "rectangle": [120, 180, 260, 230],
    }
    gateway = ScriptedGateway([
        {"content": "Моя волна запущена и подтверждена metadata.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements", "window_click", "media_control"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": {"ok": True, "result": [main_play, wave]},
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
            "media_control": {
                "ok": True,
                "result": {
                    "action": "list", "verified": True,
                    "sessions": [{
                        "session_id": "wave-session", "state": "playing",
                        "title": "Моя волна", "artist": "Персональная станция",
                    }],
                },
            },
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Запусти Мою волну в Яндекс Музыке",
        owner_goal="Запусти Мою волну в Яндекс Музыке",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=7,
    )
    assert "подтверждена" in answer
    assert [args["service"] for name, args in tools.executed if name == "open_service"] == ["Яндекс Музыке"]
    clicks = [args for name, args in tools.executed if name == "window_click"]
    assert len(clicks) == 1 and clicks[0]["stable_id"] == "my-wave"
    assert all(args.get("stable_id") != "main-play" for args in clicks)
    assert agent.last_run_outcome().get("goal_verified") is True


def test_explicit_service_uses_fresh_main_control_and_verifies_pause_state() -> None:
    target = _window(7355, "SoundCloud - Browser", 919)
    play = {
        "name": "Play", "control_type": "Button", "stable_id": "service-main-play",
        "surface_handle": target["handle"], "parent_name": "Player",
        "class_name": "MainPlayerControls_playButton", "rectangle": [900, 700, 980, 780],
    }
    pause = {
        "name": "Pause", "control_type": "Button", "stable_id": "service-main-pause",
        "surface_handle": target["handle"], "parent_name": "Player",
        "class_name": "MainPlayerControls_pauseButton", "rectangle": [900, 700, 980, 780],
    }
    gateway = ScriptedGateway([
        {"content": "Воспроизведение подтверждено на названной поверхности.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements", "window_click"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": [
                {"ok": True, "result": [play]},
                {"ok": True, "result": [pause]},
            ],
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Включи музыку в SoundCloud",
        owner_goal="Включи музыку в SoundCloud",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=6,
    )
    assert "подтверждено" in answer
    assert agent.last_run_outcome().get("goal_verified") is True
    clicks = [args for name, args in tools.executed if name == "window_click"]
    assert len(clicks) == 1
    assert clicks[0]["stable_id"] == "service-main-play"


def test_typed_media_service_without_preposition_opens_verbatim_before_transport() -> None:
    """The semantic envelope, not a second phrase parser, owns the provider slot."""
    target = _window(7360, "Яндекс Музыка - Browser", 920)
    play = {
        "name": "Play", "control_type": "Button", "stable_id": "yandex-main-play",
        "surface_handle": target["handle"], "parent_name": "Player",
        "class_name": "MainPlayerControls_playButton", "rectangle": [900, 700, 980, 780],
    }
    pause = {
        "name": "Pause", "control_type": "Button", "stable_id": "yandex-main-pause",
        "surface_handle": target["handle"], "parent_name": "Player",
        "class_name": "MainPlayerControls_pauseButton", "rectangle": [900, 700, 980, 780],
    }
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements", "window_click", "media_control"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": [
                {"ok": True, "result": [play]},
                {"ok": True, "result": [pause]},
            ],
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Включи Яндекс Музыку",
        owner_goal="Включи Яндекс Музыку",
        model_first_action={
            "action": "media_control",
            "media_action": "play",
            "service": "Яндекс Музыка",
            "target": "Яндекс Музыка",
            "target_kind": "service",
            "decision_status": "ok",
        },
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=6,
    )
    assert tools.executed[0] == ("open_service", {"service": "Яндекс Музыка"})
    assert [name for name, _args in tools.executed].count("open_service") == 1
    assert not any(name == "media_control" for name, _args in tools.executed)
    assert any(name == "window_click" and args["stable_id"] == "yandex-main-play" for name, args in tools.executed)
    assert agent.last_run_outcome().get("goal_verified") is True
    assert "подтвержден" in answer.casefold()


def test_ambiguous_service_scene_uses_vision_then_new_uia_and_exact_stable_id() -> None:
    target = _window(7399, "SoundCloud - Browser", 929)
    preview = {
        "name": "Play", "control_type": "Button", "stable_id": "preview-play",
        "surface_handle": target["handle"], "parent_name": "Recommendation card",
        "class_name": "PlayButtonWithCover", "rectangle": [20, 200, 70, 250],
    }
    visually_main = {
        "name": "Play", "control_type": "Button", "stable_id": "visual-main-play",
        "surface_handle": target["handle"], "class_name": "SoundButton",
        "rectangle": [900, 700, 980, 780],
    }
    pause = {
        "name": "Pause", "control_type": "Button", "stable_id": "main-pause",
        "surface_handle": target["handle"], "parent_name": "Player",
        "class_name": "PlayerControls_pauseButton", "rectangle": [900, 700, 980, 780],
    }
    gateway = ScriptedGateway([
        {"content": "Воспроизведение подтверждено интерфейсом.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements", "screenshot", "window_click"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": [
                {"ok": True, "result": [preview, visually_main]},
                {"ok": True, "result": [preview, visually_main]},
                {"ok": True, "result": [pause]},
            ],
            "screenshot": {
                "ok": True,
                "result": {
                    "path": "C:/fake/current.png", "width": 1200, "height": 900,
                    "coordinate_origin": {"x": 0, "y": 0}, "surface_handle": target["handle"],
                },
            },
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    vision_calls: list[tuple[Any, ...]] = []

    def structured_vision(*args):
        vision_calls.append(args)
        return {
            "summary": "Открыт плеер", "screen_state": "ready", "goal_reached": False,
            "primary_target": {
                "label": "Play", "role": "button", "action": "click", "x": 940, "y": 740,
                "confidence": 0.96, "evidence": "Главная нижняя transport-кнопка",
            },
            "controls": [], "blockers": [],
        }

    agent._describe_screenshot = structured_vision
    answer = agent.run(
        "Включи музыку в SoundCloud",
        owner_goal="Включи музыку в SoundCloud",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        auto_vision=True,
        max_steps=8,
    )
    assert "подтверждено" in answer
    assert vision_calls and any(row.get("stable_id") == "visual-main-play" for row in vision_calls[0][3])
    core = [name for name, _args in tools.executed if name in {"open_service", "window_elements", "screenshot", "window_click"}]
    assert core == ["open_service", "window_elements", "screenshot", "window_elements", "window_click", "window_elements"]
    click = next(args for name, args in tools.executed if name == "window_click")
    assert click == {
        "handle": target["handle"], "title_contains": target["title"], "stable_id": "visual-main-play",
    }
    assert "x" not in click and "y" not in click


def test_unmapped_media_vision_stops_with_one_clarification_and_no_click() -> None:
    target = _window(7407, "FutureBrand7421 - Browser", 932)
    row = {
        "name": "Play", "control_type": "Button", "stable_id": "preview-only",
        "surface_handle": target["handle"], "parent_name": "Recommendation card",
        "class_name": "PlayButtonWithCover", "rectangle": [20, 200, 70, 250],
    }
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_elements", "screenshot", "window_click"],
        {
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": [
                {"ok": True, "result": [row]},
                {"ok": True, "result": [row]},
            ],
            "screenshot": {
                "ok": True,
                "result": {
                    "path": "C:/fake/current.png", "width": 1200, "height": 900,
                    "coordinate_origin": {"x": 0, "y": 0}, "surface_handle": target["handle"],
                },
            },
        },
    )
    agent = _agent(gateway, tools)
    agent._describe_screenshot = lambda *_args: {
        "summary": "Нет готового плеера", "screen_state": "blocked", "goal_reached": False,
        "primary_target": {
            "label": "Play", "role": "button", "action": "click", "x": 940, "y": 740,
            "confidence": 0.91, "evidence": "Возможная кнопка, не подтверждённая UIA",
        },
        "controls": [], "blockers": [],
    }
    with pytest.raises(TaskNeedsUser) as exc:
        agent.run(
            "Включи музыку в FutureBrand7421",
            owner_goal="Включи музыку в FutureBrand7421",
            require_tool_action=True,
            require_side_effect=True,
            require_verification=True,
            auto_vision=True,
            max_steps=8,
        )
    assert "готовый экран плеера" in exc.value.prompt
    core = [name for name, _args in tools.executed if name in {"open_service", "window_elements", "screenshot", "window_click"}]
    assert core == ["open_service", "window_elements", "screenshot", "window_elements"]
    assert agent.last_run_outcome().get("clarification_kind") == "media_surface"


def test_window_list_compaction_preserves_surface_identity() -> None:
    window = _window(8842, "Future service - Browser", 661)
    compact = json.loads(LocalAgent._compact_result({"ok": True, "result": [window]}, goal="Открой сервис"))
    assert compact["result"] == [{
        "title": window["title"], "handle": window["handle"], "pid": window["pid"],
        "rectangle": window["rectangle"],
    }]


def test_no_sessions_named_content_is_selected_before_transport() -> None:
    window = _window(8123, "Музыкальный плеер - Chromium", 903)
    preview = {
        "name": "Воспроизведение", "control_type": "Button", "stable_id": "preview-play",
        "surface_handle": window["handle"], "parent_name": "Карточка рекомендации",
        "class_name": "PlayButtonWithCover", "rectangle": [20, 200, 70, 250],
    }
    main = {
        "name": "Воспроизведение", "control_type": "Button", "stable_id": "main-play",
        "surface_handle": window["handle"], "parent_name": "Плеер",
        "class_name": "VibePlayerControls_playButton", "rectangle": [900, 700, 980, 780],
    }
    wave = {
        "name": "Моя волна", "control_type": "Hyperlink", "stable_id": "my-wave",
        "surface_handle": window["handle"], "parent_name": "Навигация",
        "rectangle": [120, 180, 260, 230],
    }
    events: list[Any] = []
    gateway = ScriptedGateway([
        _call("media_control", {"action": "play", "target": "Моя волна"}),
        {"content": "Моя волна включена и подтверждена.", "tool_calls": []},
    ], events)
    tools = ScriptedTools(
        ["media_control", "window_list", "window_elements", "window_focus", "window_click", "foreground_window"],
        {
            "media_control": [
                {
                    "ok": False,
                    "error": "Не выбрала медиасессию",
                    "result": {
                        "action": "play", "target": "Моя волна", "selection": "no_sessions",
                        "sessions": [], "executed": False, "completed": False, "verified": False,
                    },
                },
                {
                    "ok": True,
                    "result": {
                        "action": "list", "verified": True,
                        "sessions": [{"session_id": "new-session", "state": "playing", "title": "Моя волна"}],
                    },
                },
            ],
            "window_list": {"ok": True, "result": [window]},
            "window_elements": [
                {"ok": True, "result": [preview, main, wave]},
                {"ok": True, "result": [preview, main, wave]},
            ],
            "window_focus": {"ok": True, "result": {**window, "verified": True}},
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
            "foreground_window": {"ok": True, "result": window},
        },
        events,
    )
    answer = _agent(gateway, tools).run(
        "Включи Мою волну",
        owner_goal="Включи Мою волну",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=8,
    )
    assert "подтверждена" in answer
    clicks = [args for name, args in tools.executed if name == "window_click"]
    assert clicks == [{
        "stable_id": "my-wave", "handle": window["handle"],
        "title_contains": window["title"],
    }]
    mutating_media = [args for name, args in tools.executed if name == "media_control" and args.get("action") != "list"]
    assert mutating_media == [{"action": "play", "target": "Моя волна"}]


def test_no_session_for_owner_named_service_opens_it_without_reasking_service() -> None:
    """A failed OS transport is an observation, not loss of the typed service slot."""
    target = _window(8331, "Яндекс Музыка - Google Chrome", 955)
    gateway = ScriptedGateway([
        _call("open_service", {"service": "Яндекс Музыку"}),
        {"content": "Сервис открыт; продолжаю по его свежей поверхности.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["media_control", "open_service", "foreground_window", "window_elements"],
        {
            "media_control": {
                "ok": False,
                "error": "Не выбрала медиасессию",
                "result": {
                    "action": "play", "target": "Яндекс Музыку",
                    "selection": "no_sessions", "sessions": [],
                    "executed": False, "completed": False, "verified": False,
                },
            },
            "open_service": {
                "ok": True, "verified": True, "completed": True,
                "result": {"verified": True, "completed": True, "window": target},
            },
            "foreground_window": {"ok": True, "result": target},
            "window_elements": {
                "ok": True,
                "result": [{
                    "name": "Воспроизведение", "control_type": "Button",
                    "stable_id": "main-play", "surface_handle": target["handle"],
                }],
            },
        },
    )
    agent = _agent(gateway, tools)

    agent.run(
        "Включи Яндекс Музыку",
        owner_goal="Включи Яндекс Музыку",
        model_first_action={
            "action": "media_control", "target": "Яндекс Музыку",
            "target_kind": "service", "service": "Яндекс Музыку",
            "media_action": "play", "media_content": "",
            "single_step": "false", "decision_status": "ok",
        },
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=6,
    )

    opener_calls = [args for name, args in tools.executed if name == "open_service"]
    assert opener_calls
    assert opener_calls[0] == {"service": "Яндекс Музыку"}


def test_bare_media_ui_recovery_accepts_play_to_pause_state_change() -> None:
    window = _window(8444, "Открытый веб-плеер", 944)
    play = {
        "name": "Воспроизведение", "control_type": "Button", "stable_id": "fresh-play",
        "surface_handle": window["handle"], "parent_name": "Плеер",
        "class_name": "MainPlayerControls_playButton", "rectangle": [900, 700, 980, 780],
    }
    pause = {
        "name": "Пауза", "control_type": "Button", "stable_id": "fresh-pause",
        "surface_handle": window["handle"], "parent_name": "Плеер",
        "class_name": "MainPlayerControls_pauseButton", "rectangle": [900, 700, 980, 780],
    }
    gateway = ScriptedGateway([
        {"content": "Воспроизведение подтверждено интерфейсом плеера.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["media_control", "window_list", "window_elements", "window_focus", "window_click", "foreground_window"],
        {
            "media_control": [
                {"ok": True, "result": {"action": "list", "sessions": [], "verified": True}},
                {"ok": True, "result": {"action": "list", "sessions": [], "verified": True}},
            ],
            "window_list": {"ok": True, "result": [window]},
            "window_elements": [
                {"ok": True, "result": [play]},
                {"ok": True, "result": [play]},
                {"ok": True, "result": [pause]},
            ],
            "window_focus": {"ok": True, "result": {**window, "verified": True}},
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
            "foreground_window": {"ok": True, "result": window},
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Включи музыку",
        owner_goal="Включи музыку",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=8,
    )
    assert "подтверждено" in answer
    assert agent.last_run_outcome().get("goal_verified") is True
    assert [args["stable_id"] for name, args in tools.executed if name == "window_click"] == ["fresh-play"]


def test_verified_native_pause_terminates_without_falling_through_to_uia_recovery() -> None:
    """A typed GSMTC pause postcondition completes the goal by itself.

    Regression for the live failure where ``media_control`` returned a verified
    ``paused`` state, but the agent continued into UIA recovery and eventually
    reported that the first stage could not be completed.
    """
    # If control leaked back to the policy model, it would immediately start an
    # unnecessary desktop exploration.  A trusted single-step postcondition must
    # therefore finish before this adversarial response can be consumed.
    gateway = ScriptedGateway([_call("window_list")])
    tools = ScriptedTools(
        ["media_control", "window_list", "window_elements", "foreground_window"],
        {
            "media_control": {
                "ok": True,
                "completed": True,
                "verified": True,
                "result": {
                    "action": "pause",
                    "executed": True,
                    "completed": True,
                    "verified": True,
                    "before": {"state": "playing", "session_id": "browser-session"},
                    "after": {"state": "paused", "session_id": "browser-session"},
                },
            },
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Поставь музыку на паузу",
        owner_goal="Поставь музыку на паузу",
        model_first_action={
            "action": "media_control",
            "media_action": "pause",
            "service": "",
            "target": "",
            "target_kind": "none",
            "single_step": True,
            "decision_status": "ok",
        },
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=5,
    )

    assert "пауз" in answer.casefold()
    assert gateway.calls == []
    assert tools.executed == [("media_control", {"action": "pause"})]
    outcome = agent.last_run_outcome()
    assert outcome.get("effect_verified") is True
    assert outcome.get("goal_verified") is True


def test_route_question_is_admitted_without_template_or_classifier() -> None:
    services = SimpleNamespace(
        tools=None,
        gateway=SimpleNamespace(json=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline"))),
        desktop_operator=None,
        desktop_lock=threading.RLock(),
        db=SimpleNamespace(),
        settings=_settings(),
    )
    engine = UniversalWorkflowEngine(services)
    assert engine.should_handle("Сколько до Мытищ ехать?")
    assert engine.should_handle("Посмотри, сколько времени добираться до Мытищ")


def test_same_control_can_be_used_again_after_a_fresh_changed_scene() -> None:
    window = _window()
    scenes = [
        {"ok": True, "result": [{"name": "Шаг 1", "stable_id": "next"}]},
        {"ok": True, "result": [{"name": "Шаг 2", "stable_id": "next"}]},
        {"ok": True, "result": [{"name": "Готово", "stable_id": "done"}]},
    ]
    gateway = ScriptedGateway([
        _call("window_elements"),
        _call("window_click", {"stable_id": "next", "element_text": "Далее"}),
        _call("window_elements"),
        _call("window_click", {"stable_id": "next", "element_text": "Далее"}),
        _call("window_elements"),
        {"content": "Оба перехода подтверждены.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["foreground_window", "window_elements", "window_click"],
        {
            "foreground_window": {"ok": True, "result": window},
            "window_elements": scenes,
            "window_click": {
                "ok": True, "completed": True,
                "result": {"completed": True, "surface_handle": window["handle"]},
            },
        },
    )
    answer = _agent(gateway, tools).run(
        "На этом экране нажми Далее два раза, проверяя каждый новый шаг",
        owner_goal="На этом экране нажми Далее два раза, проверяя каждый новый шаг",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=10,
    )
    assert "подтверждены" in answer
    assert sum(name == "window_click" for name, _ in tools.executed) == 2


def test_popup_form_chain_rebinds_only_to_causally_related_surface() -> None:
    main = _window(301, "State lab", 880)
    popup = _window(302, "State lab form", 880)
    success = _window(303, "State lab result", 880)
    gateway = ScriptedGateway([
        _call("window_elements"),
        _call("window_click", {"stable_id": "open-form", "element_text": "Открыть форму"}),
        _call("window_elements"),
        _call("window_type", {"stable_id": "value", "text": "alpha", "replace": True}),
        _call("window_elements"),
        _call("window_click", {"stable_id": "apply", "element_text": "Применить"}),
        _call("window_elements"),
        {"content": "Форма применена, конечное состояние видно.", "tool_calls": []},
    ])
    tools = ScriptedTools(
        ["foreground_window", "window_elements", "window_click", "window_type"],
        {
            # initial foreground, post open-form, post type, post apply
            "foreground_window": [
                {"ok": True, "result": main},
                {"ok": True, "result": popup},
                {"ok": True, "result": popup},
                {"ok": True, "result": success},
            ],
            "window_elements": [
                {"ok": True, "result": [{"name": "Открыть форму", "stable_id": "open-form"}]},
                {"ok": True, "result": [{"name": "Значение", "stable_id": "value"}]},
                {"ok": True, "result": [{"name": "alpha", "stable_id": "value"}, {"name": "Применить", "stable_id": "apply"}]},
                {"ok": True, "result": [{"name": "Применено: alpha", "stable_id": "result"}]},
            ],
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
            "window_type": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "На экране открой форму, заполни значение alpha и примени",
        owner_goal="На экране открой форму, заполни значение alpha и примени",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=12,
    )
    typed = [args for name, args in tools.executed if name == "window_type"]
    clicks = [args for name, args in tools.executed if name == "window_click"]
    assert "конечное состояние" in answer
    assert typed and typed[0]["handle"] == popup["handle"]
    assert clicks[1]["handle"] == popup["handle"]
    final_observation = [args for name, args in tools.executed if name == "window_elements"][-1]
    assert final_observation["handle"] == success["handle"]


@pytest.mark.parametrize("seed", range(8))
def test_seeded_observe_action_chains_terminate_without_dropping_actions(seed: int) -> None:
    """Broad deterministic state-machine lab, not an application scenario."""
    rng = random.Random(seed)
    transitions = rng.randint(2, 6)
    window = _window(100 + seed, f"State lab {seed}", 900 + seed)
    responses: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for index in range(transitions):
        responses.append(_call("window_elements"))
        responses.append(_call("window_click", {"stable_id": "advance", "element_text": "Продолжить"}))
        observations.append({"ok": True, "result": [{"name": f"state-{index}", "stable_id": "advance"}]})
    responses.extend([
        _call("window_elements"),
        {"content": "Цепочка достигла конечного состояния.", "tool_calls": []},
    ])
    observations.append({"ok": True, "result": [{"name": "terminal", "stable_id": "terminal"}]})
    gateway = ScriptedGateway(responses)
    tools = ScriptedTools(
        ["foreground_window", "window_elements", "window_click"],
        {
            "foreground_window": {"ok": True, "result": window},
            "window_elements": observations,
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    agent.run(
        "На экране пройди последовательность до конечного состояния",
        owner_goal="На экране пройди последовательность до конечного состояния",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        max_steps=2 * transitions + 4,
    )
    assert sum(name == "window_click" for name, _ in tools.executed) == transitions
    assert not agent.last_run_outcome().get("step_limit")


def test_identical_observation_loop_is_bounded_and_reports_recovery_exhaustion() -> None:
    window = _window(808, "Frozen state lab", 881)
    gateway = ScriptedGateway([_call("window_elements") for _ in range(25)])
    tools = ScriptedTools(
        ["foreground_window", "window_elements"],
        {
            "foreground_window": {"ok": True, "result": window},
            "window_elements": {
                "ok": True,
                "result": [{"name": "Неизменное состояние", "stable_id": "frozen"}],
            },
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Посмотри на экране, когда состояние изменится",
        owner_goal="Посмотри на экране, когда состояние изменится",
        require_tool_action=True,
        max_steps=25,
    )
    observations = sum(name == "window_elements" for name, _ in tools.executed)
    assert observations <= 17
    assert "сцена не меняется" in answer
    assert agent.last_run_outcome().get("recovery_exhausted") is True


def test_alternating_observers_do_not_reset_stagnation_budget() -> None:
    window = _window(918, "Frozen multi-observer lab", 882)
    counter = {"desktop": 0}

    def desktop_state(_args: dict[str, Any]) -> dict[str, Any]:
        counter["desktop"] += 1
        return {
            "ok": True,
            "result": {
                "path": f"C:/volatile/capture-{counter['desktop']}.png",
                "window": {"title": window["title"], "handle": window["handle"]},
            },
        }

    cycle = ["window_list", "foreground_window", "desktop_state", "window_elements"] * 6
    gateway = ScriptedGateway([_call(name) for name in cycle])
    tools = ScriptedTools(
        ["window_list", "foreground_window", "desktop_state", "window_elements"],
        {
            "window_list": {"ok": True, "result": [window]},
            "foreground_window": {"ok": True, "result": window},
            "desktop_state": desktop_state,
            "window_elements": {
                "ok": True,
                "result": [{"name": "Неизменное состояние", "stable_id": "frozen", "surface_handle": window["handle"]}],
            },
        },
    )
    agent = _agent(gateway, tools)
    answer = agent.run(
        "Посмотри на текущем экране, когда состояние изменится",
        owner_goal="Посмотри на текущем экране, когда состояние изменится",
        require_tool_action=True,
        max_steps=24,
    )
    model_observations = [name for name, _args in tools.executed if name in set(cycle)]
    assert len(model_observations) <= 10  # includes the initial foreground lease probe
    assert "сцена не меняется" in answer
    assert agent.last_run_outcome().get("recovery_exhausted") is True
    assert not agent.last_run_outcome().get("step_limit")


def test_exact_confirmation_replays_saved_tool_and_arguments_once() -> None:
    window = _window(42, "Оплата заказа 4 990 ₽", 700)
    first_events: list[Any] = []
    first_gateway = ScriptedGateway([
        _call("window_click", {"stable_id": "pay", "element_text": "Оплатить 4 990 ₽"}),
    ], first_events)
    handlers = {
        "foreground_window": {"ok": True, "result": window},
        "window_list": {"ok": True, "result": [window]},
        "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        "window_elements": {"ok": True, "result": [{"name": "Заказ оплачен", "stable_id": "paid"}]},
    }
    first_tools = ScriptedTools(
        ["foreground_window", "window_list", "window_click", "window_elements"], handlers, first_events,
    )
    first_agent = _agent(first_gateway, first_tools)
    with pytest.raises(TaskNeedsUser):
        first_agent.run(
            "На этом экране нажми Оплатить 4 990 ₽",
            owner_goal="На этом экране нажми Оплатить 4 990 ₽",
            require_tool_action=True,
            require_side_effect=True,
            require_verification=True,
            max_steps=3,
        )
    checkpoint = first_agent.last_run_outcome()
    assert checkpoint["needs_confirmation"] is True
    assert not any(name == "window_click" for name, _ in first_tools.executed)

    resume_events: list[Any] = []
    resume_gateway = ScriptedGateway([
        _call("window_elements"),
        {"content": "Оплата подтверждена интерфейсом.", "tool_calls": []},
    ], resume_events)
    resume_tools = ScriptedTools(
        ["foreground_window", "window_list", "window_click", "window_elements"], handlers, resume_events,
    )
    resume_agent = _agent(resume_gateway, resume_tools)
    resume_agent.run(
        "На этом экране нажми Оплатить 4 990 ₽",
        owner_goal="На этом экране нажми Оплатить 4 990 ₽",
        require_tool_action=True,
        require_side_effect=True,
        require_verification=True,
        confirmed_action=checkpoint["confirmation_fingerprint"],
        confirmed_tool=checkpoint["confirmation_tool"],
        confirmed_arguments=checkpoint["confirmation_arguments"],
        confirmed_surface=checkpoint["confirmation_surface"],
        max_steps=5,
    )
    click_calls = [args for name, args in resume_tools.executed if name == "window_click"]
    assert click_calls == [checkpoint["confirmation_arguments"]]
    first_model_index = next(i for i, event in enumerate(resume_events) if event[0] == "model")
    click_index = next(i for i, event in enumerate(resume_events) if event[:2] == ("tool", "window_click"))
    assert click_index < first_model_index


def test_confirmation_surface_drift_fails_closed_and_does_not_click() -> None:
    old = _window(42, "Оплата заказа 4 990 ₽", 700)
    new = _window(42, "Оплата заказа 5 490 ₽", 700)
    args = {"handle": 42, "title_contains": old["title"], "stable_id": "pay", "element_text": "Оплатить 4 990 ₽"}
    fingerprint = RiskPolicy.evaluate(
        "Нажми Оплатить 4 990 ₽",
        "window_click",
        args,
        context={"surface_handle": 42, "surface_title": old["title"], "surface_pid": 700},
    ).fingerprint
    gateway = ScriptedGateway([])
    tools = ScriptedTools(
        ["foreground_window", "window_list", "window_click"],
        {
            "foreground_window": {"ok": True, "result": new},
            "window_list": {"ok": True, "result": [new]},
            "window_click": {"ok": True, "completed": True, "result": {"completed": True}},
        },
    )
    agent = _agent(gateway, tools)
    with pytest.raises(TaskNeedsUser):
        agent.run(
            "На этом экране нажми Оплатить 4 990 ₽",
            owner_goal="На этом экране нажми Оплатить 4 990 ₽",
            require_tool_action=True,
            require_side_effect=True,
            confirmed_action=fingerprint,
            confirmed_tool="window_click",
            confirmed_arguments=args,
            confirmed_surface=old,
            max_steps=2,
        )
    assert not any(name == "window_click" for name, _ in tools.executed)
    assert agent.last_run_outcome().get("needs_confirmation") is True


def test_workflow_resume_transports_exact_confirmation_payload_to_executor() -> None:
    db = MemoryDB()

    class ResumeEngine(UniversalWorkflowEngine):
        def __init__(self):
            self.services = SimpleNamespace(camera=None, db=db)
            self._checkpoint_lock = threading.RLock()
            self._desktop_lock = threading.RLock()
            self.captured: dict[str, Any] = {}

        def _execute_goal(self, spec, _direct, *, stop_event=None):
            self.captured = copy.deepcopy(spec)
            return {"ok": True, "verified": True, "completed": True, "answer": "commit verified"}

        @staticmethod
        def _trace(*_args, **_kwargs) -> None:
            return None

        @staticmethod
        def _runtime_step(*_args, **_kwargs) -> None:
            return None

    engine = ResumeEngine()
    key = engine._pending_key("confirm-flow")
    exact_args = {"handle": 42, "stable_id": "pay", "element_text": "Оплатить 4 990 ₽"}
    exact_surface = _window(42, "Оплата 4 990 ₽", 700)
    db.set_setting(key, {
        "task": "Оплати 4 990 ₽",
        "plan_id": "p-1",
        "goals": [{"goal": "Оплати 4 990 ₽", "reactive_v2": True, "plan_step_id": "step_1"}],
        "index": 0,
        "results": [],
        "replans": 0,
        "confirmed_steps": [],
        "state": "waiting_user",
        "run_id": "run-confirm",
        "risk_confirmation_fingerprint": "fingerprint-1",
        "risk_confirmation_tool": "window_click",
        "risk_confirmation_arguments": exact_args,
        "risk_confirmation_surface": exact_surface,
        "expires_at": time.time() + 60,
    })
    result = engine.execute_task(
        "да, продолжай",
        lambda _q: (False, "", {}),
        conversation_id="confirm-flow",
    )
    assert result.ok
    assert engine.captured["confirmed_action"] == "fingerprint-1"
    assert engine.captured["confirmed_tool"] == "window_click"
    assert engine.captured["confirmed_arguments"] == exact_args
    assert engine.captured["confirmed_surface"] == exact_surface


def test_generic_future_commit_tools_are_gated_but_reads_are_not() -> None:
    send = RiskPolicy.evaluate(
        "Сообщи Алексею результат",
        "send_message",
        {"recipient": "Алексей", "text": "Готово"},
    )
    upload = RiskPolicy.evaluate(
        "Прикрепи документ",
        "browser_upload",
        {"path": "report.pdf"},
    )
    status = RiskPolicy.evaluate(
        "Проверь статус сообщения",
        "message_status",
        {"id": "m-1"},
    )
    assert send.requires_confirmation and send.category == "external_communication"
    assert upload.requires_confirmation and upload.category == "external_disclosure"
    assert not status.requires_confirmation


class MemoryDB:
    def __init__(self):
        self.values: dict[str, Any] = {}

    def get_setting(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.values.get(key, default))

    def set_setting(self, key: str, value: Any) -> None:
        self.values[key] = copy.deepcopy(value)


def _checkpoint_engine(db: MemoryDB) -> UniversalWorkflowEngine:
    engine = UniversalWorkflowEngine.__new__(UniversalWorkflowEngine)
    engine.services = SimpleNamespace(db=db)
    engine._checkpoint_lock = threading.RLock()
    return engine


def test_checkpoint_tombstone_blocks_same_generation_stale_overwrite() -> None:
    db = MemoryDB()
    engine = _checkpoint_engine(db)
    active = {"run_id": "run-1", "goals": [{"goal": "x"}], "state": "running"}
    assert engine._save_pending("dialog", active, replace=True)
    assert engine._clear_pending("dialog", "run-1")
    assert not engine._save_pending("dialog", {**active, "state": "waiting_user"})
    assert not engine.has_pending("dialog")
    stored = db.get_setting(engine._pending_key("dialog"), {})
    assert stored.get("cleared") is True and not stored.get("goals")


def test_checkpoint_generation_cannot_clear_or_overwrite_newer_run() -> None:
    db = MemoryDB()
    engine = _checkpoint_engine(db)
    newer = {"run_id": "run-2", "goals": [{"goal": "new"}], "state": "running"}
    assert engine._save_pending("dialog", newer, replace=True)
    assert not engine._save_pending(
        "dialog", {"run_id": "run-1", "goals": [{"goal": "old"}], "state": "waiting_user"}
    )
    assert not engine._clear_pending("dialog", "run-1")
    assert not engine._save_pending("dialog", {"goals": [{"goal": "missing generation"}]})
    assert not engine._clear_pending("dialog")
    assert db.get_setting(engine._pending_key("dialog"), {})["run_id"] == "run-2"


def test_stop_token_terminates_generation_instead_of_leaving_resumable_checkpoint() -> None:
    class StopEngine(UniversalWorkflowEngine):
        def __init__(self, db: MemoryDB):
            self.services = SimpleNamespace(camera=None, db=db)
            self._checkpoint_lock = threading.RLock()
            self._desktop_lock = threading.RLock()

        @staticmethod
        def _trace(*_args, **_kwargs) -> None:
            return None

        @staticmethod
        def _runtime_step(*_args, **_kwargs) -> None:
            return None

    db = MemoryDB()
    engine = StopEngine(db)
    stop = threading.Event()
    stop.set()
    result = engine.execute_task(
        "Открой произвольный сервис",
        lambda _q: (False, "", {}),
        conversation_id="cancel-lab",
        stop_event=stop,
    )
    assert result.ok is False
    assert "сам не возобновится" in result.summary
    assert not engine.has_pending("cancel-lab")
    stored = db.get_setting(engine._pending_key("cancel-lab"), {})
    assert stored.get("cleared") is True and not stored.get("goals")
