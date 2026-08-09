from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any

from .database import Database, utc_now


class MemoryStore:
    def __init__(
        self,
        db: Database,
        embedder: Any | None = None,
        embedding_model: str = "",
        semantic_enabled: bool | None = None,
    ):
        self.db = db
        self.embedder = embedder
        self.embedding_model = embedding_model
        # Directly constructed stores keep the previous behavior for tests/tools.
        # The application passes an explicit hardware-dependent value.
        self.semantic_enabled = (bool(embedder and embedding_model) if semantic_enabled is None else bool(semantic_enabled))
        self._embedding_disabled = False

    def _embed(self, text: str) -> list[float]:
        if (
            not self.semantic_enabled
            or not self.embedder
            or self._embedding_disabled
            or not text.strip()
        ):
            return []
        try:
            values = self.embedder.embed(text[:8000], model=self.embedding_model or None)
            return [float(value) for value in values]
        except Exception:
            # Memory lookup is an optimization and must never break the main chat.
            self._embedding_disabled = True
            return []

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        norm_left = math.sqrt(sum(a * a for a in left))
        norm_right = math.sqrt(sum(b * b for b in right))
        return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0

    def ensure_conversation(self, conversation_id: str | None, mode: str) -> str:
        conversation_id = conversation_id or uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(id, title, mode, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at
                """,
                (conversation_id, "Новый чат", mode, now, now),
            )
        return conversation_id

    def create_conversation(self, mode: str = "Друг", title: str = "Новый чат") -> str:
        conversation_id = uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO conversations(id, title, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, title.strip() or "Новый чат", mode, now, now),
            )
        return conversation_id

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.title, c.mode, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count,
                       COALESCE((SELECT content FROM messages mm
                                 WHERE mm.conversation_id=c.id
                                 ORDER BY mm.id DESC LIMIT 1), '') AS last_message
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id=c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_conversation(self, conversation_id: str, title: str) -> bool:
        title = title.strip()[:120]
        if not title:
            return False
        with self.db.connect() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, utc_now(), conversation_id),
            )
            return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: str) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            return cursor.rowcount > 0

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id, title, mode, created_at, updated_at FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages(conversation_id, role, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (utc_now(), conversation_id),
            )
            # First user message becomes a useful chat title without another model call.
            if role == "user":
                row = conn.execute(
                    "SELECT title, (SELECT COUNT(*) FROM messages WHERE conversation_id=?) AS n "
                    "FROM conversations WHERE id=?",
                    (conversation_id, conversation_id),
                ).fetchone()
                if row and row["title"] == "Новый чат" and int(row["n"]) <= 2:
                    clean = re.sub(r"\s+", " ", content).strip()
                    title = clean[:57] + ("…" if len(clean) > 57 else "")
                    conn.execute(
                        "UPDATE conversations SET title=? WHERE id=?", (title or "Новый чат", conversation_id)
                    )
            return int(cursor.lastrowid)

    def history(
        self,
        conversation_id: str,
        limit: int | None = 24,
        include_metadata: bool = False,
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            if limit is None:
                rows = conn.execute(
                    """
                    SELECT id, role, content, metadata, created_at FROM messages
                    WHERE conversation_id=? ORDER BY id
                    """,
                    (conversation_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, role, content, metadata, created_at FROM messages
                    WHERE conversation_id=? ORDER BY id DESC LIMIT ?
                    """,
                    (conversation_id, max(1, limit)),
                ).fetchall()
                rows = list(reversed(rows))
        output: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            if include_metadata:
                try:
                    item["metadata"] = json.loads(row["metadata"] or "{}")
                except json.JSONDecodeError:
                    item["metadata"] = {}
            output.append(item)
        return output

    def message_count(self, conversation_id: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def get_summary(self, conversation_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT summary, summarized_through_message_id, updated_at
                FROM conversation_summaries WHERE conversation_id=?
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_summary(
        self, conversation_id: str, summary: str, summarized_through_message_id: int
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_summaries(
                    conversation_id, summary, summarized_through_message_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary=excluded.summary,
                    summarized_through_message_id=excluded.summarized_through_message_id,
                    updated_at=excluded.updated_at
                """,
                (conversation_id, summary, summarized_through_message_id, utc_now()),
            )

    def unsummarized_messages(
        self, conversation_id: str, after_id: int, before_last: int = 12
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS max_id FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            max_id = int(max_row["max_id"] if max_row else 0)
            cutoff = max(0, max_id - before_last)
            rows = conn.execute(
                """
                SELECT id, role, content FROM messages
                WHERE conversation_id=? AND id>? AND id<=?
                ORDER BY id
                """,
                (conversation_id, after_id, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def add(
        self,
        content: str,
        kind: str = "fact",
        person: str = "",
        importance: int = 3,
        embedding: list[float] | None = None,
    ) -> int:
        content = content.strip()
        if not content:
            raise ValueError("Пустую память сохранить нельзя")
        if embedding is None:
            embedding = self._embed(content)
        now = utc_now()
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories(
                    kind, content, person, importance, embedding, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    content[:8000],
                    person.strip(),
                    max(1, min(5, importance)),
                    json.dumps(embedding or []),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, content, person, importance, created_at
                FROM memories ORDER BY importance DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, memory_id: int) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            return cursor.rowcount > 0

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 30))
        tokens = [
            token.lower()
            for token in re.findall(r"[\w-]{3,}", query, flags=re.UNICODE)
            if token.lower() not in {"который", "почему", "потому", "этого", "можешь"}
        ][:12]
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, content, person, importance, embedding, created_at
                FROM memories ORDER BY importance DESC, id DESC LIMIT 500
                """
            ).fetchall()
        if not rows:
            return []

        parsed: list[dict[str, Any]] = []
        has_vectors = False
        for row in rows:
            item = dict(row)
            try:
                vector = [float(value) for value in json.loads(item.pop("embedding") or "[]")]
            except (ValueError, TypeError, json.JSONDecodeError):
                vector = []
            item["_embedding"] = vector
            has_vectors = has_vectors or bool(vector)
            parsed.append(item)

        query_vector = self._embed(query) if has_vectors else []
        query_lower = query.lower()
        for item in parsed:
            content_lower = str(item["content"]).lower()
            lexical = sum(1 for token in tokens if token in content_lower)
            exact_bonus = 1.0 if query_lower and query_lower in content_lower else 0.0
            semantic = self._cosine(query_vector, item.pop("_embedding")) if query_vector else 0.0
            item["_score"] = semantic * 6.0 + lexical * 0.8 + exact_bonus + int(item["importance"]) * 0.12

        parsed.sort(key=lambda item: (item["_score"], item["importance"], item["id"]), reverse=True)
        output = []
        for item in parsed[:limit]:
            item.pop("_score", None)
            output.append(item)
        return output

    def remember_from_message(self, message: str) -> int | None:
        normalized = message.strip()
        patterns = (
            r"^(?:запомни|запомни, что|важно:|remember:)\s*(.+)$",
            r"^(?:мне нравится|я люблю|я предпочитаю)\s+(.+)$",
            r"^(?:меня зовут)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                return self.add(match.group(1).strip(), kind="user_fact", importance=4)
        return None

    def prompt_context(self, query: str) -> str:
        items = self.search(query)
        if not items:
            return "Долговременная память пока пуста."
        lines = []
        for item in items:
            person = f" [{item['person']}]" if item.get("person") else ""
            lines.append(f"- ({item['kind']}){person} {item['content']}")
        return "Релевантная память:\n" + "\n".join(lines)
