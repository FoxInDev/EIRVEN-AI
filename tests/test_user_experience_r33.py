from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import eirven_ai.api as api_module
from eirven_ai.api import _mobile_bootstrap_path_allowed, build_api
from eirven_ai.chat import ChatService
from eirven_ai.desktop_operator import DesktopOperator
from eirven_ai.mission_engine import MissionEngine
from eirven_ai.reliability_router import ReliabilityRouter
from eirven_ai.russian_speech import speech_ready_text
from eirven_ai.voice_daemon import NativeVoiceDaemon

from test_user_experience_r32 import _api_services


def test_phone_panel_qr_target_downloads_bundled_apk_without_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mobile = tmp_path / "mobile_client"
    mobile.mkdir()
    payload = b"PK\x03\x04" + (b"eirven-mobile" * 9_000)
    (mobile / "EIRVEN-Mobile.apk").write_bytes(payload)
    monkeypatch.setattr(
        api_module,
        "_lan_candidates",
        lambda _port: [{
            "url": "http://192.168.1.20:7860",
            "ip": "192.168.1.20",
            "interface": "Wi-Fi",
            "kind": "wifi",
            "source": "adapter",
            "recommended": True,
            "warning": "",
        }],
    )
    app = build_api(_api_services(tmp_path))

    with TestClient(app, client=("testclient", 50_000)) as desktop:
        config = desktop.get("/api/mobile/config")
        assert config.status_code == 200
        body = config.json()
        assert body["apk_available"] is True
        assert body["download_url"] == "http://192.168.1.20:7860/api/mobile/app.apk"
        assert body["install_url"] == "http://192.168.1.20:7860/mobile/install"
        assert body["address_options"][0]["kind"] == "wifi"
        assert len(body["apk_sha256"]) == 64

    with TestClient(app, client=("192.168.1.55", 50_000)) as phone:
        entry = phone.get("/ui/", follow_redirects=False)
        assert entry.status_code == 307
        assert entry.headers["location"] == "/mobile/install"
        landing = phone.get("/mobile/install")
        assert landing.status_code == 200
        assert "Связь с компьютером есть" in landing.text
        assert "/api/mobile/app.apk" in landing.text
        download = phone.get("/api/mobile/app.apk", headers={"Origin": "null"})
        assert download.status_code == 200
        assert download.content == payload
        assert download.headers["content-type"].startswith(
            "application/vnd.android.package-archive"
        )
        assert download.headers["access-control-allow-origin"] == "null"
        assert download.headers["cache-control"] == "no-store, max-age=0"
        assert download.headers["content-disposition"].endswith('filename="EIRVEN-Mobile-1.9.4.apk"')
        assert int(download.headers["content-length"]) == len(payload)
        head = phone.head("/api/mobile/app.apk", headers={"Origin": "null"})
        assert head.status_code == 200
        assert head.content == b""
        assert int(head.headers["content-length"]) == len(payload)
        forbidden = phone.get("/api/preferences")
        assert forbidden.status_code == 403
        assert forbidden.headers["content-type"].startswith("text/plain; charset=utf-8")
        assert forbidden.text == "Этот раздел доступен только на компьютере"

    with TestClient(app, client=("8.8.8.8", 50_000)) as internet:
        assert internet.get("/api/mobile/app.apk").status_code == 403

    assert _mobile_bootstrap_path_allowed("/api/mobile/app.apk", "GET")
    assert _mobile_bootstrap_path_allowed("/api/mobile/app.apk", "HEAD")
    assert _mobile_bootstrap_path_allowed("/mobile/install", "GET")
    assert not _mobile_bootstrap_path_allowed("/api/mobile/config", "GET")


def test_music_then_telegram_is_a_two_node_cross_app_mission() -> None:
    phrase = "Эрви, включи музыку и открой тг"
    decision = ReliabilityRouter().classify(phrase)
    assert decision.kind == "mission"

    engine = MissionEngine.__new__(MissionEngine)
    assert engine.should_handle(phrase)
    nodes = engine._deterministic_plan(phrase)
    assert [(node.kind, node.app) for node in nodes] == [
        ("media", "yandex_music"),
        ("app", "telegram"),
    ]
    assert nodes[1].dependencies == ["n1"]


@pytest.mark.parametrize(
    "phrase",
    [
        "Вруби музыку и запусти телегу",
        "Поставь песню, а потом зайди в Telegram",
        "Запусти трек, затем открой тг",
        "Воспроизведи музыку и зайди в телеграм",
    ],
)
def test_music_then_telegram_natural_wording_variants(phrase: str) -> None:
    assert ReliabilityRouter().classify(phrase).kind == "mission"
    engine = MissionEngine.__new__(MissionEngine)
    assert engine.should_handle(phrase)
    nodes = engine._deterministic_plan(phrase)
    assert [(node.kind, node.app) for node in nodes] == [
        ("media", "yandex_music"),
        ("app", "telegram"),
    ]


