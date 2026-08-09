from __future__ import annotations

import inspect
import threading
from pathlib import Path
from types import SimpleNamespace

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine, Observation
from eirven_ai.chat import ChatService
from eirven_ai.universal_workflow import UniversalWorkflowEngine
from eirven_ai.voice import VoiceService
import eirven_ai.app as app_module


class Tools:
    def __init__(self, title="Desktop"):
        self.title = title
        self.calls = []
    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "foreground_window":
            return {"ok": True, "result": {"title": self.title, "handle": 77, "class_name": "Chrome_WidgetWin_1", "rectangle": [0,0,1600,900]}}
        if name == "system_volume":
            return {"ok": True, "result": {"action": args.get("action"), "steps": args.get("steps"), "sent": True}}
        if name == "open_default_url":
            self.title = "astrostory.ru - Samsung Browser"
            return {"ok": True, "result": {"url": args.get("url")}}
        return {"ok": True, "result": {}}


class Operator:
    def __init__(self):
        self.clicked = False
    def _elements(self, title, limit=320, handle=None):
        return [
            {"control_type":"Button", "name":"Не нравится", "class_name":"DislikeButton", "visible":True, "enabled":True, "rectangle":[1200,760,1240,800]},
            {"control_type":"Button", "name":"Нравится", "class_name":("LikeButton active" if self.clicked else "LikeButton"), "visible":True, "enabled":True, "rectangle":[1300,760,1340,800]},
        ]
    def click_element(self, title, element, *, goal=""):
        self.clicked = True
        return True


def make_autonomous():
    services = SimpleNamespace(
        tools=Tools(), desktop_operator=SimpleNamespace(), gateway=SimpleNamespace(installed_models=lambda: []),
        settings=SimpleNamespace(action_model="x", fast_model="x", model="x", root_dir=Path(".")),
        style=SimpleNamespace(get=lambda: SimpleNamespace(prompt=lambda: "")),
        db=SimpleNamespace(get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None), runtime=None,
    )
    return AutonomousWorkflowEngine(services)


def observation(*rows):
    elements=[]
    for i, row in enumerate(rows):
        elements.append({"index":i,"control_type":row.get("control_type","Text"),"name":row.get("name",""),"value":row.get("value",""),"visible":True,"enabled":True,"automation_id":"","class_name":"",**row})
    compact="\n".join(f"[{i}] {e['control_type']} {e['name']} {e['value']}" for i,e in enumerate(elements))
    return Observation(title="Astrostori | Браслеты - Samsung Browser", handle=77, elements=elements, compact=compact, fingerprint=str(hash(compact)), window_class="Chrome_WidgetWin_1", window_rect=(0,0,1600,900), browser=True)


def test_r17_search_is_type_submit_scroll_not_type_type_type():
    engine=make_autonomous()
    goal="найди на этой странице браслет кармы и добавь его в корзину"
    history=[]
    s1=observation(
        {"control_type":"Group","name":"","class_name":"t838__wrapper t-site-search-input"},
        {"control_type":"Edit","name":"Поиск по фразе"},
        {"control_type":"Button","name":"Искать"},
    )
    d1=engine._heuristic_decision(goal,s1,history)
    assert d1["action"]=="type" and d1["target_index"]==1 and d1["text"]=="карма"
    history.append({**d1,"ok":True,"verified":True})
    s2=observation(
        {"control_type":"Group","name":"","class_name":"t838__wrapper t-site-search-input"},
        {"control_type":"Edit","name":"Поиск по фразе","value":""},
        {"control_type":"Button","name":"Искать"},
    )
    d2=engine._heuristic_decision(goal,s2,history)
    assert d2["action"]=="click" and d2["target_index"]==2
    history.append({**d2,"target":"Искать","ok":True,"verified":True})
    s3=observation({"control_type":"Edit","name":"Поиск по фразе","value":""})
    d3=engine._heuristic_decision(goal,s3,history)
    assert d3["action"]=="scroll" and "search recovery" in d3["reason"]
    assert d3.get("text","")==""


