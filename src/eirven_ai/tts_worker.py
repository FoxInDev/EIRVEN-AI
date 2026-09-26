from __future__ import annotations

import base64
import io
import json
import hashlib
import tempfile
import os
import shutil
import subprocess
import sys
import traceback
import wave
from pathlib import Path
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _wav_bytes(samples, sample_rate: int) -> bytes:
    """Encode neural float audio with libsndfile instead of hand-rolling PCM.

    This deliberately avoids the old manual conversion path: the browser preview and
    native daemon must receive the exact same standards-compliant WAV.
    """
    import numpy as np
    import soundfile as sf

    if hasattr(samples, "detach"):
        samples = samples.detach().float().cpu().numpy()
    data = np.asarray(samples, dtype=np.float32).squeeze()
    if data.ndim != 1:
        data = data.reshape(-1)
    if not len(data):
        raise RuntimeError("TTS produced an empty waveform")
    if not np.isfinite(data).all():
        raise RuntimeError("TTS produced invalid samples")
    peak = float(np.max(np.abs(data)))
    # Do not normalize ordinary speech. Only prevent pathological clipping from a
    # backend returning amplitudes outside the conventional [-1, 1] range.
    if peak > 1.25:
        data = data / peak
    out = io.BytesIO()
    sf.write(out, data, int(sample_rate), format="WAV", subtype="PCM_16")
    return out.getvalue()


def _piper_config_path(model_path: str) -> str:
    """Return an ASCII-safe copy of Piper JSON for Windows.

    piper-onnx 1.0.x opens the config without an explicit encoding. On Russian Windows
    that means cp1251/charmap and a perfectly valid UTF-8 model card can crash with
    byte 0x98. We parse UTF-8 ourselves and serialize ensure_ascii=True so the third-
    party loader only ever reads ASCII.
    """
    path = Path(model_path)
    candidates = [Path(str(path) + ".json"), path.with_suffix(".onnx.json"), path.with_suffix(".json")]
    source = next((item for item in candidates if item.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"TTS config not found next to {model_path}")
    raw = source.read_text(encoding="utf-8-sig")
    payload = json.loads(raw)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    cache = Path(tempfile.gettempdir()) / "eirven-piper-config"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{source.stem}-{digest}.json"
    if not target.is_file():
        target.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="ascii")
    return str(target)


def _piper_synthesize(voice, text: str, profile: dict[str, Any]) -> bytes:
    samples, sample_rate = voice.create(
        text,
        length_scale=float(profile.get("length_scale", 1.0)),
        noise_scale=float(profile.get("noise_scale", 0.667)),
        noise_w=float(profile.get("noise_w", 0.8)),
    )
    return _wav_bytes(samples, sample_rate)



def _silero_load(model_path: str):
    import torch
    path = Path(model_path)
    if not path.is_file():
        raise FileNotFoundError(f"Silero model not found: {model_path}")
    model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
    # Baya is small and fast on CPU.  Keeping it off CUDA prevents an 8 GB GPU from
    # evicting/competing with the resident Ollama model and keeps the voice available
    # while a visual or coding task uses the GPU.
    device = torch.device("cpu")
    model.to(device)
    return model


