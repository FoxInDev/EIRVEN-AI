from __future__ import annotations

import hashlib
import io
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
        self._tts_cache_lock = threading.RLock()
        # r22: ASR remains the only hard prerequisite for *listening*, but the selected
        # local TTS weights are loaded silently immediately after ASR becomes ready.  This
        # is a model load only (no dummy speech is synthesized or played).  Live traces
        # showed the first 15-character reply spending >6 s loading Silero while the action
        # model was warming in parallel; prioritising the voice worker removes that cold
        # penalty from normal interaction without delaying microphone acceptance.
        # r22 final startup: load ASR and the small local TTS weights in parallel.
        # Qwen is deliberately held back at app level until both are ready.  In live logs
        # the owner spoke ~6 s after launch; sequential ASR->TTS loading made that first
        # reply wait another 6+ s.  Parallel voice-only loading makes the first accepted
        # turn useful sooner without synthesizing or playing any dummy phrase.
        threading.Thread(target=self._prewarm_stt, daemon=True, name="eirven-asr-prewarm").start()
        threading.Thread(target=self._prewarm_tts, daemon=True, name="eirven-tts-preload").start()

    def _prewarm_stt(self) -> None:
        try:
            # Give the Russian ASR model first access to CPU/RAM. r14 started STT, TTS,
            # self-test and LLM warm-up together; the first utterance then waited >12 s.
            self._stt_worker.warmup(timeout=180)
            # Loading weights is not enough for ONNX/GigaAM: the first actual graph run
            # in the attached trace still took 21.1 seconds. Pay that cost on a harmless
            # silent WAV before the microphone is considered interactive.
            self._stt_worker.transcribe_bytes(self._probe_wav(), ".wav", timeout=90)
            self._voice_runtime["stt_inference_primed"] = True
        except Exception as exc:
            self._voice_runtime["stt_prewarm_error"] = str(exc)[:300]
        finally:
            self._stt_ready.set()

    def stt_ready(self) -> bool:
        """True only after the isolated Russian ASR model finished its cold load."""
        return self._stt_ready.is_set()

    def tts_warm_ready(self) -> bool:
        return self._tts_ready.is_set()

    def interactive_ready(self) -> bool:
        """True after both real inference paths, not merely model loading, are primed."""
        return self._stt_ready.is_set() and self._tts_ready.is_set()

    def wait_until_ready(self, timeout: float = 180.0) -> bool:
        end = time.monotonic() + max(0.0, float(timeout))
        if not self._stt_ready.wait(max(0.0, end - time.monotonic())):
            return False
        return self._tts_ready.wait(max(0.0, end - time.monotonic()))

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
        """Load the selected local TTS and prime two cached operational phrases."""
        errors: list[str] = []
        try:
            engine = (self.settings.tts_engine or "auto").lower()
            identity = self.identity.get() if self.identity else None
            selected_key = str(getattr(identity, "voice_key", "") or "")
            preset = VOICE_CATALOG.get(selected_key, {})
            reference = str(preset.get("reference") or "").strip()
            reference_path = (self.settings.root_dir / reference).resolve() if reference else None
            # On CPU, Piper is the conversational lane: the owner's trace showed Silero
            # spending 10-16 seconds on every short sentence. Keep an available expressive
            # CUDA voice, but otherwise prime the selected sub-second local voice first.
            if engine in {"auto", "natural", "piper_onnx"} and not (
                engine in {"auto", "natural"} and self._module_exists("chatterbox") and self._cuda_available()
            ):
                try:
                    _key, selected_path = self._resolve_voice(selected_key)
                    if self._valid_piper_model(selected_path):
                        self._tts_worker.preload(str(selected_path), engine="piper_onnx", timeout=30)
                        self._voice_runtime["prewarmed_tts_engine"] = "piper_onnx:selected-low-latency"
                        return
                except Exception as exc:
                    errors.append(f"Selected Piper: {exc}")
            if (
                engine in {"auto", "natural", "chatterbox_mtl"}
                and self._module_exists("chatterbox") and self._cuda_available()
            ):
                try:
                    self._tts_worker.preload("", engine="chatterbox_mtl", timeout=180)
                    self._voice_runtime["prewarmed_tts_engine"] = (
                        "chatterbox_mtl:reference" if reference_path and reference_path.is_file()
                        else "chatterbox_mtl"
                    )
                    return
                except Exception as exc:
                    errors.append(f"Chatterbox: {exc}")
            # Edge-backed catalog voices have a distinct local Piper counterpart. Warm
            # that exact counterpart, not a generic Silero speaker, so an offline/network
            # fallback preserves the voice the owner actually chose.
            if str(preset.get("preferred_engine") or "").lower() == "edge_tts":
                try:
                    _key, selected_path = self._resolve_voice(selected_key)
                    if self._valid_piper_model(selected_path):
                        self._tts_worker.preload(str(selected_path), engine="piper_onnx", timeout=15)
                        self._voice_runtime["prewarmed_tts_engine"] = "piper_onnx:selected"
                        return
                except Exception as exc:
                    errors.append(f"Selected Piper: {exc}")
            if engine == "silero" and self.settings.silero_model:
                model_path = Path(self.settings.silero_model).expanduser().resolve()
                if model_path.is_file():
                    try:
                        self._tts_worker.preload(str(model_path), engine="silero", timeout=20)
                        self._voice_runtime["prewarmed_tts_engine"] = "silero"
                        return
                    except Exception as exc:
                        errors.append(f"Silero RU: {exc}")
            if engine == "piper_onnx":
                try:
                    models = self._voice_models(); default = models.get("default")
                    if default and Path(default).expanduser().is_file():
                        self._tts_worker.preload(str(Path(default).expanduser().resolve()), engine="piper_onnx", timeout=15)
                        self._voice_runtime["prewarmed_tts_engine"] = "piper_onnx"
                        return
                except Exception as exc:
                    errors.append(f"Piper: {exc}")
            # Auto/natural still prefer the small local Silero voice first.
            if self.settings.silero_model:
                try:
                    model_path = Path(self.settings.silero_model).expanduser().resolve()
                    if model_path.is_file():
                        self._tts_worker.preload(str(model_path), engine="silero", timeout=20)
                        self._voice_runtime["prewarmed_tts_engine"] = "silero"
                        return
                except Exception as exc:
                    errors.append(f"Silero RU: {exc}")
            try:
                models = self._voice_models(); default = models.get("default")
                if default and Path(default).expanduser().is_file():
                    self._tts_worker.preload(str(Path(default).expanduser().resolve()), engine="piper_onnx", timeout=15)
                    self._voice_runtime["prewarmed_tts_engine"] = "piper_onnx"
                    return
            except Exception as exc:
                errors.append(f"Piper: {exc}")
        finally:
            # Preload only deserializes weights for several engines. Generate the two
            # latency-critical phrases now so kernels, phonemisation and the disk cache
            # are all hot before the first owner turn.
            try:
                self.synthesize("Привет, бро. Я на связи. Что делаем?", mode="warm")
                self.synthesize("Остановила.", mode="calm")
                self._voice_runtime["tts_inference_primed"] = True
            except Exception as exc:
                errors.append(f"TTS inference probe: {exc}")
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
        models = self._voice_models()
        available = {
            key: str(Path(path).expanduser())
            for key, path in models.items()
            if Path(path).expanduser().is_file()
        }
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
        expressive_ready = self._module_exists("qwen_tts")
        chatterbox_ready = self._module_exists("chatterbox")
        edge_tts_ready = self._module_exists("edge_tts")
        silero_path = Path(self.settings.silero_model).expanduser() if self.settings.silero_model else None
        silero_ready = bool(silero_path and silero_path.is_file() and silero_path.stat().st_size > 1_000_000)
        if silero_ready:
            for key, item in VOICE_CATALOG.items():
                available.setdefault(key, f"silero:{item.get('silero_speaker') or 'kseniya'}")
        stt_status = self._stt_worker.status()
        return {
            "stt_ready": gigaam_ready or whisper_ready,
            "stt_warm_ready": self._stt_ready.is_set(),
            "tts_warm_ready": self._tts_ready.is_set(),
            "interactive_ready": self.interactive_ready(),
            "stt_primary_ready": gigaam_ready,
            "stt_fallback_ready": whisper_ready,
            "tts_ready": silero_ready or piper_ready or expressive_ready or chatterbox_ready or edge_tts_ready or __import__("os").name == "nt",
            "tts_engine": self.settings.tts_engine if (silero_ready or piper_ready or expressive_ready or chatterbox_ready or edge_tts_ready or __import__("os").name == "nt") else "none",
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
            "piper_model": self.settings.piper_model,
            "voices": available,
            "voice_modes": VOICE_MODES,
            "selected_mode": identity.voice_mode if identity else "natural",
            "emotion_mode": identity.emotion_mode if identity else "auto",
            "silence_ms": self.settings.voice_silence_ms,
        }

    @staticmethod
    def _module_exists(name: str) -> bool:
        try:
            __import__(name)
            return True
        except ImportError:
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

    def transcribe_bytes(self, data: bytes, suffix: str = ".wav") -> str:
        if not data:
            return ""
        try:
            return self._stt_worker.transcribe_bytes(data, suffix)
        except VoiceWorkerError as exc:
            self._voice_runtime["fallback_reason"] = str(exc)[:300]
            raise VoiceError(f"Не удалось распознать речь: {exc}") from exc

    def _resolve_mode(self, text: str, mode: str | None, emotion: str | None) -> str:
        identity = self.identity.get() if self.identity else None
        selected = mode or (identity.voice_mode if identity else "natural")
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
        clean = " ".join(text.strip().split())
        if mode in {"energetic", "amused", "proud"}:
            clean = clean.replace("…", ".").replace("...", ".")
        if mode == "strict":
            clean = clean.replace("!", ".")
        if mode in {"sad", "empathetic", "tired"}:
            clean = clean.replace(";", ",").replace(" — ", ", ")
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
        profile["volume"] = max(0.0, min(output_volume, 1.0)) * float(profile.get("volume", 1.0))
        requested_voice = voice_key or (identity.voice_key if identity else None)
        voice_preset = VOICE_CATALOG.get(str(requested_voice or ""), {})
        for key in ("length_scale", "noise_scale", "noise_w"):
            if key in voice_preset:
                # Voice identity shapes the base timbre while the selected emotion mode
                # still controls tempo/prosody around it.
                profile[key] = float(profile.get(key, 1.0)) * float(voice_preset[key])
        prepared = self._prepare_text(text, selected_mode)
        output_dir = self.settings.data_dir / "audio"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"reply-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.wav"
        cache_dir = output_dir / "cache-v2"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_payload = "|".join((
            str(self.settings.tts_engine or "auto"), str(requested_voice or "default"),
            selected_mode, prepared,
        ))
        cache_path = cache_dir / (hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:32] + ".wav")
        with self._tts_cache_lock:
            if cache_path.is_file() and cache_path.stat().st_size >= 44:
                self._voice_runtime["last_tts_engine"] = "audio_cache"
                return str(cache_path)
        errors: list[str] = []
        data: bytes | None = None

        # r8: a UI voice choice must correspond to a real speaker, not just a label.
        # Neural Svetlana/Dmitry prefer Edge Read Aloud (no API key); the other catalog
        # entries use genuinely different Silero speakers. On CUDA, Chatterbox may clone
        # a local per-speaker reference and adds expressive prosody without collapsing all
        # choices into one default voice.
        engine = (self.settings.tts_engine or "auto").lower()
        preferred_engine = str(voice_preset.get("preferred_engine") or "").lower()
        profile["exaggeration"] = {
            "natural": .50, "warm": .60, "calm": .40, "energetic": .76,
            "strict": .35, "quiet": .32, "amused": .82, "sad": .46,
            "empathetic": .58, "curious": .67, "concerned": .54,
            "proud": .68, "tired": .37,
        }.get(selected_mode, .5)
        profile["cfg_weight"] = .5
        profile["mode"] = selected_mode
        reference = str(voice_preset.get("reference") or "").strip()
        reference_path = (self.settings.root_dir / reference).resolve() if reference else None
        if reference_path and reference_path.is_file():
            profile["audio_prompt_path"] = str(reference_path)

        edge_cooldown = float(self._voice_runtime.get("edge_disabled_until") or 0)

        # On capable CUDA hardware the local cloned voice is the most expressive route.
        # A reference is preferred, but Chatterbox's validated default Russian voice is still
        # more prosodic than the low-latency monotone fallback.
        if (
            data is None
            and engine in {"auto", "natural", "chatterbox_mtl"}
            and self._module_exists("chatterbox") and self._cuda_available()
        ):
            try:
                data = self._tts_worker.synthesize(
                    prepared, "", profile, engine="chatterbox_mtl", timeout=18
                )
                self._voice_runtime["last_tts_engine"] = "chatterbox_mtl"
            except (TTSWorkerError, VoiceError, OSError) as exc:
                errors.append(f"Chatterbox RU: {exc}")

        # Stable order after the expressive local route: emotion-capable Edge neural voice
        # -> selected local Piper with per-mode prosody -> Silero safety net. Windows SAPI is
        # never an automatic fallback.
        if (
            data is None and preferred_engine == "edge_tts"
            and engine in {"auto", "natural", "edge_tts", "chatterbox_mtl", "silero"}
            and self._module_exists("edge_tts") and time.monotonic() >= edge_cooldown
        ):
            try:
                data = self._tts_worker.synthesize(
                    prepared, "", profile, engine="edge_tts",
                    speaker=str(voice_preset.get("edge_voice") or "ru-RU-SvetlanaNeural"), timeout=2.2,
                )
                self._voice_runtime["last_tts_engine"] = "edge_tts"
            except (TTSWorkerError, VoiceError, OSError) as exc:
                self._voice_runtime["edge_disabled_until"] = time.monotonic() + 120.0
                errors.append(f"Edge neural RU: {exc}")

        # Preserve identity for Edge-backed voices when network speech is unavailable:
        # use that catalog voice's own local Piper model before a generic Silero speaker.
        if data is None and preferred_engine == "edge_tts" and engine in {"auto", "natural", "edge_tts", "chatterbox_mtl", "silero"}:
            try:
                _key, voice_path = self._resolve_voice(requested_voice)
                data = self._tts_worker.synthesize(
                    prepared, str(voice_path), profile, engine="piper_onnx", timeout=3.0,
                )
                self._voice_runtime["last_tts_engine"] = "piper_onnx_selected_fallback"
            except (TTSWorkerError, VoiceError) as exc:
                errors.append(f"Selected Piper-ONNX: {exc}")

        # For non-Edge catalog voices, local Piper is dramatically faster on the Windows
        # CPU profile from the logs. Emotion still changes prosody through the per-mode
        # length/noise profile; Silero remains a quality fallback rather than the default
        # 10-16 second conversational path.
        if data is None and preferred_engine != "edge_tts" and engine in {"auto", "natural", "chatterbox_mtl", "edge_tts", "piper_onnx"}:
            try:
                _key, voice_path = self._resolve_voice(requested_voice)
                data = self._tts_worker.synthesize(
                    prepared, str(voice_path), profile, engine="piper_onnx", timeout=4.0,
                )
                self._voice_runtime["last_tts_engine"] = "piper_onnx_low_latency"
            except (TTSWorkerError, VoiceError) as exc:
                errors.append(f"Piper-ONNX fast lane: {exc}")

        if data is None and engine in {"auto", "natural", "silero", "chatterbox_mtl", "edge_tts"} and self.settings.silero_model:
            try:
                silero_path = Path(self.settings.silero_model).expanduser().resolve()
                data = self._tts_worker.synthesize(
                    prepared, str(silero_path), profile, engine="silero",
                    speaker=str(voice_preset.get("silero_speaker") or "kseniya"), timeout=12.0,
                )
                self._voice_runtime["last_tts_engine"] = "silero"
            except (TTSWorkerError, VoiceError, OSError) as exc:
                errors.append(f"Silero RU: {exc}")

        # Piper is the local emergency voice and therefore comes BEFORE any system voice.
        if data is None and engine not in {"qwen3", "qwen3_design", "sapi", "silero"}:
            try:
                _key, voice_path = self._resolve_voice(requested_voice)
                data = self._tts_worker.synthesize(
                    prepared, str(voice_path), profile, engine="piper_onnx", timeout=3.0,
                )
                self._voice_runtime["last_tts_engine"] = "piper_onnx"
            except (TTSWorkerError, VoiceError) as exc:
                errors.append(f"Piper-ONNX: {exc}")

        # Non-Edge presets may use the neural network voice only after local engines fail.
        if (
            data is None and preferred_engine != "edge_tts"
            and engine in {"auto", "natural", "edge_tts", "chatterbox_mtl"}
            and self._module_exists("edge_tts") and time.monotonic() >= edge_cooldown
        ):
            try:
                data = self._tts_worker.synthesize(
                    prepared, "", profile, engine="edge_tts",
                    speaker=str(voice_preset.get("edge_voice") or "ru-RU-SvetlanaNeural"), timeout=2.5,
                )
                self._voice_runtime["last_tts_engine"] = "edge_tts_fallback"
            except (TTSWorkerError, VoiceError, OSError) as exc:
                self._voice_runtime["edge_disabled_until"] = time.monotonic() + 120.0
                errors.append(f"Edge neural RU: {exc}")

        if data is None and engine == "qwen3_design":
            try:
                data = self._tts_worker.synthesize(
                    prepared, "", profile, engine="qwen3_design",
                    model_name=self.settings.expressive_tts_design_model,
                    instruction=self._emotion_instruction(selected_mode),
                    design_prompt=str(voice_preset.get("design_prompt") or ""),
                    timeout=60,
                )
                self._voice_runtime["last_tts_engine"] = "qwen3_design"
            except (TTSWorkerError, VoiceError) as exc:
                errors.append(f"Qwen3-TTS VoiceDesign: {exc}")
        if data is None and engine in {"qwen3", "qwen3_design"}:
            try:
                data = self._tts_worker.synthesize(
                    prepared, "", profile, engine="qwen3",
                    speaker=str(voice_preset.get("speaker") or self.settings.expressive_tts_speaker),
                    model_name=self.settings.expressive_tts_model,
                    instruction=self._emotion_instruction(selected_mode),
                    timeout=45,
                )
                self._voice_runtime["last_tts_engine"] = "qwen3"
            except (TTSWorkerError, VoiceError) as exc:
                errors.append(f"Qwen3-TTS CustomVoice: {exc}")

        # SAPI is opt-in only. Never silently replace the selected EIRVEN voice with the
        # Windows narrator.
        if data is None and engine == "sapi" and __import__("os").name == "nt":
            try:
                data = self._tts_worker.synthesize(prepared, "", profile, engine="sapi", timeout=15)
                self._voice_runtime["last_tts_engine"] = "sapi"
            except (TTSWorkerError, VoiceError, OSError) as exc:
                errors.append(f"Windows SAPI: {exc}")

        if data is None:
            raise VoiceError("Локальный голос недоступен: " + " | ".join(errors[-1:]))
        output.write_bytes(data)
        if not output.exists() or output.stat().st_size < 44:
            raise VoiceError("Локальный TTS не создал аудио")
        # Keep breath subtle; the speech engine should carry the prosody itself.
        self._postprocess_wav(output, float(profile["volume"]))
        with self._tts_cache_lock:
            if cache_path.is_file() and cache_path.stat().st_size >= 44:
                output.unlink(missing_ok=True)
            else:
                output.replace(cache_path)
        return str(cache_path)
