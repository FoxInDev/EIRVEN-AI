# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import io
import threading
import wave
import builtins
import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from eirven_ai.identity import VOICE_MODES, IdentityService
from eirven_ai.tts_worker import _breath_samples, _silero_phrase_segments, _silero_synthesize, _tempo_wav
from eirven_ai.voice import VoiceService


def _wav_bytes(frames: int = 640, rate: int = 16_000) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames)
    return target.getvalue()


def test_explicit_live_emotion_is_not_overwritten_by_reply_keywords() -> None:
    service = VoiceService.__new__(VoiceService)
    service.identity = None

    assert service._resolve_mode("Ура, всё получилось!", "warm", None) == "warm"
    assert service._resolve_mode("Ура, всё получилось!", None, "auto") == "energetic"


def test_baya_delivery_profiles_have_bounded_emotion_controls() -> None:
    assert set(VOICE_MODES) >= {"natural", "warm", "calm", "energetic", "sad", "tired"}
    ratios = [float(profile["pitch_ratio"]) for profile in VOICE_MODES.values()]
    gains = [float(profile["energy_gain"]) for profile in VOICE_MODES.values()]
    assert max(ratios) - min(ratios) >= 0.025
    assert min(ratios) >= 0.965 and max(ratios) <= 1.035
    assert min(gains) >= 0.82 and max(gains) <= 1.08


def test_text_cues_make_voice_greeting_question_and_failure_conversational() -> None:
    assert IdentityService.infer_emotion("Привет, я снова здесь") == "warm"
    assert IdentityService.infer_emotion("Почему это не удалось?") == "concerned"
    assert IdentityService.infer_emotion("Как мне это сделать?") == "curious"