def _silero_phrase_segments(text: str, profile: dict[str, Any]) -> list[tuple[str, int]]:
    """Split one spoken chunk into natural phrases and their following pauses.

    Silero's Russian model deliberately has no arbitrary speaker-speed knob.  Earlier
    code calculated an emotional tempo but discarded it on the canonical Baya path.
    Phrase timing is the safe control surface: it changes cadence without resampling,
    pitching, or replacing the voice.  We keep commas inside lively phrases and allow
    them to breathe in calm/supportive delivery.
    """
    clean = " ".join(str(text or "").split())
    if not clean:
        return []
    mode = str(profile.get("mode") or "natural")
    speed = max(0.88, min(float(profile.get("speech_speed") or 0.98), 1.08))
    breath = max(0.0, min(float(profile.get("breath") or 0.0), 0.10))
    slow_delivery = mode in {"warm", "calm", "quiet", "sad", "empathetic", "concerned", "tired"}
    mode_pause = {
        "warm": 1.10,
        "calm": 1.22,
        "quiet": 1.28,
        "energetic": 0.72,
        "strict": 0.82,
        "amused": 0.78,
        "sad": 1.35,
        "empathetic": 1.18,
        "curious": 0.94,
        "concerned": 1.05,
        "proud": 0.88,
        "tired": 1.38,
    }.get(mode, 1.0)
    pause_scale = max(0.65, min((mode_pause / speed) + breath * 2.0, 1.55))
    base_pause = {
        ",": 72,
        ";": 118,
        ":": 105,
        ".": 152,
        "?": 176,
        "!": 124,
        "…": 238,
        "—": 96,
    }

    raw: list[tuple[str, str]] = []
    current: list[str] = []
    for index, char in enumerate(clean):
        current.append(char)
        boundary = char in ".!?…;:"
        if char == "," and slow_delivery and len(current) >= 38:
            boundary = True
        if char == "—" and slow_delivery and len(current) >= 28:
            boundary = True
        if boundary and (index + 1 == len(clean) or clean[index + 1].isspace()):
            phrase = "".join(current).strip()
            if phrase:
                raw.append((phrase, char))
            current = []
    tail = "".join(current).strip()
    if tail:
        raw.append((tail, ""))

    # Keep inference calls bounded even when a long unpunctuated sentence reaches TTS.
    bounded: list[tuple[str, str]] = []
    for phrase, mark in raw:
        rest = phrase
        while len(rest) > 220:
            cut = rest.rfind(" ", 80, 221)
            if cut < 80:
                cut = 220
            bounded.append((rest[:cut].strip(), ","))
            rest = rest[cut:].strip()
        if rest:
            bounded.append((rest, mark))

    result: list[tuple[str, int]] = []
    for index, (phrase, mark) in enumerate(bounded):
        pause_ms = 0
        if index + 1 < len(bounded):
            pause_ms = int(round(base_pause.get(mark, 82) * pause_scale))
        result.append((phrase, max(0, min(pause_ms, 360))))
    return result


def _breath_samples(
    sample_rate: int,
    frames: int,
    strength: float,
    seed: int,
    *,
    direction: str = "inhale",
):
    """Create a short, very quiet non-verbal breath for a real phrase boundary.

    This is audio only: no token or markup is ever inserted into the text given to
    Baya, so the engine cannot pronounce ``breath``/``вдох``.  A shaped, band-limited
    noise envelope is mixed into silence between phrases and scaled relative to the
    neural speech.  It is intentionally subtle enough to disappear on laptop speakers
    while still preventing the perfectly dead pauses that sound synthetic in headphones.
    """
    import numpy as np

    count = max(0, int(frames))
    amount = max(0.0, min(float(strength or 0.0), 0.10))
    if count < max(32, int(sample_rate * 0.035)) or amount <= 0.0:
        return np.zeros(count, dtype=np.float32)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    raw = rng.standard_normal(count).astype(np.float32)
    # Remove rumble and the sharpest hiss using two inexpensive moving averages.
    slow_width = max(8, int(sample_rate * 0.0016))
    fast_width = max(2, int(sample_rate * 0.00018))
    slow = np.convolve(raw, np.ones(slow_width, dtype=np.float32) / slow_width, mode="same")
    airy = raw - slow
    airy = np.convolve(airy, np.ones(fast_width, dtype=np.float32) / fast_width, mode="same")
    rms = float(np.sqrt(np.mean(airy * airy))) if airy.size else 0.0
    if rms > 1e-7:
        airy /= rms
    phase = np.linspace(0.0, 1.0, count, dtype=np.float32)
    # A quick intake and soft release sounds less like generated white noise.  An
    # exhale has the same soft centre but a slower onset and a lower level, which lets
    # the two pieces read as one quiet human breath rather than a sound effect.
    if str(direction or "inhale").casefold() == "exhale":
        envelope = np.clip(np.sin(np.pi * phase), 0.0, 1.0) ** 1.45
        envelope *= np.clip(phase * 4.0, 0.0, 1.0)
        amount *= 0.68
    else:
        envelope = np.clip(np.sin(np.pi * phase), 0.0, 1.0) ** 1.7
        envelope *= np.clip(phase * 8.0, 0.0, 1.0)
    return (airy * envelope * amount).astype(np.float32)


