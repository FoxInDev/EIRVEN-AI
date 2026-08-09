from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

# Never let a half-configured CUDA runtime take the voice server down on old GPUs.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("CT2_USE_EXPERIMENTAL_PACKED_GEMM", "0")

RUSSIAN_PROMPT = (
    "Русская естественная разговорная речь. EIRVEN, Эйрвен, Windows, Telegram, телеграм, "
    "YouTube, ютуб, GitHub, Git, Docker, Python, PowerShell, VS Code, Ollama, Москва, погода. "
    "Точно сохраняй вопросительные слова, названия программ и команды."
)
HOTWORDS = (
    "EIRVEN Эйрвен Telegram телеграм YouTube ютуб GitHub Docker Python "
    "PowerShell Windows Ollama Москва погода"
)


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


class Recognizer:
    def __init__(self, engine: str, gigaam_model: str, whisper_model: str):
        self.requested_engine = engine
        self.gigaam_model_name = gigaam_model
        self.whisper_model_name = whisper_model
        self.gigaam = None
        self.whisper = None
        self.gigaam_error = ""
        self.whisper_error = ""

    def _load_gigaam(self):
        if self.gigaam is not None:
            return self.gigaam
        try:
            import onnx_asr

            # INT8 is the right default for an older CPU. onnx-asr performs WAV
            # reading and resampling itself, so the browser can send raw PCM WAV.
            self.gigaam = onnx_asr.load_model(
                self.gigaam_model_name,
                quantization="int8",
            )
            return self.gigaam
        except Exception as exc:
            self.gigaam_error = str(exc)
            raise

    def _load_whisper(self):
        if self.whisper is not None:
            return self.whisper
        try:
            from faster_whisper import WhisperModel

            self.whisper = WhisperModel(self.whisper_model_name, device="cpu", compute_type="int8")
            return self.whisper
        except Exception as exc:
            self.whisper_error = str(exc)
            raise

    @staticmethod
    def _clean(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        text = getattr(value, "text", None)
        if isinstance(text, str):
            return text.strip()
        if isinstance(value, dict):
            for key in ("text", "transcription", "result"):
                item = value.get(key)
                if isinstance(item, str):
                    return item.strip()
        return str(value or "").strip()

    @staticmethod
    def _plausible_ru(text: str) -> bool:
        clean = re.sub(r"\s+", " ", text.strip().casefold())
        if not clean:
            return False
        letters = [ch for ch in clean if ch.isalpha()]
        if not letters:
            return False
        # GigaAM is primary for Russian. If a backend occasionally emits broken Latin/
        # symbol-heavy text, pay the Whisper cost only for that suspicious utterance.
        cyr = sum("а" <= ch <= "я" or ch == "ё" for ch in letters)
        if cyr / max(1, len(letters)) < 0.38 and len(letters) >= 4:
            return False
        if re.search(r"(.)\1{5,}", clean):
            return False
        words = re.findall(r"[а-яёa-z0-9]+", clean)
        if len(words) >= 4 and len(set(words)) == 1:
            return False
        return True

    def _gigaam_transcribe(self, path: str) -> str:
        model = self._load_gigaam()
        return self._clean(model.recognize(path))

    def _whisper_transcribe(self, path: str) -> str:
        model = self._load_whisper()
        preferred = dict(
            language="ru",
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 520,
                "speech_pad_ms": 360,
                "min_speech_duration_ms": 90,
            },
            beam_size=3,
            best_of=3,
            condition_on_previous_text=False,
            temperature=0.0,
            word_timestamps=False,
            initial_prompt=RUSSIAN_PROMPT,
            hotwords=HOTWORDS,
        )
        try:
            segments, _info = model.transcribe(path, **preferred)
        except TypeError:
            preferred.pop("hotwords", None)
            preferred.pop("vad_parameters", None)
            segments, _info = model.transcribe(path, **preferred)
        return " ".join(s.text.strip() for s in segments if s.text.strip()).strip()

    def transcribe(self, path: str) -> tuple[str, str, str]:
        errors: list[str] = []
        if self.requested_engine in {"gigaam", "auto"}:
            try:
                text = self._gigaam_transcribe(path)
                if text and self._plausible_ru(text):
                    return text, "gigaam", ""
                if text:
                    errors.append(f"GigaAM suspicious transcript: {text[:120]}")
            except Exception as exc:
                errors.append(f"GigaAM: {exc}")
        # Whisper is intentionally a fallback for mixed-language/technical speech
        # and for machines where the ONNX model was not downloaded yet.
        try:
            text = self._whisper_transcribe(path)
            return text, "whisper", " | ".join(errors)
        except Exception as exc:
            errors.append(f"Whisper: {exc}")
            raise RuntimeError(" | ".join(errors)) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="gigaam")
    parser.add_argument("--gigaam-model", default="gigaam-v3-e2e-rnnt")
    parser.add_argument("--whisper-model", default="large-v3-turbo")
    args = parser.parse_args()
    recognizer = Recognizer(args.engine, args.gigaam_model, args.whisper_model)
    # Do not download/load a 200+ MB model while FastAPI is starting. The first
    # voice request or install-time warmup owns that cost; the server is alive meanwhile.
    emit({
        "type": "ready",
        "engine": args.engine,
        "gigaam_model": args.gigaam_model,
        "whisper_model": args.whisper_model,
        "device": "cpu",
    })

    for raw in sys.stdin:
        cleanup: Path | None = None
        request_id = ""
        try:
            request = json.loads(raw)
            request_id = str(request.get("id") or "")
            command = request.get("command")
            if command == "shutdown":
                emit({"id": request_id, "ok": True})
                return 0
            if command == "warmup":
                preferred = str(request.get("engine") or args.engine)
                if preferred in {"gigaam", "auto"}:
                    recognizer._load_gigaam()
                    emit({"id": request_id, "ok": True, "engine": "gigaam"})
                else:
                    recognizer._load_whisper()
                    emit({"id": request_id, "ok": True, "engine": "whisper"})
                continue
            if command == "transcribe_bytes":
                audio = base64.b64decode(str(request.get("audio_b64") or ""), validate=True)
                suffix = str(request.get("suffix") or ".wav")
                if not suffix.startswith(".") or len(suffix) > 12:
                    suffix = ".wav"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                    temp.write(audio)
                    path = temp.name
                cleanup = Path(path)
            elif command == "transcribe":
                path = str(request.get("path") or "")
            else:
                emit({"id": request_id, "ok": False, "error": "unknown command"})
                continue

            text, engine, fallback_reason = recognizer.transcribe(path)
            emit({
                "id": request_id,
                "ok": True,
                "text": text,
                "language": "ru",
                "engine": engine,
                "fallback_reason": fallback_reason,
            })
        except Exception as exc:
            emit({
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc()[-2500:],
            })
        finally:
            if cleanup is not None:
                cleanup.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
