from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from eirven_ai.chat import ChatService
from eirven_ai.database import Database
from eirven_ai.identity import IdentityService, DEFAULT_VOICE_BY_GENDER
from eirven_ai.voice_daemon import NativeVoiceDaemon

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "eirven_ai" / "web"


def test_public_identity_is_female_baya_only(tmp_path: Path):
    assert DEFAULT_VOICE_BY_GENDER == {"female": "irina_soft"}
    db = Database(tmp_path / "id.db")
    service = IdentityService(db)
    identity = service.get()
    assert identity.gender == "female"
    assert identity.voice_key == "irina_soft"
    legacy = service.update({"gender": "male", "voice_key": "denis"})
    assert legacy.gender == "female"
    assert legacy.voice_key == "irina_soft"


def test_wake_accepts_common_asr_spelling_without_open_session(tmp_path: Path):
    db = Database(tmp_path / "wake.db")
    identity = IdentityService(db)
    identity.update({"assistant_name": "Эрви"})
    services = SimpleNamespace(db=db, identity=identity)
    daemon = NativeVoiceDaemon(services)
    for phrase in ("эрви привет", "эрве привет", "эйрви привет"):
        route = daemon._activation_route(phrase, speech_started_at=10.0, now=10.2)
        assert route["action"] == "accept"
        assert route["command"] == "привет"
    ignored = daemon._activation_route("привет", speech_started_at=11.0, now=11.2)
    assert ignored["action"] == "ignore"


def test_custom_name_is_the_only_wake_name(tmp_path: Path):
    db = Database(tmp_path / "custom-wake.db")
    identity = IdentityService(db)
    identity.update({"assistant_name": "Луна"})
    daemon = NativeVoiceDaemon(SimpleNamespace(db=db, identity=identity, voice=SimpleNamespace(interactive_ready=lambda: True)))
    accepted = daemon._activation_route("Луна открой Telegram", speech_started_at=10.0, now=10.1)
    rejected = daemon._activation_route("Эрви открой Telegram", speech_started_at=11.0, now=11.1)
    assert accepted["action"] == "accept"
    assert accepted["command"] == "открой telegram"
    assert rejected["action"] == "ignore"
    assert daemon.status()["wake_phrase"] == "Луна"


def test_identity_question_is_instant(tmp_path: Path):
    chat = object.__new__(ChatService)
    db = Database(tmp_path / "chat.db")
    chat.identity = IdentityService(db)
    chat.identity.update({"assistant_name": "Эрви", "user_address": "Дима", "onboarding_completed": True})
    answer = ChatService._instant_reply(chat, "как тебя зовут?")
    assert answer == "Я Эрви."


def test_simple_telegram_open_is_deterministic(tmp_path: Path):
    chat = object.__new__(ChatService)
    chat.tasks = None
    chat.runtime = None
    chat.universal_workflow = None
    chat.mission_engine = SimpleNamespace(should_handle=lambda q: False)
    chat.tools = SimpleNamespace(execute=lambda name, args: {"ok": True, "result": {"title": "EIRVEN"}} if name == "foreground_window" else {"ok": True})
    chat.app_skills = SimpleNamespace(
        canonical=lambda target: "telegram",
        open=lambda target: {"ok": True, "verified": True, "skill": "telegram"},
    )
    chat.desktop_operator = None
    chat.recovery = None
    chat.verifier = None
    chat.planner = None
    chat.autonomous_workflow = None
    chat.intents = None
    chat.modes = None
    handled, answer, route = ChatService._priority_control_turn(chat, "открой telegram", "c1")
    assert handled is True
    assert route["action"] in {"open_app_priority", "r22_app_open"}
    assert "Telegram" in answer


def test_settings_have_five_real_sections_and_no_manual_emotion_controls():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    for label in ("Общее", "Голос", "Внешний вид", "Приватность", "Обновления"):
        assert label in html
    for control in ("voice-output-volume", "autostart-enabled", "notifications-enabled", "mini-mode-enabled", "sphere-motion", "desktop-comments-enabled", "update-channel"):
        assert control in html
    for removed in ("emotion-mode", "speech-speed", "Нейромузыка", "Self-test", "Остановить всё"):
        assert removed not in html
    assert "/api/preferences" in js and "/api/updates/check" in js
    assert "settings-tabs" in css and "switch" in css


def test_no_spoken_yes_before_action():
    source = (ROOT / "src" / "eirven_ai" / "voice_daemon.py").read_text(encoding="utf-8")
    assert 'self._speak("Да.","quiet",turn_id)' not in source


def test_camera_still_removed():
    assert not (ROOT / "src" / "eirven_ai" / "camera.py").exists()
