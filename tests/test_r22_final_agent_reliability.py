from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from eirven_ai.autonomous_workflow import AutonomousWorkflowEngine, Observation
from eirven_ai.app_skills import AppSkills
from eirven_ai.desktop_operator import DesktopOperator
from eirven_ai.chat import ChatService
from eirven_ai.database import Database
from eirven_ai.identity import DEFAULT_VOICE_BY_GENDER, IdentityService
from eirven_ai.reliability_router import ReliabilityRouter
from eirven_ai.voice_daemon import NativeVoiceDaemon

ROOT = Path(__file__).resolve().parents[1]


def test_public_release_keeps_single_baya_identity(tmp_path: Path):
    assert DEFAULT_VOICE_BY_GENDER == {"female": "irina_soft"}
    db = Database(tmp_path / "id.db")
    service = IdentityService(db)
    identity = service.update({"gender": "male", "voice_key": "denis"})
    assert identity.gender == "female"
    assert identity.voice_key == "irina_soft"


def test_wake_accepts_erbi_asr_variant(tmp_path: Path):
    db = Database(tmp_path / "wake.db")
    daemon = NativeVoiceDaemon(SimpleNamespace(db=db, identity=IdentityService(db)))
    route = daemon._activation_route("эрби что ты умеешь", speech_started_at=10.0, now=10.1)
    assert route["action"] == "accept"
    assert route["command"] == "что ты умеешь"


def test_router_does_not_let_media_steal_youtube():
    router = ReliabilityRouter()
    d = router.classify("включи YouTube и включи любое видео")
    assert d.kind == "app_compound"
    assert d.app == "youtube"
    assert "любое видео" in d.remainder


def test_router_treats_named_brand_as_external_not_current_page():
    router = ReliabilityRouter()
    d = router.classify("открой золотое яблоко")
    assert d.kind == "external_open"
    assert d.target == "золотое яблоко"


def test_router_keeps_explicit_page_navigation_on_current_site():
    router = ReliabilityRouter()
    d = router.classify("открой на этом сайте каталог")
    assert d.kind == "page_navigation"
    assert d.target == "каталог"


def test_router_shutdown_includes_single_word_form():
    router = ReliabilityRouter()
    assert router.classify("выключись").kind == "self_shutdown"
    assert ChatService._self_shutdown_requested("выключись") is True


def _chat_stub() -> ChatService:
    chat = object.__new__(ChatService)
    chat.reliability_router = ReliabilityRouter()
    chat.tasks = None
    chat.runtime = None
    chat.universal_workflow = None
    chat.mission_engine = SimpleNamespace(should_handle=lambda q: False)
    chat.tools = SimpleNamespace(execute=lambda name, args: {"ok": True, "result": {"title": "EIRVEN"}})
    chat.desktop_operator = None
    chat.recovery = None
    chat.verifier = None
    chat.planner = None
    chat.autonomous_workflow = None
    chat.intents = None
    chat.modes = None
    return chat


def test_capabilities_question_never_calls_model(tmp_path: Path):
    chat = _chat_stub()
    db = Database(tmp_path / "chat.db")
    chat.db = db
    chat.identity = IdentityService(db)
    chat.app_skills = SimpleNamespace()
    handled, answer, route = ChatService._priority_control_turn(chat, "что ты умеешь", "c1")
    assert handled is True
    assert route["action"] == "r22_capabilities"
    assert "прилож" in answer.casefold()


def test_youtube_compound_opens_target_before_content_activation():
    calls: list[tuple[str, str]] = []
    chat = _chat_stub()
    chat.db = SimpleNamespace(set_setting=lambda *a, **k: None)
    chat.identity = None
    chat.app_skills = SimpleNamespace(
        open=lambda target: calls.append(("open", target)) or {"ok": True, "verified": True, "skill": target, "window": {"handle": 9}},
        canonical=lambda target: target,
    )
    chat.tools = SimpleNamespace(execute=lambda name, args: calls.append((name, str(args))) or {"ok": True, "result": {}})
    chat.universal_workflow = SimpleNamespace(
        activate_any_current_content=lambda goal: calls.append(("content", goal)) or {"ok": True, "completed": True, "verified": True, "matched": "Видео"},
        has_pending=lambda c: False,
    )
    handled, answer, route = ChatService._priority_control_turn(chat, "открой YouTube и включи любое видео", "c1")
    assert handled is True
    assert route["action"] == "r22_app_compound"
    assert calls[0] == ("open", "youtube")
    assert any(x[0] == "content" for x in calls)


