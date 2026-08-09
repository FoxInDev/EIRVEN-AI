from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine
from eirven_ai.chat import ChatService
from eirven_ai.mission_engine import MissionEngine, MissionNode


class DB:
    def __init__(self): self.data = {}
    def get_setting(self, key, default=None): return self.data.get(key, default)
    def set_setting(self, key, value): self.data[key] = value


class Gateway:
    def installed_models(self): return []


class Tools:
    def __init__(self, folder: Path | None = None): self.folder = folder; self.calls = []
    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "foreground_window": return {"ok": True, "result": {"title": "EIRVEN-AI", "class_name": "CabinetWClass", "handle": 7}}
        if name == "explorer_current_folder" and self.folder: return {"ok": True, "result": {"path": str(self.folder)}}
        if name == "system_find": return {"ok": True, "result": []}
        return {"ok": False, "error": name}


def make_engine(folder: Path | None = None):
    services = SimpleNamespace(
        tools=Tools(folder), gateway=Gateway(), db=DB(),
        settings=SimpleNamespace(root_dir=Path("."), action_model="", fast_model=""),
        autonomous_workflow=None, desktop_operator=None, app_skills=None,
        applications=None, universal_workflow=None, chat=None, agent=None, router=None,
    )
    return MissionEngine(services), services


def make_chat(mission_engine=None, tasks=None):
    c = ChatService.__new__(ChatService)
    c._lock = threading.RLock(); c._stop_events = {}; c._conversation_locks = {}
    c.tasks = tasks; c.runtime = None; c.universal_workflow = None; c.mission_engine = mission_engine
    c.tools = None; c.settings = SimpleNamespace(root_dir=Path("."))
    return c


def test_live_file_phrase_with_telegram_inflection_gets_typed_plan(tmp_path):
    e, _ = make_engine(tmp_path)
    q = "отправь файл Loce 2 из открытой папки мне в избранное в Телеграме"
    assert e.should_handle(q)
    nodes = e.plan(q, context={"explorer_folder": str(tmp_path)})["nodes"]
    assert [x["kind"] for x in nodes] == ["resolve_file", "telegram_file"]
    assert nodes[0]["metadata"]["file_name"] == "Loce 2"
    assert nodes[1]["metadata"]["recipient"] == "Избранное"


def test_file_resolver_recovers_short_asr_filename_inside_captured_folder(tmp_path):
    wanted = tmp_path / "logs2.txt"
    wanted.write_text("ok", encoding="utf-8")
    e, _ = make_engine(tmp_path)
    mission = {"context": {"explorer_folder": str(tmp_path)}, "artifacts": {}}
    node = MissionNode(id="n1", goal="find", kind="resolve_file", metadata={"file_name": "Loce 2"})
    result = e._resolve_file(node, mission)
    assert result["ok"] is True
    assert Path(result["artifact"]["path"]) == wanted.resolve()


def test_exact_live_yandex_to_telegram_phrase_stays_one_mission():
    e, _ = make_engine()
    q = "открой ЯндексМузыку, найди песню Банк, включи её, потом открой Телеграм и напиши Кириллу, что включил музыку"
    nodes = e.plan(q)["nodes"]
    assert [n["kind"] for n in nodes] == ["app", "ui", "media", "app", "telegram_message"]
    assert [n["app"] for n in nodes] == ["yandex_music", "yandex_music", "yandex_music", "telegram", "telegram"]
    assert nodes[-1]["goal"] == "напиши Кириллу, что включил музыку"
    assert nodes[-1]["commit"] is True


def test_priority_fast_paths_cannot_steal_tail_of_cross_app_mission():
    e, _ = make_engine()
    c = make_chat(e)
    acted, answer, route = c._priority_control_turn(
        "открой ЯндексМузыку, найди песню Банк, включи её, потом открой Телеграм и напиши Кириллу, что включил музыку", "c1"
    )
    assert acted is False and answer == "" and route == {}


def test_continuation_words_never_become_ui_nodes():
    e, _ = make_engine()
    q = "ещё после этого открой острой, найди браслет Карма и добавь его в корзину"
    nodes = e.plan(q)["nodes"]
    assert [n["kind"] for n in nodes] == ["open_target", "ui", "ui"]
    assert all(n["goal"].casefold().strip() not in {"еще", "ещё", "после этого"} for n in nodes)
    assert nodes[0]["metadata"]["target"] == "острой"


