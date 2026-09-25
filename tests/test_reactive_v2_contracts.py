# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

from eirven_ai.agent import LocalAgent, MediaUIRecovery, SurfaceLease
from eirven_ai.app_skills import AppSkills
from eirven_ai.browser import BrowserAutomation
from eirven_ai.risk_policy import RiskPolicy
from eirven_ai.tasks import TaskNeedsUser
from eirven_ai.tools import ToolExecutor
from eirven_ai.universal_workflow import UniversalWorkflowEngine
from eirven_ai.voice_daemon import NativeVoiceDaemon


def test_canonical_capability_ids_are_idempotent() -> None:
    ids = {
        "telegram", "yandex_music", "youtube", "spotify", "discord",
        "vscode", "explorer", "windows_settings", "browser",
    }
    assert {AppSkills.canonical(value) for value in ids} == ids
    assert AppSkills.canonical(AppSkills.canonical("Яндекс Музыка")) == "yandex_music"


def test_scientific_notation_hwnd_is_coerced_without_surface_crash() -> None:
    assert LocalAgent._coerce_id("5.308716e+06") == 5308716
    assert LocalAgent._coerce_id(5308716) == 5308716
    assert LocalAgent._coerce_id("not-a-hwnd") == 0


def test_typed_media_purpose_disambiguates_owner_provider_without_chat_catalogue() -> None:
    skills = AppSkills.__new__(AppSkills)
    skills.services = SimpleNamespace()
    skills.operator = SimpleNamespace(
        yandex_surface=lambda **_kwargs: {"title": "Яндекс Музыка", "handle": 77},
    )
    skills._focus_existing_browser_tab = lambda _aliases: None
    result = skills.open("яндекс", "музыка")
    assert result["ok"] is True
    assert result["skill"] == "yandex_music"


def test_legacy_music_adapter_uses_generic_verified_lane_without_brand_choices() -> None:
    calls = []
    skills = AppSkills.__new__(AppSkills)
    skills.services = SimpleNamespace(
        universal_workflow=SimpleNamespace(
            ensure_media_goal=lambda goal, **kwargs: (
                calls.append((goal, kwargs))
                or {"ok": True, "completed": True, "verified": True, "method": "fresh_uia"}
            ),
        ),
    )
    skills.operator = SimpleNamespace(
        yandex_player_control=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider-specific adapter must not run")
        ),
    )
    result = skills.play_music("Включи музыку")
    assert result["verified"] is True
    assert calls == [("Включи музыку", {"allow_implicit": True})]

    skills.services.universal_workflow.ensure_media_goal = lambda *_args, **_kwargs: None
    clarification = skills.play_music("Включи музыку")
    assert clarification["needs_user"] is True
    assert clarification["choices"] == []
    assert clarification["allow_free_text"] is True


def test_explicit_music_provider_is_bound_before_transport_control() -> None:
    opened = []
    skills = AppSkills.__new__(AppSkills)
    skills.services = SimpleNamespace(
        universal_workflow=SimpleNamespace(
            ensure_media_goal=lambda goal, **kwargs: {
                "ok": True, "completed": True, "verified": True,
                "goal": goal, "kwargs": kwargs,
            },
        ),
    )
    skills.open = lambda target, purpose="": (
        opened.append((target, purpose))
        or {"ok": True, "verified": True, "skill": "generic_web"}
    )
    result = skills.play_music("Включи музыку в SoundCloud")
    assert result["verified"] is True
    assert opened == [("SoundCloud", "музыка")]


def test_generic_service_resolution_prefers_entry_over_auth_recovery_without_catalogue() -> None:
    query = "FutureBrand7421 официальный сайт web app"
    root = "https://futurebrand7421.example/"
    recovery = "https://futurebrand7421.example/signin/forgot?next=%2F"
    assert BrowserAutomation._official_score(query, "FutureBrand7421", root) > BrowserAutomation._official_score(
        query, "FutureBrand7421 account", recovery,
    )
    assert BrowserAutomation._service_entry_url(recovery) == root
    assert BrowserAutomation._service_entry_url(
        "https://futurebrand7421.example/products/player"
    ) == "https://futurebrand7421.example/products/player"
    assert BrowserAutomation._official_host_grounded(query, root)
    assert not BrowserAutomation._official_host_grounded(
        query, "https://futurebrand7421.en.thirdparty.example/web-apps",
    )


