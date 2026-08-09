from __future__ import annotations

import inspect
import threading
from pathlib import Path
from types import SimpleNamespace

from eirven_ai.capabilities import CapabilityRegistry
from eirven_ai.desktop_operator import DesktopOperator
from eirven_ai.mission_engine import MissionEngine
from eirven_ai.tasks import TaskManager
from eirven_ai.tools import ToolExecutor


class DB:
    def __init__(self): self.data = {}
    def get_setting(self, key, default=None): return self.data.get(key, default)
    def set_setting(self, key, value): self.data[key] = value


class Gateway:
    def installed_models(self): return []


class Tools:
    def __init__(self, folder: Path | None = None):
        self.folder = folder
        self.calls = []
    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "foreground_window":
            return {"ok": True, "result": {"title": "Проводник", "class_name": "CabinetWClass", "handle": 11}}
        if name == "explorer_current_folder" and self.folder:
            return {"ok": True, "result": {"path": str(self.folder), "verified": True}}
        if name == "system_find":
            return {"ok": True, "result": []}
        if name == "system_volume":
            return {"ok": True, "result": {"sent": True, **args}}
        return {"ok": False, "error": name}


class Operator:
    def __init__(self): self.sent = []
    def telegram_send_file(self, recipient, path):
        self.sent.append((recipient, path))
        return {"ok": True, "sent": True, "completed": True, "verified": True, "recipient": recipient, "file": path}


class Manager:
    def live_instructions(self, task_id): return []


class Context:
    def __init__(self, task_id="mission-1"):
        self.task_id = task_id
        self.stop_event = threading.Event()
        self.manager = Manager()
        self.total = 0
        self.completed = 0
    def set_total(self, value): self.total = value
    def update(self, _message, *, completed_steps=None, progress=None, **_kw):
        if completed_steps is not None: self.completed = completed_steps
    def check_cancelled(self):
        assert not self.stop_event.is_set()


def engine(tmp_path: Path | None = None):
    db = DB(); tools = Tools(tmp_path); operator = Operator()
    services = SimpleNamespace(
        tools=tools, gateway=Gateway(), db=db,
        settings=SimpleNamespace(root_dir=Path("."), action_model="x", fast_model="x"),
        autonomous_workflow=None, desktop_operator=operator, app_skills=None,
        agent=None, router=None, chat=None,
    )
    return MissionEngine(services), services, operator


def test_r19_claims_cross_app_and_long_horizon_but_not_r18_shopping(tmp_path):
    e, _s, _o = engine(tmp_path)
    assert e.should_handle("Открой Яндекс Музыку, найди Oxxxymiron, поставь последний альбом")
    assert e.should_handle("Открой Telegram, ответь на все непрочитанные личные чаты в моем стиле, каналы пропусти")
    assert e.should_handle("отправь файл log2 из открытой папки мне в Избранное в Telegram")
    assert not e.should_handle("Найди браслет Карма и добавь его в корзину")


def test_file_transfer_plan_keeps_object_source_destination_separate(tmp_path):
    e, _s, _o = engine(tmp_path)
    plan = e.plan("отправь файл log2 из папки открытой мне в избранное в telegram")
    nodes = plan["nodes"]
    assert [n["kind"] for n in nodes] == ["resolve_file", "telegram_file"]
    assert nodes[0]["metadata"]["file_name"] == "log2"
    assert nodes[1]["metadata"]["recipient"] == "Избранное"
    assert nodes[1]["dependencies"] == ["n1"]
    assert "из папки" not in nodes[0]["metadata"]["file_name"]


def test_capture_context_freezes_open_explorer_folder_before_app_switch(tmp_path):
    e, _s, _o = engine(tmp_path)
    captured = e.capture_context()
    assert captured["explorer_folder"] == str(tmp_path)
    assert captured["foreground"]["handle"] == 11


def test_file_artifact_is_resolved_then_sent_once(tmp_path):
    wanted = tmp_path / "log2.txt"
    wanted.write_text("hello", encoding="utf-8")
    e, _s, operator = engine(tmp_path)
    ctx = Context("m-file")
    result = e.run_task(ctx, {
        "goal": "отправь файл log2 из открытой папки мне в избранное в telegram",
        "context": {"explorer_folder": str(tmp_path)},
    })
    assert result["ok"] is True
    assert operator.sent == [("Избранное", str(wanted.resolve()))]
    assert result["artifacts"]["n1"]["name"] == "log2.txt"


def test_yandex_long_goal_propagates_app_context(tmp_path):
    e, _s, _o = engine(tmp_path)
    nodes = e.plan("Открой Яндекс Музыку, найди Oxxxymiron, поставь последний альбом")["nodes"]
    assert [n["app"] for n in nodes] == ["yandex_music", "yandex_music", "yandex_music"]
    assert nodes[0]["kind"] == "app"
    assert nodes[1]["dependencies"] == ["n1"] and nodes[2]["dependencies"] == ["n2"]


def test_telegram_unread_becomes_bounded_collection_node(tmp_path):
    e, _s, _o = engine(tmp_path)
    nodes = e.plan("Открой Telegram, ответь на все непрочитанные личные чаты в моем стиле, каналы пропусти")["nodes"]
    assert nodes[0]["kind"] == "app"
    assert nodes[1]["kind"] == "telegram_unread"
    assert nodes[1]["commit"] is True


def test_mission_has_dedicated_multitask_lane_and_desktop_is_serialized():
    assert "mission" in TaskManager.MISSION_KINDS
    assert "mission" not in TaskManager.FAST_KINDS
    src = inspect.getsource(MissionEngine)
    assert "_desktop_lock" in src and "ThreadPoolExecutor" in src
    assert "R19_LIVE_UPDATE" in src and "r19_mission_state" in src


def test_r19_exposes_real_file_and_telegram_primitives():
    assert hasattr(ToolExecutor, "tool_explorer_current_folder")
    assert hasattr(DesktopOperator, "telegram_send_file")
    sender = inspect.getsource(DesktopOperator.telegram_send_file)
    assert "telegram_send_file_commit" in sender
    assert "Never click Send again" in sender


def test_capability_source_advertises_final_agent_layers():
    src = inspect.getsource(CapabilityRegistry.refresh)
    assert "long_horizon_missions" in src
    assert "cross_app_task_graph" in src
    assert "parallel_background_nodes" in src


def test_cross_app_voice_chain_splits_plain_and_and_marks_message_commit(tmp_path):
    e, _s, _o = engine(tmp_path)
    nodes = e.plan(
        "Открой Яндекс Музыку, найди Oxxxymiron, поставь последний альбом, "
        "потом открой Telegram и напиши Кириллу, что включил музыку"
    )["nodes"]
    assert len(nodes) == 5
    assert [n["app"] for n in nodes] == ["yandex_music", "yandex_music", "yandex_music", "telegram", "telegram"]
    assert nodes[3]["kind"] == "app"
    assert nodes[4]["goal"].startswith("напиши Кириллу")
    assert nodes[4]["commit"] is True
    assert nodes[4]["dependencies"] == ["n4"]


def test_task_manager_source_starts_two_mission_coordinators():
    src = inspect.getsource(TaskManager.start)
    assert 'args=("mission",)' in src
    assert 'range(2)' in src
    claim = inspect.getsource(TaskManager._claim)
    assert 'lane == "mission"' in claim