def test_page_navigation_priority_is_one_shot_not_autonomous():
    chat = _chat_stub()
    chat.db = SimpleNamespace(set_setting=lambda *a, **k: None)
    chat.identity = None
    chat.app_skills = SimpleNamespace()
    chat.universal_workflow = SimpleNamespace(
        click_named_current=lambda q: {"ok": True, "completed": True, "verified": True, "answer": "Перешла в раздел «каталог»."},
        has_pending=lambda c: False,
    )
    handled, _answer, route = ChatService._priority_control_turn(chat, "открой на этом сайте каталог", "c1")
    assert handled is True
    assert route["action"] == "r22_page_navigation"


def _engine_stub() -> AutonomousWorkflowEngine:
    engine = object.__new__(AutonomousWorkflowEngine)
    engine.operator = SimpleNamespace(click_element=lambda *a, **k: (_ for _ in ()).throw(AssertionError("stale click executed")))
    engine.tools = SimpleNamespace(execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("stale tool executed")))
    return engine


def test_cancelled_autonomous_step_cannot_click():
    engine = _engine_stub()
    stop = threading.Event(); stop.set()
    obs = Observation(
        title="Shop - Browser", handle=1, compact="", fingerprint="a", browser=True,
        elements=[{"index": 0, "name": "Каталог", "control_type": "Button", "visible": True, "enabled": True, "rectangle": [1, 1, 20, 20]}],
    )
    result = AutonomousWorkflowEngine._execute_local(
        engine, "открой каталог", obs,
        {"action": "click", "target_index": 0, "reason": "test", "expected": "catalog"}, set(), stop_event=stop,
    )
    assert result["cancelled"] is True


def test_revealed_unnamed_search_field_is_used_without_model():
    engine = object.__new__(AutonomousWorkflowEngine)
    obs = Observation(
        title="Shop - Browser", handle=1, compact="", fingerprint="b", browser=True,
        elements=[
            {"index": 0, "name": "", "value": "", "control_type": "Edit", "visible": True, "enabled": True, "focused": True, "rectangle": [300, 180, 900, 230]},
        ],
    )
    history = [{"action": "click", "ok": True, "reason": "search trigger: reveal field", "target": "Поиск"}]
    decision = AutonomousWorkflowEngine._heuristic_decision(engine, "найди товар гель и добавь его в корзину", obs, history)
    assert decision is not None
    assert decision["action"] == "type"
    assert decision["text"] == "гель"
    assert decision["reason"] == "revealed generic search field"


def test_arbitrary_product_gets_visible_card_without_model():
    engine = object.__new__(AutonomousWorkflowEngine)
    obs = Observation(
        title="Shop - Browser", handle=1, compact="", fingerprint="c", browser=True,
        elements=[
            {"index": 0, "name": "Каталог", "control_type": "Button", "visible": True, "enabled": True, "rectangle": [10, 180, 120, 230], "class_name": "nav"},
            {"index": 1, "name": "Увлажняющий крем 1290 ₽", "control_type": "Hyperlink", "visible": True, "enabled": True, "rectangle": [400, 420, 850, 760], "class_name": "product-card"},
        ],
    )
    decision = AutonomousWorkflowEngine._heuristic_decision(engine, "из списка выбери любой товар и добавь его в корзину", obs, [])
    assert decision is not None
    assert decision["action"] == "click"
    assert decision["target_index"] == 1
    assert "any product" in decision["reason"]


def test_startup_prioritizes_tts_weight_load_before_qwen():
    voice = (ROOT / "src" / "eirven_ai" / "voice.py").read_text(encoding="utf-8")
    app = (ROOT / "src" / "eirven_ai" / "app.py").read_text(encoding="utf-8")
    assert 'name="eirven-tts-preload"' in voice
    assert 'tts_warm_ready' in app
    assert 'tts_preload=True' in app
    # Model-weight preload only; no dummy phrase is synthesized at startup.
    prewarm = voice.split("def _prewarm_tts", 1)[1].split("def ", 1)[0]
    assert ".preload(" in prewarm
    assert ".synthesize(" not in prewarm


def test_camera_still_removed():
    assert not (ROOT / "src" / "eirven_ai" / "camera.py").exists()


def test_literal_site_command_stays_with_mature_site_opener():
    d=ReliabilityRouter().classify("открой сайт Золотое яблоко")
    assert d.kind=="unknown"


def test_telegram_unread_collection_is_a_mission_not_single_send():
    d=ReliabilityRouter().classify("открой Telegram ответь на все непрочитанные личные чаты в моём стиле и каналы пропусти")
    assert d.kind=="mission"


