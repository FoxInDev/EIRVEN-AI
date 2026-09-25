# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import threading
import time
from typing import Any

from .trace import log_event


class TeachingSession:
    """Learn a task by watching the owner do it once.

    Deliberately built on UI-Automation focus sampling rather than a global mouse or
    keyboard hook. A hook would capture everything typed anywhere on the machine --
    passwords included -- for a feature that only needs to know *which controls* were
    used. Sampling the focused element gives the same signatures the operator already
    replays, and cannot record content the owner did not intend to share.

    The recorded elements are written to the same store the desktop operator reads
    first, so a taught sequence is tried before any blind search on the next attempt.
    """

    POLL_SECONDS = 0.6
    MAX_STEPS = 40
    MAX_MINUTES = 10

    def __init__(self, services: Any) -> None:
        self.services = services
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._goal = ""
        self._app = ""
        self._steps: list[dict[str, Any]] = []
        self._started_at = 0.0
        self._last_title = ""
        self._last_controls = ""

    # ------------------------------------------------------------------ state

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = bool(self._thread and self._thread.is_alive())
            return {
                "active": active,
                "goal": self._goal,
                "app": self._app,
                "steps": len(self._steps),
                "recorded": [
                    {"name": s.get("name", ""), "control_type": s.get("control_type", "")}
                    for s in self._steps
                ],
                "elapsed_seconds": round(time.monotonic() - self._started_at, 1) if active else 0.0,
            }

    # ------------------------------------------------------------------ control

    def start(self, goal: str) -> dict[str, Any]:
        goal = " ".join(str(goal or "").split()).strip()
        if not goal:
            return {"ok": False, "message": "Скажи, чему именно тебя учить."}
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "message": f"Я уже записываю: «{self._goal}». Скажи «готово», чтобы закончить."}
            self._goal = goal
            self._app = ""
            self._steps = []
            self._stop.clear()
            self._started_at = time.monotonic()
            self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-teaching")
            self._thread.start()
        log_event(self.services.settings.root_dir, "TEACHING_START", goal=goal)
        return {
            "ok": True,
            "message": (
                "Записываю. Покажи действия по порядку — я запоминаю, на какие элементы "
                "ты нажимаешь, но не то, что ты печатаешь. Когда закончишь, скажи «готово»."
            ),
        }

    def finish(self) -> dict[str, Any]:
        with self._lock:
            active = bool(self._thread and self._thread.is_alive())
            goal, app, steps = self._goal, self._app, list(self._steps)
        if not active and not steps:
            return {"ok": False, "message": "Сейчас я ничему не учусь."}
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=3.0)
        if not steps:
            log_event(self.services.settings.root_dir, "TEACHING_EMPTY", goal=goal)
            return {
                "ok": False,
                "message": "Я не увидела ни одного элемента. Попробуем ещё раз — открой нужное окно и кликай по кнопкам.",
            }
        stored = 0
        learning = getattr(self.services, "learning", None)
        for index, element in enumerate(steps):
            try:
                if learning is not None:
                    learning.remember(app or element.get("app", ""), f"{goal} #{index + 1}", element)
                    stored += 1
            except Exception:
                continue
        try:
            self.services.db.set_setting(
                f"taught_sequence:{goal.casefold()}",
                {"app": app, "goal": goal, "steps": steps},
            )
        except Exception:
            pass
        log_event(self.services.settings.root_dir, "TEACHING_SAVED", goal=goal, steps=len(steps), stored=stored)
        names = ", ".join(s.get("name", "") for s in steps[:4] if s.get("name"))
        return {
            "ok": True,
            "steps": len(steps),
            "message": (
                f"Запомнила «{goal}»: {len(steps)} шаг(ов)"
                + (f" — {names}" if names else "")
                + ". В следующий раз попробую сама."
            ),
        }

    def cancel(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            self._steps = []
        return {"ok": True, "message": "Обучение отменено, ничего не сохранила."}

    # ------------------------------------------------------------------ capture

    def _run(self) -> None:
        deadline = time.monotonic() + self.MAX_MINUTES * 60
        last_signature = ""
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                snapshot = self.services.tools.execute("window_elements", {"max_elements": 120})
                result = snapshot.get("result") if isinstance(snapshot, dict) else None
                # window_elements returns a list of element dicts. Treating it as a
                # mapping raised AttributeError on every poll, which this method's
                # broad except then hid -- recording silently captured nothing.
                if isinstance(result, dict):
                    elements = result.get("elements") or []
                elif isinstance(result, list):
                    elements = result
                else:
                    elements = []
                title = ""
                try:
                    fg = self.services.tools.execute("foreground_window", {})
                    fg_result = fg.get("result") if isinstance(fg, dict) else None
                    if isinstance(fg_result, dict):
                        title = str(fg_result.get("title") or "")
                except Exception:
                    title = ""
                # Смена окна — тоже шаг. Раньше записывались только элементы,
                # получившие фокус клавиатуры, а кнопка в браузере его не
                # забирает: фокус остаётся на документе. Поэтому человек кликал,
                # а не записывалось ничего.
                if title and title != self._last_title:
                    self._last_title = title
                    with self._lock:
                        if not self._app:
                            self._app = title
                        if len(self._steps) < self.MAX_STEPS:
                            self._steps.append({
                                "name": title, "control_type": "Window",
                                "automation_id": "", "class_name": "", "app": title,
                                "kind": "window",
                            })

                focused = None
                for item in elements:
                    if isinstance(item, dict) and item.get("focused"):
                        focused = item
                        break
                # Если фокус никуда не перешёл, запоминаем управляющие элементы
                # окна: для повтора действия важно, что здесь вообще было.
                if focused is None and elements:
                    clickable = [e for e in elements
                                 if isinstance(e, dict)
                                 and str(e.get("control_type") or "").casefold() in
                                 {"button", "hyperlink", "menuitem", "tabitem", "listitem"}
                                 and str(e.get("name") or "").strip()]
                    if clickable:
                        sig = "|".join(str(e.get("name") or "")[:40] for e in clickable[:6])
                        if sig != self._last_controls:
                            self._last_controls = sig
                            with self._lock:
                                if len(self._steps) < self.MAX_STEPS:
                                    self._steps.append({
                                        "name": clickable[0].get("name") or "",
                                        "control_type": clickable[0].get("control_type") or "",
                                        "automation_id": clickable[0].get("automation_id") or "",
                                        "class_name": clickable[0].get("class_name") or "",
                                        "app": title, "kind": "control",
                                    })
                if focused:
                    signature = "|".join(str(focused.get(k) or "") for k in
                                         ("name", "control_type", "automation_id", "class_name"))
                    # Only record a control the first time focus lands on it: polling
                    # would otherwise store the same button dozens of times.
                    if signature.strip("|") and signature != last_signature:
                        last_signature = signature
                        with self._lock:
                            if not self._app and title:
                                self._app = title
                            if len(self._steps) < self.MAX_STEPS:
                                self._steps.append({
                                    "name": str(focused.get("name") or ""),
                                    "control_type": str(focused.get("control_type") or ""),
                                    "automation_id": str(focused.get("automation_id") or ""),
                                    "class_name": str(focused.get("class_name") or ""),
                                    "app": title,
                                })
            except Exception:
                pass
            self._stop.wait(self.POLL_SECONDS)