def test_context_infers_telegram_unread_without_repeating_app_name():
    e, _ = make_engine()
    ctx = {"foreground": {"title": "Telegram - Samsung Browser", "class_name": "Chrome_WidgetWin_1"}}
    nodes = e.plan("ответь на все непрочитанные чаты в моём стиле", context=ctx)["nodes"]
    assert len(nodes) == 1 and nodes[0]["kind"] == "telegram_unread" and nodes[0]["app"] == "telegram"


def test_explicit_telegram_unread_is_not_stolen_by_current_chat_style_fastpath():
    e, _ = make_engine()
    c = make_chat(e)
    acted, _, _ = c._priority_control_turn("открой Telegram, ответь на все непрочитанные личные чаты в моём стиле и каналы пропусти", "c1")
    assert acted is False


class ActiveTasks:
    def __init__(self): self.cancelled = []
    def latest(self, **kwargs): return {"id": "m1", "status": "running", "kind": "mission"}
    def cancel(self, task_id): self.cancelled.append(task_id); return True


def test_short_cancel_stops_background_mission_too():
    tasks = ActiveTasks()
    c = make_chat(None, tasks)
    acted, answer, route = c._priority_control_turn("отмена", "c1")
    assert acted is True and tasks.cancelled == ["m1"]
    assert route["mission_cancelled"] is True
    assert "миссию" in answer


def test_explicit_self_shutdown_needs_no_second_yes_turn():
    assert ChatService._self_shutdown_requested("Эрви, выключи себя") is False  # wake word is removed before ChatService
    assert ChatService._self_shutdown_requested("выключи себя") is True
    assert ChatService._self_shutdown_requested("закрой себя полностью") is True


def test_telegram_unread_badge_and_header_classification_are_generic():
    assert MissionEngine._telegram_row_unread_count("Просто Парни voice message 16") == 16
    assert MissionEngine._telegram_row_unread_count("Кирилл ЛД last seen recently Fri рил") == 0
    channel = [{"name": "192,695 subscribers", "rectangle": [1200, 220, 1600, 260]}]
    personal = [{"name": "last seen recently", "rectangle": [1200, 220, 1600, 260]}]
    assert MissionEngine._telegram_header_kind(channel) == "non_personal"
    assert MissionEngine._telegram_header_kind(personal) == "personal"


def test_autonomous_semantics_ignore_continuation_only_words():
    dummy = AutonomousWorkflowEngine.__new__(AutonomousWorkflowEngine)
    assert dummy._goal_terms("ещё после этого дальше") == []


def test_telegram_message_executor_uses_existing_verified_send_lane():
    e, services = make_engine()
    class Chat:
        def _telegram_send_turn(self, target):
            assert "Кириллу" in target and "telegram" in target
            return True, "Отправила", {"action":"telegram_send_verified", "result":{"sent":True,"verified":True,"completed":True}}
    services.chat = Chat()
    result = e._telegram_message(MissionNode(id="n1", goal="напиши Кириллу, что включил музыку", kind="telegram_message", app="telegram", commit=True))
    assert result["ok"] is True and result["verified"] is True and result["completed"] is True


def test_media_executor_uses_state_verifier_not_generic_ui():
    e, services = make_engine()
    class Workflow:
        def ensure_media_goal(self, goal, **kwargs):
            assert goal == "включи её"
            return {"ok":True,"completed":True,"verified":True,"desired":"playing"}
    services.universal_workflow = Workflow()
    result = e._media_node(MissionNode(id="n1", goal="включи её", kind="media", app="yandex_music"), threading.Event())
    assert result["verified"] is True and result["desired"] == "playing"


def test_unknown_open_target_falls_back_app_then_official_web():
    e, services = make_engine()
    class Skills:
        def open(self, target): return {"ok":False,"target":target}
    class Apps:
        def web_fallback(self, target): return {"url":"https://example.test/", "query":target}
    services.app_skills = Skills(); services.applications = Apps()
    node = MissionNode(id="n1", goal="открой острой", kind="open_target", metadata={"target":"острой"})
    result = e._open_target(node)
    assert result["ok"] is True and result["verified"] is True
