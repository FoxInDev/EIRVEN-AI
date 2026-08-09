from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine, Observation
from eirven_ai import supervisor


class FakeTools:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "result": {}}


class FakeOperator:
    def __init__(self):
        self.clicks: list[str] = []

    def click_element(self, title: str, element: dict, *, goal: str = "") -> bool:
        self.clicks.append(str(element.get("name") or ""))
        return True

    def type_verified(self, acquired: dict, text: str, *, submit: bool = False, require_verified: bool = True):
        return {"ok": True, "verified": True, "evidence": text}


class FakeGateway:
    def installed_models(self):
        return ["test-model"]

    def json(self, *args, **kwargs):
        return {"done": False, "evidence": "", "confidence": 0.0}


class FakeStyle:
    def get(self):
        return SimpleNamespace(prompt=lambda: "")


class FakeDB:
    def __init__(self):
        self.values = {}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def make_engine() -> AutonomousWorkflowEngine:
    services = SimpleNamespace(
        tools=FakeTools(),
        desktop_operator=FakeOperator(),
        gateway=FakeGateway(),
        settings=SimpleNamespace(action_model="test-model", fast_model="test-model", model="test-model", root_dir=Path(".")),
        style=FakeStyle(),
        db=FakeDB(),
        runtime=None,
    )
    return AutonomousWorkflowEngine(services)


def obs(*elements: dict, title: str = "Astrostory") -> Observation:
    rows = []
    lines = []
    for i, raw in enumerate(elements):
        row = {
            "index": i,
            "control_type": raw.get("control_type", "Text"),
            "name": raw.get("name", ""),
            "value": raw.get("value", ""),
            "automation_id": raw.get("automation_id", ""),
            "class_name": raw.get("class_name", ""),
            "visible": True,
            "enabled": True,
            **raw,
        }
        rows.append(row)
        lines.append(f"[{i}] {row['control_type']} name={row['name']!r} value={row['value']!r}")
    compact = f"TITLE={title}\n" + "\n".join(lines)
    return Observation(title=title, handle=123, elements=rows, compact=compact, fingerprint=f"state-{abs(hash(compact))}")


def test_r16_routes_dependent_goals_but_not_atomic_open():
    engine = make_engine()
    assert engine.should_handle("Найди браслет Карма и добавь его в корзину")
    assert engine.should_handle("Открой Яндекс Музыку, найди Oxxxymiron и включи последний альбом")
    assert engine.should_handle("Открой Telegram, ответь всем непрочитанным личным чатам в моём стиле, каналы пропусти")
    assert not engine.should_handle("Открой Telegram")


def test_r16_acceptance_walk_is_state_driven_not_site_recipe():
    engine = make_engine()
    goal = "Найди браслет Карма и добавь его в корзину"
    history: list[dict] = []

    state1 = obs({"control_type": "Edit", "name": "Поиск", "value": ""})
    d1 = engine._heuristic_decision(goal, state1, history)
    assert d1 == {"action": "type", "target_index": 0, "text": "Карма", "reason": "visible search field", "expected": "search query visible"}
    history.append({"action": "type", "text": "Карма", "reason": "visible search field", "ok": True})

    state2 = obs(
        {"control_type": "Edit", "name": "Поиск", "value": "Карма"},
        {"control_type": "Button", "name": "Искать"},
    )
    d2 = engine._heuristic_decision(goal, state2, history)
    assert d2["action"] == "click" and d2["target_index"] == 1
    assert d2["reason"] == "visible search submit"
    history.append({"action": "click", "target": "Искать", "reason": "visible search submit", "ok": True})

    state3 = obs({"control_type": "Hyperlink", "name": "Карма разберётся"})
    d3 = engine._heuristic_decision(goal, state3, history)
    assert d3["action"] == "click" and d3["target_index"] == 0
    assert d3["reason"] == "semantic result match"
    history.append({"action": "click", "target": "Карма разберётся", "ok": True})

    state4 = obs(
        {"control_type": "Text", "name": "Карма разберётся"},
        {"control_type": "Button", "name": "Добавить в корзину"},
    )
    d4 = engine._heuristic_decision(goal, state4, history)
    assert d4["action"] == "click" and d4["target_index"] == 1
    assert d4["commit"] is True
    assert engine._precommit_check(goal, state4, d4, state4.elements[1]) == (True, "")


