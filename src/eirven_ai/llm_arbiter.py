from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


class CompositeStop:
    """Small Event-compatible view over multiple threading.Events."""

    def __init__(self, *events: threading.Event | None):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


@dataclass(slots=True)
class Lease:
    priority: str
    preempt: threading.Event


class LLMArbiter:
    """Serialises local model access and lets interactive chat pre-empt background work.

    Ollama can technically queue/parallelise requests, but on mixed CPU/GPU laptops that
    often creates long stalls and model swapping. EIRVEN intentionally keeps one active
    generation and gives foreground chat priority. Background callers receive a preempt
    Event; when a chat arrives their streaming HTTP request is closed and transparently
    retried after the chat finishes.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.RLock())
        self._active: Lease | None = None
        self._waiting_interactive = 0

    @contextmanager
    def acquire(self, priority: str = "interactive") -> Iterator[Lease]:
        priority = "background" if priority == "background" else "interactive"
        lease = Lease(priority=priority, preempt=threading.Event())
        with self._cv:
            if priority == "interactive":
                self._waiting_interactive += 1
                # Latest owner turn wins. Pre-empt both background work and an older
                # interactive generation so five quick phrases do not queue behind phrase one.
                if self._active:
                    self._active.preempt.set()
                try:
                    while self._active is not None:
                        self._cv.wait(timeout=0.1)
                    self._active = lease
                finally:
                    self._waiting_interactive -= 1
            else:
                while self._active is not None or self._waiting_interactive > 0:
                    self._cv.wait(timeout=0.1)
                self._active = lease
        try:
            yield lease
        finally:
            with self._cv:
                if self._active is lease:
                    self._active = None
                self._cv.notify_all()

    def foreground_waiting(self) -> bool:
        with self._cv:
            return self._waiting_interactive > 0 or bool(
                self._active and self._active.priority == "interactive"
            )


GLOBAL_LLM_ARBITER = LLMArbiter()