class _WindowTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict):
        self.calls.append((name, dict(arguments)))
        if name == "foreground_window":
            return {"ok": True, "result": {"title": "EIRVEN", "handle": 5}}
        return {"ok": True}


def test_telegram_search_uses_resolved_window_and_accepts_scaled_top_field() -> None:
    operator = DesktopOperator.__new__(DesktopOperator)
    operator.tools = _WindowTools()
    search = {
        "visible": True,
        "enabled": True,
        "focused": True,
        "control_type": "Edit",
        "name": "Search",
        "automation_id": "telegram-search-input",
        "class_name": "input-search-input",
        "rectangle": [80, 109, 360, 145],
    }
    operator._elements = lambda *_args, **_kwargs: [search]
    clicked: list[dict] = []
    operator._click_input_rect = lambda element: clicked.append(element) or True
    operator._trace = lambda *_args, **_kwargs: None

    acquired = operator.acquire_input(
        purpose="search",
        aliases=["telegram-search-input", "search", "поиск"],
        trigger_aliases=["Search", "Поиск"],
        title_hint="Telegram Web",
        handle_hint=77,
    )

    assert acquired["ok"] is True
    assert acquired["title"] == "Telegram Web"
    assert acquired["handle"] == 77
    assert not operator._is_browser_chrome(search)
    assert not any(name == "foreground_window" for name, _args in operator.tools.calls)
    assert ("window_focus", {"handle": 77}) in operator.tools.calls
    assert clicked and clicked[0] is search


def test_telegram_platform_words_never_leak_into_message_text() -> None:
    cases = {
        "отправь Маше в телеграм сообщение буду через десять минут":
            (("Маше", "буду через десять минут"), ("маша", "telegram", "буду через десять минут")),
        "скинь Кириллу в телеге текст уже выхожу":
            (("Кириллу", "уже выхожу"), ("кирилл", "telegram", "уже выхожу")),
        "в Telegram напиши Анне доброе утро":
            (("Анне", "доброе утро"), ("анна", "telegram", "доброе утро")),
    }
    for phrase, (parsed, normalized) in cases.items():
        recipient, message = ChatService._parse_telegram_command(phrase)
        assert (recipient, message) == parsed
        assert ChatService._parse_send_target(
            f"{recipient} в telegram сообщение {message}"
        ) == normalized


def test_telegram_send_passes_known_window_to_search_acquisition() -> None:
    operator = DesktopOperator.__new__(DesktopOperator)
    operator.tools = _WindowTools()
    operator.wait_window = lambda *_args, **_kwargs: {
        "title": "Telegram Web", "handle": 77,
    }
    operator._elements = lambda *_args, **_kwargs: [{"name": "Search"}]
    operator._telegram_ready = lambda _rows: True
    captured: dict = {}

    def acquire(**kwargs):
        captured.update(kwargs)
        return {"ok": False}

    operator.acquire_input = acquire
    with pytest.raises(RuntimeError, match="поле поиска"):
        operator.telegram_send("Тима", "привет")
    assert captured["title_hint"] == "Telegram Web"
    assert captured["handle_hint"] == 77
    assert captured["visual_fallback"] is True


def test_english_words_are_words_while_real_acronyms_remain_acronyms() -> None:
    spoken = speech_ready_text("Music Search Chrome HELLOWORLD Go QR APK MP4 и 42%.")
    assert not re.search(r"[A-Za-z0-9]", spoken)
    for word in ("мьюзик", "сёч", "хроум", "хелловорлд", "гоу"):
        assert word in spoken
    assert "эйч и эл эл оу" not in spoken
    assert "кью ар" in spoken
    assert "эй пи кей" in spoken
    assert "эм пи четыре" in spoken
    assert "сорок два процента" in spoken


def test_stream_done_suffix_is_spoken_and_playback_has_flush_tail() -> None:
    class _Chat:
        @staticmethod
        def stream_events(*_args, **_kwargs):
            yield {"type": "start", "conversation_id": "voice-test"}
            yield {"type": "token", "content": "Привет", "full": "Привет"}
            yield {"type": "done", "answer": "Привет, мир"}

        @staticmethod
        def enforce_gender(text: str) -> str:
            return text

    daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
    daemon.services = SimpleNamespace(chat=_Chat())
    daemon._conversation_id = ""
    daemon._stop = threading.Event()
    daemon._generation_active = threading.Event()
    daemon._is_current_turn = lambda _turn: True
    spoken: list[str] = []
    daemon._speak = lambda text, _emotion, _turn: spoken.append(text)

    answer, conversation_id = daemon._stream_chat_to_voice("привет", "natural", 1)
    assert answer == "Привет, мир"
    assert conversation_id == "voice-test"
    assert spoken == ["Привет, мир"]
    assert NativeVoiceDaemon._synthesis_chunk("Последнее слово") == "Последнее слово."

    audio = np.ones((100, 1), dtype=np.float32)
    flushed = NativeVoiceDaemon._append_playback_tail(audio, 10_000, 1)
    assert flushed.shape == (1_500, 1)
    assert np.all(flushed[-1_400:] == 0)