def test_r16_cart_precommit_requires_requested_product_visible():
    engine = make_engine()
    goal = "Найди браслет Карма и добавь его в корзину"
    state = obs({"control_type": "Button", "name": "Добавить в корзину"})
    decision = {"action": "click", "target_index": 0, "commit": True}
    allowed, error = engine._precommit_check(goal, state, decision, state.elements[0])
    assert not allowed
    assert "товар" in error.casefold()


def test_r16_committed_click_is_single_shot_even_when_unverified():
    engine = make_engine()
    state = obs(
        {"control_type": "Text", "name": "Карма разберётся"},
        {"control_type": "Button", "name": "Добавить в корзину"},
    )
    decision = {"action": "click", "target_index": 1, "reason": "commit", "expected": "cart changed", "commit": True}
    committed: set[str] = set()
    engine._wait_transition = lambda *args, **kwargs: {"changed": False, "settled": True}

    first = engine._execute_local("Найди браслет Карма и добавь его в корзину", state, decision, committed)
    second = engine._execute_local("Найди браслет Карма и добавь его в корзину", state, decision, committed)

    assert first["completed"] is True and first["verified"] is False
    assert second["completed"] is True and second["ok"] is False
    assert "duplicate blocked" in second["error"]
    assert engine.operator.clicks == ["Добавить в корзину"]


def test_r16_launch_uses_real_tool_schema():
    engine = make_engine()
    before = obs(title="Desktop")
    after = obs({"control_type": "Window", "name": "Telegram"}, title="Telegram")
    engine._observe = lambda: after
    decision = {"action": "launch_application", "target": "Telegram", "reason": "bootstrap", "expected": "Telegram window"}
    result = engine._execute_local("Открой Telegram и найди чат", before, decision, set())
    assert result["ok"] is True
    assert ("launch_application", {"application": "Telegram"}) in engine.tools.calls


def test_supervisor_reused_pid_is_not_accepted(monkeypatch):
    monkeypatch.setattr(supervisor, "pid_alive", lambda pid: True)
    monkeypatch.setattr(supervisor, "_process_command_line", lambda pid: r"C:\\Windows\\System32\\notepad.exe")
    assert supervisor.is_eirven_supervisor(8856) is False
    monkeypatch.setattr(supervisor, "_process_command_line", lambda pid: r"python.exe -m eirven_ai.supervisor")
    assert supervisor.is_eirven_supervisor(8856) is True


def test_r16_cart_commit_guard_survives_cosmetic_state_change():
    engine = make_engine()
    goal = "Найди браслет Карма и добавь его в корзину"
    first_state = obs(
        {"control_type": "Text", "name": "Карма разберётся"},
        {"control_type": "Button", "name": "Добавить в корзину"},
    )
    second_state = Observation(
        title=first_state.title,
        handle=first_state.handle,
        elements=first_state.elements,
        compact=first_state.compact + "\nText name='обновлено'",
        fingerprint="cosmetically-different-state",
    )
    decision = {"action": "click", "target_index": 1, "reason": "commit", "expected": "cart changed", "commit": True}
    committed: set[str] = set()
    engine._wait_transition = lambda *args, **kwargs: {"changed": True, "settled": True}

    first = engine._execute_local(goal, first_state, decision, committed)
    second = engine._execute_local(goal, second_state, decision, committed)

    assert first["completed"] is True
    assert second["ok"] is False and second["completed"] is True
    assert "duplicate blocked" in second["error"]
    assert engine.operator.clicks == ["Добавить в корзину"]


def test_r16_never_autonomously_commits_purchase_or_unrequested_enter():
    engine = make_engine()
    state = obs({"control_type": "Button", "name": "Купить"})
    buy = {"action": "click", "target_index": 0, "reason": "buy", "expected": "order", "commit": True}
    result = engine._execute_local("Найди товар и купи его", state, buy, set())
    assert result["precommit_blocked"] is True
    assert engine.operator.clicks == []

    enter = {"action": "press", "key": "enter", "reason": "submit", "expected": "", "commit": True}
    result2 = engine._execute_local("Найди Карма", state, enter, set())
    assert result2["precommit_blocked"] is True
    assert not any(name == "press_key" for name, _ in engine.tools.calls)


