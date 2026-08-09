from __future__ import annotations

import base64
import io
import json
import hashlib
import tempfile
import os
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


def _qwen_synthesize(model, text: str, speaker: str, instruction: str) -> bytes:
    wavs, sample_rate = model.generate_custom_voice(
        text=text, language="Russian", speaker=speaker,
        instruct=instruction or "Speak natural conversational Russian with clear diction.",
    )
    return _wav_bytes(wavs[0], sample_rate)


def _qwen_design_synthesize(model, text: str, design_prompt: str, instruction: str) -> bytes:
    combined = design_prompt.strip()
    if instruction.strip():
        combined = (combined + " " + instruction.strip()).strip()
    wavs, sample_rate = model.generate_voice_design(
        text=text, language="Russian",
        instruct=combined or "Native Russian conversational voice, natural and clear.",
    )
    return _wav_bytes(wavs[0], sample_rate)



def _silero_load(model_path: str):
    import torch
    path = Path(model_path)
    if not path.is_file():
        raise FileNotFoundError(f"Silero model not found: {model_path}")
    model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    return model


def _silero_synthesize(model, text: str, speaker: str, sample_rate: int = 48000) -> bytes:
    # Use Silero's own WAV writer. The previous revisions converted the tensor
    # ourselves; using the model's official save_wav path removes one more possible
    # source of malformed/too-fast preview audio on Windows.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        path = Path(temp.name)
    try:
        model.save_wav(
            text=text, speaker=speaker or "kseniya", sample_rate=int(sample_rate),
            audio_path=str(path),
        )
        data = path.read_bytes()
        if len(data) < 1000 or not data.startswith(b"RIFF"):
            raise RuntimeError("Silero produced an invalid WAV")
        return data
    finally:
        path.unlink(missing_ok=True)


def _load_chatterbox_multilingual():
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    # V3 is the current recommended multilingual checkpoint. Fall back to the package
    # default when an older chatterbox build (e.g. the working Jarvis archive) does
    # not expose the t3_model argument yet.
    try:
        return ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
    except TypeError:
        return ChatterboxMultilingualTTS.from_pretrained(device=device)


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
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SetOutputToWaveFile('{str(wav_path).replace("'", "''")}'); "
            f"$t=[IO.File]::ReadAllText('{str(text_path).replace("'", "''")}',[Text.Encoding]::UTF8); "
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


def _load_qwen_model(chosen: str):
    from qwen_tts import Qwen3TTSModel
    import torch

    if torch.cuda.is_available():
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        device_map = "cuda:0"
    else:
        dtype = torch.float32
        device_map = "cpu"
    return Qwen3TTSModel.from_pretrained(chosen, device_map=device_map, dtype=dtype)

def main() -> int:
    piper_voices: dict[str, object] = {}
    qwen_models: dict[str, object] = {}
    silero_models: dict[str, object] = {}
    chatterbox_models: dict[str, object] = {}
    emit({"type": "ready", "engines": ["chatterbox_mtl", "edge_tts", "silero", "sapi", "piper_onnx", "qwen3", "qwen3_design"]})

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
                if engine in {"qwen3", "qwen3_design"}:
                    chosen = model_name or ("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign" if engine == "qwen3_design" else "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
                    if chosen not in qwen_models:
                        qwen_models[chosen] = _load_qwen_model(chosen)
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
                data = _silero_synthesize(model, text, str(request.get("speaker") or "kseniya"), 48000)
            elif engine == "piper_onnx":
                if not model_path or not Path(model_path).is_file():
                    raise FileNotFoundError(f"TTS model not found: {model_path}")
                from piper_onnx import Piper

                voice = piper_voices.get(model_path)
                if voice is None:
                    voice = Piper(model_path, _piper_config_path(model_path))
                    piper_voices[model_path] = voice
                data = _piper_synthesize(voice, text, dict(request.get("profile") or {}))
            elif engine == "qwen3":
                chosen = model_name or "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
                model = qwen_models.get(chosen)
                if model is None:
                    model = _load_qwen_model(chosen)
                    qwen_models[chosen] = model
                data = _qwen_synthesize(model, text, str(request.get("speaker") or "Ryan"), str(request.get("instruction") or ""))
            elif engine == "qwen3_design":
                chosen = model_name or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
                model = qwen_models.get(chosen)
                if model is None:
                    model = _load_qwen_model(chosen)
                    qwen_models[chosen] = model
                data = _qwen_design_synthesize(
                    model, text, str(request.get("design_prompt") or ""), str(request.get("instruction") or "")
                )
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