def test_risk_policy_binds_confirmation_to_exact_grounded_commit() -> None:
    first = RiskPolicy.evaluate(
        "Оплати корзину после проверки суммы",
        "window_click",
        {"handle": 42, "stable_id": "pay-button", "element_text": "Оплатить 4 990 ₽"},
    )
    second = RiskPolicy.evaluate(
        "Оплати корзину после проверки суммы",
        "window_click",
        {"handle": 42, "stable_id": "pay-button", "element_text": "Оплатить 5 490 ₽"},
    )
    assert first.requires_confirmation and first.category == "payment"
    assert second.requires_confirmation
    assert first.fingerprint != second.fingerprint
    assert not RiskPolicy.evaluate(
        "Собери товары в корзину",
        "window_click",
        {"handle": 42, "stable_id": "catalog", "element_text": "Каталог"},
    ).requires_confirmation


def test_confirmation_modes_apply_to_exact_typed_action() -> None:
    safe_click = {"handle": 42, "stable_id": "play", "element_text": "Play"}
    for mode in ("critical", "full"):
        decision = RiskPolicy.evaluate(
            "Включи музыку", "window_click", safe_click,
            context={"confirmation_mode": mode, "desktop_control_enabled": True},
        )
        assert decision.requires_confirmation is False

    every = RiskPolicy.evaluate(
        "Включи музыку", "window_click", safe_click,
        context={"confirmation_mode": "every", "desktop_control_enabled": True},
    )
    assert every.requires_confirmation is True
    assert every.category == "action_confirmation"

    # Engine-owned UI recovery is still a mutating click.  It may avoid a false
    # external-commit classification, but it must not bypass the user's `every` mode.
    recovery = RiskPolicy.evaluate(
        "Включи музыку", "window_click", safe_click,
        context={
            "confirmation_mode": "every", "desktop_control_enabled": True,
            "media_recovery": True,
        },
    )
    assert recovery.requires_confirmation is True
    assert recovery.category == "action_confirmation"

    assert not RiskPolicy.evaluate(
        "Покажи плееры", "media_control", {"action": "status"},
        context={"confirmation_mode": "every", "desktop_control_enabled": True},
    ).requires_confirmation
    assert RiskPolicy.evaluate(
        "Включи музыку", "media_control", {"action": "play"},
        context={"confirmation_mode": "every", "desktop_control_enabled": True},
    ).requires_confirmation


def test_immutable_confirmation_survives_every_critical_and_full_modes() -> None:
    immutable_calls = [
        ("Напиши Тиме привет", "message_send", {"platform": "Telegram", "recipient": "Тиме", "text": "привет"}),
        ("Оплати корзину", "window_click", {"element_text": "Оплатить 4 990 ₽"}),
        ("Оформи заказ", "window_click", {"element_text": "Оформить заказ"}),
        ("Удали файл", "window_click", {"element_text": "Удалить"}),
        ("Прикрепи файл", "browser_upload", {"paths": ["proposal.pdf"]}),
    ]
    for mode in ("every", "critical", "full"):
        for task, tool, arguments in immutable_calls:
            decision = RiskPolicy.evaluate(
                task, tool, arguments,
                context={"confirmation_mode": mode, "desktop_control_enabled": True},
            )
            assert decision.requires_confirmation is True, (mode, tool, arguments)
            assert decision.category != "action_confirmation", (mode, tool, arguments)


def test_desktop_permission_blocks_local_capabilities_but_not_mail_connector() -> None:
    context = {"confirmation_mode": "full", "desktop_control_enabled": False}
    for tool, arguments in (
        ("launch_application", {"application": "Telegram"}),
        ("window_click", {"element_text": "Play"}),
        ("media_control", {"action": "play"}),
    ):
        decision = RiskPolicy.evaluate("Локальное действие", tool, arguments, context=context)
        assert decision.blocked is True
        assert decision.requires_confirmation is False
        assert decision.category == "permission"

    mail = RiskPolicy.evaluate(
        "Проверь подключённую почту", "mail_review", {"limit": 20}, context=context,
    )
    assert mail.blocked is False
    assert mail.requires_confirmation is False


def test_tool_executor_enforces_desktop_toggle_before_adapter_call() -> None:
    called = []
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.settings = SimpleNamespace(enable_desktop_control=False)
    executor._scope = SimpleNamespace(task_scoped=True)
    executor._stop_requested = lambda: False
    executor._log = lambda *_args, **_kwargs: None
    executor.tool_window_click = lambda **kwargs: called.append(kwargs) or {"ok": True}
    result = executor.execute("window_click", {"element_text": "Play"})
    assert result["ok"] is False
    assert result["permission_required"] == "desktop_control"
    assert called == []


def test_app_skills_cannot_bypass_disabled_desktop_control() -> None:
    def touched(*_args, **_kwargs):
        raise AssertionError("visible operator must not be touched")

    skills = AppSkills.__new__(AppSkills)
    skills.services = SimpleNamespace(settings=SimpleNamespace(enable_desktop_control=False))
    skills.operator = SimpleNamespace(
        telegram_send=touched, yandex_player_control=touched, answer_discord_call=touched,
    )
    for result in (
        skills.open("Telegram"),
        skills.play_music("Включи музыку"),
        skills.control_music("следующий трек"),
        skills.send_telegram("Тиме", "привет"),
        skills.answer_discord_call(),
    ):
        assert result["ok"] is False
        assert result["permission_required"] == "desktop_control"
        assert result["completed"] is False
        assert result["verified"] is False


