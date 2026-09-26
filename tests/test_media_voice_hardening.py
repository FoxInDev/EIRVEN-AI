from __future__ import annotations

import threading
import sys
import weakref
from pathlib import Path
from types import ModuleType, SimpleNamespace

from eirven_ai.tools import ToolExecutor
from eirven_ai.voice import VoiceService
from eirven_ai.voice_daemon import NativeVoiceDaemon
from eirven_ai.voice_worker import Recognizer


def _session(session_id: str, source: str, *, current: bool = False, title: str = "", artist: str = ""):
    return {
        "session_id": session_id,
        "source": source,
        "is_current": current,
        "title": title,
        "artist": artist,
        "album": "",
        "subtitle": "",
    }


def test_media_without_target_uses_windows_current_session_not_first() -> None:
    sessions = [
        _session("video", "browser.exe", title="Видео"),
        _session("music", "music.player", current=True, title="Песня"),
    ]
    selected, reason, _ranking = ToolExecutor._select_media_snapshot(sessions)
    assert selected == 1
    assert reason == "windows_current_session"


def test_media_target_selects_live_metadata_without_service_catalogue() -> None:
    sessions = [
        _session("video", "browser.exe", current=True, title="Новости"),
        _session("music", "unknown.future.player", title="Midnight", artist="Future Artist"),
    ]
    selected, reason, _ranking = ToolExecutor._select_media_snapshot(
        sessions, target="Future Artist",
    )
    assert selected == 1
    assert reason == "session_metadata_match"


def test_media_named_browser_window_fails_closed_when_tabs_share_source() -> None:
    sessions = [
        _session("one", "chrome.exe", current=True, title="Видео"),
        _session("two", "chrome.exe", title="Другая песня"),
    ]
    windows = [{"title": "Яндекс Музыка", "process": "chrome.exe", "foreground": True}]
    selected, reason, ranking = ToolExecutor._select_media_snapshot(
        sessions, target="Яндекс Музыка", windows=windows,
    )
    assert selected is None
    assert reason == "browser_window_not_session_identity"
    assert len(ranking) == 2


def test_media_named_browser_window_does_not_hijack_one_unrelated_session() -> None:
    sessions = [_session("one", "chrome.exe", current=True, title="Новости")]
    windows = [{"title": "Яндекс Музыка", "process": "chrome.exe", "foreground": True}]
    selected, reason, ranking = ToolExecutor._select_media_snapshot(
        sessions, target="Яндекс Музыка", windows=windows,
    )
    assert selected is None
    assert reason == "browser_window_not_session_identity"
    assert len(ranking) == 1


def test_media_explicit_session_id_is_exact_even_with_same_browser_source() -> None:
    sessions = [
        _session("one", "chrome.exe", current=True),
        _session("two", "chrome.exe"),
    ]
    selected, reason, _ranking = ToolExecutor._select_media_snapshot(
        sessions, session_id="two",
    )
    assert selected == 1
    assert reason == "explicit_session_id"


def test_media_snapshot_ids_do_not_depend_on_enumeration_order() -> None:
    first = _session("", "chrome.exe", title="One", artist="Artist A")
    second = _session("", "chrome.exe", title="Two", artist="Artist B")
    first_id = ToolExecutor._media_snapshot_session_id(first)
    second_id = ToolExecutor._media_snapshot_session_id(second)
    assert first_id != second_id
    original = {row["title"]: ToolExecutor._media_snapshot_session_id(row) for row in [first, second]}
    reordered = {row["title"]: ToolExecutor._media_snapshot_session_id(row) for row in [second, first]}
    assert original == reordered


def test_indistinguishable_media_snapshot_ids_fail_closed() -> None:
    first = _session("", "chrome.exe", title="Same")
    second = _session("", "chrome.exe", title="Same")
    shared = ToolExecutor._media_snapshot_session_id(first)
    first["session_id"] = shared
    second["session_id"] = ToolExecutor._media_snapshot_session_id(second)
    selected, reason, _ranking = ToolExecutor._select_media_snapshot(
        [first, second], session_id=shared,
    )
    assert selected is None
    assert reason == "ambiguous_session_id"


def test_media_schema_exposes_status_list_target_and_session_id() -> None:
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.settings = SimpleNamespace(enable_browser=False, enable_desktop_control=False)
    description = next(row for row in executor.descriptions() if row["name"] == "media_control")
    assert set(description["arguments"]) >= {"action", "target", "session_id"}
    assert "status|list" in description["arguments"]["action"]


def test_loopback_capture_endpoints_are_rejected() -> None:
    assert NativeVoiceDaemon._loopback_like_device("Stereo Mix (Realtek Audio)")
    assert NativeVoiceDaemon._loopback_like_device("Speakers (loopback)")
    assert not NativeVoiceDaemon._loopback_like_device("Microphone Array (Realtek Audio)")


def test_voice_playback_guard_uses_background_windows_media_sessions(tmp_path: Path) -> None:
    class Tools:
        @staticmethod
        def tool_media_control(**_kwargs):
            return {
                "sessions": [
                    {"source": "future-player", "state": "playing", "title": "Anything"},
                ]
            }

    daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
    daemon.services = SimpleNamespace(
        tools=Tools(),
        proactive=None,
        settings=SimpleNamespace(root_dir=tmp_path),
    )
    daemon._media_probe_at = 0.0
    daemon._media_playing_guard = False
    daemon._media_session_count = 0
    assert daemon._foreground_media_kind() == "media"
    assert daemon._media_playing_guard is True
    assert daemon._media_session_count == 1