def test_r17_scroll_reveals_product_then_semantic_click_wins():
    engine=make_autonomous()
    goal="найди браслет кармы и добавь его в корзину"
    history=[
        {"action":"type","text":"карма","reason":"visible search field","ok":True},
        {"action":"click","target":"Искать","reason":"visible search submit","ok":True},
        {"action":"scroll","reason":"search recovery: scan results","ok":True},
    ]
    state=observation({"control_type":"Hyperlink","name":"Карма разберётся Без лишних мыслей 2 490 р."})
    decision=engine._heuristic_decision(goal,state,history)
    assert decision["action"]=="click"
    assert decision["reason"]=="semantic result match"


def test_r17_yandex_media_snapshot_understands_playing_class_and_paused_button():
    engine=UniversalWorkflowEngine.__new__(UniversalWorkflowEngine)
    engine.operator=object()
    engine._active_window=lambda: {"title":"Яндекс Музыка", "handle":77}
    engine._interactive_elements=lambda *a, **k: [{"control_type":"Button","name":"Пауза","class_name":"VibePlayerControls_playButton__vnoer VibePlayerControls_playButton_playing__abc","rectangle":[500,700,560,760]}]
    assert engine._media_snapshot()["state"]=="playing"
    engine._interactive_elements=lambda *a, **k: [{"control_type":"Button","name":"Воспроизведение","class_name":"VibePlayerControls_playButton__vnoer","rectangle":[500,700,560,760]}]
    assert engine._media_snapshot()["state"]=="paused"


def bare_chat(title="Desktop"):
    service=ChatService.__new__(ChatService)
    service.runtime=None
    service.universal_workflow=None
    service.tools=Tools(title)
    service.desktop_operator=None
    service.app_skills=None
    return service


def test_r17_quieter_is_deterministic_system_volume():
    service=bare_chat()
    acted, answer, route=service._priority_control_turn("сделай потише", "c")
    assert acted and route["action"]=="system_volume_priority" and route["model"]=="deterministic"
    assert ("system_volume", {"action":"down","steps":2}) in service.tools.calls


def test_r17_literal_domain_open_skips_planner():
    service=bare_chat("EIRVEN - Samsung Browser")
    acted, answer, route=service._priority_control_turn("открой открой сайт astrostory.ru", "c")
    assert acted and route["action"]=="open_site_priority" and route["completed"] is True
    assert ("open_default_url", {"url":"https://astrostory.ru"}) in service.tools.calls


def test_r17_yandex_like_clicks_positive_affordance_not_dislike():
    service=bare_chat("Яндекс Музыка — Samsung Browser")
    op=Operator(); service.desktop_operator=op
    acted, answer, route=service._priority_control_turn("поставь лайк музыки", "c")
    assert acted and op.clicked
    assert route["action"]=="yandex_like_priority" and route["completed"] is True
    assert route["verified"] is True


def test_r17_voice_readiness_does_not_wait_for_tts_prewarm():
    voice=VoiceService.__new__(VoiceService)
    voice._stt_ready=threading.Event(); voice._tts_ready=threading.Event()
    voice._stt_ready.set()
    assert voice.interactive_ready() is True
    source=inspect.getsource(VoiceService._prewarm_stt)
    assert "_prewarm_tts()" not in source


def test_r17_startup_prewarms_action_model_after_voice_weights_are_ready():
    source=inspect.getsource(app_module.lifespan)
    assert "MODEL_PREWARM_BEGIN" in source
    assert "services.voice, \"stt_ready\"" in source
    assert "tts_warm_ready" in source
    assert "tts_preload=True" in source


def test_r17_official_site_resolver_prefers_fuzzy_brand_domain_over_social_mention():
    from eirven_ai.browser import BrowserAutomation
    q="astrostory официальный сайт web app"
    official=BrowserAutomation._official_score(q,"Astrostori — персонализированные изделия","https://astrostori.ru/")
    social=BrowserAutomation._official_score(q,"Стильный браслет от бренда astrostory","https://ru.pinterest.com/pin/123/")
    assert official > social
