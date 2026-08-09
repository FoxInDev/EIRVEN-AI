from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


class TTSWorkerError(RuntimeError):
    pass


class TTSWorkerClient:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._ready = threading.Event()
        self._fatal = ""
        self._lock = threading.RLock()

    def _start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None and self._ready.is_set():
                return
            self.close()
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._ready.clear(); self._fatal = ""; self._stderr_lines.clear()
            self._process = subprocess.Popen(
                [sys.executable, "-u", "-m", "eirven_ai.tts_worker"],
                cwd=self.root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
            self._reader = threading.Thread(target=self._read_loop, daemon=True, name="eirven-tts-reader")
            self._reader.start()
            self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True, name="eirven-tts-stderr")
            self._stderr_reader.start()
        if not self._ready.wait(timeout=60):
            detail = self._fatal or "\n".join(self._stderr_lines)[-1200:] or "worker did not become ready"
            self.close()
            raise TTSWorkerError(f"Озвучивание не запустилось: {detail}")

    def _stderr_loop(self) -> None:
        p = self._process
        if not p or not p.stderr: return
        try:
            for raw in p.stderr:
                if raw.strip(): self._stderr_lines.append(raw.strip()[-600:])
        except Exception:
            pass

    def _read_loop(self) -> None:
        p = self._process
        if not p or not p.stdout: return
        try:
            for raw in p.stdout:
                try: msg = json.loads(raw)
                except json.JSONDecodeError: continue
                if msg.get("type") == "ready": self._ready.set(); continue
                if msg.get("type") == "fatal": self._fatal = str(msg.get("error") or "fatal"); self._ready.set(); continue
                rid = str(msg.get("id") or "")
                with self._lock: target = self._responses.get(rid)
                if target: target.put(msg)
        finally:
            self._ready.clear()
            with self._lock: pending = list(self._responses.values())
            for target in pending: target.put({"ok": False, "error": "Процесс озвучивания завершился"})

    def preload(self, model_path: str = "", *, engine: str = "piper_onnx", model_name: str = "", timeout: float = 120) -> None:
        self._start()
        rid = uuid.uuid4().hex
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._responses[rid] = q
            p = self._process
            if not p or not p.stdin or p.poll() is not None:
                raise TTSWorkerError("Процесс озвучивания недоступен")
            p.stdin.write(json.dumps({"id": rid, "command": "preload", "engine": engine, "model_path": model_path, "model_name": model_name}, ensure_ascii=True) + "\n")
            p.stdin.flush()
        try:
            response = q.get(timeout=timeout)
        except queue.Empty as exc:
            raise TTSWorkerError("Предзагрузка голоса заняла слишком много времени") from exc
        finally:
            with self._lock:
                self._responses.pop(rid, None)
        if not response.get("ok"):
            raise TTSWorkerError(str(response.get("error") or "Не удалось предзагрузить TTS"))

    def synthesize(
        self,
        text: str,
        model_path: str,
        profile: dict[str, Any],
        *,
        engine: str = "piper_onnx",
        speaker: str = "Ryan",
        model_name: str = "",
        instruction: str = "",
        design_prompt: str = "",
        timeout: float = 120,
    ) -> bytes:
        attempts = 1  # every utterance is bounded; the next utterance can restart a failed worker
        for attempt in range(attempts):
            try:
                self._start()
                rid = uuid.uuid4().hex
                q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
                with self._lock:
                    self._responses[rid] = q
                    p = self._process
                    if not p or not p.stdin or p.poll() is not None: raise TTSWorkerError("Процесс озвучивания недоступен")
                    p.stdin.write(json.dumps({"id": rid, "command": "synthesize", "text": text, "model_path": model_path, "profile": profile, "engine": engine, "speaker": speaker, "model_name": model_name, "instruction": instruction, "design_prompt": design_prompt}, ensure_ascii=True) + "\n")
                    p.stdin.flush()
                try:
                    response = q.get(timeout=timeout)
                except queue.Empty as exc:
                    self.close(); raise TTSWorkerError("Озвучивание заняло слишком много времени") from exc
                finally:
                    with self._lock: self._responses.pop(rid, None)
                if not response.get("ok"): raise TTSWorkerError(str(response.get("error") or "Неизвестная ошибка TTS"))
                return base64.b64decode(str(response.get("audio_b64") or ""), validate=True)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                self.close(); time.sleep(0.15)
        raise TTSWorkerError("Не удалось озвучить ответ")

    def status(self) -> dict[str, Any]:
        p = self._process
        return {"isolated": True, "running": bool(p and p.poll() is None and self._ready.is_set()), "fatal": self._fatal}

    def close(self) -> None:
        with self._lock:
            p = self._process; self._process = None; self._ready.clear()
        if p and p.poll() is None:
            try:
                if p.stdin:
                    p.stdin.write(json.dumps({"id": "shutdown", "command": "shutdown"}) + "\n"); p.stdin.flush()
                p.wait(timeout=2)
            except Exception:
                try: p.kill()
                except Exception: pass