def test_service_surface_cannot_silently_bind_to_codex() -> None:
    lease = SurfaceLease()
    assert not LocalAgent._bind_surface(
        lease, "Открой Яндекс Музыку", {"handle": 5, "title": "Codex", "pid": 1}
    )
    assert not lease.bound
    assert LocalAgent._bind_surface(
        lease, "Открой Яндекс Музыку", {"handle": 7, "title": "Яндекс Музыка", "pid": 2}
    )
    assert lease.handle == 7


def test_media_blocker_candidate_ignores_browser_chrome_and_keeps_page_dismissal() -> None:
    rows = [
        {
            "name": "Назад", "control_type": "Button", "stable_id": "browser-back",
            "surface_handle": 77, "class_name": "BackForwardButton",
        },
        {
            "name": "Вернуться назад", "control_type": "Hyperlink", "stable_id": "promo-back",
            "surface_handle": 77, "class_name": "SlidesPage_desktopBackButton",
        },
    ]
    candidate = LocalAgent._media_blocker_ui_candidate(rows, 77)
    assert candidate and candidate["stable_id"] == "promo-back"


class _FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def model_capabilities(_model: str) -> list[str]:
        return ["tools"]

    def chat(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "window_list", "arguments": {}}},
                    {"function": {"name": "click", "arguments": {"x": 1, "y": 1}}},
                ],
            }
        return {"content": "Наблюдение получено.", "tool_calls": []}


class _FakeTools:
    def __init__(self) -> None:
        self.executed: list[str] = []

    @staticmethod
    def native_descriptions():
        return [
            {"type": "function", "function": {"name": "window_list", "description": "windows", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "click", "description": "click", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}}},
        ]

    @staticmethod
    def task_scope(_stop=None):
        return nullcontext()

    def execute(self, name: str, _arguments):
        self.executed.append(name)
        if name == "window_list":
            return {"ok": True, "result": [{"title": "Test", "handle": 99}]}
        return {"ok": True, "result": {}}

    @staticmethod
    def stop() -> None:
        return None


def test_agent_executes_only_one_tool_from_each_model_turn() -> None:
    settings = SimpleNamespace(
        max_agent_steps=4, fast_model="fake", model="fake", vision_model="fake",
        task_num_ctx=4096, chat_num_ctx=4096, llm_first_token_timeout=6,
        keep_alive="1m",
    )
    tools = _FakeTools()
    agent = LocalAgent(settings, _FakeGateway(), tools, SimpleNamespace())
    answer = agent.run("Проверь открытые окна", require_tool_action=True)
    assert "Наблюдение" in answer
    assert tools.executed == ["window_list"]


class _ClickThenScreenshotGateway(_FakeGateway):
    def chat(self, *_args, **_kwargs):
        self.calls += 1
        sequence = [
            {"function": {"name": "window_focus", "arguments": {"handle": 99}}},
            {"function": {"name": "window_click", "arguments": {"handle": 99, "element_text": "Продолжить"}}},
            {"function": {"name": "screenshot", "arguments": {"handle": 99}}},
        ]
        if self.calls <= len(sequence):
            return {"content": "", "tool_calls": [sequence[self.calls - 1]]}
        return {"content": "Готово и проверено.", "tool_calls": []}


class _ClickThenScreenshotTools(_FakeTools):
    def __init__(self, screenshot_path: str) -> None:
        super().__init__()
        self.screenshot_path = screenshot_path

    @staticmethod
    def native_descriptions():
        def schema(name: str) -> dict:
            return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}
        return [schema(name) for name in ("foreground_window", "window_focus", "window_click", "screenshot")]

    def execute(self, name: str, _arguments):
        self.executed.append(name)
        surface = {"title": "Тестовая форма", "handle": 99, "pid": 123}
        if name in {"foreground_window", "window_focus"}:
            return {"ok": True, "result": surface}
        if name == "window_click":
            return {"ok": True, "completed": True, "result": {"completed": True}}
        if name == "screenshot":
            return {"ok": True, "result": {"path": self.screenshot_path}}
        return {"ok": False, "error": name}


