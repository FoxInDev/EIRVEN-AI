from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from eirven_ai.api import MOBILE_TOKEN_HEADER, _mobile_path_allowed, build_api
from eirven_ai.dialogue import is_mobile_app_setup_request, is_resume_confirmation
from eirven_ai.reliability_router import ReliabilityRouter
from eirven_ai.russian_speech import speech_ready_text
from eirven_ai.voice_daemon import NativeVoiceDaemon


class _SettingsDb:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_setting(self, key: str, default=None):
        return self.values.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.values[key] = value


def _api_services(tmp_path: Path):
    settings = SimpleNamespace(
        data_dir=tmp_path / "data",
        root_dir=tmp_path,
        host="0.0.0.0",
        port=7860,
        model="main-model",
        fast_model="fast-model",
        code_model="code-model",
        vision_model="vision-model",
        asr_engine="gigaam",
        gigaam_model="gigaam-v3",
        whisper_model="whisper",
        tts_engine="silero",
    )
    settings.data_dir.mkdir()
    inbox = tmp_path / "video"
    inbox.mkdir()
    identity = SimpleNamespace(get=lambda: SimpleNamespace(assistant_name="Эрви"))
    return SimpleNamespace(
        settings=settings,
        db=_SettingsDb(),
        identity=identity,
        video=SimpleNamespace(inbox=inbox),
    )


def test_mobile_api_is_private_token_scoped(tmp_path: Path) -> None:
    services = _api_services(tmp_path)
    app = build_api(services)

    with TestClient(app, client=("testclient", 50000)) as local:
        config = local.get("/api/mobile/config")
        assert config.status_code == 200
        raw_token = config.json()["token"].replace("-", "")

    with TestClient(app, client=("192.168.1.50", 50000)) as phone:
        assert phone.get("/api/mobile/status").status_code == 401
        status = phone.get(
            "/api/mobile/status", headers={MOBILE_TOKEN_HEADER: raw_token, "Origin": "null"}
        )
        assert status.status_code == 200
        assert status.json()["assistant_name"] == "Эрви"
        assert status.headers["access-control-allow-origin"] == "null"
        assert phone.get(
            "/api/preferences", headers={MOBILE_TOKEN_HEADER: raw_token}
        ).status_code == 403
        preflight = phone.options(
            "/api/mobile/status",
            headers={"Origin": "null", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.status_code == 204

    with TestClient(app, client=("8.8.8.8", 50000)) as internet:
        assert internet.get(
            "/api/mobile/status", headers={MOBILE_TOKEN_HEADER: raw_token}
        ).status_code == 403


def test_mobile_video_upload_reaches_numbering_inbox(tmp_path: Path) -> None:
    services = _api_services(tmp_path)
    app = build_api(services)
    with TestClient(app, client=("testclient", 50000)) as local:
        token = local.get("/api/mobile/config").json()["token"]
    with TestClient(app, client=("192.168.1.51", 50000)) as phone:
        response = phone.post(
            "/api/mobile/video",
            headers={MOBILE_TOKEN_HEADER: token},
            files={"file": ("мой ролик.mp4", b"not-a-real-video", "video/mp4")},
        )
    assert response.status_code == 200
    uploaded = list((tmp_path / "video").glob("*.mp4"))
    assert len(uploaded) == 1
    assert uploaded[0].read_bytes() == b"not-a-real-video"
    assert not list((tmp_path / "video").glob(".eirven-upload-*"))


def test_mobile_allowlist_has_only_needed_methods() -> None:
    assert _mobile_path_allowed("/api/chat/stream", "POST")
    assert _mobile_path_allowed("/api/chat/abc123/stop", "POST")
    assert not _mobile_path_allowed("/api/preferences", "GET")
    assert not _mobile_path_allowed("/api/mobile/config", "GET")
    assert not _mobile_path_allowed("/api/conversations/abc123", "DELETE")


def test_russian_tts_pronounces_digits_and_latin_tokens() -> None:
    spoken = speech_ready_text(
        "Wi-Fi 6 работает на 42%. Python 3.12, VS Code, OpenAI API, MP4 и 1080p."
    )
    assert not re.search(r"[A-Za-z0-9]", spoken)
    for fragment in (
        "вай фай шесть",
        "сорок два процента",
        "пайтон",
        "ви эс код",
        "оупен эй ай",
        "эм пи четыре",
        "одна тысяча восемьдесят пи",
    ):
        assert fragment in spoken


def test_long_voice_answer_is_hard_split_for_low_latency() -> None:
    text = " ".join(["длинная пользовательская фраза"] * 80)
    chunks = NativeVoiceDaemon._speech_chunks(text, limit=120)
    assert len(chunks) > 1
    assert all(1 <= len(chunk) <= 120 for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_natural_command_variants_keep_their_intent() -> None:
    router = ReliabilityRouter()
    cases = {
        "Эрви, открой Telegram и напиши Тиме привет": ("app_compound", "telegram"),
        "Пожалуйста, скинь Тиме привет в телеге": ("app_compound", "telegram"),
        "Зайди на ютуб": ("app_open", "youtube"),
        "Вруби музыку": ("media_start", "yandex_music"),
        "Открой Telegram, потом Discord": ("mission", ""),
    }
    for phrase, expected in cases.items():
        decision = router.classify(phrase)
        assert (decision.kind, decision.app) == expected, phrase


def test_user_facing_followups_are_understood() -> None:
    assert is_resume_confirmation("Я уже вошёл, можешь продолжить")
    assert is_resume_confirmation("Эрви, возобновить")
    assert not is_resume_confirmation("я не готов")
    assert is_mobile_app_setup_request("Как подключить APK на Android?")
    assert is_mobile_app_setup_request("Где код подключения телефона?")
    assert not is_mobile_app_setup_request("Настрой Telegram на телефоне")