def _ffmpeg_executable() -> str:
    """Resolve the already-packaged FFmpeg before falling back to PATH.

    Installed EIRVEN bundles ``imageio-ffmpeg`` for the video engine, but that binary
    is not necessarily registered in Windows PATH.  Voice prosody must work on a clean
    user machine too, without downloading another program.
    """
    executable = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if executable:
        return executable
    try:
        import imageio_ffmpeg  # type: ignore

        candidate = str(imageio_ffmpeg.get_ffmpeg_exe() or "")
        if candidate and Path(candidate).is_file():
            return candidate
    except Exception:
        pass
    return ""


def _tempo_wav(data: bytes, speed: float, pitch_ratio: float = 1.0) -> bytes:
    """Apply bounded tempo and pitch-preserving emotion shaping with FFmpeg.

    Silero Baya exposes timbre and punctuation but no speech-rate control.  ``atempo``
    is a waveform-similarity time scaler: unlike sample-rate tricks it does not turn
    her younger/older or create the warped robotic pitch heard in older releases.  The
    operation is bounded and has a byte-for-byte fallback when FFmpeg is unavailable.
    """
    tempo = max(0.88, min(float(speed or 1.0), 1.08))
    pitch = max(0.965, min(float(pitch_ratio or 1.0), 1.035))
    if abs(tempo - 1.0) < 0.012 and abs(pitch - 1.0) < 0.002:
        return data
    executable = _ffmpeg_executable()
    if not executable:
        return data
    try:
        with wave.open(io.BytesIO(data), "rb") as source_wav:
            source_rate = int(source_wav.getframerate() or 48_000)
        filters: list[str] = []
        if abs(pitch - 1.0) >= 0.002:
            # Changing the sample rate raises/lowers pitch.  Resampling back and then
            # applying the reciprocal atempo keeps the phrase duration unchanged.
            shifted_rate = max(8_000, int(round(source_rate * pitch)))
            filters.extend([
                f"asetrate={shifted_rate}",
                f"aresample={source_rate}",
                f"atempo={1.0 / pitch:.5f}",
            ])
        if abs(tempo - 1.0) >= 0.012:
            filters.append(f"atempo={tempo:.5f}")
        if not filters:
            return data
        # A WAV written to stdout has an intentionally unknown RIFF length, which a
        # few Windows playback APIs interpret as a multi-hour file.  A private temp
        # pair lets FFmpeg finalize the header before bytes return to the daemon.
        with tempfile.TemporaryDirectory(prefix="eirven-baya-tempo-") as folder:
            source = Path(folder) / "source.wav"
            target = Path(folder) / "rendered.wav"
            source.write_bytes(data)
            completed = subprocess.run(
                [
                    executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", str(source), "-filter:a", ",".join(filters),
                    "-c:a", "pcm_s16le", str(target),
                ],
                capture_output=True, timeout=8,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            rendered = target.read_bytes() if target.is_file() else b""
        if completed.returncode == 0 and len(rendered) > 44 and rendered.startswith(b"RIFF"):
            return rendered
    except Exception:
        pass
    return data


def _silero_synthesize(
    model,
    text: str,
    profile: dict[str, Any] | None = None,
    speaker: str = "baya",
    sample_rate: int = 48000,
) -> bytes:
    """Synthesize fresh Baya speech with semantic phrase timing.

    ``apply_tts`` lets us join neural phrases with real silence.  For long calm/supportive
    pauses we add a very low-level, shaped inhale/exhale pair outside the text stream;
    unsupported non-verbal tokens are often spoken literally and would sound artificial.
    Very old Silero model objects without ``apply_tts`` retain the official
    ``save_wav`` compatibility path.
    """
    selected_profile = dict(profile or {})
    mode = str(selected_profile.get("mode") or "natural")
    apply_tts = getattr(model, "apply_tts", None)
    if callable(apply_tts):
        import numpy as np

        pieces: list[Any] = []
        segments = _silero_phrase_segments(text, selected_profile)
        breath_strength = max(0.0, min(float(selected_profile.get("breath") or 0.0), 0.10))
        seed_base = int(selected_profile.get("prosody_seed") or 0) & 0xFFFFFFFF
        for phrase_index, (phrase, pause_ms) in enumerate(segments):
            try:
                samples = apply_tts(
                    text=phrase,
                    speaker="baya",
                    sample_rate=int(sample_rate),
                    put_accent=True,
                    put_yo=True,
                )
            except TypeError:
                samples = apply_tts(
                    text=phrase,
                    speaker="baya",
                    sample_rate=int(sample_rate),
                )
            if hasattr(samples, "detach"):
                samples = samples.detach().float().cpu().numpy()
            audio = np.asarray(samples, dtype=np.float32).reshape(-1)
            if audio.size:
                pieces.append(audio)
            if pause_ms:
                pause_frames = max(1, int(sample_rate * pause_ms / 1000))
                # One natural inhale at meaningful boundaries; short lively pauses
                # remain clean.  Scale it from the phrase RMS so it follows Baya's
                # volume instead of becoming a fixed artificial sound effect.
                use_breath = breath_strength > 0.0 and pause_ms >= 105
                if use_breath:
                    breath_frames = min(
                        int(sample_rate * (0.075 + breath_strength * 0.85)),
                        max(0, int(pause_frames * 0.28)),
                    )
                    phrase_rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.04
                    audible = min(0.012, max(0.0012, phrase_rms * (0.032 + breath_strength * 0.62)))
                    slow_breath = mode in {"warm", "calm", "quiet", "sad", "empathetic", "concerned", "tired"}
                    can_cycle = slow_breath and pause_frames >= int(sample_rate * 0.19)
                    exhale_frames = min(
                        int(sample_rate * (0.065 + breath_strength * 0.58)),
                        max(0, int(pause_frames * 0.25)),
                    ) if can_cycle else 0
                    free = max(0, pause_frames - breath_frames - exhale_frames)
                    quiet_before = int(free * (0.27 if can_cycle else 0.38))
                    quiet_between = int(free * 0.24) if can_cycle else 0
                    quiet_after = max(0, free - quiet_before - quiet_between)
                    if quiet_before:
                        pieces.append(np.zeros(quiet_before, dtype=np.float32))
                    pieces.append(_breath_samples(
                        int(sample_rate), breath_frames, audible,
                        seed=seed_base + (phrase_index + 1) * 104729 + len(phrase) * 1009,
                        direction="inhale",
                    ))
                    if can_cycle:
                        if quiet_between:
                            pieces.append(np.zeros(quiet_between, dtype=np.float32))
                        pieces.append(_breath_samples(
                            int(sample_rate), exhale_frames, audible,
                            seed=seed_base + (phrase_index + 1) * 130363 + len(phrase) * 811,
                            direction="exhale",
                        ))
                    if quiet_after:
                        pieces.append(np.zeros(quiet_after, dtype=np.float32))
                else:
                    pieces.append(np.zeros(pause_frames, dtype=np.float32))
        if not pieces:
            raise RuntimeError("Silero produced an empty waveform")
        rendered = _wav_bytes(np.concatenate(pieces), int(sample_rate))
        return _tempo_wav(
            rendered,
            float(selected_profile.get("speech_speed") or 1.0),
            float(selected_profile.get("pitch_ratio") or 1.0),
        )

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        path = Path(temp.name)
    try:
        model.save_wav(
            text=text, speaker="baya", sample_rate=int(sample_rate),
            audio_path=str(path),
        )
        data = path.read_bytes()
        if len(data) < 1000 or not data.startswith(b"RIFF"):
            raise RuntimeError("Silero produced an invalid WAV")
        return data
    finally:
        path.unlink(missing_ok=True)



def _local_first(load):
    """Загрузить модель сначала ТОЛЬКО с диска, в сеть — лишь если её там нет.

    Модели распознавания грузятся по имени через Hugging Face, а библиотека по
    умолчанию сначала идёт в интернет проверить, нет ли новой версии, — даже когда
    модель давно скачана. Без интернета это ожидание соединения по каждому файлу
    или ошибка: микрофон не поднимался, и Эрви «без интернета не работала». А это
    как раз проверка, которую мы предлагаем людям: отключи сеть — она работает.
    Функции библиотеки читают HF_HUB_OFFLINE при каждом вызове, поэтому режим
    можно включить на время загрузки и вернуть как было.
    """
    try:
        import huggingface_hub.constants as hub_constants
    except Exception:
        return load()
    previous = hub_constants.HF_HUB_OFFLINE
    hub_constants.HF_HUB_OFFLINE = True
    try:
        return load()
    except Exception:
        # Модели ещё нет на диске — это первая установка: скачиваем как раньше.
        hub_constants.HF_HUB_OFFLINE = previous
        return load()
    finally:
        hub_constants.HF_HUB_OFFLINE = previous

def _load_chatterbox_multilingual():
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    # V3 is the current recommended multilingual checkpoint. Fall back to the package
    # default when an older chatterbox build (e.g. the working Jarvis archive) does
    # not expose the t3_model argument yet.
    try:
        return _local_first(lambda: ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3"))
    except TypeError:
        return _local_first(lambda: ChatterboxMultilingualTTS.from_pretrained(device=device))


def _chatterbox_synthesize(model, text: str, profile: dict[str, Any]) -> bytes:
    exaggeration = float(profile.get("exaggeration", 0.52))
    cfg_weight = float(profile.get("cfg_weight", 0.5))
    kwargs: dict[str, Any] = {
        "language_id": "ru",
        "exaggeration": max(0.25, min(1.2, exaggeration)),
        "cfg_weight": max(0.2, min(0.8, cfg_weight)),
        "temperature": 0.72,
    }
    reference = str(profile.get("audio_prompt_path") or "").strip()
    if reference and Path(reference).is_file():
        kwargs["audio_prompt_path"] = reference
    wav = model.generate(text[:700], **kwargs)
    return _wav_bytes(wav, int(model.sr))


def _sapi_synthesize(text: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows SAPI is only available on Windows")
    with tempfile.TemporaryDirectory(prefix="eirven-sapi-") as td:
        root = Path(td)
        text_path = root / "speech.txt"
        wav_path = root / "speech.wav"
        text_path.write_text(text, encoding="utf-8")
        escaped_wav_path = str(wav_path).replace("'", "''")
        escaped_text_path = str(text_path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SetOutputToWaveFile('{escaped_wav_path}'); "
            f"$t=[IO.File]::ReadAllText('{escaped_text_path}',[Text.Encoding]::UTF8); "
            "$s.Speak($t); $s.Dispose();"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if completed.returncode != 0 or not wav_path.is_file():
            raise RuntimeError((completed.stderr or completed.stdout or "SAPI synthesis failed")[-800:])
        return wav_path.read_bytes()


def _edge_tts_synthesize(text: str, voice: str, profile: dict[str, Any]) -> bytes:
    """Use Edge Read Aloud neural voices without an API key, then normalize to WAV.

    This path is intentionally optional/network-backed. If the service is unavailable,
    VoiceService immediately falls back to a fully local engine.
    """
    import asyncio
    import edge_tts
    import soundfile as sf

    mode = str(profile.get("mode") or "natural")
    speed = max(.88, min(float(profile.get("speech_speed") or 0.98), 1.08))
    base = {
        "energetic": 7, "calm": -7, "quiet": -5, "strict": -1, "warm": -2,
        "amused": 6, "sad": -10, "empathetic": -7, "curious": 1,
        "concerned": -4, "proud": 2, "tired": -11,
    }.get(mode, 0)
    pct = int(round(base + (speed - 1.0) * 100))
    pct = max(-14, min(16, pct))
    rate = f"{pct:+d}%"
    pitch = {
        "energetic": "+3Hz", "calm": "-2Hz", "quiet": "-1Hz", "strict": "-2Hz",
        "warm": "+1Hz", "amused": "+4Hz", "sad": "-4Hz", "empathetic": "-1Hz",
        "curious": "+3Hz", "concerned": "-2Hz", "proud": "+1Hz", "tired": "-5Hz",
    }.get(mode, "+0Hz")
    volume = {
        "quiet": "-12%", "energetic": "+5%", "warm": "+2%", "strict": "+1%",
        "amused": "+4%", "sad": "-8%", "empathetic": "-2%", "curious": "+1%",
        "concerned": "-1%", "proud": "+3%", "tired": "-10%",
    }.get(mode, "+0%")

    async def collect() -> bytes:
        communicate = edge_tts.Communicate(text=text[:1200], voice=voice or "ru-RU-SvetlanaNeural", rate=rate, volume=volume, pitch=pitch)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(bytes(chunk["data"]))
        return b"".join(chunks)

    encoded = asyncio.run(collect())
    if len(encoded) < 1000:
        raise RuntimeError("Edge TTS returned no usable audio")
    data, sample_rate = sf.read(io.BytesIO(encoded), dtype="float32", always_2d=False)
    return _wav_bytes(data, int(sample_rate))


def main() -> int:
    piper_voices: dict[str, object] = {}
    silero_models: dict[str, object] = {}
    chatterbox_models: dict[str, object] = {}
    emit({"type": "ready", "engines": ["chatterbox_mtl", "edge_tts", "silero", "sapi", "piper_onnx"]})

    for raw in sys.stdin:
        request_id = ""
        try:
            request = json.loads(raw)
            request_id = str(request.get("id") or "")
            command = request.get("command")
            if command == "shutdown":
                emit({"id": request_id, "ok": True})
                return 0

            engine = str(request.get("engine") or "piper_onnx").lower()
            model_path = str(request.get("model_path") or "")
            model_name = str(request.get("model_name") or "")

            if command == "preload":
                if engine == "silero":
                    if not model_path or not Path(model_path).is_file():
                        raise FileNotFoundError(f"Silero model not found: {model_path}")
                    if model_path not in silero_models:
                        silero_models[model_path] = _silero_load(model_path)
                    emit({"id": request_id, "ok": True, "engine": engine, "preloaded": True})
                    continue
                if engine == "chatterbox_mtl":
                    if "default" not in chatterbox_models:
                        chatterbox_models["default"] = _load_chatterbox_multilingual()
                    emit({"id": request_id, "ok": True, "engine": engine, "preloaded": True})
                    continue
                if engine == "edge_tts":
                    import edge_tts  # noqa: F401
                    emit({"id": request_id, "ok": True, "engine": engine, "preloaded": True})
                    continue
                if engine == "sapi":
                    if os.name != "nt":
                        raise RuntimeError("Windows SAPI unavailable")
                    emit({"id": request_id, "ok": True, "engine": engine, "preloaded": True})
                    continue
                if engine == "piper_onnx":
                    if not model_path or not Path(model_path).is_file():
                        raise FileNotFoundError(f"TTS model not found: {model_path}")
                    from piper_onnx import Piper

                    if model_path not in piper_voices:
                        piper_voices[model_path] = Piper(model_path, _piper_config_path(model_path))
                    emit({"id": request_id, "ok": True, "engine": engine, "preloaded": True})
                    continue
                raise ValueError(f"unknown TTS engine: {engine}")

            if command != "synthesize":
                emit({"id": request_id, "ok": False, "error": "unknown command"})
                continue

            text = str(request.get("text") or "").strip()
            if not text:
                raise ValueError("empty text")

            if engine == "chatterbox_mtl":
                model = chatterbox_models.get("default")
                if model is None:
                    model = _load_chatterbox_multilingual()
                    chatterbox_models["default"] = model
                data = _chatterbox_synthesize(model, text, dict(request.get("profile") or {}))
            elif engine == "edge_tts":
                data = _edge_tts_synthesize(text, str(request.get("speaker") or "ru-RU-SvetlanaNeural"), dict(request.get("profile") or {}))
            elif engine == "sapi":
                data = _sapi_synthesize(text)
            elif engine == "silero":
                if not model_path or not Path(model_path).is_file():
                    raise FileNotFoundError(f"Silero model not found: {model_path}")
                model = silero_models.get(model_path)
                if model is None:
                    model = _silero_load(model_path)
                    silero_models[model_path] = model
                # The public runtime exposes one canonical timbre. Ignore stale speaker
                # values from older clients instead of unexpectedly changing her voice.
                data = _silero_synthesize(
                    model,
                    text,
                    dict(request.get("profile") or {}),
                    "baya",
                    48000,
                )
            elif engine == "piper_onnx":
                if not model_path or not Path(model_path).is_file():
                    raise FileNotFoundError(f"TTS model not found: {model_path}")
                from piper_onnx import Piper

                voice = piper_voices.get(model_path)
                if voice is None:
                    voice = Piper(model_path, _piper_config_path(model_path))
                    piper_voices[model_path] = voice
                data = _piper_synthesize(voice, text, dict(request.get("profile") or {}))
            else:
                raise ValueError(f"unknown TTS engine: {engine}")

            emit({
                "id": request_id,
                "ok": True,
                "engine": engine,
                "audio_b64": base64.b64encode(data).decode("ascii"),
            })
        except Exception as exc:
            emit({
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc()[-2500:],
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