def test_r16_search_subject_strips_page_scope_and_object_words():
    engine = make_engine()
    assert engine._search_subject("Найди на странице браслет кармы и добавь его в корзину") == "карма"
    assert engine._search_subject("Найди на этом сайте товар Карма и добавь в корзину") == "Карма"


def test_r16_browser_chrome_is_not_a_page_affordance():
    engine = make_engine()

    class BrowserTools(FakeTools):
        def execute(self, name: str, arguments: dict):
            if name == "foreground_window":
                return {"ok": True, "result": {
                    "title": "Astrostori | Браслеты - Samsung Browser",
                    "handle": 777,
                    "class_name": "Chrome_WidgetWin_1",
                    "rectangle": [0, 0, 1600, 900],
                }}
            return super().execute(name, arguments)

    class BrowserOperator(FakeOperator):
        def _elements(self, title: str, limit: int = 300, handle=None):
            return [
                {"control_type": "Group", "name": "Адресная строка и строка поиска, astrostori.ru/braslets", "class_name": "OmniboxViewViews", "visible": True, "enabled": True, "rectangle": [170, 40, 1300, 75]},
                {"control_type": "Edit", "name": "Поиск по товарам", "class_name": "site-search", "visible": True, "enabled": True, "rectangle": [300, 250, 900, 310]},
            ]

        def _is_browser_chrome(self, element: dict) -> bool:
            return "omnibox" in str(element.get("class_name") or "").casefold()

    engine.tools = BrowserTools()
    engine.operator = BrowserOperator()
    observation = engine._observe()
    assert observation.browser is True
    assert len(observation.elements) == 1
    assert observation.elements[0]["name"] == "Поиск по товарам"
    decision = engine._heuristic_decision("Найди браслет Карма и добавь его в корзину", observation, [])
    assert decision and decision["action"] == "type"
    assert decision["text"] == "Карма"
    assert decision["target_index"] == 0


def test_r16_only_omnibox_uses_visual_page_grounding_not_address_bar():
    engine = make_engine()
    observation = Observation(
        title="Astrostori | Браслеты - Samsung Browser", handle=777, elements=[], compact="TITLE=Astrostori\nBROWSER=True",
        fingerprint="browser-empty", window_class="Chrome_WidgetWin_1", window_rect=(0, 0, 1600, 900), browser=True,
    )
    engine._visual_browser_decision = lambda goal, observation, history: {
        "action": "visual_click", "target_index": -1, "x": .5, "y": .5,
        "reason": "page screenshot", "expected": "product details", "commit": False,
    }
    decision = engine._decision("Найди на странице браслет кармы и добавь его в корзину", observation, [])
    assert decision["action"] == "visual_click"
    assert decision.get("target_index") == -1


def test_r16_stops_when_foreground_context_changes():
    engine = make_engine()
    first = Observation(
        title="Astrostori - Samsung Browser", handle=123, elements=[], compact="TITLE=Astrostori", fingerprint="one",
        window_class="Chrome_WidgetWin_1", window_rect=(0,0,1600,900), browser=True,
    )
    lost = Observation(
        title="Блокнот", handle=456, elements=[], compact="TITLE=Блокнот\nCONTEXT_LOST", fingerprint="two",
        window_class="Notepad", window_rect=(0,0,1000,700), browser=False, context_lost=True,
    )
    states = iter([first, lost])
    engine._observe = lambda: next(states)
    engine._decision = lambda goal, observation, history: {
        "action": "wait", "target_index": -1, "amount": 0.01, "reason": "wait", "expected": ""
    }
    result = engine.execute_goal("Найди браслет Карма и добавь его в корзину", max_steps=3)
    assert result.ok is False
    assert "Активное окно сменилось" in result.summary


def test_r16_instant_greeting_accepts_fuzzy_wake_name():
    from eirven_ai.chat import ChatService
    service = ChatService.__new__(ChatService)
    service.identity = None
    assert service._instant_reply("Привет, Эрли.") is not None
    assert service._instant_reply("Привет, Эрви.") is not None