def test_existing_browser_tab_is_reused_before_launch():
    calls=[]
    class Tools:
        def execute(self,name,args):
            calls.append((name,args))
            if name=="window_list":
                return {"ok":True,"result":[{"title":"Shop - Samsung Browser","handle":77,"class_name":"Chrome_WidgetWin_1"}]}
            if name=="window_elements":
                assert args["max_elements"]>=360
                return {"ok":True,"result":[
                    {"name":"Магазин","control_type":"TabItem","visible":True,"enabled":True,"rectangle":[100,0,300,70]},
                    {"name":"Telegram Web","control_type":"TabItem","visible":True,"enabled":True,"rectangle":[300,0,500,70]},
                ]}
            if name=="window_focus":
                return {"ok":True,"result":{"title":"Shop - Samsung Browser","handle":77}}
            if name=="foreground_window":
                return {"ok":True,"result":{"title":"Telegram Web - Samsung Browser","handle":77}}
            if name=="launch_application":
                raise AssertionError("must reuse existing browser tab before launch")
            return {"ok":True,"result":{}}
    operator=SimpleNamespace(click_element=lambda title,el,goal="": calls.append(("click",el["name"])) or True)
    services=SimpleNamespace(tools=Tools(),settings=SimpleNamespace(root_dir=ROOT),applications=SimpleNamespace())
    skills=AppSkills(services,operator)
    result=skills.open("Telegram")
    assert result["ok"] is True and result["reused_tab"] is True
    assert any(x[0]=="click" and x[1]=="Telegram Web" for x in calls)


def test_telegram_ready_does_not_require_globally_stable_spa():
    op=object.__new__(DesktopOperator)
    op._is_browser_chrome=lambda e: False
    rows=[{"name":"Search","automation_id":"telegram-search-input","class_name":"form-control","visible":True}]
    assert DesktopOperator._telegram_ready(op,rows) is True


def test_telegram_chat_evidence_accepts_header_immediately():
    op=object.__new__(DesktopOperator)
    op._norm=lambda s: str(s or "").casefold().replace("ё","е")
    rows=[
        {"name":"Тима","class_name":"fullName","automation_id":"","visible":True,"rectangle":[1100,190,1500,250]},
        {"name":"Message","class_name":"","automation_id":"editable-message-text","visible":True,"rectangle":[1000,1500,2200,1650]},
    ]
    ok,evidence=DesktopOperator._telegram_chat_evidence(op,rows,"тима","")
    assert ok is True and evidence["header"] is True and evidence["composer"] is True


def test_any_content_requires_content_grade_score():
    from eirven_ai.universal_workflow import UniversalWorkflowEngine
    class Operator:
        def wait_for_state(self,**kwargs): return {"changed":True}
    engine=object.__new__(UniversalWorkflowEngine)
    engine.operator=Operator()
    engine.tools=SimpleNamespace()
    engine._active_window=lambda: {"title":"Browser","handle":1}
    engine._interactive_elements=lambda title,limit=420,handle=None: [
        {"name":"Раздел","control_type":"Button","visible":True,"enabled":True,"rectangle":[300,300,500,360],"class_name":"navigation"},
    ]
    engine._click=lambda *a,**k: (_ for _ in ()).throw(AssertionError("weak navigation must not be clicked"))
    engine._trace=lambda *a,**k: None
    result=engine.activate_any_current_content("включи любое видео")
    assert result["ok"] is False


def test_page_navigation_strips_asr_hesitation():
    d=ReliabilityRouter().classify("открой э э раздел новинки")
    assert d.kind=="page_navigation"
    assert d.target=="новинки"


def test_telegram_compound_focuses_surface_before_send():
    calls=[]
    chat=_chat_stub()
    chat.db=SimpleNamespace(set_setting=lambda *a,**k: None)
    chat.identity=None
    chat.app_skills=SimpleNamespace(
        open=lambda target: calls.append(("open",target)) or {"ok":True,"verified":True,"reused_tab":True,"window":{"handle":42}},
        canonical=lambda target: target,
    )
    chat.tools=SimpleNamespace(execute=lambda name,args: calls.append((name,args)) or {"ok":True,"result":{}})
    chat._telegram_send_turn=lambda target: calls.append(("send",target)) or (True,"Отправила.",{"action":"telegram_send_verified"})
    handled,answer,route=ChatService._priority_control_turn(chat,"открой Telegram и напиши Тиме привет","c1")
    assert handled is True
    assert calls[0]==("open","telegram")
    assert any(x[0]=="send" for x in calls)
    assert route.get("r22_surface_open",{}).get("reused_tab") is True