def test_voice_worker_does_not_load_heavy_fallback_for_playback_noise() -> None:
    recognizer = Recognizer("gigaam", "unused", "unused")
    recognizer._gigaam_transcribe = lambda _path: "LOUD LATIN MEDIA NOISE"  # type: ignore[method-assign]
    recognizer._whisper_transcribe = lambda _path: (_ for _ in ()).throw(AssertionError("fallback loaded"))  # type: ignore[method-assign]
    text, engine, reason = recognizer.transcribe("unused.wav", allow_fallback=False)
    assert text == ""
    assert engine == "gigaam"
    assert "suspicious" in reason


def test_media_capture_gate_rejects_steady_playback_and_keeps_near_field_speech() -> None:
    assert not NativeVoiceDaemon._media_start_frame_likely(
        0.009, True, 0.004, 0.20,
    )
    assert not NativeVoiceDaemon._media_start_frame_likely(
        0.040, False, 0.004, 0.20,
    )
    assert NativeVoiceDaemon._media_start_frame_likely(
        0.060, True, 0.004, 0.20,
    )


def test_low_energy_asr_hallucination_is_dropped_before_llm() -> None:
    assert not NativeVoiceDaemon._owner_speech_gate(1.65, 0.00666, 0.0045, False)
    assert not NativeVoiceDaemon._owner_speech_gate(1.65, 0.009, 0.0045, True)
    assert NativeVoiceDaemon._owner_speech_gate(1.2, 0.018, 0.0045, False)
    # Long dictation is intentionally more tolerant than a short noise fragment.
    assert NativeVoiceDaemon._owner_speech_gate(4.0, 0.007, 0.0045, False)


def test_playback_evidence_is_latched_until_activation_policy() -> None:
    daemon = NativeVoiceDaemon.__new__(NativeVoiceDaemon)
    daemon._session_activated = False
    daemon._active_until = 0.0
    daemon._speaking = threading.Event()
    daemon._foreground_media_kind = lambda: ""
    daemon._extract_explicit_wake = lambda text: (False, text.casefold())
    daemon._extract_after_wake = lambda text: (False, text.casefold())
    daemon._normalize = lambda text: text.casefold()

    # The output may already be silent when ASR finishes. Evidence collected during
    # capture must still keep a video's sentence out of the command pipeline.
    route = daemon._activation_route(
        "Фраза из видео",
        speech_started_at=10.0,
        now=12.0,
        playback_seen=True,
    )
    assert route["action"] == "ignore"
    assert route["reason"] == "playback_requires_wake"


def test_output_meter_releases_owned_com_references_before_uninitialize(monkeypatch) -> None:
    events: list[str] = []
    references: dict[str, weakref.ReferenceType] = {}

    class MeterInterface:
        _iid_ = "meter-iid"

    class Meter:
        def __init__(self):
            references["meter"] = weakref.ref(self)

        @staticmethod
        def GetPeakValue() -> float:
            return 0.125

        def __del__(self):
            events.append("meter_released")

    class ActivatedInterface:
        def __init__(self):
            references["interface"] = weakref.ref(self)

        @staticmethod
        def QueryInterface(interface_type):
            assert interface_type is MeterInterface
            events.append("query_interface")
            return Meter()

        def __del__(self):
            events.append("interface_released")

    class NativeDevice:
        def __init__(self):
            references["native_device"] = weakref.ref(self)

        @staticmethod
        def Activate(iid, context, activation_params):
            assert (iid, context, activation_params) == ("meter-iid", 7, None)
            return ActivatedInterface()

        def __del__(self):
            events.append("native_device_released")

    class Device:
        def __init__(self):
            self._dev = NativeDevice()
            references["device"] = weakref.ref(self)

        def __del__(self):
            events.append("device_released")

    class AudioUtilities:
        @staticmethod
        def GetSpeakers():
            return Device()

    fake_comtypes = ModuleType("comtypes")
    fake_comtypes.CLSCTX_ALL = 7
    fake_comtypes.CoInitialize = lambda: events.append("initialized")

    def uninitialize() -> None:
        assert all(reference() is None for reference in references.values())
        events.append("uninitialized")

    fake_comtypes.CoUninitialize = uninitialize
    fake_pycaw = ModuleType("pycaw.pycaw")
    fake_pycaw.AudioUtilities = AudioUtilities
    fake_pycaw.IAudioMeterInformation = MeterInterface
    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", fake_pycaw)

    assert NativeVoiceDaemon._default_output_peaks(samples=2, interval=0.0) == (0.125, 0.125)
    assert events[0:2] == ["initialized", "query_interface"]
    assert events[-1] == "uninitialized"


def test_tts_prewarm_locks_only_local_baya(tmp_path: Path) -> None:
    model = tmp_path / "silero.pt"
    model.write_bytes(b"model")

    class Worker:
        calls: list[tuple[str, str]] = []

        def preload(self, path: str, *, engine: str, timeout: float):
            self.calls.append((path, engine))

    service = VoiceService.__new__(VoiceService)
    service.settings = SimpleNamespace(silero_model=str(model))
    service._tts_worker = Worker()
    service._voice_runtime = {"fallback_reason": ""}
    service._tts_ready = threading.Event()
    synth_calls: list[tuple[str, str | None]] = []
    service.synthesize = lambda text, **kwargs: synth_calls.append((text, kwargs.get("voice_key"))) or "probe.wav"  # type: ignore[method-assign]

    service._prewarm_tts()

    assert service._tts_worker.calls == [(str(model.resolve()), "silero")]
    assert service._voice_runtime["locked_tts_engine"] == "silero"
    assert service._voice_runtime["locked_tts_speaker"] == "baya"
    assert service._voice_runtime["locked_voice_key"] == "ervi_soft"
    assert synth_calls == [("Готова.", "ervi_soft")]
    assert service._tts_ready.is_set()