def test_optional_native_import_failure_cannot_break_voice_health(monkeypatch) -> None:
    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "optional_native_dll":
            raise OSError("DLL load failed while importing optional module")
        if name == "optional_runtime":
            raise RuntimeError("optional backend initialization failed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)

    assert VoiceService._module_exists("optional_native_dll") is False
    assert VoiceService._module_exists("optional_runtime") is False


def test_baya_phrase_plan_changes_cadence_without_audio_resampling() -> None:
    text = "Я всё внимательно проверила. Теперь можно спокойно продолжать."
    calm = _silero_phrase_segments(
        text,
        {"mode": "calm", "speech_speed": 0.92, "breath": 0.04},
    )
    lively = _silero_phrase_segments(
        text,
        {"mode": "energetic", "speech_speed": 1.05, "breath": 0.0},
    )

    assert [phrase for phrase, _pause in calm] == [phrase for phrase, _pause in lively]
    assert calm[0][1] > lively[0][1] > 0
    assert calm[-1][1] == lively[-1][1] == 0


def test_silero_prosody_keeps_canonical_baya_and_inserts_real_silence() -> None:
    class Model:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def apply_tts(self, **kwargs):
            self.calls.append(kwargs)
            return np.full(480, 0.04, dtype=np.float32)

    model = Model()
    data = _silero_synthesize(
        model,
        "Первая фраза. Вторая фраза.",
        {"mode": "empathetic", "speech_speed": 0.92, "breath": 0.04},
        speaker="aidar",
        sample_rate=48_000,
    )

    assert data.startswith(b"RIFF")
    assert len(model.calls) == 2
    assert {call["speaker"] for call in model.calls} == {"baya"}
    with wave.open(io.BytesIO(data), "rb") as wav:
        # Two 10 ms neural phrases plus a semantic pause must be longer than raw audio.
        assert wav.getframerate() == 48_000
        assert wav.getnframes() > 960


def test_baya_emotions_have_audibly_different_tempo_and_cadence() -> None:
    class Model:
        @staticmethod
        def apply_tts(**_kwargs):
            phase = np.linspace(0.0, 42.0, 4_800, dtype=np.float32)
            return np.sin(phase) * np.float32(0.06)

    text = "Я всё проверила. Теперь можно спокойно продолжать."
    lively = _silero_synthesize(
        Model(), text,
        {"mode": "energetic", "speech_speed": 1.05, "breath": 0.008},
        sample_rate=48_000,
    )
    tired = _silero_synthesize(
        Model(), text,
        {"mode": "tired", "speech_speed": 0.88, "breath": 0.075},
        sample_rate=48_000,
    )
    with wave.open(io.BytesIO(lively), "rb") as lively_wav, wave.open(io.BytesIO(tired), "rb") as tired_wav:
        assert tired_wav.getnframes() > lively_wav.getnframes() * 1.35


def test_baya_breath_is_audio_only_with_soft_zero_edges() -> None:
    breath = _breath_samples(48_000, 5_760, 0.006, seed=42)
    assert breath.dtype == np.float32
    assert np.isfinite(breath).all()
    assert float(np.max(np.abs(breath))) > 0.001
    assert abs(float(breath[0])) < 1e-7
    assert abs(float(breath[-1])) < 1e-5


def test_baya_pitch_transform_preserves_wav_rate_and_duration() -> None:
    rate = 48_000
    phase = np.linspace(0.0, 2.0 * np.pi * 220.0 * 0.35, int(rate * 0.35), endpoint=False)
    source = io.BytesIO()
    import soundfile as sf

    sf.write(source, np.sin(phase).astype(np.float32), rate, format="WAV", subtype="PCM_16")
    transformed = _tempo_wav(source.getvalue(), 1.0, 1.02)
    with wave.open(io.BytesIO(transformed), "rb") as wav:
        assert wav.getframerate() == rate
        assert abs(wav.getnframes() / rate - 0.35) < 0.04


def test_identical_utterances_are_synthesized_fresh_not_returned_from_wav_cache(tmp_path: Path) -> None:
    model = tmp_path / "silero.pt"
    model.write_bytes(b"model")

    class Worker:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def synthesize(self, text, model_path, profile, **kwargs):
            self.calls.append({
                "text": text,
                "model_path": model_path,
                "profile": dict(profile),
                **kwargs,
            })
            return _wav_bytes()

    worker = Worker()
    service = VoiceService.__new__(VoiceService)
    service.settings = SimpleNamespace(
        data_dir=tmp_path,
        silero_model=str(model),
        tts_engine="silero",
    )
    service.db = SimpleNamespace(get_setting=lambda _key, default: default)
    service.identity = None
    service._tts_worker = worker
    service._tts_ready = threading.Event()
    service._tts_ready.set()
    service._synthesis_active = threading.Event()
    service._voice_runtime = {
        "locked_tts_engine": "silero",
        "locked_tts_model": str(model),
        "locked_tts_speaker": "baya",
        "locked_voice_key": "ervi_soft",
    }

    first = Path(service.synthesize("Рада тебя слышать.", mode="warm"))
    second = Path(service.synthesize("Рада тебя слышать.", mode="warm"))

    assert first != second
    assert first.is_file() and second.is_file()
    assert len(worker.calls) == 2
    assert {call["speaker"] for call in worker.calls} == {"baya"}
    assert {call["engine"] for call in worker.calls} == {"silero"}
    assert service._voice_runtime["last_tts_engine"] == "silero:baya:fresh"
    assert not any(path.name.startswith("cache-") for path in tmp_path.rglob("*"))


def test_ui_uses_pinned_open_color_emoji_instead_of_os_native_stack() -> None:
    root = Path(__file__).resolve().parents[1]
    web = root / "src" / "eirven_ai" / "web"
    styles = (web / "eirven-ui.css").read_text(encoding="utf-8")
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    packaging = (root / "pyproject.toml").read_text(encoding="utf-8")
    font = web / "NotoColorEmoji-v2.051.ttf"
    license_text = (web / "NotoColorEmoji-OFL.txt").read_text(encoding="utf-8")

    assert 'font-family:"Eirven Color Emoji"' in styles
    local_source = 'url("/ui/NotoColorEmoji-v2.051.ttf")'
    assert local_source in styles
    assert "googlefonts/noto-emoji" not in styles
    assert 'Inter,"Eirven Color Emoji","Noto Color Emoji"' in styles
    assert '"Segoe UI Emoji"' not in styles
    assert '"Apple Color Emoji"' not in styles
    assert font.stat().st_size == 10_673_480
    font_bytes = font.read_bytes()
    assert b"CBDT" in font_bytes[:4096] and b"CBLC" in font_bytes[:4096]
    assert hashlib.sha256(font_bytes).hexdigest() == (
        "72a635cb3d2f3524c51620cdde406b217204e8a6a06c6a096ff8ed4b5fd6e27b"
    )
    assert "SIL Open Font License 1.1" in notices
    assert "not Apple Color Emoji" in notices
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
    assert "Copyright 2013 Google LLC" in license_text
    assert 'eirven_ai = ["web/*", "web/katex-fonts/*"]' in packaging
