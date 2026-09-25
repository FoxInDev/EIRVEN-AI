# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eirven_ai.food import FoodService
from eirven_ai.app_skills import AppSkills
from eirven_ai.agent import LocalAgent
from eirven_ai.chat import ChatService
from eirven_ai.database import Database
from eirven_ai.memory import MemoryStore
from test_reactive_v2_adversarial import ScriptedGateway, ScriptedTools, _settings, _call


@pytest.fixture
def food(tmp_path):
    gateway = SimpleNamespace(chat=lambda *a, **k: {"content": ""})
    instance = FoodService(SimpleNamespace(root_dir=tmp_path, fast_model="fake", chat_num_ctx=4096), gateway, None)
    instance._session_id = "test"
    instance.available = lambda: True
    return instance


def mcp_result(payload):
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


def test_real_mcp_envelope_search_and_two_item_cart(food):
    calls = []
    def post(payload, **kwargs):
        name, args = payload["params"]["name"], payload["params"]["arguments"]
        calls.append((name, args))
        if name == "vkusvill_products_search":
            assert "query" not in args and args["q"] in {"суп", "морс"}
            return mcp_result({"ok": True, "data": {"items": [{
                "xml_id": 100 if args["q"] == "суп" else 200,
                "name": args["q"], "price": {"current": 150}, "rating": {"average": 4.9},
            }]}})
        assert name == "vkusvill_cart_link_create"
        assert args["products"] == [{"xml_id": 100, "q": 1}, {"xml_id": 200, "q": 1}]
        return mcp_result({"ok": True, "data": {"url": "https://vkusvill.ru/cart/test"}})
    food._post = post
    result = food.respond("Собери корзину из супа любого и морса", {
        "intent": "cart", "items": ["суп", "морс"], "min_rating": 4.8,
    })
    assert result["completed"] is True
    assert result["cart_url"] in result["answer"]
    assert len(calls) == 3
    assert "150 ₽" in result["answer"]


@pytest.mark.parametrize("payload", [
    {"result": {"isError": True, "content": [{"type": "text", "text": "error"}]}},
    mcp_result({"ok": False, "error": {"message": "bad arguments"}}),
    {"error": {"code": -32602}},
])
def test_mcp_errors_are_not_products(food, payload):
    food._post = lambda *a, **k: payload
    with pytest.raises(RuntimeError):
        food.call_tool("vkusvill_products_search", {"q": "суп"})


def test_missing_product_does_not_create_partial_cart_as_complete(food):
    food.find_products = lambda q, **kw: [{"xml_id": 10, "name": "суп"}] if q == "суп" else []
    food.build_cart = lambda *a: pytest.fail("incomplete basket must not be created")
    result = food.respond("суп и морс", {"intent": "cart", "items": ["суп", "морс"]})
    assert result["completed"] is False and "морс" in result["answer"]


@pytest.mark.parametrize("query,expected", [
    ("Запусти в браузере яндекс еду", "яндекс еду"),
    ("Эрви, открой Яндекс Еду", "Яндекс Еду"),
    ("Открой ВкусВилл", "ВкусВилл"),
    ("Открой в браузере яндекс еду и закрой браузер", ""),
    ("Собери корзину из супа", ""),
    ("Открой файл report.txt", ""),
])
def test_browser_open_target(query, expected):
    assert AppSkills.browser_open_target(query) == expected


@pytest.mark.parametrize("verified", [True, False])
def test_open_service_never_turns_into_media_or_close(verified):
    window = {"title": "Яндекс Еда - Chrome", "handle": 99, "pid": 42, "class_name": "Chrome_WidgetWin_1", "rectangle": [0, 0, 1280, 900]}
    tools = ScriptedTools(
        ["open_service", "foreground_window", "window_list", "window_elements", "close_browsers", "media_control"],
        {"open_service": {"ok": True, "verified": verified, "result": {"verified": verified, "window": window}},
         "foreground_window": {"ok": True, "result": window},
         "window_elements": {"ok": True, "result": []}},
    )
    gateway = ScriptedGateway([_call("close_browsers")])
    agent = LocalAgent(_settings(), gateway, tools, SimpleNamespace())
    answer = agent.run("Запусти в браузере яндекс еду", require_tool_action=True, model_first_action={
        "action": "open_service", "target": "яндекс еду", "service": "яндекс еду",
        "target_kind": "service", "single_step": True, "decision_status": "ok",
    })
    names = [name for name, args in tools.executed]
    assert names.count("open_service") == 1
    assert "close_browsers" not in names and "media_control" not in names
    assert "воспроизведение" not in answer


def test_food_turn_uses_history_persists_once_and_finishes(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    memory = MemoryStore(db)
    cid = memory.ensure_conversation(None, "Друг")
    memory.add_message(cid, "user", "Я хочу заказать обед")
    settings = SimpleNamespace(root_dir=tmp_path, fast_model="fake", chat_num_ctx=4096, model_only_mode=False)
    chat = ChatService(settings, db, SimpleNamespace(), SimpleNamespace(), memory, SimpleNamespace())
    chat._fast_data_turn = lambda *a: (False, "", {})
    chat._priority_control_turn = lambda *a: (False, "", {})
    chat._explicit_local_paths = lambda *a: []
    chat._guided_file_publish_turn = lambda q, c, paths: (q, False, "", {}, [])
    seen = []
    def understand(query, context=""):
        seen.append(context)
        return {"food": True, "intent": "search"}
    chat.food = SimpleNamespace(understand=understand, respond=lambda *a, **k: {
        "answer": "Нашла суп во ВкусВилле.", "intent": "search", "products": [{"xml_id": 1}], "completed": True,
    })
    events = list(chat.stream_events("Предложи пару вариантов доставки", cid))
    assert events[-1]["type"] == "done", events
    assert events[-1]["route"]["action"] == "food", events
    assert "заказать обед" in seen[0]
    assert len([row for row in memory.history(cid, 20) if row["role"] == "assistant"]) == 1
