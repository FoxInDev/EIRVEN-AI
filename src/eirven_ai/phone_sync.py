from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .database import Database, utc_now


DEFAULT_REMINDERS = (120, 60, 30, 10)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    try:
        item = datetime.fromisoformat(str(value or ""))
        return item if item.tzinfo else item.astimezone()
    except Exception:
        return None


@dataclass(slots=True)
class ParsedEvent:
    title: str
    start: datetime
    end: datetime
    ambiguous: bool = False
    ambiguity_hour: int = 0
    ambiguity_minute: int = 0
    raw_query: str = ""


class PhoneSyncService:
    """Local organizer + phone sync bridge.

    The PC owns the canonical organizer state.  The Android client receives a small
    outbox over the already token-protected LAN API and can display notifications even
    when the chat view is not open.  No cloud account, API key or public endpoint is
    involved.
    """

    def __init__(self, db: Database):
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notifier: Callable[[str], None] | None = None
        self._init_schema()

    def _init_schema(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS phone_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'eirven',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_phone_notes_created
                ON phone_notes(created_at DESC);

                CREATE TABLE IF NOT EXISTS phone_events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    reminders TEXT NOT NULL DEFAULT '[120,60,30,10]',
                    source TEXT NOT NULL DEFAULT 'eirven',
                    native_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_phone_events_start
                ON phone_events(starts_at, status);

                CREATE TABLE IF NOT EXISTS phone_reminder_deliveries (
                    event_id TEXT NOT NULL,
                    minutes_before INTEGER NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, minutes_before),
                    FOREIGN KEY(event_id) REFERENCES phone_events(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS phone_external_reminder_deliveries (
                    native_id TEXT NOT NULL,
                    minutes_before INTEGER NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY(native_id, minutes_before)
                );

                CREATE TABLE IF NOT EXISTS phone_sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_phone_sync_pending
                ON phone_sync_outbox(acked_at, id);

                CREATE TABLE IF NOT EXISTS phone_calendar_snapshot (
                    native_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL DEFAULT 'legacy',
                    title TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phone_devices (
                    device_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT 'android',
                    paired_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    sync_cursor INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_phone_devices_active
                ON phone_devices(revoked_at, last_seen_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(phone_calendar_snapshot)"
                ).fetchall()
            }
            if "device_id" not in columns:
                conn.execute(
                    "ALTER TABLE phone_calendar_snapshot "
                    "ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy'"
                )
            # Upgrade events created by r44 and earlier without disturbing custom
            # reminder schedules chosen by the owner.
            conn.execute(
                "UPDATE phone_events SET reminders='[120,60,30,10]', updated_at=? "
                "WHERE reminders IN ('[120, 60, 10]', '[120,60,10]')",
                (utc_now(),),
            )

    def bind_notifier(self, callback: Callable[[str], None] | None) -> None:
        self._notifier = callback

    @staticmethod
    def _clean_device_id(device_id: str) -> str:
        value = "".join(
            char for char in str(device_id or "").strip()
            if char.isalnum() or char in {"-", "_", ".", ":"}
        )[:160]
        if len(value) < 8:
            raise ValueError("Некорректный идентификатор телефона")
        return value

    def pair_device(
        self,
        device_id: str,
        *,
        display_name: str = "",
        platform: str = "android",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Remember one native client until explicit revoke or privacy wipe."""
        clean_id = self._clean_device_id(device_id)
        now = utc_now()
        name = " ".join(str(display_name or "").split())[:120]
        platform_name = " ".join(str(platform or "android").split())[:40] or "android"
        encoded_metadata = json.dumps(metadata or {}, ensure_ascii=False, default=str)[:4000]
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO phone_devices(
                    device_id,display_name,platform,paired_at,last_seen_at,
                    revoked_at,sync_cursor,metadata
                ) VALUES(?,?,?,?,?,NULL,0,?)
                ON CONFLICT(device_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    platform=excluded.platform,
                    last_seen_at=excluded.last_seen_at,
                    revoked_at=NULL,
                    metadata=excluded.metadata
                """,
                (clean_id, name, platform_name, now, now, encoded_metadata),
            )
        return {
            "device_id": clean_id,
            "display_name": name,
            "platform": platform_name,
            "paired_at": now,
            "last_seen_at": now,
            "paired": True,
        }

    def touch_device(self, device_id: str) -> bool:
        try:
            clean_id = self._clean_device_id(device_id)
        except ValueError:
            return False
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE phone_devices SET last_seen_at=?
                WHERE device_id=? AND revoked_at IS NULL
                """,
                (utc_now(), clean_id),
            )
        return bool(cur.rowcount)

    def devices(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        where = "" if include_revoked else "WHERE revoked_at IS NULL"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT device_id,display_name,platform,paired_at,last_seen_at,
                       revoked_at,sync_cursor
                FROM phone_devices {where}
                ORDER BY last_seen_at DESC, paired_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_device(self, device_id: str) -> bool:
        clean_id = self._clean_device_id(device_id)
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE phone_devices SET revoked_at=?
                WHERE device_id=? AND revoked_at IS NULL
                """,
                (utc_now(), clean_id),
            )
        return bool(cur.rowcount)

    def revoke_all_devices(self) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE phone_devices SET revoked_at=? WHERE revoked_at IS NULL",
                (utc_now(),),
            )
        return max(0, int(cur.rowcount or 0))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="eirven-phone-organizer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.2)
        self._thread = None

    def _enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO phone_sync_outbox(kind,payload,created_at,acked_at) VALUES(?,?,?,NULL)",
                (str(kind), json.dumps(payload, ensure_ascii=False, default=str), utc_now()),
            )
            return int(cur.lastrowid)

    def add_note(self, text: str, *, source: str = "eirven") -> dict[str, Any]:
        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        if not clean:
            raise ValueError("Пустая заметка")
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO phone_notes(text,source,created_at) VALUES(?,?,?)",
                (clean[:12000], source, utc_now()),
            )
            note_id = int(cur.lastrowid)
        row = {"id": note_id, "text": clean[:12000], "source": source, "created_at": utc_now()}
        self._enqueue("note_upsert", row)
        return row

    def recent_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,text,source,created_at FROM phone_notes ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime | None = None,
        *,
        reminders: tuple[int, ...] | list[int] = DEFAULT_REMINDERS,
        source: str = "eirven",
    ) -> dict[str, Any]:
        clean = re.sub(r"\s+", " ", str(title or "")).strip() or "Событие"
        start = start.astimezone()
        end = (end or (start + timedelta(hours=1))).astimezone()
        if end <= start:
            end = start + timedelta(hours=1)
        reminder_values = sorted({max(0, int(x)) for x in reminders}, reverse=True)
        event_id = uuid.uuid4().hex
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO phone_events(id,title,starts_at,ends_at,reminders,source,native_id,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'active',?,?)""",
                (
                    event_id, clean[:400], _iso(start), _iso(end),
                    json.dumps(reminder_values), source, "", now, now,
                ),
            )
        payload = {
            "id": event_id,
            "title": clean[:400],
            "starts_at": _iso(start),
            "ends_at": _iso(end),
            "reminders": reminder_values,
            "source": source,
        }
        # A newer native shell can consume this packet and write the event directly to
        # CalendarContract. Older r37 WebView shells simply keep the EIRVEN calendar and
        # notifications working; the packet remains forward-compatible.
        self._enqueue("calendar_upsert", payload)
        return payload

    def events_between(self, start: datetime, end: datetime, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id,title,starts_at,ends_at,reminders,source,native_id,status,created_at,updated_at
                   FROM phone_events
                   WHERE status='active' AND starts_at>=? AND starts_at<?
                   ORDER BY starts_at ASC LIMIT ?""",
                (_iso(start), _iso(end), max(1, min(int(limit), 500))),
            ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            try:
                item["reminders"] = json.loads(item.get("reminders") or "[]")
            except Exception:
                item["reminders"] = list(DEFAULT_REMINDERS)
            result.append(item)
        return result

    def today(self) -> dict[str, Any]:
        now = _local_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        events = self.events_between(start, end)
        external = self._external_events(start, end, limit=200)
        merged: list[dict[str, Any]] = list(events)
        for item in external:
            ext = dict(item)
            ext["id"] = "phone:" + str(ext.get("native_id") or "")
            ext["source"] = "phone_calendar"
            ext["reminders"] = []
            merged.append(ext)
        merged.sort(key=lambda item: str(item.get("starts_at") or ""))
        return {
            "date": start.date().isoformat(),
            "events": merged,
            "notes": self.recent_notes(12),
        }

    def context_text(self) -> str:
        now = _local_now()
        upcoming = self.events_between(now - timedelta(minutes=5), now + timedelta(days=7), limit=30)
        notes = self.recent_notes(12)
        external = self._external_events(now, now + timedelta(days=7), limit=30)
        if not upcoming and not notes and not external:
            return ""
        lines = ["\n\nЛичный органайзер владельца (локальный телефон/календарь; используй только когда релевантно):"]
        if upcoming:
            lines.append("Ближайшие события EIRVEN:")
            for item in upcoming[:20]:
                start = _parse_iso(str(item.get("starts_at") or ""))
                stamp = start.strftime("%d.%m %H:%M") if start else str(item.get("starts_at") or "")
                lines.append(f"- {stamp} — {item.get('title')}")
        if external:
            lines.append("События, прочитанные из календаря телефона:")
            for item in external[:20]:
                start = _parse_iso(str(item.get("starts_at") or ""))
                stamp = start.strftime("%d.%m %H:%M") if start else str(item.get("starts_at") or "")
                lines.append(f"- {stamp} — {item.get('title')}")
        if notes:
            lines.append("Недавние заметки:")
            for item in notes[:10]:
                lines.append(f"- {str(item.get('text') or '')[:500]}")
        return "\n".join(lines)[:12000]

    def _external_events(self, start: datetime, end: datetime, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT native_id,device_id,title,starts_at,ends_at,captured_at
                   FROM phone_calendar_snapshot WHERE starts_at>=? AND starts_at<?
                   ORDER BY starts_at ASC LIMIT ?""",
                (_iso(start), _iso(end), max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_calendar_snapshot(
        self, events: list[dict[str, Any]], *, device_id: str = "legacy"
    ) -> int:
        clean_device_id = (
            self._clean_device_id(device_id)
            if device_id != "legacy"
            else "legacy"
        )
        now = utc_now()
        clean: list[tuple[str, str, str, str, str, str]] = []
        for index, item in enumerate(events[:1000]):
            if not isinstance(item, dict):
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:400]
            start = _parse_iso(str(item.get("starts_at") or ""))
            end = _parse_iso(str(item.get("ends_at") or "")) or (start + timedelta(hours=1) if start else None)
            if not title or not start or not end:
                continue
            native_id = str(item.get("native_id") or item.get("id") or f"snapshot-{index}-{int(start.timestamp())}")[:160]
            clean.append(
                (native_id, clean_device_id, title, _iso(start), _iso(end), now)
            )
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM phone_calendar_snapshot WHERE device_id=?",
                (clean_device_id,),
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO phone_calendar_snapshot(
                    native_id,device_id,title,starts_at,ends_at,captured_at
                ) VALUES(?,?,?,?,?,?)
                """,
                clean,
            )
        if clean_device_id != "legacy":
            self.touch_device(clean_device_id)
        return len(clean)

    def sync_payload(
        self, limit: int = 100, *, device_id: str = ""
    ) -> dict[str, Any]:
        clean_device_id = ""
        cursor = 0
        if str(device_id or "").strip():
            clean_device_id = self._clean_device_id(device_id)
            with self.db.connect() as conn:
                device = conn.execute(
                    """
                    SELECT sync_cursor FROM phone_devices
                    WHERE device_id=? AND revoked_at IS NULL
                    """,
                    (clean_device_id,),
                ).fetchone()
            if device is None:
                raise ValueError("Телефон не привязан или был отключён")
            cursor = max(0, int(device["sync_cursor"] or 0))
            self.touch_device(clean_device_id)
        with self.db.connect() as conn:
            if clean_device_id:
                rows = conn.execute(
                    """
                    SELECT id,kind,payload,created_at FROM phone_sync_outbox
                    WHERE id>? ORDER BY id ASC LIMIT ?
                    """,
                    (cursor, max(1, min(int(limit), 250))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id,kind,payload,created_at FROM phone_sync_outbox
                    WHERE acked_at IS NULL ORDER BY id ASC LIMIT ?
                    """,
                    (max(1, min(int(limit), 250)),),
                ).fetchall()
        outbox = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            outbox.append({"id": int(row["id"]), "kind": row["kind"], "payload": payload, "created_at": row["created_at"]})
        now = _local_now()
        # Full upcoming snapshot makes native calendar sync idempotent.  A phone that
        # was offline or still used the legacy WebView shell can install the enhanced
        # shell later and receive all active EIRVEN events without recreating them.
        calendar_events = self.events_between(now - timedelta(days=1), now + timedelta(days=45), limit=500)
        return {
            "ok": True,
            "schema_version": "eirven.phone-sync/v2",
            "device_id": clean_device_id,
            "paired": bool(clean_device_id),
            "server_time": _iso(now),
            "outbox": outbox,
            "today": self.today(),
            "calendar_events": calendar_events,
            "notes": self.recent_notes(500),
            "default_reminders": list(DEFAULT_REMINDERS),
        }

    def ack(self, ids: list[int], *, device_id: str = "") -> int:
        values = sorted({int(x) for x in ids if str(x).strip()})[:250]
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self.db.connect() as conn:
            if str(device_id or "").strip():
                clean_device_id = self._clean_device_id(device_id)
                device = conn.execute(
                    """
                    SELECT sync_cursor FROM phone_devices
                    WHERE device_id=? AND revoked_at IS NULL
                    """,
                    (clean_device_id,),
                ).fetchone()
                if device is None:
                    raise ValueError("Телефон не привязан или был отключён")
                row = conn.execute(
                    f"SELECT MAX(id) AS max_id, COUNT(*) AS n FROM phone_sync_outbox WHERE id IN ({placeholders})",
                    values,
                ).fetchone()
                acked = int(row["n"] if row else 0)
                max_id = int(row["max_id"] or 0) if row else 0
                if max_id:
                    conn.execute(
                        """
                        UPDATE phone_devices
                        SET sync_cursor=MAX(sync_cursor, ?), last_seen_at=?
                        WHERE device_id=? AND revoked_at IS NULL
                        """,
                        (max_id, utc_now(), clean_device_id),
                    )
                return acked
            cur = conn.execute(
                f"UPDATE phone_sync_outbox SET acked_at=? WHERE acked_at IS NULL AND id IN ({placeholders})",
                (utc_now(), *values),
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _day_from_text(text: str, now: datetime) -> datetime | None:
        low = text.casefold().replace("ё", "е")
        base = now.replace(second=0, microsecond=0)
        if "послезавтра" in low:
            return base + timedelta(days=2)
        if "завтра" in low:
            return base + timedelta(days=1)
        if "сегодня" in low:
            return base
        # dd.mm / dd.mm.yyyy
        m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", low)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            if year < 100:
                year += 2000
            try:
                return base.replace(year=year, month=month, day=day)
            except ValueError:
                return None
        # A bare clock time with no day word ("напомни в 18:00 позвонить") is the most
        # common phrasing of all. Refusing it here skipped the whole reminder path and
        # sent the request to the general agent, which cannot create events. Assume the
        # nearest sensible day: today if that time is still ahead, otherwise tomorrow.
        point = re.search(r"\bв\s+(\d{1,2})(?::(\d{2}))?\b", low)
        if point:
            hour, minute = int(point.group(1)), int(point.group(2) or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                candidate = base.replace(hour=hour, minute=minute)
                return candidate if candidate > base else candidate + timedelta(days=1)
        return None

    @staticmethod
    def _apply_daypart(hour: int, text: str) -> int:
        low = text.casefold().replace("ё", "е")
        if re.search(r"\bвечер(?:а|ом)?\b", low) and 1 <= hour <= 11:
            return hour + 12
        if re.search(r"\bдн(?:я|ем)\b", low) and 1 <= hour <= 6:
            return hour + 12
        if re.search(r"\bноч(?:и|ью)\b", low):
            if hour == 12:
                return 0
            if hour >= 8:
                return hour + 12 if hour < 12 else hour
        if re.search(r"\bутр(?:а|ом)?\b", low) and hour == 12:
            return 0
        return hour

    @staticmethod
    def _has_daypart(text: str) -> bool:
        return bool(re.search(r"\b(?:утр(?:а|ом)?|вечер(?:а|ом)?|дн(?:я|ем)|ноч(?:и|ью))\b", text, re.I))

    def _parse_event(self, query: str, *, force_daypart: str = "") -> ParsedEvent | None:
        now = _local_now()
        text = re.sub(r"\s+", " ", query).strip()

        # Relative offsets ("через час", "через 20 минут", "через полтора часа").
        # These carry no day word and no "в HH", so the absolute-time path below
        # cannot see them at all -- yet they are the most natural way to ask.
        relative = re.search(
            r"\bчерез\s+(?:(\d{1,3})\s*)?(минут\w*|час\w*|полчаса|полтора\s+часа)\b",
            text, re.I,
        )
        if relative:
            raw_amount, unit = relative.group(1), relative.group(2).casefold()
            if unit.startswith("полчаса"):
                delta = timedelta(minutes=30)
            elif unit.startswith("полтора"):
                delta = timedelta(minutes=90)
            elif unit.startswith("минут"):
                delta = timedelta(minutes=int(raw_amount or 1))
            else:
                delta = timedelta(hours=int(raw_amount or 1))
            start = (now + delta).replace(second=0, microsecond=0)
            title = re.sub(
                r"\b(?:напомни(?:\s+мне)?|поставь(?:\s+мне)?\s+напоминание)\b\s*[,;:]?\s*(?:что\s+)?",
                "", text, flags=re.I,
            )
            title = re.sub(
                r"\bчерез\s+(?:\d{1,3}\s*)?(?:минут\w*|час\w*|полчаса|полтора\s+часа)\b",
                "", title, flags=re.I,
            )
            title = re.sub(r"\s+", " ", title).strip(" ,.!;:-") or "Напоминание"
            return ParsedEvent(
                title=title, start=start, end=start + timedelta(minutes=30),
                ambiguous=False, ambiguity_hour=start.hour, ambiguity_minute=start.minute,
                raw_query=query,
            )

        day = self._day_from_text(text, now)
        if day is None:
            return None

        range_match = re.search(
            r"\b(?:с\s*)?(\d{1,2})(?::(\d{2}))?\s*(?:до|[-–—])\s*(\d{1,2})(?::(\d{2}))?\b",
            text, re.I,
        )
        point_match = re.search(r"\bв\s+(\d{1,2})(?::(\d{2}))?\b", text, re.I)
        if not range_match and not point_match:
            return None

        if range_match:
            h1, m1 = int(range_match.group(1)), int(range_match.group(2) or 0)
            h2, m2 = int(range_match.group(3)), int(range_match.group(4) or 0)
        else:
            h1, m1 = int(point_match.group(1)), int(point_match.group(2) or 0)  # type: ignore[union-attr]
            h2, m2 = h1 + 1, m1
        if not (0 <= h1 <= 23 and 0 <= m1 <= 59 and 0 <= h2 <= 24 and 0 <= m2 <= 59):
            return None

        daypart_text = f"{text} {force_daypart}".strip()
        ambiguous = False
        if not self._has_daypart(daypart_text) and 1 <= h1 <= 7:
            ambiguous = True
        h1 = self._apply_daypart(h1, daypart_text)
        h2 = self._apply_daypart(h2, daypart_text)
        if h2 == 24:
            h2 = 0
        start = day.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = day.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)

        title = text
        title = re.sub(r"^\s*(?:эрви|эрви|eirven)[,;:!\-]*\s*", "", title, flags=re.I)
        title = re.sub(
            r"\b(?:напомни(?:\s+мне)?|поставь(?:\s+мне)?\s+напоминание|"
            r"добавь(?:\s+мне)?\s+в\s+календарь|запиши(?:\s+мне)?\s+в\s+календарь)\b"
            r"\s*[,;:]?\s*(?:что\s+)?",
            "", title, flags=re.I,
        )
        title = re.sub(r"\b(?:сегодня|завтра|послезавтра)\b", "", title, flags=re.I)
        title = re.sub(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b", "", title)
        # Remove the time expression from the *current* string. Using a span captured
        # before the earlier substitutions corrupts phrases such as "у меня работа".
        title = re.sub(
            r"\b(?:с\s*)?\d{1,2}(?::\d{2})?\s*(?:до|[-–—])\s*\d{1,2}(?::\d{2})?\b",
            "", title, flags=re.I,
        )
        title = re.sub(r"\bв\s+\d{1,2}(?::\d{2})?\b", "", title, flags=re.I)
        title = re.sub(r"\b(?:утра|утром|вечера|вечером|дня|днем|ночью|ночи)\b", "", title, flags=re.I)
        title = re.sub(r"\bу\s+меня\b", "", title, flags=re.I)
        title = re.sub(r"[,;:]?\s*(?:запиши\s+это|добавь\s+это|поставь\s+это)(?:\s+в\s+календарь)?[.!]?\s*$", "", title, flags=re.I)
        title = re.sub(r"^\s*(?:что\s+)", "", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" ,.!;:-")
        if title.casefold() in {"работаю", "работать"}:
            title = "Работа"
        if not title:
            title = "Напоминание"
        return ParsedEvent(title=title, start=start, end=end, ambiguous=ambiguous, ambiguity_hour=int(range_match.group(1) if range_match else point_match.group(1)), ambiguity_minute=m1, raw_query=query)

    def _pending_key(self, conversation_id: str) -> str:
        return f"phone_organizer_pending:{conversation_id}"

    def _pending_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        pending = self.db.get_setting(self._pending_key(conversation_id), None)
        if not isinstance(pending, dict):
            return False, "", {}
        low = query.casefold().replace("ё", "е")
        daypart = ""
        if re.search(r"\b(?:утро|утра|утром)\b", low):
            daypart = "утра"
        elif re.search(r"\b(?:вечер|вечера|вечером)\b", low):
            daypart = "вечера"
        if not daypart:
            if re.search(r"\b(?:отмена|не надо|отмени)\b", low):
                self.db.set_setting(self._pending_key(conversation_id), None)
                return True, "Не стала добавлять событие.", {"action": "organizer_cancelled", "model": "deterministic"}
            return False, "", {}
        parsed = self._parse_event(str(pending.get("query") or ""), force_daypart=daypart)
        self.db.set_setting(self._pending_key(conversation_id), None)
        if parsed is None:
            return True, "Не смогла восстановить время. Скажи событие ещё раз.", {"action": "organizer_parse_failed", "needs_user": True, "model": "deterministic"}
        parsed.ambiguous = False
        event = self.add_event(parsed.title, parsed.start, parsed.end)
        return True, self._event_confirmation(event), {"action": "calendar_event_created", "event": event, "model": "deterministic"}

    @staticmethod
    def _event_confirmation(event: dict[str, Any]) -> str:
        start = _parse_iso(str(event.get("starts_at") or ""))
        when = start.strftime("%d.%m в %H:%M") if start else str(event.get("starts_at") or "")
        return (
            f"Добавила «{event.get('title')}» на {when}. "
            "Напоминания: за 2 часа, за час, за 30 и за 10 минут."
        )

    @staticmethod
    def _reminder_context(title: str, minutes: int) -> str:
        low = str(title or "").casefold().replace("ё", "е")
        if re.search(r"\b(?:рейс\w*|поезд\w*|аэропорт\w*|вокзал\w*|дорог\w*)", low):
            return " Проверь дорогу, билеты и время на сборы."
        if re.search(r"\b(?:встреч\w*|совещан\w*|презентац\w*|созвон\w*|интервью\w*)", low):
            if minutes >= 60:
                return " Можно заранее открыть материалы и проверить связь."
            return " Пора заканчивать текущую задачу и переключаться."
        if re.search(r"\b(?:врач\w*|клиник\w*|прием\w*|приём\w*|анализ\w*)", low):
            return " Проверь документы и нужные результаты."
        if re.search(r"\b(?:дедлайн\w*|сдать\w*|отправить\w*|экзамен\w*|тест\w*)", low) and minutes >= 30:
            return " Проверь готовность и оставь время на финальную проверку."
        return ""

    def handle_command(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        pending = self._pending_turn(query, conversation_id)
        if pending[0]:
            return pending
        clean = re.sub(r"^\s*(?:эрви|эрви|eirven)[,;:!\-]*\s*", "", query, flags=re.I).strip()
        low = clean.casefold().replace("ё", "е")

        note = re.match(
            r"^\s*(?:напиши|запиши|добавь|сохрани)(?:\s+мне)?\s+в\s+заметк(?:у|и)\s*(?:,|что)?\s+(.+)$",
            clean, re.I | re.S,
        )
        if note:
            note_text = re.sub(r"^\s*что\s+", "", note.group(1), flags=re.I).strip()
            item = self.add_note(note_text)
            return True, "Записала в заметки.", {"action": "phone_note_created", "note": item, "model": "deterministic"}

        if re.search(r"\b(?:покажи|прочитай|какие)\b.{0,30}\bзаметк", low) or re.fullmatch(r"(?:мои\s+)?заметки[?!. ]*", low):
            notes = self.recent_notes(12)
            if not notes:
                answer = "В заметках пока пусто."
            else:
                answer = "Последние заметки:\n" + "\n".join(f"• {str(item['text'])[:500]}" for item in notes[:8])
            return True, answer, {"action": "phone_notes_list", "model": "deterministic", "count": len(notes)}

        if re.search(r"\b(?:план\s+на\s+(?:сегодня|день)|что\s+у\s+меня\s+сегодня|что\s+сегодня\s+по\s+плану|события\s+на\s+сегодня)\b", low):
            today = self.today()
            events = today["events"]
            notes = today["notes"]
            parts = []
            if events:
                parts.append("Сегодня:\n" + "\n".join(
                    f"• {(_parse_iso(str(item['starts_at'])) or _local_now()).strftime('%H:%M')} — {item['title']}" for item in events
                ))
            else:
                parts.append("На сегодня событий в календаре EIRVEN нет.")
            if notes:
                parts.append("Из недавних заметок:\n" + "\n".join(f"• {str(item['text'])[:280]}" for item in notes[:5]))
            return True, "\n\n".join(parts), {"action": "phone_day_plan", "model": "deterministic", "today": today}

        # Explicit calendar/reminder wording, plus natural implicit phrases such as
        # "завтра в 18 у меня работа" and "сегодня с 7 до 8 работаю".
        has_temporal = bool(re.search(r"\b(?:сегодня|завтра|послезавтра)\b|\b\d{1,2}[./]\d{1,2}\b", low))
        has_time = bool(re.search(r"\bв\s+\d{1,2}(?::\d{2})?\b|\b(?:с\s*)?\d{1,2}(?::\d{2})?\s*(?:до|[-–—])\s*\d{1,2}(?::\d{2})?\b", low))
        has_relative = bool(re.search(r"\bчерез\s+(?:\d{1,3}\s*)?(?:минут\w*|час\w*|полчаса|полтора\s+часа)\b", low))
        explicit = bool(re.search(r"\b(?:напомни|напоминан|календар|событи|встреч|совещан|работа|работаю|прием|приём|запись)\w*", low))
        reminder_word = bool(re.search(r"\b(?:напомни|напоминан)\w*", low))
        # An explicit reminder request needs only one readable time expression; the
        # implicit "завтра в 18 у меня работа" form still needs both, so ordinary
        # sentences that merely mention a time are not turned into calendar events.
        if (reminder_word and (has_temporal or has_time or has_relative)) or (has_temporal and has_time and explicit):
            parsed = self._parse_event(clean)
            if parsed is not None:
                if parsed.ambiguous:
                    self.db.set_setting(self._pending_key(conversation_id), {"query": clean, "created_at": utc_now()})
                    h = parsed.ambiguity_hour
                    minute = parsed.ambiguity_minute
                    morning = f"{h % 24:02d}:{minute:02d}"
                    evening_h = h + 12 if 1 <= h <= 11 else h
                    evening = f"{evening_h % 24:02d}:{minute:02d}"
                    answer = f"Уточни время: {morning} утра или {evening} вечера?"
                    route = {
                        "action": "organizer_time_clarification",
                        "needs_user": True,
                        "model": "deterministic",
                        "ui_question": answer,
                        "ui_choices": [
                            {"label": f"Утра · {morning}", "value": "утра"},
                            {"label": f"Вечера · {evening}", "value": "вечера"},
                            {"label": "Отмена", "value": "отмена"},
                        ],
                    }
                    return True, answer, route
                event = self.add_event(parsed.title, parsed.start, parsed.end)
                return True, self._event_confirmation(event), {"action": "calendar_event_created", "event": event, "model": "deterministic"}

        return False, "", {}

    def _deliver_due(self) -> None:
        now = _local_now()
        # Include events up to two hours in the past only so a machine waking after a
        # long outage doesn't announce a week of stale reminders.
        events = self.events_between(now - timedelta(hours=2), now + timedelta(hours=3), limit=200)
        for external in self._external_events(now - timedelta(hours=2), now + timedelta(hours=3), limit=200):
            item = dict(external)
            item.update({
                "id": f"external:{item.get('native_id') or ''}",
                "source": "phone_calendar",
                "reminders": list(DEFAULT_REMINDERS),
            })
            events.append(item)
        for event in events:
            start = _parse_iso(str(event.get("starts_at") or ""))
            if start is None:
                continue
            reminders = event.get("reminders") or DEFAULT_REMINDERS
            for minutes in reminders:
                minutes = int(minutes)
                target = start - timedelta(minutes=minutes)
                if not (target <= now < target + timedelta(seconds=45)):
                    continue
                with self.db.connect() as conn:
                    if str(event.get("source") or "") == "phone_calendar":
                        native_id = str(event.get("native_id") or "")
                        seen = conn.execute(
                            "SELECT 1 FROM phone_external_reminder_deliveries WHERE native_id=? AND minutes_before=?",
                            (native_id, minutes),
                        ).fetchone()
                        if seen:
                            continue
                        conn.execute(
                            "INSERT INTO phone_external_reminder_deliveries(native_id,minutes_before,delivered_at) VALUES(?,?,?)",
                            (native_id, minutes, utc_now()),
                        )
                    else:
                        seen = conn.execute(
                            "SELECT 1 FROM phone_reminder_deliveries WHERE event_id=? AND minutes_before=?",
                            (event["id"], minutes),
                        ).fetchone()
                        if seen:
                            continue
                        conn.execute(
                            "INSERT INTO phone_reminder_deliveries(event_id,minutes_before,delivered_at) VALUES(?,?,?)",
                            (event["id"], minutes, utc_now()),
                        )
                if minutes >= 120:
                    prefix = "Через 2 часа"
                elif minutes >= 60:
                    prefix = "Через час"
                elif minutes == 30:
                    prefix = "Через 30 минут"
                elif minutes == 10:
                    prefix = "Через 10 минут"
                elif minutes:
                    prefix = f"Через {minutes} минут"
                else:
                    prefix = "Сейчас"
                text = f"{prefix}: {event.get('title')}." + self._reminder_context(str(event.get("title") or ""), minutes)
                self._enqueue("notification", {"title": "Эрви · напоминание", "text": text, "event_id": event["id"]})
                if self._notifier is not None:
                    try:
                        self._notifier(text)
                    except Exception:
                        pass

    def _run(self) -> None:
        # Align checks closely enough that the 10-minute reminder is never skipped by a
        # slow model turn.  This thread does not invoke the model and is practically free.
        while not self._stop.wait(15.0):
            try:
                self._deliver_due()
            except Exception:
                pass
