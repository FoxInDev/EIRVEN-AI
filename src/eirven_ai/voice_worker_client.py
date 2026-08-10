from __future__ import annotations

import base64
import json
import os
import queue
from collections import deque
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class VoiceWorkerError(RuntimeError):
    pass


class VoiceWorkerClient:
    def __init__(
        self,
        model: str,
        root_dir: Path,
        *,
        engine: str = "gigaam",
        gigaam_model: str = "gigaam-v3-e2e-ctc",
    ):
        self.model = model
        self.root_dir = root_dir
        self.engine = engine
        self.gigaam_model = gigaam_model
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=60)
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._ready = threading.Event()
        self._fatal = ""
        self._last_engine = ""
        self._last_fallback = ""
        self._lock = threading.RLock()

    def _start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None and self._ready.is_set():
                return
            self.close()
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = "-1"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._ready.clear(); self._fatal = ""; self._stderr_lines.clear()
            self._process = subprocess.Popen(
                [
                    sys.executable, "-u", "-m", "eirven_ai.voice_worker",
                    "--engine", self.engine,
                    "--gigaam-model", self.gigaam_model,
                    "--whisper-model", self.model,
                ],
                cwd=self.root_dir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            self._reader = threading.Thread(target=self._read_loop, daemon=True, name="eirven-stt-reader")
            self._reader.start()
            self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True, name="eirven-stt-stderr")
            self._stderr_reader.start()
        if not self._ready.wait(timeout=20):
            detail = self._fatal or self._stderr_tail() or "worker did not become ready"
            self.close()
            raise VoiceWorkerError(f"Распознавание речи не запустилось: {detail}")

    def _stderr_loop(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        try:
            for raw in process.stderr:
                clean = raw.strip()
                if clean:
                    self._stderr_lines.append(clean[-600:])
        except Exception:
            return

    def _read_loop(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        try:
            for raw in process.stdout:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "ready":
                    self._ready.set(); continue
                if message.get("type") == "fatal":
                    self._fatal = str(message.get("error") or "fatal voice worker error")
                    self._ready.set(); continue
                request_id = str(message.get("id") or "")
                with self._lock:
                    target = self._responses.get(request_id)
                if target:
                    target.put(message)
        finally:
            self._ready.clear()
            with self._lock:
                pending = list(self._responses.values())
            for target in pending:
                target.put({"ok": False, "error": "Процесс распознавания завершился"})

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)[-1200:]

    def _request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._start()
        request_id = uuid.uuid4().hex
        payload = {"id": request_id, **payload}
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._responses[request_id] = response_queue
            process = self._process
            if not process or not process.stdin or process.poll() is not None:
                raise VoiceWorkerError("Процесс распознавания недоступен")
            process.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
            process.stdin.flush()
        try:
            return response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            self.close()
            raise VoiceWorkerError("Распознавание заняло слишком много времени") from exc
        finally:
            with self._lock:
                self._responses.pop(request_id, None)

    def warmup(self, timeout: float = 240) -> str:
        response = self._request({"command": "warmup", "engine": self.engine}, timeout)
        if not response.get("ok"):
            raise VoiceWorkerError(str(response.get("error") or "Не удалось подготовить ASR"))
        return str(response.get("engine") or self.engine)

    def transcribe(self, path: str, timeout: float = 180) -> str:
        source = Path(path)
        if not source.is_file():
            raise VoiceWorkerError(f"Аудиофайл не найден: {source}")
        return self.transcribe_bytes(source.read_bytes(), source.suffix or ".wav", timeout=timeout)

    def transcribe_bytes(self, data: bytes, suffix: str = ".wav", timeout: float = 180) -> str:
        if not data:
            return ""
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        encoded = base64.b64encode(data).decode("ascii")
        for attempt in range(2):
            try:
                response = self._request({
                    "command": "transcribe_bytes",
                    "audio_b64": encoded,
                    "suffix": suffix[:10],
                }, timeout)
                if not response.get("ok"):
                    raise VoiceWorkerError(str(response.get("error") or "Неизвестная ошибка распознавания"))
                self._last_engine = str(response.get("engine") or "")
                self._last_fallback = str(response.get("fallback_reason") or "")
                text = str(response.get("text") or "").strip()
                return text or "Речь не распознана."
            except Exception:
                if attempt:
                    raise
                self.close(); time.sleep(0.2)
        raise VoiceWorkerError("Не удалось распознать речь")

    def status(self) -> dict[str, Any]:
        process = self._process
        return {
            "isolated": True,
            "running": bool(process and process.poll() is None and self._ready.is_set()),
            "device": "cpu",
            "engine": self._last_engine or self.engine,
            "gigaam_model": self.gigaam_model,
            "whisper_model": self.model,
            "fallback_reason": self._last_fallback,
            "fatal": self._fatal,
        }

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._ready.clear()
        if process and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.write(json.dumps({"id": "shutdown", "command": "shutdown"}) + "\n")
                    process.stdin.flush()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
