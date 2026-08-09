from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine, Observation
from eirven_ai.chat import ChatService
from eirven_ai.tools import ToolExecutor
from eirven_ai.universal_workflow import UniversalWorkflowEngine


class FakeDB:
    def get_setting(self, *args, **kwargs): return None
    def set_setting(self, *args, **kwargs): return None


class FakeGateway:
    def installed_models(self): return []
    def json(self, *args, **kwargs): raise RuntimeError("model unavailable")


class FakeTools:
    def __init__(self, title: str = "Shop - Samsung Browser"):
        self.calls: list[tuple[str, dict]] = []
        self.title = title

    def execute(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "foreground_window":
            return {"ok": True, "result": {"title": self.title, "class_name": "Chrome_WidgetWin_1", "handle": 77}}
        if name == "system_volume":
            return {"ok": True, "result": {"action": arguments.get("action"), "steps": arguments.get("steps"), "sent": True}}
        if name == "process_terminate":
            return {"ok": True, "result": {"verified": True, "terminated_count": 3, "protected_count": 2}}
        return {"ok": True, "result": {}}


def engine() -> AutonomousWorkflowEngine:
    services = SimpleNamespace(
        tools=FakeTools(), desktop_operator=SimpleNamespace(), gateway=FakeGateway(),
        settings=SimpleNamespace(action_model="x", fast_model="x", model="x", root_dir=Path(".")),
        style=SimpleNamespace(get=lambda: SimpleNamespace(prompt=lambda: "")), db=FakeDB(), runtime=None,
    )
    return AutonomousWorkflowEngine(services)


def obs(*rows: dict) -> Observation:
    elements = []
    for i, row in enumerate(rows):
        elements.append({
            "index": i, "control_type": row.get("control_type", "Text"), "name": row.get("name", ""),
            "value": row.get("value", ""), "visible": row.get("visible", True), "enabled": True,
            "automation_id": row.get("automation_id", ""), "class_name": row.get("class_name", ""),
            "rectangle": row.get("rectangle", [50, 200 + i * 40, 300, 235 + i * 40]),
        })
    compact = "\n".join(str(e) for e in elements)
    return Observation(
        title="Shop - Samsung Browser", handle=77, elements=elements, compact=compact,
        fingerprint=str(hash(compact)), window_class="Chrome_WidgetWin_1", window_rect=(0, 0, 1600, 900), browser=True,
    )


def test_browser_section_is_site_context_not_windows_object():
    e = engine()
    assert e.should_handle("Зайди в раздел каталог.")
    d = e._heuristic_decision("Зайди в раздел каталог.", obs({"control_type": "Button", "name": "каталог"}), [])
    assert d and d["action"] == "click" and d["reason"] == "site navigation target"


def test_cart_navigation_is_generic_named_site_destination():
    e = engine()
    d = e._heuristic_decision("перейди в корзину", obs({"control_type": "Button", "name": "Корзина", "class_name": "header cart"}), [])
    assert d and d["action"] == "click" and d["target_index"] == 0


def test_hidden_search_is_revealed_before_typing():
    e = engine()
    goal = "найди на этой странице холика холика и добавь в корзину"
    initial = obs(
        {"control_type": "Button", "name": "Поиск", "class_name": "header search"},
        {"control_type": "Button", "name": "Корзина", "class_name": "header cart"},
        {"control_type": "Button", "name": "каталог"},
    )
    d1 = e._heuristic_decision(goal, initial, [])
    assert d1 and d1["action"] == "click" and d1["reason"].startswith("search trigger")
    history = [{**d1, "ok": True, "verified": True}]
    modal = obs({"control_type": "Edit", "name": "Поиск товаров"}, {"control_type": "Button", "name": "Найти"})
    d2 = e._heuristic_decision(goal, modal, history)
    assert d2 and d2["action"] == "type" and d2["text"] == "холика холика"
    history.append({**d2, "ok": True, "verified": True})
    d3 = e._heuristic_decision(goal, modal, history)
    assert d3 and d3["action"] == "click" and d3["reason"] == "visible search submit"


def _chat(title: str = "EIRVEN - Samsung Browser") -> ChatService:
    c = ChatService.__new__(ChatService)
    c.tools = FakeTools(title)
    c.runtime = None
    c.universal_workflow = None
    c.app_skills = None
    c.desktop_operator = None
    c.db = SimpleNamespace(set_setting=lambda *a, **k: None)
    c._trace = lambda *a, **k: None
    return c


def test_explicit_system_volume_delta_routes_without_llm():
    c = _chat()
    acted, _answer, route = c._priority_control_turn("Увеличь системную громкость на 5.", "x")
    assert acted and route["action"] == "system_volume_priority"
    assert ("system_volume", {"action": "up", "steps": 5}) in c.tools.calls


def test_python_process_close_is_real_system_tool_not_browser_agent():
    c = _chat()
    acted, answer, route = c._priority_control_turn("закрой все пайтон процессы", "x")
    assert acted and route["action"] == "python_process_terminate_priority" and route["verified"]
    assert any(name == "process_terminate" for name, _args in c.tools.calls)
    assert "EIRVEN" in answer


def test_play_wording_includes_igrai():
    u = UniversalWorkflowEngine.__new__(UniversalWorkflowEngine)
    u._norm = lambda x: " ".join(str(x).casefold().replace("ё", "е").split())
    assert u._media_action_for_goal("играй", implicit=True) == "play_pause"
    assert u._desired_media_state("играй") == "playing"


def test_process_terminate_primitive_is_exposed():
    assert hasattr(ToolExecutor, "tool_process_terminate")
