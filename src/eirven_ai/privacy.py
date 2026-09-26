from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


# Child tables come first so the wipe also works when a SQLite build refuses to
# disable foreign keys for an already-open transaction.
PERSONAL_TABLES = (
    "phone_devices",
    "phone_reminder_deliveries",
    "phone_external_reminder_deliveries",
    "phone_sync_outbox",
    "phone_calendar_snapshot",
    "phone_events",
    "phone_notes",
    "task_events",
    "chat_jobs",
    "action_logs",
    "performance_samples",
    "attachments",
    "conversation_summaries",
    "messages",
    "tasks",
    "relationships",
    "memories",
    "conversations",
    "settings",
)


def _remove_path(path: Path) -> bool:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        return True
    except OSError:
        # A current Windows log/session file may still be held for the few hundred
        # milliseconds before shutdown. The database has already forgotten its entry;
        # the next privacy wipe/start can remove the stale file.
        return False


def wipe_personal_data(services: Any, *, preserve_entitlement: bool = True) -> dict[str, int | bool]:
    """Forget EIRVEN's owner-specific state while preserving installed runtimes/models.

    User-created workspace/video results are deliberately not deleted: they are ordinary
    files owned by the user, not assistant memory. The server-issued update entitlement
    may be retained so clearing chat history does not destroy a paid delivery right.
    """

    stopped = 0
    for component_name in (
        "voice_daemon",
        "proactive",
        "telegram",
        "phone_sync",
        "chat_jobs",
        "tasks",
        "updater",
    ):
        component = getattr(services, component_name, None)
        stop = getattr(component, "stop", None)
        if callable(stop):
            try:
                stop()
                stopped += 1
            except Exception:
                pass

    deleted_rows = 0
    with services.db.connect() as conn:
        existing = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in PERSONAL_TABLES:
            if table not in existing:
                continue
            cursor = conn.execute(f'DELETE FROM "{table}"')
            deleted_rows += max(0, int(cursor.rowcount or 0))
        if "sqlite_sequence" in existing:
            conn.execute("DELETE FROM sqlite_sequence")

    data_dir = Path(services.settings.data_dir).resolve()
    database = Path(services.db.path).resolve()
    keep = {database.name, f"{database.name}-wal", f"{database.name}-shm"}
    if preserve_entitlement:
        keep.add("distribution.json")

    deleted_paths = 0
    if data_dir.is_dir():
        for child in data_dir.iterdir():
            if child.name in keep:
                continue
            deleted_paths += int(_remove_path(child))

    # Runtime traces can contain snippets of prompts, window titles and performance
    # diagnostics, so a privacy wipe clears them too.
    log_dir = Path(services.settings.root_dir).resolve() / "logs"
    if log_dir.is_dir():
        for child in log_dir.iterdir():
            deleted_paths += int(_remove_path(child))

    try:
        with services.db.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    return {
        "ok": True,
        "deleted_rows": deleted_rows,
        "deleted_paths": deleted_paths,
        "stopped_components": stopped,
        "entitlement_preserved": bool(preserve_entitlement),
        "created_files_preserved": True,
    }