def test_screenshot_after_side_effect_uses_current_result(tmp_path) -> None:
    screenshot = tmp_path / "surface.png"
    screenshot.write_bytes(b"test")
    settings = SimpleNamespace(
        max_agent_steps=6, fast_model="fake", model="fake", vision_model="fake",
        task_num_ctx=4096, chat_num_ctx=4096, llm_first_token_timeout=6,
        keep_alive="1m",
    )
    tools = _ClickThenScreenshotTools(str(screenshot))
    agent = LocalAgent(settings, _ClickThenScreenshotGateway(), tools, SimpleNamespace())
    agent._describe_screenshot = lambda *_args: "После клика открылась следующая форма"
    agent._observation_verifies = lambda *_args: True
    answer = agent.run(
        "На текущем экране нажми Продолжить и проверь следующий экран",
        require_tool_action=True,
        auto_vision=True,
    )
    assert "Готово" in answer
    assert "window_click" in tools.executed and "screenshot" in tools.executed


class _VisionCaptureGateway:
    def __init__(self, raw) -> None:
        self.raw = raw
        self.messages = []
        self.kwargs = {}

    def json(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        return self.raw


def _vision_agent(raw) -> tuple[LocalAgent, _VisionCaptureGateway]:
    gateway = _VisionCaptureGateway(raw)
    settings = SimpleNamespace(
        vision_model="vision", chat_num_ctx=4096, fast_model="fake", model="fake",
        task_num_ctx=4096, llm_first_token_timeout=6, keep_alive="1m",
    )
    return LocalAgent(settings, gateway, _FakeTools(), SimpleNamespace()), gateway


def _vision_payload(*, reached: bool = False, confidence: float = 0.94, x: int = 940, y: int = 740):
    target = {
        "label": "Play", "role": "button", "action": "click",
        "x": x, "y": y, "confidence": confidence,
        "evidence": "Главная кнопка плеера в нижней панели",
    }
    return {
        "summary": "Открыт плеер", "screen_state": "ready", "goal_reached": reached,
        "primary_target": target,
        "controls": [dict(target, x=index, y=index) for index in range(1, 7)],
        "blockers": [{"label": f"b{index}", "evidence": "e"} for index in range(5)],
    }


def test_structured_vision_schema_is_bounded_and_receives_geometry_and_ranked_uia(tmp_path) -> None:
    screenshot = tmp_path / "vision.png"
    screenshot.write_bytes(b"png")
    agent, gateway = _vision_agent(_vision_payload())
    meta = {
        "width": 1200, "height": 800, "coordinate_origin": {"x": 100, "y": 80},
        "surface_handle": 99,
    }
    rows = [
        {
            "name": "Play", "control_type": "Button", "stable_id": "preview-0",
            "parent_name": "Recommendation card", "class_name": "PlayButtonWithCover",
            "rectangle": [120, 200, 180, 260], "surface_handle": 99,
        },
        {
            "name": "Play", "control_type": "Button", "stable_id": "main-play",
            "parent_name": "Player", "class_name": "PlayerControls_playButton",
            "rectangle": [1000, 780, 1080, 860], "surface_handle": 99,
        },
    ]
    result = agent._describe_screenshot(str(screenshot), "Включи музыку", meta, rows)
    assert set(result) == {"summary", "screen_state", "goal_reached", "primary_target", "controls", "blockers"}
    assert result["goal_reached"] is False
    assert len(result["controls"]) == 4 and len(result["blockers"]) == 3
    target = result["primary_target"]
    assert set(target) == {"label", "role", "action", "x", "y", "confidence", "evidence"}
    assert 0 <= target["x"] < 1200 and 0 <= target["y"] < 800 and 0 <= target["confidence"] <= 1
    schema = gateway.kwargs["schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["properties"]["controls"]["maxItems"] == 4
    assert schema["properties"]["blockers"]["maxItems"] == 3
    prompt = gateway.messages[-1]["content"]
    assert all(mark in prompt for mark in ("width=1200", "height=800", "origin=100,80", "surface_handle=99"))
    assert "png_rectangle" in prompt and prompt.index("main-play") < prompt.index("preview-0")


def test_structured_vision_invalid_truncated_or_out_of_bounds_fails_closed() -> None:
    for raw in ("", "{", {"goal_reached": True}):
        result = LocalAgent._normalize_vision_analysis(raw, 800, 600)
        assert result["goal_reached"] is False
        assert result["primary_target"]["confidence"] == 0
        assert result["controls"] == [] and result["blockers"]
    result = LocalAgent._normalize_vision_analysis(
        _vision_payload(reached=True, x=800, y=-1), 800, 600,
    )
    assert result["goal_reached"] is False
    assert result["primary_target"]["confidence"] == 0
    assert result["primary_target"]["x"] == 0 and result["primary_target"]["y"] == 0


def test_screenshot_postcondition_requires_structured_goal_reached_on_exact_surface() -> None:
    valid = LocalAgent._normalize_vision_analysis(_vision_payload(reached=True, x=40, y=40), 800, 600)
    effect = {"handle": 99, "stable_id": "next"}
    observation = {"handle": 99}
    result = {"ok": True, "result": {"surface_handle": 99, "vision_analysis": valid}}
    assert LocalAgent._observation_verifies("window_click", effect, "screenshot", observation, result)
    assert not LocalAgent._observation_verifies(
        "window_click", effect, "screenshot", observation,
        {"ok": True, "result": {"surface_handle": 99, "vision_analysis": "looks done"}},
    )
    not_reached = dict(valid, goal_reached=False)
    assert not LocalAgent._observation_verifies(
        "window_click", effect, "screenshot", observation,
        {"ok": True, "result": {"surface_handle": 99, "vision_analysis": not_reached}},
    )
    assert not LocalAgent._observation_verifies(
        "window_click", effect, "screenshot", observation,
        {"ok": True, "result": {"surface_handle": 100, "vision_analysis": valid}},
    )
    assert not LocalAgent._observation_verifies(
        "window_click", dict(effect, _media_recovery_action="play"), "screenshot", observation, result,
    )


def test_media_vision_hint_maps_only_to_unique_fresh_main_uia_stable_id() -> None:
    analysis = LocalAgent._normalize_vision_analysis(_vision_payload(), 1200, 800)
    meta = {"width": 1200, "height": 800, "coordinate_origin": {"x": 100, "y": 80}, "surface_handle": 99}
    main = {
        "name": "Play", "control_type": "Button", "stable_id": "main-play",
        "parent_name": "Player", "class_name": "PlayerControls_playButton",
        "rectangle": [1000, 780, 1080, 860], "surface_handle": 99,
    }
    preview = dict(
        main, stable_id="preview", parent_name="Recommendation card",
        class_name="PlayButtonWithCover",
    )
    candidate = LocalAgent._vision_media_uia_candidate(
        [preview, main], analysis, meta, "Включи музыку", "play", 99,
    )
    assert candidate and candidate["stable_id"] == "main-play" and candidate["surface_handle"] == 99
    assert LocalAgent._vision_media_uia_candidate(
        [preview], analysis, meta, "Включи музыку", "play", 99,
    ) is None
    assert LocalAgent._vision_media_uia_candidate(
        [main, dict(main, stable_id="main-play-2")], analysis, meta,
        "Включи музыку", "play", 99,
    ) is None
    low = LocalAgent._normalize_vision_analysis(_vision_payload(confidence=0.4), 1200, 800)
    assert LocalAgent._vision_media_uia_candidate(
        [main], low, meta, "Включи музыку", "play", 99,
    ) is None


def test_vision_uia_context_is_discarded_after_surface_or_generation_change() -> None:
    row = {"stable_id": "fresh", "name": "Play"}
    lease = SurfaceLease(handle=99, title="Player", generation=3)
    context = {"handle": 99, "generation": 3, "scan_serial": 4, "rows": [row]}
    assert LocalAgent._fresh_vision_uia_rows(context, lease) == [row]
    assert LocalAgent._fresh_vision_uia_rows(dict(context, handle=100), lease) == []
    assert LocalAgent._fresh_vision_uia_rows(dict(context, generation=2), lease) == []


def test_large_uia_tree_keeps_goal_relevant_page_controls() -> None:
    chrome = [
        {"name": f"Browser chrome {index}", "control_type": "Button", "class_name": "Browser ToolbarView", "rectangle": [index, 1, index + 10, 50]}
        for index in range(180)
    ]
    page = [
        {"name": "Моя волна", "control_type": "Hyperlink", "stable_id": "wave", "rectangle": [40, 220, 180, 260]},
        {"name": "Воспроизведение", "control_type": "Button", "stable_id": "play", "rectangle": [900, 1180, 960, 1240]},
    ]
    compact = LocalAgent._compact_result({"ok": True, "result": chrome + page}, 1800, "Включи Мою волну")
    assert "Моя волна" in compact
    assert "Воспроизведение" in compact
    assert "\u2026" not in compact


def test_media_goal_prioritizes_main_player_over_preview_buttons() -> None:
    rows = [
        {"name": "Воспроизведение", "control_type": "Button", "stable_id": f"preview-{index}", "parent_name": f"Карточка {index}", "class_name": "PlayButtonWithCover", "rectangle": [20, 150 + index * 55, 70, 200 + index * 55]}
        for index in range(30)
    ]
    rows.append({"name": "Воспроизведение", "control_type": "Button", "stable_id": "main-play", "parent_name": "Плеер", "class_name": "VibePlayerControls_playButton", "rectangle": [900, 700, 980, 780]})
    compact = LocalAgent._compact_result({"ok": True, "result": rows}, 1600, "Включи музыку")
    assert compact.index("main-play") < compact.index("preview-0")


def test_named_page_heading_grounds_main_transport_over_navigation_wrapper() -> None:
    rows = [
        {
            "name": "Моя волна", "control_type": "ListItem", "stable_id": "wave-wrapper",
            "surface_handle": 99, "parent_name": "Главное меню", "rectangle": [20, 200, 260, 260],
        },
        {
            "name": "Моя волна", "control_type": "Hyperlink", "stable_id": "wave-link",
            "surface_handle": 99, "parent_name": "Моя волна", "rectangle": [20, 200, 260, 260],
        },
        {
            "name": "Моя волна", "control_type": "Text", "stable_id": "wave-heading",
            "surface_handle": 99, "parent_name": "", "rectangle": [800, 120, 1300, 240],
        },
        {
            "name": "Воспроизведение", "control_type": "Button", "stable_id": "main-play",
            "surface_handle": 99, "parent_name": "Плеер",
            "class_name": "MainPlayerControls_playButton", "rectangle": [900, 700, 980, 780],
        },
    ]
    candidate = LocalAgent._media_content_ui_candidate(rows, "Мою волну", 99)
    assert candidate and candidate["stable_id"] == "main-play"
    assert candidate["page_grounded"] is True


def test_page_grounded_transport_verifies_fresh_play_to_pause_on_same_rect() -> None:
    recovery = MediaUIRecovery(
        action="play", target="Мою волну", handle=99,
        candidate={
            "rectangle": [900, 700, 980, 780],
            "context": "Плеер", "class_name": "MainPlayerControls_playButton",
        },
    )
    rows = [{
        "name": "Пауза", "control_type": "Button", "stable_id": "fresh-pause",
        "surface_handle": 99, "parent_name": "Плеер",
        "class_name": "MainPlayerControls_playButton_playing",
        "rectangle": [900, 700, 980, 780], "visible": True, "enabled": True,
    }]
    assert LocalAgent._media_ui_candidate_state_changed(rows, recovery)["stable_id"] == "fresh-pause"


def test_media_recovery_never_promotes_preview_or_autoplay_to_primary_control() -> None:
    rows = [
        {
            "name": "Воспроизведение", "control_type": "Button", "stable_id": "only-preview",
            "parent_name": "Карточка альбома", "class_name": "PlayerControlsCard PlayButtonWithCover",
            "rectangle": [20, 200, 70, 250],
        },
        {
            "name": "Автовоспроизведение", "control_type": "Button", "stable_id": "autoplay",
            "parent_name": "Плеер", "class_name": "AutoplayButton",
            "rectangle": [900, 700, 980, 780],
        },
    ]
    assert LocalAgent._media_ui_candidate(rows, "Включи музыку", "play") is None


class _ClarifyingGateway(_FakeGateway):
    def chat(self, *_args, **_kwargs):
        return {"content": "На чём поедешь до Мытищ: машине или общественном транспорте?", "tool_calls": []}


def test_agent_returns_free_form_clarification_without_fake_ready_protocol() -> None:
    settings = SimpleNamespace(
        max_agent_steps=4, fast_model="fake", model="fake", vision_model="fake",
        task_num_ctx=4096, chat_num_ctx=4096, llm_first_token_timeout=6,
        keep_alive="1m",
    )
    agent = LocalAgent(settings, _ClarifyingGateway(), _FakeTools(), SimpleNamespace())
    try:
        agent.run("Узнай, сколько ехать до Мытищ", require_tool_action=True)
    except TaskNeedsUser as exc:
        assert "На чём" in exc.prompt
    else:
        raise AssertionError("a genuine clarification must suspend for the owner's answer")
    outcome = agent.last_run_outcome()
    assert outcome.get("clarification") is True


class _ReactiveEngine(UniversalWorkflowEngine):
    def __init__(self) -> None:
        self.services = SimpleNamespace(camera=None, db=SimpleNamespace(set_setting=lambda *_: None))
        self._desktop_lock = threading.RLock()
        self.plan_called = False
        self.last_goal = ""
        self.last_spec = {}

    def plan(self, _query: str):
        self.plan_called = True
        raise AssertionError("reactive v2 must not build an upfront model plan")

    def _fallback_goals(self, query: str):
        return [{"goal": query, "mode": "auto", "tool": "deterministic", "text": ""}]

    def _prepare_executor_specs(self, _query, specs):
        return specs

    def _execute_goal(self, spec, _direct, *, stop_event=None):
        assert spec.get("reactive_v2") is True
        self.last_spec = dict(spec)
        self.last_goal = str(spec.get("goal") or "")
        return {"ok": True, "verified": True, "completed": False, "answer": "42"}

    def _save_pending(self, *_args, **_kwargs):
        return None

    def _clear_pending(self, *_args, **_kwargs):
        return None

    def _trace(self, *_args, **_kwargs):
        return None

    def _runtime_step(self, *_args, **_kwargs):
        return None


class _StatefulTravelEngine(_ReactiveEngine):
    def __init__(self) -> None:
        super().__init__()
        self.pending = None
        self.services.db = SimpleNamespace(
            get_setting=lambda _key, _default=None: self.pending,
            set_setting=lambda *_args, **_kwargs: None,
        )

    def _save_pending(self, _conversation_id, payload, **_kwargs):
        self.pending = payload
        return True

    def _clear_pending(self, _conversation_id, run_id=""):
        self.pending = {"run_id": run_id, "cleared": True}
        return True


def test_universal_entry_uses_one_reactive_goal_without_upfront_plan() -> None:
    engine = _ReactiveEngine()
    result = engine.execute_task("Проверь подключённую почту", lambda _q: (False, "", {}))
    assert result.ok and result.summary == "42"
    assert not engine.plan_called


def test_pending_clarification_accepts_a_free_form_answer() -> None:
    engine = _ReactiveEngine()
    pending = {
        "task": "Узнай, сколько ехать до Мытищ",
        "goals": [{"goal": "Узнай, сколько ехать до Мытищ", "reactive_v2": True, "plan_step_id": "step_1"}],
        "index": 0,
        "results": [],
        "replans": 0,
        "confirmed_steps": [],
        "clarification": True,
        "clarification_prompt": "На чём поедешь?",
    }
    engine.services.db = SimpleNamespace(
        get_setting=lambda _key, _default=None: pending,
        set_setting=lambda *_args, **_kwargs: None,
    )
    assert engine.should_handle("на машине", "dialog-1")
    result = engine.execute_task("на машине", lambda _q: (False, "", {}), conversation_id="dialog-1")
    assert result.ok
    assert "Ответ владельца: на машине" in engine.last_goal


def test_pending_media_clarification_preserves_typed_service_slot() -> None:
    engine = _ReactiveEngine()
    pending = {
        "task": "Включи музыку",
        "goals": [{"goal": "Включи музыку", "reactive_v2": True, "plan_step_id": "step_1"}],
        "index": 0,
        "results": [],
        "replans": 0,
        "confirmed_steps": [],
        "clarification": True,
        "clarification_kind": "media_owner",
        "clarification_prompt": "В каком сервисе или плеере включить музыку?",
        "missing_fields": ["service_or_player"],
    }
    engine.services.db = SimpleNamespace(
        get_setting=lambda _key, _default=None: pending,
        set_setting=lambda *_args, **_kwargs: None,
    )
    result = engine.execute_task("яндекс", lambda _q: (False, "", {}), conversation_id="media-dialog")
    assert result.ok
    assert engine.last_spec["owner_slots"] == {"service_or_player": "яндекс"}
    assert engine.last_spec["clarification_kind"] == "media_owner"


def test_travel_slot_guard_asks_before_desktop_observation() -> None:
    assert UniversalWorkflowEngine._travel_missing_fields("Сколько до Мытищ ехать?") == ["origin", "transport"]
    assert UniversalWorkflowEngine._travel_missing_fields(
        "Сколько до Мытищ ехать?\n\nУточняющий вопрос Эрви: Назови точку отправления и способ передвижения.\n"
        "Ответ владельца: из Москвы"
    ) == ["transport"]
    assert UniversalWorkflowEngine._travel_missing_fields(
        "Сколько до Мытищ ехать?\n\nУточняющий вопрос Эрви: Назови точку отправления и способ передвижения.\n"
        "Ответ владельца: из Москвы на машине"
    ) == []
    engine = _ReactiveEngine()
    result = engine.execute_task("Сколько до Мытищ ехать?", lambda _q: (False, "", {}))
    assert result.needs_user is True
    assert "точку отправления" in result.prompt
    assert "способ передвижения" in result.prompt


def test_travel_slots_accumulate_origin_then_transport_without_reasking() -> None:
    engine = _StatefulTravelEngine()
    first = engine.execute_task(
        "Сколько до Мытищ?", lambda _q: (False, "", {}), conversation_id="travel-origin-first",
    )
    assert first.needs_user
    assert engine.pending["missing_fields"] == ["origin", "transport"]
    assert engine.pending["clarification_kind"] == "travel_owner"

    second = engine.execute_task(
        "из Москвы", lambda _q: (False, "", {}), conversation_id="travel-origin-first",
    )
    assert second.needs_user
    assert "способ" in second.prompt.casefold()
    assert "точку отправления" not in second.prompt.casefold()
    assert engine.pending["goals"][0]["owner_slots"] == {"origin": "из Москвы"}
    assert engine.pending["missing_fields"] == ["transport"]

    final = engine.execute_task(
        "общественным транспортом",
        lambda _q: (False, "", {}),
        conversation_id="travel-origin-first",
    )
    assert final.ok
    assert engine.last_spec["owner_slots"] == {
        "origin": "из Москвы",
        "transport": "общественным транспортом",
    }


def test_travel_slots_accumulate_transport_then_origin_without_reasking() -> None:
    engine = _StatefulTravelEngine()
    first = engine.execute_task(
        "Сколько до Мытищ?", lambda _q: (False, "", {}), conversation_id="travel-mode-first",
    )
    assert first.needs_user

    second = engine.execute_task(
        "общественным транспортом",
        lambda _q: (False, "", {}),
        conversation_id="travel-mode-first",
    )
    assert second.needs_user
    assert "отправления" in second.prompt.casefold()
    assert "способ передвижения" not in second.prompt.casefold()
    assert engine.pending["goals"][0]["owner_slots"] == {
        "transport": "общественным транспортом",
    }
    assert engine.pending["missing_fields"] == ["origin"]

    final = engine.execute_task(
        "из Москвы", lambda _q: (False, "", {}), conversation_id="travel-mode-first",
    )
    assert final.ok
    assert engine.last_spec["owner_slots"] == {
        "transport": "общественным транспортом",
        "origin": "из Москвы",
    }


def test_vague_travel_origin_is_not_guessed_and_filled_transport_is_retained() -> None:
    engine = _StatefulTravelEngine()
    engine.execute_task(
        "Сколько до Мытищ?", lambda _q: (False, "", {}), conversation_id="travel-vague-origin",
    )
    vague = engine.execute_task(
        "от меня", lambda _q: (False, "", {}), conversation_id="travel-vague-origin",
    )
    assert vague.needs_user
    assert set(engine.pending["missing_fields"]) == {"origin", "transport"}
    assert "owner_slots" not in engine.pending["goals"][0]

    mode = engine.execute_task(
        "общественным транспортом",
        lambda _q: (False, "", {}),
        conversation_id="travel-vague-origin",
    )
    assert mode.needs_user
    assert "отправления" in mode.prompt.casefold()
    assert "способ передвижения" not in mode.prompt.casefold()
    assert engine.pending["goals"][0]["owner_slots"] == {
        "transport": "общественным транспортом",
    }


def test_travel_bare_location_answer_fills_origin_when_question_asked_two_slots() -> None:
    missing = ["origin", "transport"]
    assert UniversalWorkflowEngine._travel_answer_slots(
        "Москва, Красная площадь", missing,
    ) == {"origin": "Москва, Красная площадь"}
    assert UniversalWorkflowEngine._travel_answer_slots("пешком", missing) == {
        "transport": "пешком",
    }
    query = UniversalWorkflowEngine._travel_search_query(
        "Сколько до Мытищ ехать?\n\nУточняющий вопрос Эрви: Каким способом ехать?\n"
        "Ответ владельца: пешком",
        {"origin": "Москва, Красная площадь", "transport": "пешком"},
    )
    assert query == "маршрут отправление Москва, Красная площадь назначение Мытищ способ пешком"


def _voice_daemon_for_policy(media: bool, speaking: bool = False) -> NativeVoiceDaemon:
    daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
    daemon._session_activated = False
    daemon._active_until = 0.0
    daemon._speaking = threading.Event()
    if speaking:
        daemon._speaking.set()
    daemon._foreground_media_kind = lambda: "media" if media else ""
    daemon._extract_explicit_wake = lambda text: (text.startswith("Эрви "), text.removeprefix("Эрви "))
    daemon._normalize = lambda text: text.casefold().strip()
    return daemon


def test_voice_is_open_mic_when_quiet_but_requires_wake_during_media() -> None:
    assert _voice_daemon_for_policy(False)._activation_route("открой музыку", 0, 10)["action"] == "accept"
    assert _voice_daemon_for_policy(True)._activation_route("фраза из видео", 0, 10)["action"] == "ignore"
    route = _voice_daemon_for_policy(True)._activation_route("Эрви поставь на паузу", 0, 10)
    assert route["action"] == "accept" and route["command"] == "поставь на паузу"


def test_voice_tolerates_asr_wake_variant_and_speech_started_inside_armed_window() -> None:
    daemon = _voice_daemon_for_policy(True)
    fuzzy = daemon._activation_route("эйрви поставь на паузу", 0, 10)
    assert fuzzy["action"] == "accept" and fuzzy["has_wake"] is True

    armed = _voice_daemon_for_policy(True)
    armed._session_activated = True
    armed._active_until = 15.0
    late_asr = armed._activation_route("открой маршрут", 14.5, 18.0)
    assert late_asr["action"] == "accept"
