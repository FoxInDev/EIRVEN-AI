from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .database import Database, utc_now


class TaskCancelled(RuntimeError):
    pass


class TaskNeedsUser(RuntimeError):
    def __init__(self, prompt: str):
        super().__init__(prompt)
        self.prompt = prompt


@dataclass(slots=True)
class TaskContext:
    manager: "TaskManager"
    task_id: str
    stop_event: threading.Event
    started_monotonic: float
    conversation_id: str | None = None
    total_steps: int = 0
    completed_steps: int = 0

    def check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise TaskCancelled("Задача остановлена пользователем")

    def set_total(self, total_steps: int) -> None:
        self.total_steps = max(0, int(total_steps))
        self.manager._update_task(
            self.task_id,
            total_steps=self.total_steps,
            completed_steps=self.completed_steps,
        )

    def update(
        self,
        message: str,
        *,
        completed_steps: int | None = None,
        progress: float | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.check_cancelled()
        if completed_steps is not None:
            self.completed_steps = max(0, int(completed_steps))
        if progress is None and self.total_steps:
            progress = self.completed_steps / self.total_steps
        progress = max(0.0, min(float(progress or 0.0), 0.99))

        eta: int | None = None
        elapsed = time.monotonic() - self.started_monotonic
        if self.completed_steps > 0 and self.total_steps > self.completed_steps:
            average = elapsed / self.completed_steps
            eta = max(1, round(average * (self.total_steps - self.completed_steps)))

        self.manager._update_task(
            self.task_id,
            progress=progress,
            current_step=message,
            completed_steps=self.completed_steps,
            total_steps=self.total_steps,
            eta_seconds=eta,
        )
        self.manager._event(self.task_id, level, message, data or {})


TaskHandler = Callable[[TaskContext, dict[str, Any]], dict[str, Any] | str | None]


class TaskManager:
    # Operations that do not need a long LLM generation get their own lane so opening
    # an app is never stuck behind a 20-minute project build.
    FAST_KINDS = frozenset({
        # Interactive computer work must never sit behind a long project generation.
        # The LLM arbiter still gives live chat priority over an agent step.
        "application_launch", "identity_change", "crypto_price", "media_open",
        "git_action", "agent", "screen_query",
    })
    # r19 missions have their own coordinators. Multiple missions may therefore keep
    # background work moving at once, while MissionEngine's shared desktop lock still
    # serializes the one physical mouse/keyboard resource.
    MISSION_KINDS = frozenset({"mission"})

    def __init__(self, db: Database, max_workers: int = 1):
        self.db = db
        self.max_workers = max(1, min(int(max_workers), 4))
        self.handlers: dict[str, TaskHandler] = {}
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._workers: list[threading.Thread] = []
        self._running_stop: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def register(self, kind: str, handler: TaskHandler) -> None:
        self.handlers[kind] = handler

    def start(self) -> None:
        with self._lock:
            if self._workers:
                return
            self._shutdown.clear()
            fast = threading.Thread(
                target=self._worker,
                args=("fast",),
                daemon=True,
                name="eirven-task-fast",
            )
            self._workers.append(fast)
            fast.start()
            # Two lightweight mission coordinators are enough to make independent
            # missions concurrent without multiplying GUI contention.
            for index in range(2):
                thread = threading.Thread(
                    target=self._worker,
                    args=("mission",),
                    daemon=True,
                    name=f"eirven-task-mission-{index + 1}",
                )
                self._workers.append(thread)
                thread.start()
            for index in range(self.max_workers):
                thread = threading.Thread(
                    target=self._worker,
                    args=("heavy",),
                    daemon=True,
                    name=f"eirven-task-heavy-{index + 1}",
                )
                self._workers.append(thread)
                thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        self._wake.set()
        with self._lock:
            for event in self._running_stop.values():
                event.set()
        for thread in self._workers:
            thread.join(timeout=3)
        self._workers.clear()

    def enqueue(
        self,
        kind: str,
        title: str,
        payload: dict[str, Any],
        conversation_id: str | None = None,
    ) -> str:
        if kind not in self.handlers:
            raise ValueError(f"Нет обработчика задачи: {kind}")
        task_id = uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    id, kind, title, input, status, progress, current_step,
                    total_steps, completed_steps, result, error, conversation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, 'В очереди', 0, 0, '{}', '', ?, ?, ?)
                """,
                (
                    task_id,
                    kind,
                    title.strip()[:200] or "Задача",
                    json.dumps(payload, ensure_ascii=False),
                    conversation_id,
                    now,
                    now,
                ),
            )
        self._event(task_id, "info", "Задача добавлена в очередь", {})
        self._wake.set()
        return task_id

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._decode_task(dict(row)) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode_task(dict(row)) if row else None

    def append_live_instruction(self, task_id: str, instruction: str) -> bool:
        """Attach a correction to a queued/running project without cancelling its build."""
        instruction = str(instruction or "").strip()
        if not instruction:
            return False
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT input,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] not in {"queued", "running"}:
                return False
            try:
                payload = json.loads(row["input"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            items = payload.get("live_instructions")
            if not isinstance(items, list):
                items = []
            items.append(instruction[:12000])
            payload["live_instructions"] = items[-30:]
            conn.execute("UPDATE tasks SET input=?, updated_at=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), utc_now(), task_id))
        self._event(task_id, "info", "Принял правку во время выполнения", {"instruction": instruction[:500]})
        return True

    def live_instructions(self, task_id: str) -> list[str]:
        task = self.get(task_id)
        if not task:
            return []
        values = (task.get("input") or {}).get("live_instructions") or []
        return [str(item).strip() for item in values if str(item).strip()] if isinstance(values, list) else []

    def latest(
        self,
        *,
        kind: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at DESC, rowid DESC LIMIT 1",
                params,
            ).fetchone()
        return self._decode_task(dict(row)) if row else None

    def events(self, task_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, level, message, data, created_at
                FROM task_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?
                """,
                (task_id, after_id, max(1, min(limit, 1000))),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["data"] = json.loads(item.get("data") or "{}")
            except json.JSONDecodeError:
                item["data"] = {}
            output.append(item)
        return output

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            event = self._running_stop.get(task_id)
            if event:
                event.set()
                self._event(task_id, "warning", "Запрошена остановка", {})
                return True
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks SET status='cancelled', current_step='Отменено',
                    finished_at=?, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (utc_now(), utc_now(), task_id),
            )
        if cursor.rowcount:
            self._event(task_id, "warning", "Задача отменена до запуска", {})
            return True
        return False

    def latest_waiting(self, conversation_id: str | None = None) -> dict[str, Any] | None:
        clauses = ["status='waiting_user'"]
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                params,
            ).fetchone()
        return self._decode_task(dict(row)) if row else None

    def resume(self, task_id: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status='queued', current_step='Продолжаю после действия пользователя',
                    error='', updated_at=? WHERE id=? AND status='waiting_user'""",
                (utc_now(), task_id),
            )
        if cursor.rowcount:
            self._event(task_id, "info", "Пользователь подтвердил продолжение", {})
            self._wake.set()
            return True
        return False

    def retry(self, task_id: str) -> bool:
        """Requeue a failed/cancelled task with the same input.

        Project builders are checkpoint-aware, so this normally continues from the last
        written file instead of throwing away previous work.
        """
        with self.db.connect() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status='queued', progress=0, current_step='Повторный запуск',
                    completed_steps=0, eta_seconds=NULL, error='', finished_at=NULL,
                    started_at=NULL, updated_at=?
                    WHERE id=? AND status IN ('failed','cancelled')""",
                (utc_now(), task_id),
            )
        if cursor.rowcount:
            self._event(task_id, "info", "Задача поставлена на повторное выполнение", {})
            self._wake.set()
            return True
        return False

    @staticmethod
    def _decode_task(item: dict[str, Any]) -> dict[str, Any]:
        for key in ("input", "result"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                item[key] = {}
        return item

    def _claim(self, lane: str = "heavy") -> dict[str, Any] | None:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fast_kinds = sorted(self.FAST_KINDS)
            mission_kinds = sorted(self.MISSION_KINDS)
            fast_placeholders = ",".join("?" for _ in fast_kinds)
            mission_placeholders = ",".join("?" for _ in mission_kinds)
            if lane == "fast":
                row = conn.execute(
                    f"SELECT * FROM tasks WHERE status='queued' AND kind IN ({fast_placeholders}) "
                    "ORDER BY created_at, rowid LIMIT 1",
                    fast_kinds,
                ).fetchone()
            elif lane == "mission":
                row = conn.execute(
                    f"SELECT * FROM tasks WHERE status='queued' AND kind IN ({mission_placeholders}) "
                    "ORDER BY created_at, rowid LIMIT 1",
                    mission_kinds,
                ).fetchone()
            else:
                excluded = fast_kinds + mission_kinds
                placeholders = ",".join("?" for _ in excluded)
                row = conn.execute(
                    f"SELECT * FROM tasks WHERE status='queued' AND kind NOT IN ({placeholders}) "
                    "ORDER BY created_at, rowid LIMIT 1",
                    excluded,
                ).fetchone()
            if not row:
                return None
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE tasks SET status='running', started_at=?, updated_at=?,
                    current_step='Запуск'
                WHERE id=? AND status='queued'
                """,
                (now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["status"] = "running"
            claimed["started_at"] = now
            return self._decode_task(claimed)

    def _worker(self, lane: str = "heavy") -> None:
        while not self._shutdown.is_set():
            task = self._claim(lane)
            if task is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            task_id = task["id"]
            stop_event = threading.Event()
            with self._lock:
                self._running_stop[task_id] = stop_event
            started = time.monotonic()
            context = TaskContext(
                self, task_id, stop_event, started, task.get("conversation_id")
            )
            self._event(task_id, "info", "Выполнение началось", {})
            try:
                handler = self.handlers.get(task["kind"])
                if handler is None:
                    raise RuntimeError(f"Обработчик {task['kind']} не зарегистрирован")
                result = handler(context, task["input"])
                context.check_cancelled()
                elapsed = time.monotonic() - started
                self._finish(task_id, "done", result or {}, "", elapsed)
                self._event(
                    task_id,
                    "success",
                    f"Задача завершена за {self._human_duration(elapsed)}",
                    {},
                )
            except TaskNeedsUser as exc:
                elapsed = time.monotonic() - started
                with self.db.connect() as conn:
                    conn.execute(
                        """UPDATE tasks SET status='waiting_user', current_step=?, eta_seconds=NULL,
                            error='', updated_at=? WHERE id=?""",
                        (str(exc), utc_now(), task_id),
                    )
                self._event(task_id, "warning", str(exc), {"waiting_for_user": True})
                self._notify_conversation(
                    task.get("conversation_id"),
                    f"Нужно твоё действие: {exc}\n\nКогда закончишь, просто напиши «готово» — продолжу эту задачу.",
                    task_id,
                    "waiting_user",
                )
            except TaskCancelled as exc:
                elapsed = time.monotonic() - started
                self._finish(task_id, "cancelled", {}, str(exc), elapsed)
                self._event(task_id, "warning", str(exc), {})
                self._notify_conversation(
                    task.get("conversation_id"),
                    f"Задача «{task['title']}» остановлена.",
                    task_id,
                    "cancelled",
                )
            except Exception as exc:
                elapsed = time.monotonic() - started
                self._finish(task_id, "failed", {}, str(exc), elapsed)
                self._event(task_id, "error", f"Ошибка: {exc}", {})
                self._notify_conversation(
                    task.get("conversation_id"),
                    f"Не получилось завершить задачу «{task['title']}»: {exc}",
                    task_id,
                    "failed",
                )
            finally:
                with self._lock:
                    self._running_stop.pop(task_id, None)


    def _notify_conversation(
        self,
        conversation_id: str | None,
        content: str,
        task_id: str,
        status: str,
    ) -> None:
        if not conversation_id:
            return
        now = utc_now()
        metadata = json.dumps(
            {"task_id": task_id, "task_status": status}, ensure_ascii=False
        )
        try:
            with self.db.connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
                if not exists:
                    return
                conn.execute(
                    """
                    INSERT INTO messages(conversation_id, role, content, metadata, created_at)
                    VALUES (?, 'assistant', ?, ?, ?)
                    """,
                    (conversation_id, content, metadata, now),
                )
                conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now, conversation_id),
                )
        except Exception:
            return

    def _finish(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | str,
        error: str,
        elapsed: float,
    ) -> None:
        encoded = json.dumps(
            result if isinstance(result, dict) else {"message": result},
            ensure_ascii=False,
            default=str,
        )
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE tasks SET status=?, progress=?, current_step=?, eta_seconds=0,
                    result=?, error=?, finished_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    1.0 if status == "done" else 0.0,
                    "Готово" if status == "done" else ("Отменено" if status == "cancelled" else "Ошибка"),
                    encoded,
                    error,
                    utc_now(),
                    utc_now(),
                    task_id,
                ),
            )

    def _update_task(self, task_id: str, **values: Any) -> None:
        allowed = {
            "progress",
            "current_step",
            "total_steps",
            "completed_steps",
            "eta_seconds",
            "status",
        }
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        assignment = ", ".join(f"{key}=?" for key, _ in pairs)
        params = [value for _, value in pairs] + [task_id]
        with self.db.connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignment} WHERE id=?", params)

    def _event(
        self, task_id: str, level: str, message: str, data: dict[str, Any]
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, level, message, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, level, message, json.dumps(data, ensure_ascii=False), utc_now()),
            )

    @staticmethod
    def _human_duration(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        if seconds < 60:
            return f"{seconds} сек."
        minutes, seconds = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} мин. {seconds} сек."
        hours, minutes = divmod(minutes, 60)
        return f"{hours} ч. {minutes} мин."
