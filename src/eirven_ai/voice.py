# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database
from .identity import IdentityService, VOICE_CATALOG, VOICE_MODES
from .russian_speech import speech_ready_text
from .voice_worker_client import VoiceWorkerClient, VoiceWorkerError
from .tts_worker_client import TTSWorkerClient, TTSWorkerError


class VoiceError(RuntimeError):
    pass


class VoiceService:
    def __init__(
        self,
        settings: Settings,
        db: Database | None = None,
        identity: IdentityService | None = None,
    ):
        self.settings = settings
        self.db = db
        self.identity = identity
        self._voice_runtime = {"fallback_reason": ""}
        # STT runs in an isolated CPU process. GigaAM is primary for Russian;
        # faster-whisper stays as a CPU fallback. Native ASR failures cannot kill FastAPI.
        self._stt_worker = VoiceWorkerClient(
            settings.whisper_model, settings.root_dir,
            engine=settings.asr_engine, gigaam_model=settings.gigaam_model,
        )
        # TTS runs in a separate process as well. A native ONNX/TTS failure must never
        # terminate FastAPI or make the desktop companion disappear.
        self._tts_worker = TTSWorkerClient(settings.root_dir)
        self._stt_ready = threading.Event()
        self._tts_ready = threading.Event()
        self._synthesis_active = threading.Event()
        # r22: ASR remains the only hard prerequisite for *listening*, but the selected
        # local TTS weights are loaded silently immediately after ASR becomes ready.  This
        # is a model load only (no dummy speech is synthesized or played).  Live traces
        # showed the first 15-character reply spending >6 s loading Silero while the action
        # model was warming in parallel; prioritising the voice worker removes that cold
        # penalty from normal interaction without delaying microphone acceptance.
        # r22 final startup: load ASR and the small local TTS weights in parallel.
        # Expressive engines are deliberately held back until both are ready. In live logs
        # the owner spoke ~6 s after launch; sequential ASR->TTS loading made that first
        # reply wait another 6+ s.  Parallel voice-only loading makes the first accepted
        # turn useful sooner without synthesizing or playing any dummy phrase.
        threading.Thread(target=self._prewarm_stt, daemon=True, name="eirven-asr-prewarm").start()
        threading.Thread(target=self._prewarm_tts, daemon=True, name="eirven-tts-preload").start()

    def _prewarm_stt(self) -> None:
        started = time.monotonic()
        # This used to run once and set the ready flag only on complete success. Any
        # single failure (device busy, model file still locked, timeout under load)
        # left stt_ready() False for the whole session -- and because voice_daemon
        # discards every captured block until that flag is set, the microphone stayed
        # open while hearing nothing, with no error shown anywhere in the UI. Recovery
        # required restarting the whole app. Retry instead, with growing gaps.
        delays = (0.0, 5.0, 15.0, 30.0, 60.0)
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                # Loading weights is not enough for ONNX/GigaAM: the first actual graph
                # run in the attached trace still took 21.1 seconds. Pay that cost on a
                # harmless silent WAV before the microphone is considered interactive.
                self._stt_worker.warmup(timeout=180)
                self._stt_worker.prime_primary(self._probe_wav(), timeout=90)
                self._voice_runtime["stt_inference_primed"] = True
                self._voice_runtime["stt_prewarm_attempts"] = attempt + 1
                self._voice_runtime.pop("stt_prewarm_error", None)
                self._voice_runtime["stt_prewarm_ms"] = round((time.monotonic() - started) * 1000)
                self._stt_ready.set()
                return
            except Exception as exc:
                self._voice_runtime["stt_prewarm_error"] = str(exc)[:300]
                self._voice_runtime["stt_prewarm_attempts"] = attempt + 1
        self._voice_runtime["stt_prewarm_ms"] = round((time.monotonic() - started) * 1000)

    def stt_ready(self) -> bool:
        """True only after the isolated Russian ASR model finished its cold load."""
        return self._stt_ready.is_set()

    def tts_warm_ready(self) -> bool:
        return self._tts_ready.is_set()

    def interactive_ready(self) -> bool:
        """True when a fresh microphone phrase can be recognized immediately.

        TTS warms in parallel and must not keep the UI in ``запускаюсь`` after ASR has
        completed a real inference pass.  Synthesis still waits for the one selected
        local Baya worker when a reply is ready.
        """
        return self._stt_ready.is_set()

    def wait_until_ready(self, timeout: float = 180.0) -> bool:
        return self._stt_ready.wait(max(0.0, float(timeout)))

    @staticmethod
    def _probe_wav(seconds: float = 0.42, sample_rate: int = 16_000) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\x00\x00" * max(1, int(seconds * sample_rate)))
        return out.getvalue()

    def _prewarm_tts(self) -> None:
        """Load and inference-probe the one canonical local Baya voice."""
        started = time.monotonic()
        errors: list[str] = []
        # A single failure used to lock the engine to "unavailable" for the whole
        # session: she kept answering in text while every spoken reply raised
        # "Локальный голос Бая не загрузился", with recovery only via restart. The
        # first attempt also has to cover a cold torch import plus a 138 MB package
        # load while the LLM prewarm competes for the same disk and CPU, which does
        # not reliably fit in 30 seconds.
        attempts = ((0.0, 90), (6.0, 120), (20.0, 150), (45.0, 180))
        for index, (delay, timeout) in enumerate(attempts):
            if delay:
                time.sleep(delay)
            try:
                silero_path = Path(self.settings.silero_model).expanduser().resolve()
                if not silero_path.is_file() and index == 0:
                    # The model has disappeared more than once on a working install
                    # (antivirus quarantine and folder cleanups are the usual causes).
                    # It has a known source and size, so restore it instead of leaving
                    # the assistant mute until someone runs a command by hand.
                    try:
                        self._voice_runtime["tts_prewarm_error"] = "Файл голоса пропал — восстанавливаю…"
                        silero_path.parent.mkdir(parents=True, exist_ok=True)
                        import torch  # local import: heavy, and only needed here

                        torch.hub.download_url_to_file(
                            "https://models.silero.ai/models/tts/ru/v5_5_ru.pt",
                            str(silero_path), progress=False,
                        )
                    except Exception as exc:
                        self._voice_runtime["tts_prewarm_error"] = (
                            f"Файл голоса не найден: {silero_path}. "
                            f"Автовосстановление не удалось: {str(exc)[:200]}"
                        )
                if not silero_path.is_file():
                    # A missing file is not a transient condition: retrying cannot
                    # create it, and the generic failure message hid the one thing
                    # that actually needed doing. Stop immediately and say where.
                    self._voice_runtime["locked_tts_engine"] = "unavailable"
                    self._voice_runtime.setdefault(
                        "tts_prewarm_error",
                        f"Файл голоса не найден: {silero_path}.",
                    )
                    self._voice_runtime["tts_prewarm_ms"] = round((time.monotonic() - started) * 1000)
                    self._tts_ready.set()
                    return
                self._tts_worker.preload(str(silero_path), engine="silero", timeout=timeout)
                self._voice_runtime.update({
                    "locked_tts_engine": "silero",
                    "locked_tts_model": str(silero_path),
                    "locked_tts_speaker": "baya",
                    "locked_voice_key": "ervi_soft",
                    "prewarmed_tts_engine": "silero:baya",
                })
                # One very short real inference pays the phonemisation/kernel cold cost.
                # Warming several full phrases delayed readiness and could queue ahead of the
                # owner's first reply; one probe is sufficient for the resident Baya model.
                self.synthesize("Готова.", mode="natural", voice_key="ervi_soft")
                self._voice_runtime["tts_inference_primed"] = True
                self._voice_runtime["tts_prewarm_attempts"] = index + 1
                self._voice_runtime.pop("tts_prewarm_error", None)
                self._voice_runtime["tts_prewarm_ms"] = round((time.monotonic() - started) * 1000)
                self._tts_ready.set()
                return
            except Exception as exc:
                errors.append(f"попытка {index + 1}: {exc}")
                self._voice_runtime["tts_prewarm_attempts"] = index + 1
                # Never replace Baya by an unrelated system/network voice after failure.
                self._voice_runtime["locked_tts_engine"] = "unavailable"
                # Let the first reply fall back to text immediately rather than
                # blocking on a 30s wait inside synthesize while retries continue.
                self._tts_ready.set()
        self._voice_runtime["tts_prewarm_ms"] = round((time.monotonic() - started) * 1000)
        if errors:
            self._voice_runtime["tts_prewarm_error"] = " | ".join(errors)[:500]
        self._tts_ready.set()

    @staticmethod
    def _valid_piper_model(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size < 10_000_000:
                return False
            head = path.read_bytes()[:256].lstrip().lower()
            if head.startswith(b"version https://git-lfs") or head.startswith(b"<html") or b"<!doctype html" in head:
                return False
            config = Path(str(path) + ".json")
            if not config.is_file() or config.stat().st_size < 500:
                return False
            import json
            payload = json.loads(config.read_text(encoding="utf-8-sig"))
            return isinstance(payload, dict) and bool(payload.get("audio")) and bool(payload.get("language"))
        except Exception:
            return False

    def _voice_models(self) -> dict[str, str]:
        models: dict[str, str] = {}
        # Discover the bundled Russian Piper voices by filename. Multiple presets may
        # deliberately share one physical speaker model (for example Irina soft/lively).
        voice_dir = self.settings.root_dir / "models" / "piper"
        for key, item in VOICE_CATALOG.items():
            path = voice_dir / str(item.get("model") or "")
            if self._valid_piper_model(path):
                models[key] = str(path)
        if self.settings.piper_model:
            legacy_path = Path(self.settings.piper_model).expanduser()
            if self._valid_piper_model(legacy_path) or (legacy_path.is_file() and not self._module_exists("piper_onnx")):
                legacy = str(legacy_path)
                models.setdefault("denis", legacy)
                models.setdefault("default", legacy)
        if self.db:
            raw = self.db.get_setting("voice_models", {})
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(key, str) and isinstance(value, str) and value.strip():
                        models[key[:40]] = value.strip()
        identity = self.identity.get() if self.identity else None
        if identity and identity.voice_key in models:
            models["default"] = models[identity.voice_key]
        elif "irina" in models:
            models["default"] = models["irina"]
        elif "denis" in models:
            models["default"] = models["denis"]
        return models

    def status(self) -> dict[str, Any]:
        available: dict[str, str] = {}
        identity = self.identity.get() if self.identity else None
        gigaam_ready = self._module_exists("onnx_asr")
        whisper_ready = self._module_exists("faster_whisper")
        piper_model = Path(self.settings.piper_model).expanduser() if self.settings.piper_model else None
        piper_config = Path(str(piper_model) + ".json") if piper_model else None
        piper_ready = bool(
            self._module_exists("piper_onnx") and piper_model
            and self._valid_piper_model(piper_model)
            and piper_config and piper_config.is_file()
        )
        expressive_ready = False
        chatterbox_ready = self._module_exists("chatterbox")
        edge_tts_ready = False
        silero_path = Path(self.settings.silero_model).expanduser() if self.settings.silero_model else None
        silero_ready = bool(silero_path and silero_path.is_file() and silero_path.stat().st_size > 1_000_000)
        baya_ready = bool(
            silero_ready
            and self._tts_ready.is_set()
            and self._voice_runtime.get("locked_tts_engine") == "silero"
            and self._voice_runtime.get("locked_tts_speaker") == "baya"
        )
        selected_engine = str(self._voice_runtime.get("locked_tts_engine") or "")
        selected_ready = bool(
            self._tts_ready.is_set()
            and self._voice_runtime.get("tts_inference_primed")
            and selected_engine == "silero"
            and self._voice_runtime.get("locked_tts_speaker") == "baya"
        )
        if silero_ready:
            available["ervi_soft"] = "silero:baya"
        stt_status = self._stt_worker.status()
        return {
            "stt_ready": gigaam_ready or whisper_ready,
            "stt_warm_ready": self._stt_ready.is_set(),
            "tts_warm_ready": self._tts_ready.is_set(),
            "interactive_ready": self.interactive_ready(),
            "stt_primary_ready": gigaam_ready,
            "stt_fallback_ready": whisper_ready,
            # Public readiness follows the engine that was actually inference-probed.
            # The offline Baya flag remains separate so degraded operation is visible.
            "tts_ready": selected_ready,
            "tts_engine": selected_engine if selected_ready else "none",
            "tts_prewarm_error": str(self._voice_runtime.get("tts_prewarm_error") or ""),
            "tts_prewarm_attempts": int(self._voice_runtime.get("tts_prewarm_attempts") or 0),
            "tts_prewarm_ms": int(self._voice_runtime.get("tts_prewarm_ms") or 0),
            "chatterbox_ready": chatterbox_ready,
            "edge_tts_ready": edge_tts_ready,
            "silero_ready": silero_ready,
            "silero_model": str(silero_path or ""),
            "piper_ready": piper_ready,
            "expressive_tts_ready": expressive_ready,
            "expressive_design_ready": expressive_ready,
            "expressive_tts_design_model": self.settings.expressive_tts_design_model,
            "asr_engine": self.settings.asr_engine,
            "gigaam_model": self.settings.gigaam_model,
            "whisper_model": self.settings.whisper_model,
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "whisper_fallback_reason": stt_status.get("fallback_reason") or self._voice_runtime.get("fallback_reason") or "",
            "stt_process": stt_status,
            "tts_process": self._tts_worker.status(),
            "last_tts_engine": self._voice_runtime.get("last_tts_engine", ""),
            "locked_tts_engine": self._voice_runtime.get("locked_tts_engine", ""),
            "locked_tts_speaker": self._voice_runtime.get("locked_tts_speaker", ""),
            "locked_voice_key": self._voice_runtime.get("locked_voice_key", ""),
            "alternate_tts_enabled": False,
            "stt_prewarm_ms": self._voice_runtime.get("stt_prewarm_ms"),
            "tts_prewarm_ms": self._voice_runtime.get("tts_prewarm_ms"),
            "synthesis_active": self._synthesis_active.is_set(),
            "piper_model": self.settings.piper_model,
            "voices": available,
            "voice_modes": VOICE_MODES,
            "selected_mode": identity.voice_mode if identity else "natural",
            "emotion_mode": identity.emotion_mode if identity else "auto",
            "silence_ms": self.settings.voice_silence_ms,
        }

    def cancel_synthesis(self) -> bool:
        """Pre-empt only stale TTS inference; text generation has its own stop token."""
        if not self._synthesis_active.is_set():
            return False
        interrupted = self._tts_worker.interrupt()
        if interrupted:
            self._voice_runtime["last_tts_cancelled_at"] = time.time()
        return interrupted

    @staticmethod
    def _module_exists(name: str) -> bool:
        """Probe an optional native module without making health/status fragile.

        A package can be discoverable while importing its DLL fails (missing runtime,
        incompatible CUDA/ONNX build, or a locked binary).  Availability probes must
        report ``False`` for all ordinary import failures; they must never turn
        ``/api/health`` into HTTP 500.  Process-control exceptions still propagate.
        """
        try:
            __import__(name)
            return True
        except Exception:
            return False

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def _resolve_voice(self, voice_key: str | None = None) -> tuple[str, Path]:
        models = self._voice_models()
        identity = self.identity.get() if self.identity else None
        requested = voice_key or (identity.voice_key if identity else "")
        selected = requested if requested in models else "default"
        if selected not in models:
            raise VoiceError("Русский локальный голос ещё не скачан. Запустите восстановление компонентов.")
        path = Path(models[selected]).expanduser().resolve()
        if not self._valid_piper_model(path) and self._module_exists("piper_onnx"):
            raise VoiceError(f"Резервный голос повреждён или скачан не полностью: {path}")
        config = Path(str(path) + ".json")
        # Tests and externally supplied voices may inject a worker directly; the actual
        # piper-onnx worker will validate its JSON config on load. Keep path resolution
        # independent so synthesis errors remain isolated in the worker process.
        if not config.is_file() and self._module_exists("piper_onnx"):
            raise VoiceError(f"Конфигурация голоса не найдена: {config}")
        return selected, path

    def transcribe(self, audio_path: str | None) -> str:
        if not audio_path:
            return ""
        try:
            return self._stt_worker.transcribe(str(audio_path))
        except VoiceWorkerError as exc:
            self._voice_runtime["fallback_reason"] = str(exc)[:300]
            raise VoiceError(f"Не удалось распознать речь: {exc}") from exc

    def transcribe_bytes(self, data: bytes, suffix: str = ".wav", *, allow_fallback: bool = True) -> str:
        if not data:
            return ""
        try:
            return self._stt_worker.transcribe_bytes(data, suffix, allow_fallback=allow_fallback)
        except VoiceWorkerError as exc:
            self._voice_runtime["fallback_reason"] = str(exc)[:300]
            raise VoiceError(f"Не удалось распознать речь: {exc}") from exc

    def _resolve_mode(self, text: str, mode: str | None, emotion: str | None) -> str:
        identity = self.identity.get() if self.identity else None
        # A caller such as the live voice daemon has already combined the owner's vocal
        # affect with the meaning of the reply.  Preserve that explicit direction instead
        # of letting a keyword in the reply overwrite it a second time.
        if emotion in VOICE_MODES:
            return str(emotion)
        if mode in VOICE_MODES:
            return str(mode)
        selected = identity.voice_mode if identity else "natural"
        emotion_mode = emotion or (identity.emotion_mode if identity else "auto")
        if emotion_mode == "auto":
            inferred = IdentityService.infer_emotion(text)
            if inferred != "natural":
                selected = inferred
        elif emotion_mode in VOICE_MODES:
            selected = emotion_mode
        return selected if selected in VOICE_MODES else "natural"

    @staticmethod
    def _prepare_text(text: str, mode: str) -> str:
        clean = " ".join(speech_ready_text(text).split())
        if mode in {"energetic", "amused", "proud"}:
            clean = clean.replace("…", ".").replace("...", ".")
        if mode == "strict":
            clean = clean.replace("!", ".")
        if mode in {"sad", "empathetic", "tired"}:
            # Semicolons and em dashes become explicit phrase boundaries in Silero's
            # neural punctuation model.  The worker turns those boundaries into short
            # clean silences; no synthetic inhale sample or spoken markup is injected.
            clean = clean.replace(" — ", "; ")
        return clean

    @staticmethod
    def _emotion_instruction(mode: str) -> str:
        mapping = {
            "natural": "Speak natural conversational Russian, warm and human, with subtle pauses.",
            "warm": "Speak warm friendly Russian with gentle emotion and natural pauses.",
            "calm": "Speak calm relaxed Russian, slightly slower, with soft natural breathing.",
            "quiet": "Speak quietly and intimately in Russian, with restrained emotion.",
            "energetic": "Speak energetic confident Russian, lively but not theatrical.",
            "strict": "Speak concise serious Russian with controlled firm intonation.",
            "amused": "Speak natural Russian with an audible subtle smile and playful timing; never overact.",
            "sad": "Speak softly in Russian with subdued, sincere sadness and slower pauses.",
            "empathetic": "Speak supportive, attentive Russian with warmth, gentle pauses and no artificial cheerfulness.",
            "curious": "Speak curious conversational Russian with light questioning intonation.",
            "concerned": "Speak focused, caring Russian with restrained concern and clear diction.",
            "proud": "Speak warm confident Russian with quiet pride and a subtle smile.",
            "tired": "Speak slightly tired, soft Russian with unhurried natural pauses.",
        }
        return mapping.get(mode, mapping["natural"])

    @staticmethod
    def _postprocess_wav(path: Path, volume: float, breath: float = 0.0, pitch: float = 1.0) -> None:
        """Apply only safe gain. Prosody/pitch belong to the neural TTS itself.

        r3 resampled raw PCM to fake pitch changes; that produced the stuttered/warped
        sound users heard. Never time-stretch generated speech here.
        """
        if abs(float(volume) - 1.0) < 0.001:
            return
        try:
            import numpy as np
            import soundfile as sf
            data, rate = sf.read(str(path), dtype="float32", always_2d=False)
            data = np.clip(data * float(volume), -1.0, 1.0)
            sf.write(str(path), data, rate, subtype="PCM_16")
        except Exception:
            return

    def close(self) -> None:
        self._stt_worker.close()
        self._tts_worker.close()

    @staticmethod
    def _automatic_speech_speed(text: str, mode: str) -> float:
        """Choose conversational tempo from meaning instead of a user slider."""
        base = {
            "natural": 0.98,
            "warm": 0.96,
            "calm": 0.92,
            "quiet": 0.94,
            "energetic": 1.05,
            "strict": 1.00,
            "amused": 1.04,
            "sad": 0.89,
            "empathetic": 0.92,
            "curious": 0.99,
            "concerned": 0.94,
            "proud": 1.00,
            "tired": 0.88,
        }.get(mode, 0.98)
        clean = str(text or "").strip()
        words = len(clean.split())
        if words > 42:
            base += 0.025
        elif words <= 5:
            base -= 0.015
        if clean.endswith("?"):
            base -= 0.012
        if "…" in clean or "..." in clean:
            base -= 0.02
        return max(0.88, min(base, 1.08))

    def _store_tts_audio(self, data: bytes, output: Path, volume: float) -> str:
        if len(data) < 44 or not data.startswith(b"RIFF"):
            raise VoiceError("Локальный голос создал повреждённое аудио")
        output.write_bytes(data)
        self._postprocess_wav(output, float(volume))
        return str(output)

    def synthesize(
        self,
        text: str,
        *,
        mode: str | None = None,
        emotion: str | None = None,
        voice_key: str | None = None,
    ) -> str:
        text = text.strip()
        if not text:
            raise VoiceError("Пустой текст для озвучивания")
        selected_mode = self._resolve_mode(text, mode, emotion)
        profile = dict(VOICE_MODES[selected_mode])
        # The worker needs the semantic mode explicitly; VOICE_MODES stores only the
        # numeric controls. Without this field every locked Edge utterance falls back
        # to the same neutral prosody.
        profile["mode"] = selected_mode
        identity = self.identity.get() if self.identity else None
        # r21: speed and emotion are automatic. A manual slider made the voice sound
        # uniformly accelerated/robotic; each utterance now gets a small semantic tempo.
        speed = self._automatic_speech_speed(text, selected_mode)
        profile["speech_speed"] = speed
        profile["length_scale"] = max(0.76, float(profile.get("length_scale", 1.0)) / speed)
        try:
            output_volume = float(self.db.get_setting("voice_output_volume", 0.82)) if self.db is not None else 0.82
        except Exception:
            output_volume = 0.82
        # Keep the owner's volume preference as the ceiling while allowing the
        # emotional profile to breathe: energetic/proud delivery is a little more
        # present, while intimate/sad delivery is softer.  This is deliberately a
        # small gain change, not loudness normalisation that would erase emotion.
        profile["energy_gain"] = max(0.82, min(float(profile.get("energy_gain", 1.0)), 1.08))
        profile["volume"] = (
            max(0.0, min(output_volume, 1.0))
            * float(profile.get("volume", 1.0))
            * profile["energy_gain"]
        )
        profile["pitch_ratio"] = max(0.965, min(float(profile.get("pitch_ratio", 1.0)), 1.035))
        # Silero is deterministic by design.  A tiny per-utterance seed keeps pauses
        # and the pitch contour from being byte-identical on every repeated greeting,
        # while the bounded jitter remains far below a semitone and never changes Baya's
        # identity.  No spoken text or response is cached.
        prosody_seed = (time.time_ns() ^ id(profile)) & 0xFFFFFFFF
        profile["prosody_seed"] = prosody_seed
        profile["pitch_ratio"] = max(
            0.965,
            min(1.035, float(profile["pitch_ratio"]) * (1.0 + ((prosody_seed % 17) - 8) * 0.00018)),
        )
        # Public EIRVEN has one stable voice identity. Legacy/API voice keys are ignored
        # instead of silently selecting a different speaker.
        requested_voice = "ervi_soft"
        voice_preset = VOICE_CATALOG["ervi_soft"]
        for key in ("length_scale", "noise_scale", "noise_w"):
            if key in voice_preset:
                # Voice identity shapes the base timbre while the selected emotion mode
                # still controls tempo/prosody around it.
                profile[key] = float(profile.get(key, 1.0)) * float(voice_preset[key])
        prepared = self._prepare_text(text, selected_mode)
        output_dir = self.settings.data_dir / "audio"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"reply-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.wav"
        locked_engine = str(self._voice_runtime.get("locked_tts_engine") or "")
        # A direct preview can arrive while the preload thread is still selecting the
        # resident model. Wait for the canonical voice lock before synthesizing.
        if not locked_engine and not self._tts_ready.is_set():
            self._tts_ready.wait(30.0)
            locked_engine = str(self._voice_runtime.get("locked_tts_engine") or "")
        if locked_engine != "silero":
            raise VoiceError("Локальный голос Бая не загрузился; другой голос автоматически не включаю")
        locked_model = str(self._voice_runtime.get("locked_tts_model") or "")
        locked_speaker = str(self._voice_runtime.get("locked_tts_speaker") or "")
        if locked_speaker != "baya":
            raise VoiceError("Нарушена фиксация голоса: разрешена только Бая")
        model_path = str(self._voice_runtime.get("locked_tts_model") or self.settings.silero_model)
        self._synthesis_active.set()
        try:
            data = self._tts_worker.synthesize(
                prepared, model_path, profile, engine="silero", speaker="baya", timeout=15.0,
            )
        except (TTSWorkerError, OSError) as exc:
            raise VoiceError(f"Голос Бая временно недоступен: {exc}") from exc
        finally:
            self._synthesis_active.clear()
        # Utterance WAVs are intentionally fresh.  Reusing a cached greeting made every
        # delivery identical and could preserve stale prosody after an emotion change.
        # Only the neural model weights remain resident in the isolated worker.
        self._voice_runtime["last_tts_engine"] = "silero:baya:fresh"
        return self._store_tts_audio(data or b"", output, float(profile["volume"]))
