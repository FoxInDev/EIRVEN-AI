from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .chat import ChatService
from .database import Database, utc_now


class ChatJobManager:
    """Persistent chat generations independent from a browser connection.

    A job continues if the user switches chats, reloads the page, or closes the UI.
    A new message supersedes only active generations in the same conversation.
    """

    def __init__(self, db: Database, chat: ChatService, max_workers: int = 2):
        self.db = db
        self.chat = chat
        self.max_workers = max(1, min(int(max_workers), 2))
        self._executor: ThreadPoolExecutor | None = None
        self._stops: dict[str, threading.Event] = {}
        self._submitted: set[str] = set()
        self._lock = threading.RLock()
        self._shutdown = False

    def start(self) -> None:
        with self._lock:
            if self._executor is not None:
                return
            self._shutdown = False
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="eirven-chat",
            )
        # Jobs interrupted by a server crash are safe to retry because the user turn
        # is persisted before generation and stream_events(persist_user=False) is idempotent.
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM chat_jobs WHERE status='queued' ORDER BY created_at, rowid"
            ).fetchall()
        for row in rows:
            self._submit(row["id"])

    def stop(self) -> None:
        with self._lock:
            self._shutdown = True
            for event in self._stops.values():
                event.set()
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=False)

    def enqueue(
        self,
        *,
        message: str,
        conversation_id: str | None,
        mode: str,
        model: str | None,
        image_paths: list[str] | None,
        attachment_paths: list[str] | None = None,
        voice_mode: bool = False,
        supersede_same_chat: bool = True,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        if client_request_id:
            existing = self.get_by_client_request_id(client_request_id)
            if existing:
                return existing
        message = message.strip()
        if not message:
            raise ValueError("Введите сообщение")
        conversation_id = self.chat.memory.ensure_conversation(conversation_id, mode)

        if supersede_same_chat:
            self.cancel_conversation(conversation_id, reason="Новая реплика в этом чате")

        user_message_id = self.chat.memory.add_message(
            conversation_id,
            "user",
            message,
            metadata={"images": image_paths or [], "attachments": attachment_paths or [], "source": "voice" if voice_mode else "text"},
        )
        if self.chat.settings.auto_memory:
            self.chat.memory.remember_from_message(message)

        job_id = uuid.uuid4().hex
        now = utc_now()
        request = {
            "message": message,
            "mode": mode,
            "model": model or "auto",
            "image_paths": image_paths or [],
            "attachment_paths": attachment_paths or [],
            "voice_mode": bool(voice_mode),
            "user_message_id": user_message_id,
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_jobs(
                    id, conversation_id, request, status, partial, answer, error,
                    route, voice_mode, created_at, updated_at, client_request_id
                ) VALUES (?, ?, ?, 'queued', '', '', '', '{}', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    conversation_id,
                    json.dumps(request, ensure_ascii=False),
                    int(bool(voice_mode)),
                    now,
                    now,
                    client_request_id,
                ),
            )
        self._submit(job_id)
        return self.get(job_id) or {"id": job_id, "conversation_id": conversation_id}

    def get_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_jobs WHERE client_request_id=? LIMIT 1",
                (client_request_id,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def create_completed(
        self,
        *,
        conversation_id: str,
        answer: str,
        action: str = "task_created",
        task_id: str | None = None,
        client_request_id: str | None = None,
        voice_mode: bool = False,
    ) -> dict[str, Any]:
        if client_request_id:
            existing = self.get_by_client_request_id(client_request_id)
            if existing:
                return existing
        job_id = uuid.uuid4().hex
        now = utc_now()
        route = {"action": action, "task_id": task_id}
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_jobs(
                    id, conversation_id, request, status, partial, answer, error,
                    route, voice_mode, created_at, started_at, finished_at, updated_at, client_request_id
                ) VALUES (?, ?, '{}', 'done', ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    conversation_id,
                    answer,
                    answer,
                    json.dumps(route, ensure_ascii=False),
                    int(bool(voice_mode)),
                    now,
                    now,
                    now,
                    now,
                    client_request_id,
                ),
            )
        return self.get(job_id) or {"id": job_id, "conversation_id": conversation_id}

    def _submit(self, job_id: str) -> None:
        with self._lock:
            if self._shutdown or job_id in self._submitted:
                return
            executor = self._executor
            if executor is None:
                return
            self._submitted.add(job_id)
            executor.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        stop_event = threading.Event()
        with self._lock:
            self._stops[job_id] = stop_event
        try:
            with self.db.connect() as conn:
                row = conn.execute("SELECT * FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["status"] != "queued":
                    return
                now = utc_now()
                updated = conn.execute(
                    """
                    UPDATE chat_jobs SET status='running', started_at=?, updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (now, now, job_id),
                )
                if updated.rowcount != 1:
                    return
                request = json.loads(row["request"] or "{}")
                conversation_id = row["conversation_id"]

            partial = ""
            route: dict[str, Any] = {}
            last_write = 0.0
            final_event: dict[str, Any] | None = None
            stream_kwargs = {
                "image_paths": list(request.get("image_paths") or []),
                "persist_user": False,
                "external_stop_event": stop_event,
                "persist_assistant": False,
            }
            # Keep compatibility with lightweight/custom ChatService implementations
            # that predate generic attachments; only pass the new keyword when used.
            attachment_paths = list(request.get("attachment_paths") or [])
            if attachment_paths:
                stream_kwargs["attachment_paths"] = attachment_paths
            for event in self.chat.stream_events(
                request.get("message", ""),
                conversation_id,
                request.get("mode", "Друг"),
                request.get("model", "auto"),
                **stream_kwargs,
            ):
                kind = event.get("type")
                if kind == "start":
                    route = dict(event.get("route") or {})
                    self._update(job_id, route=json.dumps(route, ensure_ascii=False))
                elif kind == "token":
                    partial = str(event.get("full") or partial + str(event.get("content") or ""))
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_write >= 0.12:
                        self._update(job_id, partial=partial)
                        last_write = now_monotonic
                elif kind == "error":
                    self._finish(job_id, "failed", partial, str(event.get("message") or "Ошибка"), route, {})
                    return
                elif kind == "done":
                    final_event = event

            stopped = bool(stop_event.is_set() or (final_event or {}).get("stopped"))
            answer = str((final_event or {}).get("answer") or partial)
            metrics = dict((final_event or {}).get("metrics") or {})
            if stopped:
                self._finish(job_id, "cancelled", answer, "Ответ прерван новой репликой или пользователем", route, metrics)
            else:
                self._finish(job_id, "done", answer, "", route, metrics)
        except Exception as exc:
            self._finish(job_id, "failed", "", str(exc), {}, {})
        finally:
            with self._lock:
                self._stops.pop(job_id, None)
                self._submitted.discard(job_id)

    def _update(self, job_id: str, **values: Any) -> None:
        allowed = {"partial", "route"}
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        assignment = ", ".join(f"{key}=?" for key, _ in pairs)
        with self.db.connect() as conn:
            conn.execute(
                f"UPDATE chat_jobs SET {assignment} WHERE id=?",
                [value for _, value in pairs] + [job_id],
            )

    def _finish(
        self,
        job_id: str,
        status: str,
        answer: str,
        error: str,
        route: dict[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Finish a chat job and persist its assistant turn atomically.

        This prevents duplicate assistant messages if FastAPI dies after generation but
        before the job status is written: message insertion and status transition commit
        in the same SQLite transaction.
        """
        now = utc_now()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, assistant_message_id FROM chat_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            assistant_message_id = int(row["assistant_message_id"]) if row and row["assistant_message_id"] else None
            if status == "done" and answer and row and assistant_message_id is None:
                metadata = {
                    "model": route.get("model") or "",
                    "metrics": metrics or {},
                    "chat_job_id": job_id,
                }
                cursor = conn.execute(
                    """
                    INSERT INTO messages(conversation_id, role, content, metadata, created_at)
                    VALUES (?, 'assistant', ?, ?, ?)
                    """,
                    (row["conversation_id"], answer, json.dumps(metadata, ensure_ascii=False), now),
                )
                assistant_message_id = int(cursor.lastrowid)
                conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now, row["conversation_id"]),
                )
            conn.execute(
                """
                UPDATE chat_jobs SET status=?, partial=?, answer=?, error=?, route=?,
                    assistant_message_id=?, finished_at=?, updated_at=? WHERE id=?
                """,
                (
                    status, answer, answer, error, json.dumps(route, ensure_ascii=False),
                    assistant_message_id, now, now, job_id,
                ),
            )

    def cancel(self, job_id: str, reason: str = "Остановлено пользователем") -> bool:
        with self._lock:
            event = self._stops.get(job_id)
            if event:
                event.set()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, status FROM chat_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row or row["status"] not in {"queued", "running"}:
                return False
            conn.execute(
                """
                UPDATE chat_jobs SET status='cancelled', error=?, finished_at=?, updated_at=?
                WHERE id=? AND status IN ('queued','running')
                """,
                (reason, utc_now(), utc_now(), job_id),
            )
        if row["conversation_id"]:
            self.chat.stop(row["conversation_id"])
        return True

    def cancel_conversation(self, conversation_id: str, reason: str) -> int:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM chat_jobs
                WHERE conversation_id=? AND status IN ('queued','running')
                """,
                (conversation_id,),
            ).fetchall()
        count = 0
        for row in rows:
            if self.cancel(row["id"], reason=reason):
                count += 1
        return count

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(dict(row)) if row else None

    def list_active(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM chat_jobs WHERE status IN ('queued','running')"
        params: list[Any] = []
        if conversation_id:
            query += " AND conversation_id=?"
            params.append(conversation_id)
        query += " ORDER BY created_at, rowid"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode(dict(row)) for row in rows]

    @staticmethod
    def _decode(item: dict[str, Any]) -> dict[str, Any]:
        for field in ("request", "route"):
            try:
                item[field] = json.loads(item.get(field) or "{}")
            except json.JSONDecodeError:
                item[field] = {}
        item["voice_mode"] = bool(item.get("voice_mode"))
        return item
