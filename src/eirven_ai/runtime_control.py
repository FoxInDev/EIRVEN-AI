from __future__ import annotations

import gc
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any

from .trace import log_event


@dataclass(slots=True)
class ActivitySnapshot:
    action: str = "idle"
    goal: str = ""
    step: str = ""
    lane: str = "idle"
    started_at: float = 0.0
    updated_at: float = 0.0
    elapsed_ms: int = 0
    cancellable: bool = False
    paused: bool = False
    last_result: str = ""


class RuntimeControl:
    """Global lightweight command center and latency telemetry.

    Deterministic commands update this state without touching the LLM. Long actions get a
    cancel generation token. New owner turns can invalidate older work while background
    project tasks keep their own TaskManager stop events.
    """

    def __init__(self, services: Any | None = None):
        self.services = services
        self._lock = threading.RLock()
        self._generation = 0
        self._cancel = threading.Event()
        self._paused = False
        self._activity = ActivitySnapshot(updated_at=time.time())
        self._perf: deque[dict[str, Any]] = deque(maxlen=120)
        self._camera_reset_lock = threading.Lock()
        # A user starting to speak while an interactive task is in flight is a
        # pre-commit signal.  We do not cancel on VAD alone (speaker echo/noise is
        # common), but irreversible GUI side effects must wait until ASR/policy has
        # decided whether the utterance is a cancel/new command or harmless chatter.
        self._voice_hold = threading.Event()
        self._voice_hold_started = 0.0

    def bind(self, services: Any) -> None:
        self.services = services

    def new_turn(self, text: str = "") -> tuple[int, threading.Event]:
        with self._lock:
            self._generation += 1
            old = self._cancel
            old.set()
            self._cancel = threading.Event()
            generation = self._generation
            self._voice_hold.clear()
            self._voice_hold_started = 0.0
        self._log("RUNTIME_INTERRUPT", generation=generation, query=str(text)[:400])
        return generation, self._cancel

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._cancel.is_set()

    def begin(self, action: str, goal: str, *, lane: str = "direct", cancellable: bool = True) -> int:
        generation, _ = self.new_turn(goal)
        now = time.time()
        with self._lock:
            self._activity = ActivitySnapshot(
                action=action or "work", goal=str(goal)[:500], step="Запуск", lane=lane,
                started_at=now, updated_at=now, cancellable=cancellable, paused=self._paused,
            )
        self._log("RUNTIME_BEGIN", generation=generation, action=action, goal=str(goal)[:800], lane=lane)
        return generation

    def step(self, text: str, **data: Any) -> None:
        now = time.time()
        with self._lock:
            activity = self._activity
            activity.step = str(text)[:500]
            activity.updated_at = now
            if activity.started_at:
                activity.elapsed_ms = int((now - activity.started_at) * 1000)
        self._log("RUNTIME_STEP", step=str(text)[:500], **data)

    def finish(self, result: str = "", *, ok: bool = True) -> None:
        now = time.time()
        with self._lock:
            activity = self._activity
            if activity.started_at:
                activity.elapsed_ms = int((now - activity.started_at) * 1000)
            activity.updated_at = now
            activity.step = "Готово" if ok else "Ошибка"
            activity.last_result = str(result)[:800]
            activity.cancellable = False
        self._log("RUNTIME_END", ok=ok, result=str(result)[:1000], elapsed_ms=self._activity.elapsed_ms)

    def status(self) -> dict[str, Any]:
        with self._lock:
            item = asdict(self._activity)
            item["generation"] = self._generation
            item["paused"] = self._paused
            if item.get("started_at") and item.get("cancellable"):
                item["elapsed_ms"] = int((time.time() - float(item["started_at"])) * 1000)
            return item

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._activity.paused = True
        self._log("RUNTIME_PAUSE")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._activity.paused = False
        self._log("RUNTIME_RESUME")

    def stop_interactive(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel.set()
            self._cancel = threading.Event()
            self._activity.cancellable = False
            self._activity.step = "Остановлено"
            self._activity.updated_at = time.time()
            self._voice_hold.clear()
            self._voice_hold_started = 0.0
        services = self.services
        if services is not None:
            try:
                cid = str(services.db.get_setting("native_voice_conversation", "") or "")
                if cid:
                    services.chat.stop(cid)
            except Exception:
                pass
            try:
                services.tools.stop()
                services.tools.reset_stop()
            except Exception:
                pass
        self._log("RUNTIME_STOP_INTERACTIVE")

    def voice_activity_started(self, *, rms: float = 1.0) -> bool:
        """Hold future side effects only for a credible owner speech onset.

        WebRTC VAD occasionally labels near-silence as speech. r15.9 opened the
        commit gate even for RMS around 0.00002-0.0002, which left the pointer
        hovering over Play/Add-to-cart while the click timed out. The accepted cancel in the Windows r15.9 log began around RMS 0.00058,
        while the false holds that blocked Play/Add-to-cart were around 0.00002-0.00023.
        Keep a conservative floor below the proven cancel onset and above those false holds.
        """
        try:
            level = float(rms)
        except Exception:
            level = 0.0
        if level < 0.0004:
            return False
        with self._lock:
            active = bool(
                self._activity.cancellable
                and self._activity.step not in {"Готово", "Ошибка", "Остановлено"}
            )
            if not active:
                return False
            self._voice_hold_started = time.monotonic()
            self._voice_hold.set()
            goal = self._activity.goal
        self._log("RUNTIME_PRECOMMIT_HOLD", goal=str(goal)[:400])
        return True

    def voice_activity_resolved(self, *, accepted: bool = False) -> None:
        with self._lock:
            was_set = self._voice_hold.is_set()
            self._voice_hold.clear()
            self._voice_hold_started = 0.0
        if was_set:
            self._log("RUNTIME_PRECOMMIT_RELEASE", accepted=bool(accepted))

    def voice_hold_active(self) -> bool:
        with self._lock:
            return self._voice_hold.is_set()

    def voice_hold_age(self) -> float:
        with self._lock:
            if not self._voice_hold.is_set() or not self._voice_hold_started:
                return 0.0
            return max(0.0, time.monotonic() - self._voice_hold_started)

    def stop_all(self) -> dict[str, int]:
        self.stop_interactive()
        cancelled = 0
        services = self.services
        if services is not None:
            try:
                for task in services.tasks.list(500):
                    if task.get("status") in {"queued", "running", "waiting_user"}:
                        if services.tasks.cancel(str(task.get("id") or "")):
                            cancelled += 1
            except Exception:
                pass
            try:
                for job in services.chat_jobs.list_active():
                    if job.get("status") in {"queued", "running"}:
                        if services.chat_jobs.cancel(str(job.get("id") or "")):
                            cancelled += 1
            except Exception:
                pass
        self._log("RUNTIME_STOP_ALL", cancelled=cancelled)
        return {"cancelled": cancelled}

    def record_perf(self, stage: str, ms: float, **meta: Any) -> None:
        row = {"ts": time.time(), "stage": str(stage), "ms": round(float(ms), 1), **meta}
        with self._lock:
            self._perf.append(row)
        self._log("PERF", **row)

    def performance(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._perf)[-max(1, min(int(limit), 120)):]

    def reset_after_camera(self) -> None:
        """Release only camera-local memory; never restart the assistant after camera use.

        r14 treats camera as a lightweight sensor. Unloading Ollama, resetting ASR/TTS or
        tearing down unrelated browser state here caused the severe post-camera latency
        seen in previous builds, so camera exit is deliberately isolated.
        """
        if not self._camera_reset_lock.acquire(blocking=False):
            return
        try:
            services = self.services
            if services is None:
                return
            try:
                gc.collect()
            except Exception:
                pass
            self._log("CAMERA_RESOURCE_RESET", lightweight=True, models_kept=True, voice_kept=True)
        finally:
            self._camera_reset_lock.release()

    def _log(self, event: str, **payload: Any) -> None:
        services = self.services
        if services is None:
            return
        try:
            log_event(services.settings.root_dir, event, **payload)
        except Exception:
            pass
