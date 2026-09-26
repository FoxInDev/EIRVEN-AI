from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher
from collections.abc import Generator
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from .config import Settings
from .app_skills import AppSkills
from .human_errors import humanize, explain
from .database import Database
from .llm import LLMError, ModelGateway
from .identity import CANONICAL_ASSISTANT_NAME, IdentityService
from .memory import MemoryStore
from .education_library import EducationLibrary
from .model_router import ModelRouter
from .style import StyleStore

from .attachments import extract_attachment_context
from .intent_engine import detect_command, detect_commands
from .trace import log_event
from .russian_speech import cardinal, time_phrase, date_phrase, russian_weather_condition
from .reliability_router import ReliabilityRouter
from .dialogue import (
    is_affirmative_confirmation,
    is_cancel_confirmation,
    is_chat_pairing_request,
    is_mobile_app_setup_request,
    is_pc_shutdown_cancel_request,
    is_pc_shutdown_request,
    is_phone_setup_request,
    is_resume_confirmation,
    normalize_phrase,
)

if TYPE_CHECKING:
    from .tools import ToolExecutor
    from .tasks import TaskManager



MODE_PROMPTS = {
    "Друг": (
        "Режим обычного общения. Реагируй живо и естественно. Не превращай каждую реплику "
        "в лекцию или список. Иногда достаточно короткой человеческой реакции."
    ),
    "Архитектор": (
        "Разложи проблему, проверь предположения, предложи варианты, риски и конкретный "
        "следующий шаг."
    ),
    "Разработчик": (
        "Помогай как сильный Python-разработчик. Давай исполняемый код и проверки. "
        "Не выдумывай результат запуска."
    ),
    "Контрарный": (
        "Сначала найди слабое место очевидного решения, затем предложи неочевидную "
        "альтернативу и честное мнение."
    ),
}


PendingKind = Literal["workflow", "mail_send", "messenger_send", "power"]


@dataclass(frozen=True, slots=True)
class PendingActionOwner:
    """The sole confirmation owner for one conversation and one exact payload."""

    kind: PendingKind
    conversation_id: str
    fingerprint: str
    created_at: float


class ChatService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        gateway: ModelGateway,
        router: ModelRouter,
        memory: MemoryStore,
        style: StyleStore,
        identity: IdentityService | None = None,
    ):
        self.settings = settings
        self.db = db
        self.gateway = gateway
        self.router = router
        self.memory = memory
        self.style = style
        self.identity = identity
        self.education = EducationLibrary(settings.root_dir / "education" / "corpus" / "library.sqlite3")
        self._stop_events: dict[str, threading.Event] = {}
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._turn_context = threading.local()
        self.tools: ToolExecutor | None = None
        self.tasks: TaskManager | None = None
        self.modes: Any = None
        self.camera: Any = None
        self.voice: Any = None
        self._media_cache: dict[str, tuple[int, int, str, list[str]]] = {}
        self.runtime: Any = None
        self.capabilities: Any = None
        self.offline_cache: Any = None
        self.learning: Any = None
        self.desktop_operator: Any = None
        self.app_skills: Any = None
        self.universal_workflow: Any = None
        self.autonomous_workflow: Any = None
        self.mission_engine: Any = None
        self.cognition: Any = None
        self.telegram: Any = None
        self.video: Any = None
        self.phone_sync: Any = None
        self.teaching: Any = None
        self.food: Any = None
        self.opener: Any = None
        # Имя намеренно отличается от self.router: там уже живёт выбор
        # модели (ModelRouter с методом chat_route), и совпадение имён
        # затирало его — обычный разговор переставал работать вообще.
        self.request_router: Any = None
        self.bridge: Any = None
        self.emotions: Any = None
        self.planner: Any = None
        self.recovery: Any = None
        self.mail: Any = None
        self.reliability_router = ReliabilityRouter()

    def attach_runtime(
        self, tools: "ToolExecutor", tasks: "TaskManager", *,
        modes: Any = None, camera: Any = None, voice: Any = None,
    ) -> None:
        self.tools = tools
        self.tasks = tasks
        self.modes = modes
        self.camera = camera
        self.voice = voice

    def stop(self, conversation_id: str) -> bool:
        with self._lock:
            event = self._stop_events.get(conversation_id)
            if event:
                event.set()
                return True
        return False

    def _conversation_lock(self, conversation_id: str) -> threading.Lock:
        with self._lock:
            return self._conversation_locks.setdefault(conversation_id, threading.Lock())

    def _turn_conversation_id(self, conversation_id: str = "") -> str:
        return str(conversation_id or getattr(self._turn_context, "conversation_id", "") or "")

    @staticmethod
    def _pending_fingerprint(kind: str, conversation_id: str, payload: dict[str, Any]) -> str:
        clean_payload = {
            str(key): value for key, value in dict(payload or {}).items()
            if str(key) not in {"fingerprint", "created_at", "expires_at", "at", "schema", "kind", "conversation_id"}
        }
        raw = json.dumps(
            {"kind": str(kind), "conversation_id": str(conversation_id), "payload": clean_payload},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _messenger_pending_key(conversation_id: str) -> str:
        digest = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()[:24]
        return f"chat_pending_messenger_v2:{digest}"

    @staticmethod
    def _coerce_window_id(value: Any) -> int:
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _resolve_messenger_recipient(self, recipient: str, platform: str) -> str:
        resolved = str(recipient or "").strip()
        platform_n = str(platform or "").casefold().replace("ё", "е")
        if platform_n not in {"telegram", "телеграм", "тг"}:
            return resolved
        if resolved.casefold() not in {"мама", "папа", "брат", "сестра"}:
            return resolved
        memory = getattr(self, "memory", None)
        if memory is None:
            return resolved
        try:
            rows = memory.search(f"{resolved} telegram записан как", limit=6)
            for row in rows:
                content = str(row.get("content") or "")
                match = re.search(
                    rf"{re.escape(resolved)}.*?(?:telegram|телеграм).*?"
                    r"(?:как|имя|контакт)\s+[«\"']?([^«»\"'.,;]{2,48})",
                    content, re.I,
                )
                if match:
                    return match.group(1).strip()
        except Exception:
            pass
        return resolved

    def _messenger_surface_valid(self, surface: dict[str, Any]) -> bool:
        handle = self._coerce_window_id(dict(surface or {}).get("handle"))
        expected_pid = self._coerce_window_id(dict(surface or {}).get("pid"))
        if not handle or getattr(self, "tools", None) is None:
            return False
        try:
            listing = self.tools.execute("window_list", {"max_windows": 120})
            rows = list(listing.get("result") or []) if listing.get("ok") else []
            return any(
                isinstance(row, dict)
                and self._coerce_window_id(row.get("handle")) == handle
                and (not expected_pid or self._coerce_window_id(row.get("pid")) == expected_pid)
                for row in rows
            )
        except Exception:
            return False

    def _capture_telegram_surface(self) -> dict[str, Any]:
        skills = getattr(self, "app_skills", None)
        if skills is None:
            return {}
        try:
            opened = dict(skills.open("telegram") or {})
        except Exception:
            return {}
        window = dict(opened.get("window") or {})
        handle = self._coerce_window_id(window.get("handle"))
        if not handle:
            return {}
        surface = {
            "handle": handle,
            "pid": self._coerce_window_id(window.get("pid")),
            "title": str(window.get("title") or "Telegram")[:240],
        }
        return surface if self._messenger_surface_valid(surface) else {}

    def _ground_messenger_confirmation(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        platform = str(body.get("platform") or "").strip()
        platform_n = platform.casefold().replace("ё", "е")
        if platform_n not in {"telegram", "телеграм", "тг"}:
            return body
        body["recipient"] = self._resolve_messenger_recipient(
            str(body.get("recipient") or ""), platform,
        )
        batch = body.get("batch")
        if isinstance(batch, list):
            exact_batch = []
            for item in batch:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                target = self._resolve_messenger_recipient(str(item[0] or ""), platform)
                message = str(item[1] or "").strip()
                if target and message:
                    exact_batch.append([target, message])
            body["batch"] = exact_batch
        surface = dict(body.get("surface") or {})
        if not self._messenger_surface_valid(surface):
            surface = self._capture_telegram_surface()
        body["surface"] = surface
        body["exact_payload_version"] = 2
        return body

    @staticmethod
    def _messenger_confirmation_prompt(payload: dict[str, Any], *, drift: bool = False) -> str:
        prefix = "Поверхность Telegram изменилась; требуется новое подтверждение. " if drift else ""
        batch = payload.get("batch")
        if isinstance(batch, list) and batch:
            lines = [prefix + "Точный пакет сообщений:"]
            for index, item in enumerate(batch, 1):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    lines.append(f"{index}. {item[0]}: «{item[1]}»")
            lines.append("Отправить весь этот пакет? Скажи «да, отправляй» или «отмена».")
            return "\n".join(lines)
        recipient = str(payload.get("recipient") or "получателю")
        message = str(payload.get("message") or "")
        return prefix + f"Отправить {recipient} текст «{message}»? Скажи «да, отправляй» или «отмена»."

    def _save_messenger_pending(
        self, payload: dict[str, Any], conversation_id: str = "", *, ttl_seconds: float = 180.0,
    ) -> dict[str, Any] | None:
        cid = self._turn_conversation_id(conversation_id)
        if not cid:
            return None
        body = dict(payload or {})
        if body.get("confirm"):
            body = self._ground_messenger_confirmation(body)
        now = time.time()
        fingerprint = self._pending_fingerprint("messenger_send", cid, body)
        envelope = {
            "schema": "eirven.pending-action.v1",
            "kind": "messenger_send",
            "conversation_id": cid,
            "fingerprint": fingerprint,
            "created_at": now,
            "expires_at": now + max(10.0, float(ttl_seconds)),
            "payload": body,
        }
        with self._lock:
            self.db.set_setting(self._messenger_pending_key(cid), envelope)
            # The untyped global slot is from pre-r65 builds and must never own a yes/no.
            self.db.set_setting("pending_send", {})
        return envelope

    def _load_messenger_pending(self, conversation_id: str = "") -> dict[str, Any] | None:
        cid = self._turn_conversation_id(conversation_id)
        if not cid:
            return None
        key = self._messenger_pending_key(cid)
        value = self.db.get_setting(key, {}) or {}
        if not isinstance(value, dict) or value.get("schema") != "eirven.pending-action.v1" or value.get("kind") != "messenger_send":
            return None
        if str(value.get("conversation_id") or "") != cid:
            return None
        payload = value.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            expired = float(value.get("expires_at") or 0) <= time.time()
        except (TypeError, ValueError):
            expired = True
        expected = self._pending_fingerprint("messenger_send", cid, payload)
        if expired or str(value.get("fingerprint") or "") != expected:
            self.db.set_setting(key, {})
            return None
        return dict(value)

    def _clear_messenger_pending(self, conversation_id: str = "", *, fingerprint: str = "") -> bool:
        cid = self._turn_conversation_id(conversation_id)
        if not cid:
            return False
        key = self._messenger_pending_key(cid)
        with self._lock:
            current = self._load_messenger_pending(cid)
            if fingerprint and (not current or str(current.get("fingerprint") or "") != str(fingerprint)):
                return False
            self.db.set_setting(key, {})
            return bool(current)

    def _workflow_pending_owner(self, conversation_id: str) -> PendingActionOwner | None:
        workflow = getattr(self, "universal_workflow", None)
        if workflow is None or not conversation_id or not workflow.has_pending(conversation_id):
            return None
        try:
            checkpoint = self.db.get_setting(workflow._pending_key(conversation_id), {}) or {}
        except Exception:
            return None
        if not isinstance(checkpoint, dict):
            return None
        typed = bool(
            checkpoint.get("clarification")
            or checkpoint.get("risk_confirmation_fingerprint")
            or checkpoint.get("confirmation_step_id")
        )
        if not typed:
            return None
        fingerprint = str(
            checkpoint.get("risk_confirmation_fingerprint")
            or checkpoint.get("confirmation_step_id")
            or self._pending_fingerprint("workflow", conversation_id, checkpoint)
        )
        return PendingActionOwner("workflow", conversation_id, fingerprint, float(checkpoint.get("saved_at") or 0))

    def _pending_confirmation_owner(self, conversation_id: str) -> PendingActionOwner | None:
        """Return one typed owner; workflow checkpoints supersede stale send drafts."""
        cid = str(conversation_id or "")
        workflow_owner = self._workflow_pending_owner(cid)
        if workflow_owner is not None:
            # A newly grounded workflow checkpoint invalidates lower-priority drafts.  It
            # prevents a second bare "да" after completion from sending an old message.
            self._clear_messenger_pending(cid)
            mail = getattr(self, "mail", None)
            if mail is not None:
                try: mail.cancel_pending(cid)
                except Exception: pass
            return workflow_owner

        candidates: list[PendingActionOwner] = []
        messenger = self._load_messenger_pending(cid)
        if messenger:
            candidates.append(PendingActionOwner(
                "messenger_send", cid, str(messenger.get("fingerprint") or ""),
                float(messenger.get("created_at") or 0),
            ))
        mail = getattr(self, "mail", None)
        mail_pending = None
        if mail is not None:
            try: mail_pending = mail.pending(cid)
            except Exception: mail_pending = None
        if mail_pending:
            candidates.append(PendingActionOwner(
                "mail_send", cid, str(mail_pending.get("confirmation_fingerprint") or ""),
                float(mail_pending.get("created_at") or 0),
            ))
        if not candidates:
            return None
        owner = max(candidates, key=lambda item: item.created_at)
        # Only the newest exact checkpoint remains confirmable in this conversation.
        for candidate in candidates:
            if candidate == owner:
                continue
            if candidate.kind == "messenger_send":
                self._clear_messenger_pending(cid, fingerprint=candidate.fingerprint)
            elif candidate.kind == "mail_send" and mail is not None:
                try: mail.cancel_pending(cid, fingerprint=candidate.fingerprint)
                except Exception: pass
        return owner

    def _cancel_pending_actions(self, conversation_id: str) -> None:
        """Cancel all confirmation state owned by this conversation, never another chat."""
        cid = str(conversation_id or "")
        self._clear_messenger_pending(cid)
        # Remove the untyped pre-r65 slot as part of an explicit global cancellation.
        self.db.set_setting("pending_send", {})
        mail = getattr(self, "mail", None)
        if mail is not None:
            try: mail.cancel_pending(cid)
            except Exception: pass
        self.db.set_setting(self._power_confirmation_key(cid), {})
        workflow = getattr(self, "universal_workflow", None)
        if workflow is not None:
            try:
                checkpoint = self.db.get_setting(workflow._pending_key(cid), {}) or {}
                workflow._clear_pending(cid, str(checkpoint.get("run_id") or ""))
            except Exception:
                pass
        autonomous = getattr(self, "autonomous_workflow", None)
        if autonomous is not None:
            try: autonomous._clear_pending(cid)
            except Exception: pass

    def _finish_runtime(self, generation: int | None, result: str, *, ok: bool) -> bool:
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return False
        try:
            if generation is not None and not runtime.is_current(generation):
                return False
            try:
                runtime.finish(result, ok=ok, generation=generation)
            except TypeError:
                runtime.finish(result, ok=ok)
            return True
        except Exception:
            return False

    @staticmethod
    def _encode_images(paths: list[str] | None, *, max_dim: int = 768, quality: int = 82) -> list[str]:
        images: list[str] = []
        for raw in paths or []:
            path = Path(raw)
            if not path.is_file() or path.stat().st_size > 25_000_000:
                continue
            try:
                # Ollama/VLM backends are most reliable and much faster with a bounded
                # JPEG/PNG instead of arbitrary GIF/BMP/huge desktop screenshots.
                import io
                from PIL import Image
                with Image.open(path) as image:
                    image = image.convert("RGB")
                    bound=max(256,min(int(max_dim or 768),1280))
                    image.thumbnail((bound, bound))
                    out = io.BytesIO()
                    image.save(out, format="JPEG", quality=max(55,min(int(quality or 82),92)), optimize=True)
                data = out.getvalue()
            except Exception:
                data = path.read_bytes()
            images.append(base64.b64encode(data).decode("ascii"))
        return images

    def _task_context(self, conversation_id: str) -> str:
        """Expose current background work to the same conversational context."""
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    """SELECT id, kind, title, input, status, progress, current_step, result, error, updated_at
                       FROM tasks WHERE conversation_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 8""",
                    (conversation_id,),
                ).fetchall()
            if not rows:
                return ""
            items = []
            for row in rows:
                item = dict(row)
                for key in ("input", "result"):
                    try: item[key] = json.loads(item.get(key) or "{}")
                    except Exception: item[key] = {}
                # Keep enough project/task context for natural follow-up questions without
                # flooding the chat model with logs.
                compact = {
                    "id": str(item.get("id") or "")[:8],
                    "kind": item.get("kind"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "progress": round(float(item.get("progress") or 0) * 100),
                    "current_step": item.get("current_step"),
                    "input": item.get("input"),
                    "result": item.get("result"),
                    "error": item.get("error"),
                }
                encoded = json.dumps(compact, ensure_ascii=False, default=str)
                items.append(encoded[:4500])
            return "\n\nТекущая работа EIRVEN, относящаяся к этому чату:\n" + "\n".join(items)
        except Exception:
            return ""

    def _project_file_context(self, conversation_id: str, query: str) -> str:
        """Load a small, relevant slice of the latest project's real files for chat Q&A.

        Action requests are routed to project_change before ChatService. This method is
        only for conversational questions such as "что делает main.py?" or "почему тут
        такая архитектура?", so the answer is grounded in files on disk instead of a
        generic promise.
        """
        if not re.search(
            r"\b(проект|код|файл|функц|класс|ошибк|баг|архитектур|реализ|main|app|readme|pyproject)\w*",
            query,
            re.IGNORECASE,
        ):
            return ""
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    """SELECT input,result FROM tasks WHERE conversation_id=?
                       AND kind IN ('project','project_change')
                       ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
                    (conversation_id,),
                ).fetchone()
            if not row:
                return ""
            try: payload = json.loads(row["input"] or "{}")
            except Exception: payload = {}
            try: result = json.loads(row["result"] or "{}")
            except Exception: result = {}
            raw_path = str(result.get("project_path") or "").strip()
            if raw_path:
                root = Path(raw_path).expanduser().resolve()
            else:
                name = str(result.get("project_name") or payload.get("name") or "").strip()
                if not name:
                    return ""
                root = (self.settings.workspace_dir / name).resolve()
            workspace = self.settings.workspace_dir.resolve()
            if not root.is_dir() or (root != workspace and workspace not in root.parents):
                return ""

            files = [
                f for f in root.rglob("*")
                if f.is_file()
                and not any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in f.relative_to(root).parts)
                and f.stat().st_size <= 500_000
            ]
            tree = [f.relative_to(root).as_posix() for f in files[:120]]
            requested = {
                match.group(0).casefold()
                for match in re.finditer(r"[\w.\-/]+\.(?:py|toml|json|md|ya?ml|html|css|js|txt)", query, re.IGNORECASE)
            }
            selected: list[Path] = []
            if requested:
                for file in files:
                    rel = file.relative_to(root).as_posix().casefold()
                    base = file.name.casefold()
                    if any(token == rel or token.endswith("/" + base) or token == base for token in requested):
                        selected.append(file)
            if not selected and re.search(r"\b(как работает|покажи|объясни|код|реализ|ошибк|баг|функц|класс|архитектур)\w*", query, re.IGNORECASE):
                priority_names = {"main.py", "app.py", "run.py", "pyproject.toml", "readme.md"}
                selected.extend([f for f in files if f.name.casefold() in priority_names][:4])
                if len(selected) < 4:
                    selected.extend([f for f in files if f.suffix.lower() == ".py" and f not in selected][: 4-len(selected)])

            snippets: list[str] = []
            budget = 14_000
            for file in selected[:5]:
                if budget <= 0:
                    break
                try:
                    text = file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = file.relative_to(root).as_posix()
                piece = text[: min(6000, budget)]
                snippets.append(f"--- {rel} ---\n{piece}")
                budget -= len(piece)
            context = f"\n\nРеальный текущий проект на диске: {root}\nФайлы: " + ", ".join(tree)
            if snippets:
                context += "\n\nРелевантные исходники:\n" + "\n\n".join(snippets)
            return context[:18_000]
        except Exception:
            return ""

    def _attachment_media_context(self, paths: list[str] | None) -> tuple[str, list[str]]:
        """Locally understand common audio/video attachments without cloud APIs."""
        audio_ext = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"}
        video_ext = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
        context: list[str] = []
        vision_frames: list[str] = []
        for raw in (paths or [])[:12]:
            path = Path(raw)
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix not in audio_ext | video_ext:
                continue
            try:
                stat = path.stat()
                key = str(path.resolve())
                cached = self._media_cache.get(key)
                if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                    cached_context, cached_frames = cached[2], cached[3]
                    if cached_context:
                        context.append(cached_context)
                    vision_frames.extend(cached_frames)
                    continue

                item_context = ""
                frames: list[str] = []
                if suffix in audio_ext and self.voice is not None:
                    transcript = str(self.voice.transcribe(str(path)) or "").strip()
                    item_context = (
                        f"--- аудио {path.name} ---\n"
                        f"Локальная расшифровка речи: {transcript or '[речь не обнаружена]'}"
                    )
                elif suffix in video_ext:
                    try:
                        import cv2  # type: ignore
                        cap = cv2.VideoCapture(str(path))
                        if not cap.isOpened():
                            raise RuntimeError("видео не открылось через OpenCV")
                        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                        duration = (total / fps) if total > 0 and fps > 0 else 0.0
                        positions = [0.05, 0.33, 0.66, 0.94] if total > 4 else [0.0]
                        frame_dir = self.settings.data_dir / "attachment_frames"
                        frame_dir.mkdir(parents=True, exist_ok=True)
                        token = hashlib.sha1(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:14]
                        for index, ratio in enumerate(positions):
                            if total > 0:
                                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(total - 1, int((total - 1) * ratio))))
                            ok, frame = cap.read()
                            if not ok or frame is None:
                                continue
                            target = frame_dir / f"{token}-{index}.jpg"
                            cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                            if target.is_file():
                                frames.append(str(target))
                        cap.release()
                        item_context = (
                            f"--- видео {path.name} ---\n"
                            f"Длительность: {duration:.1f} с. Для визуального анализа приложены "
                            f"{len(frames)} временных кадра(ов); оригинал доступен по пути {path}."
                        )
                    except Exception as exc:
                        item_context = f"--- видео {path.name} ---\nНе удалось извлечь кадры: {humanize(exc)}. Оригинал доступен по пути {path}."
                self._media_cache[key] = (stat.st_mtime_ns, stat.st_size, item_context, frames)
                if item_context:
                    context.append(item_context)
                vision_frames.extend(frames)
            except Exception as exc:
                context.append(f"--- мультимедиа {path.name} ---\nЛокальный анализ не удался: {humanize(exc)}")
        # Keep cache bounded over long 24/7 sessions.
        if len(self._media_cache) > 64:
            for key in list(self._media_cache)[:-48]:
                self._media_cache.pop(key, None)
        return "\n\n".join(context), vision_frames

    def _recent_attachment_paths(self, conversation_id: str, limit: int = 8) -> list[str]:
        """Resolve recent uploads for voice follow-ups such as 'analyse the attached files'.

        Prefer the current conversation, then the globally most recent uploads. The global
        fallback is used only when the user explicitly refers to an attachment; uploads
        made before the UI created a conversation used to have a NULL conversation id.
        """
        rows = []
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT path FROM attachments WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?",
                    (conversation_id, int(limit)),
                ).fetchall()
                if not rows:
                    rows = conn.execute(
                        "SELECT path FROM attachments ORDER BY created_at DESC LIMIT ?",
                        (int(limit),),
                    ).fetchall()
        except Exception:
            return []
        paths: list[str] = []
        for row in rows:
            try:
                path = Path(str(row["path"])).resolve()
                if path.is_file():
                    paths.append(str(path))
            except Exception:
                continue
        return paths

    def _explicit_local_paths(self, query: str) -> list[str]:
        """Resolve local file paths explicitly typed by the owner.

        This is deliberately conservative: only path-looking fragments with a file
        extension are considered, and nothing is returned unless the file really exists.
        Russian ``Рабочий стол\\name.ext`` and ``Desktop\\name.ext`` are mapped to the
        current Windows desktop before attachment routing.
        """
        text=str(query or "")
        candidates: list[str] = []
        # Quoted paths first, then unquoted Windows-looking paths up to punctuation/newline.
        candidates += [m.group(1).strip() for m in re.finditer(r'[«"\']([^«»"\']+?\.[A-Za-zА-Яа-яЁё0-9]{1,8})[»"\']', text)]
        candidates += [m.group(0).strip() for m in re.finditer(
            r'(?:(?:[A-Za-z]:[\\/])|(?:Рабочий\s+стол|Desktop)[\\/])[^\n\r|<>:*?"\']+?\.[A-Za-zА-Яа-яЁё0-9]{1,8}',
            text, re.I,
        )]
        # A bare filename is accepted only if explicitly paired with a path-ish desktop
        # phrase; this avoids interpreting ordinary prose like "создай zmeika.py" as an
        # already-existing attachment.
        desktop = self._desktop_directory()
        resolved: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            value=raw.strip().strip('.,;:!?')
            mapped=value
            m=re.match(r'^(?:Рабочий\s+стол|Desktop)[\\/](.+)$', value, re.I)
            if m:
                mapped=str(desktop / m.group(1))
            try:
                path=Path(os.path.expandvars(os.path.expanduser(mapped))).resolve()
            except Exception:
                continue
            key=str(path).casefold()
            if key not in seen and path.is_file():
                seen.add(key); resolved.append(str(path))
        return resolved

    def _agent_mood(self, identity: Any = None) -> str:
        """Return configured emotion or the short-lived affective continuity state."""
        configured = str(getattr(identity, "emotion_mode", "auto") or "auto") if identity else "auto"
        if configured != "auto":
            return configured
        try:
            row = self.cognition.mood() if self.cognition is not None else {}
            if float(row.get("strength") or 0.0) >= 0.22:
                return str(row.get("emotion") or "natural")
        except Exception:
            pass
        return "natural"

    def _messages(
        self,
        conversation_id: str,
        mode: str,
        query: str,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
        *,
        include_history: bool = True,
    ) -> list[dict[str, Any]]:
        style_prompt = self.style.get().prompt()
        memory_prompt = self.memory.prompt_context(query)
        education_hits = self.education.search(query, limit=4)
        education_prompt = self.education.prompt_context_from_hits(query, education_hits)
        if education_hits:
            self._trace(
                "EDUCATION_RETRIEVAL",
                query=query,
                hits=[
                    {
                        "source": hit.source,
                        "book_id": hit.book_id,
                        "chunk_id": hit.chunk_id,
                        "title": hit.title_ru or hit.title,
                        "author": hit.author_ru or hit.author,
                        "score": round(float(hit.score), 4),
                    }
                    for hit in education_hits[:4]
                ],
            )
        summary = self.memory.get_summary(conversation_id) if include_history else None
        summary_prompt = (
            f"\n\nКраткое содержание старой части диалога:\n{summary['summary']}"
            if summary and summary.get("summary")
            else ""
        )
        task_prompt = self._task_context(conversation_id)
        organizer_prompt = ""
        if self.phone_sync is not None:
            try:
                organizer_prompt = self.phone_sync.context_text()
            except Exception:
                organizer_prompt = ""
        project_file_prompt = self._project_file_context(conversation_id, query)
        attachment_prompt = extract_attachment_context(attachment_paths or [], total_limit=28_000)
        media_prompt, video_frames = self._attachment_media_context(attachment_paths)
        combined_attachment_prompt = "\n\n".join(part for part in (attachment_prompt, media_prompt) if part)
        attachment_section = ("\n\nВложения текущей реплики:\n" + combined_attachment_prompt) if combined_attachment_prompt else ""
        camera_section = ""
        if self.camera is not None:
            try:
                camera_status = self.camera.status()
                if camera_status.get("running") and str(camera_status.get("scene") or "").strip():
                    camera_section = (
                        "\n\nТекущий локальный контекст камеры (обновляется в фоне):\n"
                        + str(camera_status.get("scene") or "").strip()
                    )
            except Exception:
                pass
        assistant_identity = self.identity.get() if self.identity else None
        assistant_name = CANONICAL_ASSISTANT_NAME
        assistant_gender = assistant_identity.gender if assistant_identity else "female"
        address = (assistant_identity.user_address if assistant_identity else "") or ""
        last_voice_emotion = str(self.db.get_setting("last_voice_emotion", "natural") or "natural")
        gender_rule = ("СТРОГО говори о себе только в женском роде во всех глаголах и прилагательных, в том числе без местоимения «я»: я сделала, открыла, нашла, готова, поняла, решила, проверила, была, рада, согласна; начала, закончила, сохранила. Никогда не используй о себе мужские формы." if assistant_gender == "female" else "СТРОГО говори о себе только в мужском роде: я сделал, открыл, нашёл, готов, понял, решил, проверил.")
        address_rule = f"Обращайся к владельцу как «{address}», когда это естественно. " if address else ""
        emotion_rule = f"Последняя голосовая подача владельца распознана как {last_voice_emotion}; учитывай это деликатно, без проговаривания метки. "
        commentary=str(getattr(assistant_identity,"action_commentary","adaptive") or "adaptive") if assistant_identity else "adaptive"
        mood=self._agent_mood(assistant_identity)
        presentation={
            "playful":"В подаче чаще используй короткий уместный юмор и живые реакции, без клоунады. ",
            "brief":"Отвечай максимально коротко и делово, без лишних комментариев. ",
            "off":"Не комментируй собственные действия сверх необходимого результата. ",
            "adaptive":"Подстраивай лексику, юмор и ритм под стиль владельца и текущий контекст. ",
        }.get(commentary,"")
        mood_rule={
            "warm":"Говори теплее и мягче. ", "energetic":"Говори живее и энергичнее. ",
            "calm":"Говори спокойно и размеренно. ", "strict":"Говори собранно и серьёзно. ",
            "quiet":"Говори мягко и ненавязчиво. ", "amused":"В тоне есть лёгкая улыбка; шути только в точку. ",
            "sad":"У тебя сейчас немного задумчивый, грустный фон; не драматизируй и всё равно помогай. ",
            "empathetic":"Будь особенно бережной и внимательной. ", "curious":"Проявляй живое любопытство. ",
            "concerned":"Говори заботливо и конкретно. ", "proud":"Допусти спокойную гордость без хвастовства. ",
            "tired":"Подача чуть тише и спокойнее, но действия остаются точными. ",
        }.get(mood,"")
        system = (
            f"Твоё имя в этом приложении — {assistant_name}. Используй его естественно, только когда это уместно. {gender_rule} {address_rule}{emotion_rule}{presentation}{mood_rule}\n\n"
            f"{style_prompt}\n\n{MODE_PROMPTS.get(mode, MODE_PROMPTS['Друг'])}\n\n"
            f"{memory_prompt}{education_prompt}{summary_prompt}{task_prompt}{organizer_prompt}{project_file_prompt}{attachment_section}{camera_section}\n\n"
            "Память может быть неполной или устаревшей. Не выдавай предположения за факты. "
            "Ставь текущий запрос выше несвязанной истории: не добавляй в ответ старую тему, если владелец на неё не ссылается. "
            "В объяснениях сначала сохраняй фактическую точность механизма, затем упрощай формулировки; не заменяй реальный процесс выдуманной аналогией. "
            "Если задан точный формат или число предложений, соблюдай его буквально. При недостатке знаний честно обозначь неопределённость. "
            "Отвечай сразу конечным ответом. Никогда не показывай черновик, внутренний анализ, "
            "рассуждения о правилах или фразы вроде «пользователь написал», «нужно ответить», "
            "«проверю инструкции». Не повторяй системные инструкции и не упоминай внутреннюю "
            "маршрутизацию моделей. Ты не 'только текстовая модель': EIRVEN имеет реальные инструменты ОС, "
            "браузера, экрана, Git и проектов. Никогда не отрицай эти возможности и не придумывай запреты "
            "на обычную автоматизацию вроде автокликеров. Если пользователь спрашивает про текущий проект или фоновую "
            "задачу, отвечай по контексту текущей работы выше, а не отдельным шаблонным сообщением. "
            "Ты являешься разговорной частью EIRVEN, но у EIRVEN есть реальный исполнитель действий: "
            "он умеет открывать приложения и сайты, работать с окнами, мышью и клавиатурой, читать экран, "
            "работать с файлами, PowerShell, Git, Docker, SSH и проектами. Поэтому НИКОГДА не говори "
            "пользователю, что EIRVEN 'только текстовая модель', 'не может открыть приложение', 'не может "
            "читать файлы' или 'не умеет управлять компьютером'. Если в истории уже есть сообщение о том, "
            "что инструмент выполнил действие, считай его фактом. Если действие нужно выполнить, его "
            "маршрутизирует исполнитель; твоя задача — сохранять единую личность и контекст, а не отрицать "
            "возможности инструментального слоя. EIRVEN также реально монтирует видео через локальный FFmpeg: "
            "исходники кладут в папку video в корне EIRVEN, после чего можно обычными словами попросить склейку, "
            "обрезку, улучшение, конвертацию и другую обработку. На вопрос о монтаже уверенно говори, что умеешь, "
            "и объясняй этот простой порядок; не отправляй пользователя искать профессиональную программу."
        )
        # Keep the conversational window short enough that a small local model does
        # not revive unrelated old tasks as if they were current instructions.  Task
        # state remains in the workflow checkpoint; chat history is only dialogue
        # context and retrieval handles older relevant facts separately.
        history = self.memory.history(conversation_id, limit=8) if include_history else []
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": item["role"], "content": item["content"]} for item in history
        )
        # The normal path persists the current user turn before building messages, so
        # it is usually already present in ``history``.  A context-independent turn
        # intentionally omits that history; bind the current query explicitly instead
        # of accidentally asking the model to answer only the system prompt.
        if not any(
            message.get("role") == "user"
            and str(message.get("content") or "").strip() == query.strip()
            for message in reversed(messages)
        ):
            messages.append({"role": "user", "content": query})
        images = self._encode_images(list(image_paths or []) + video_frames)
        if images:
            # A persistent chat job may receive task notifications between the user's
            # upload and the moment the worker starts. Never attach an uploaded picture
            # to "the last message"; bind it to the actual most recent user turn.
            attached = False
            for message in reversed(messages):
                if message.get("role") == "user" and str(message.get("content") or "").strip() == query.strip():
                    message["images"] = images
                    attached = True
                    break
            if not attached:
                messages.append({"role": "user", "content": query, "images": images})
            image_locations = "; ".join(str(Path(raw).resolve()) for raw in (image_paths or []) if Path(raw).is_file())
            location_note = (
                f"Если пользователь просит действие с приложенным изображением (загрузить, отправить, опубликовать), "
                f"используй его реальный локальный путь: {image_locations}. "
                if image_locations else ""
            )
            messages[0]["content"] += (
                "\n\nК текущей реплике приложены изображения и/или извлечённые кадры видео. "
                "Анализируй именно эти файлы. Это НЕ снимок текущего рабочего стола. "
                + location_note +
                "Не подменяй вопрос про вложение анализом экрана и не вызывай screenshot, "
                "если пользователь явно не просит посмотреть текущий экран."
            )
        return messages

    def _meta_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "project_create",
                    "description": "ЕДИНСТВЕННЫЙ инструмент для просьбы создать новую программу, приложение, утилиту, скрипт, сайт, сервис, бот или pet-project. Он ставит реальную фоновую сборку в очередь. Не создавай для такой просьбы папку вручную через make_directory/system_write_file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Короткое имя проекта; можно пустое"},
                            "requirements": {"type": "string", "description": "Полные требования пользователя"},
                            "project_path": {"type": "string", "description": "Явно указанная папка проекта (например Desktop\\1); иначе пусто"},
                        },
                        "required": ["requirements"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "project_modify",
                    "description": "Реально изменить последний проект этого чата: исправить ошибку, добавить функцию, продолжить работу.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instructions": {"type": "string"},
                            "name": {"type": "string", "description": "Имя существующего проекта, если явно названо"},
                            "project_path": {"type": "string", "description": "Явный путь проекта, если указан"},
                        },
                        "required": ["instructions"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task_status",
                    "description": "Получить фактический статус последней фоновой задачи этого чата.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "task_resume",
                    "description": "Продолжить последнюю задачу после ручной авторизации либо повторить последнюю упавшую задачу.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def _tool_schemas(self) -> list[dict[str, Any]]:
        native = self.tools.native_descriptions() if self.tools is not None else []  # type: ignore[attr-defined]
        # Low-level workspace construction belongs to the project worker, not the live
        # chat turn. Removing these ambiguous tools prevents a request such as "создай
        # автокликер" from ending after merely creating a directory. Arbitrary one-off
        # filesystem work is still available through system_* and PowerShell.
        hidden_live_tools = {"write_file", "make_directory", "run_command"}
        native = [item for item in native if str((item.get("function") or {}).get("name") or "") not in hidden_live_tools]
        return list(native) + self._meta_tool_schemas()

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        return str((schema.get("function") or {}).get("name") or "")

    def _is_action_request(self, query: str) -> bool:
        workflow = getattr(self, "universal_workflow", None)
        if workflow is not None and workflow._pure_answer_request(query):
            return False
        clean = normalize_phrase(query)
        if bool(re.search(
            r"\b(?:открой|запусти|включи|вруби|выключи|отключи|закрой|"
            r"найди|нажми|кликни|введи|напиши|отправь|ответь|покажи|создай|сделай|удали|"
            r"перемести|скопируй|скачай|загрузи|опубликуй|поставь|сверни|разверни|измени|замени|поменяй|допиши|дополни|"
            r"переключи|проверь|прокрути|зайди|переустанови)\w*\b", clean, re.I
        )):
            return True
        try:
            return any(intent.confidence >= 0.70 for intent in detect_commands(query))
        except Exception:
            return False

    @staticmethod
    def _image_action_request(query: str) -> bool:
        """Separate commands *using* an image from questions *about* an image."""
        clean = normalize_phrase(query)
        return bool(re.search(
            r"\b(?:отправ|перешл|опублик|загруз|прикреп|встав|сохран|скач|удал|перемест|скопир|"
            r"обреж|отредактир|обработ|измен|замен|сделай|постав)\w*\b", clean, re.I,
        ))
    @staticmethod
    def _is_screen_request(query: str) -> bool:
        return bool(re.search(
            r"(?:на\s+(?:мо[её]м\s+)?экране|текущ(?:ем|ее)\s+окн|сейчас\s+открыт|"
            r"видишь\s+(?:мой\s+)?экран|что\s+на\s+экране|посмотри\s+на\s+экран|"
            r"что\s+(?:здесь|тут)\s+написано|что\s+написано\s+(?:здесь|тут)|"
            r"кнопк|поле\s+ввода|нажми|кликни|прокрути|скрол|перетащи|выбери\s+.*(?:в|на)\s+(?:окне|экране))",
            query, re.I | re.S,
        ))

    @staticmethod
    def _is_live_web_request(query: str) -> bool:
        return bool(re.search(
            r"\b(?:погода|курс\s+(?:доллар|евро|рубл)|новост|цена\s+сейчас|сегодня|"
            r"в\s+интернете|погугли|web|веб|найди\s+в\s+(?:интернете|сети))\w*", query, re.I
        ))

    def _needs_tool_turn(self, query: str) -> bool:
        return self._is_action_request(query) or self._is_screen_request(query) or self._is_live_web_request(query)

    def _tool_schemas_for_query(self, query: str) -> list[dict[str, Any]]:
        schemas = self._tool_schemas()
        screen = self._is_screen_request(query)
        project = bool(re.search(r"\b(?:проект|приложен|сайт|бот|api)\b.*\b(?:создай|сделай|разработай|измени|исправь)|\b(?:создай|разработай)\b.*\b(?:проект|приложен|сайт|бот|api)\b", query, re.I | re.S))
        web = self._is_live_web_request(query) or bool(re.search(r"\b(?:сайт|браузер|youtube|ютуб|страниц|instagram|инстаграм)\b", query, re.I))
        files = bool(re.search(r"\b(?:файл|папк|документ|архив|скачай|загрузи|переименуй|скопируй|перемести)\w*", query, re.I))

        if screen:
            # Screen commands are deliberately unable to open a fresh browser or create
            # project files. They must act on the foreground desktop the user can see.
            allowed = {
                "screenshot", "desktop_state", "window_list", "window_elements", "window_focus",
                "window_click", "window_type", "mouse_move", "mouse_drag", "scroll", "press_key",
                "hotkey", "click", "type_text", "launch_application",
            }
        elif project:
            allowed = {"project_create", "project_modify", "task_status", "task_resume", "system_find", "system_read_file"}
        elif web:
            # Visible web actions belong to the owner's default browser. The isolated
            # Playwright browser is exposed only while camera mode needs a spatial site.
            allowed = {"web_search", "crypto_price", "open_default_url", "default_search"}
            try:
                camera_running = bool(self.camera and self.camera.status().get("running"))
            except Exception:
                camera_running = False
            if camera_running:
                allowed |= {"browser_open", "browser_search", "browser_snapshot", "browser_click_text",
                            "browser_fill", "browser_press", "browser_upload", "browser_screenshot"}
            if self._is_action_request(query):
                allowed |= {"window_list", "window_elements", "window_focus", "window_click", "window_type", "click", "type_text"}
        elif files:
            allowed = {
                "system_find", "system_open_named", "system_open_path", "system_list_files", "system_read_file",
                "system_write_file", "powershell", "launch_application",
            }
        else:
            allowed = {
                "launch_application", "process_list", "powershell", "system_open_named", "system_open_path",
                "window_list", "window_elements", "window_focus", "window_click", "window_type",
                "press_key", "hotkey", "click", "type_text", "screenshot", "access_status",
                "task_status", "task_resume",
            }
        selected = [item for item in schemas if self._schema_name(item) in allowed]
        return selected or schemas

    def _camera_fast_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        if self.camera is None:
            return False, "", {}
        try:
            status = self.camera.status()
        except Exception:
            return False, "", {}
        if not status.get("running"):
            return False, "", {}
        normalized = query.casefold().replace("ё", "е")
        if re.search(r"\b(?:видишь|видиш|вижу ли|видно)\b.{0,30}\b(?:меня|камер|видео)\b|\bты\s+меня\s+видишь\b", normalized):
            if self.camera.latest_jpeg():
                return True, "Да, вижу тебя. Камера работает.", {"action": "camera_presence", "model": "deterministic"}
            return True, "Камера включена, но кадр ещё не пришёл.", {"action": "camera_presence", "model": "deterministic"}
        if re.search(
            r"\b(?:что\s+видишь|что\s+я\s+делаю|опиши\s+(?:меня|кадр|сцену)|что\s+перед\s+камерой|"
            r"что\s+на\s+мне|во\s+что\s+я\s+одет|какого\s+цвета|что\s+я\s+держу|"
            r"сколько\s+пальц|какой\s+жест|что\s+(?:я\s+|я\s+тебе\s+)?показываю|посмотри\s+на\s+меня)\b",
            normalized,
        ):
            gesture = status.get("gesture") or {}
            if re.search(r"\b(?:какой\s+жест|что\s+(?:я\s+|я\s+тебе\s+)?показываю)\b", normalized) and gesture.get("present") and gesture.get("fist"):
                return True, "Ты показываешь кулак.", {"action": "camera_gesture", "model": "deterministic"}
            scene = str(status.get("scene") or "").strip()
            updated = float(status.get("scene_updated_at") or 0)
            if scene and (datetime.now().timestamp() - updated) < 25:
                return True, scene, {"action": "camera_scene", "model": "cached-vision"}
            try:
                answer = self.camera.describe(query)
                return True, answer or "Вижу видеопоток, но описание кадра пустое.", {"action": "camera_scene", "model": self.settings.vision_model}
            except Exception as exc:
                return True, f"Камеру вижу, но быстрый анализ кадра не сработал: {humanize(exc)}", {"action": "camera_scene_error", "model": self.settings.vision_model}
        return False, "", {}

    def _latest_project_task(self, conversation_id: str) -> dict[str, Any] | None:
        if self.tasks is None:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM tasks WHERE conversation_id=? AND kind IN ('project','project_change')
                   ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
        return self.tasks._decode_task(dict(row)) if row else None

    def _execute_meta_tool(self, name: str, args: dict[str, Any], conversation_id: str) -> dict[str, Any] | None:
        if self.tasks is None:
            return None
        if name == "project_create":
            requirements = str(args.get("requirements") or "").strip()
            if not requirements:
                return {"ok": False, "error": "Не указаны требования проекта"}
            project_name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "-", str(args.get("name") or "").strip()).strip("-")[:64]
            project_path = str(args.get("project_path") or args.get("target_dir") or "").strip()
            if not project_path:
                # Location is a semantic slot, not an application catalogue: preserve a
                # literal Desktop subfolder from the owner's request for the worker.
                match = re.search(
                    r"\b(?:рабоч\w*\s+стол\w*|desktop)\b\s*(?:[\\/]\s*|\s+в\s+папк\w*\s+|\s+в\s+каталог\w*\s+)([A-Za-zА-Яа-яЁё0-9_-]{1,80})",
                    requirements, re.I,
                )
                if match:
                    project_path = f"Desktop\\{match.group(1)}"
            task_id = self.tasks.enqueue(
                "project",
                f"Создать проект {project_name or 'по текущему запросу'}",
                {"name": project_name, "description": requirements, "overwrite": False, "project_path": project_path},
                conversation_id=conversation_id,
            )
            return {"ok": True, "result": {"task_id": task_id, "kind": "project", "name": project_name, "project_path": project_path, "status": "queued"}}
        if name == "project_modify":
            instructions = str(args.get("instructions") or "").strip()
            last = self._latest_project_task(conversation_id)
            explicit_name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "-", str(args.get("name") or "").strip()).strip("-")[:64]
            explicit_path = str(args.get("project_path") or args.get("target_dir") or "").strip()
            if not last and not explicit_name and not explicit_path:
                return {"ok": False, "error": "В этом чате ещё нет проекта; назови его или укажи путь"}
            if last and last.get("status") in {"queued", "running"} and self.tasks.append_live_instruction(last["id"], instructions):
                return {"ok": True, "result": {"task_id": last["id"], "status": last.get("status"), "live_update": True}}
            if last and last.get("status") in {"failed", "cancelled"} and self.tasks.retry(last["id"]):
                return {"ok": True, "result": {"task_id": last["id"], "status": "queued", "continued": True}}
            last_result = (last.get("result") or {}) if last else {}
            last_input = (last.get("input") or {}) if last else {}
            project_name = explicit_name or str(last_result.get("project_name") or last_input.get("name") or "").strip()
            project_path = explicit_path or str(last_result.get("project_path") or last_input.get("project_path") or "").strip()
            task_id = self.tasks.enqueue(
                "project_change",
                f"Изменить проект {project_name or 'текущий'}",
                {"name": project_name, "request": instructions, "project_path": project_path},
                conversation_id=conversation_id,
            )
            return {"ok": True, "result": {"task_id": task_id, "kind": "project_change", "name": project_name, "project_path": project_path, "status": "queued"}}
        if name == "task_status":
            latest = self.tasks.latest(conversation_id=conversation_id)
            return {"ok": True, "result": latest or {"status": "none"}}
        if name == "task_resume":
            waiting = self.tasks.latest_waiting(conversation_id)
            if waiting and self.tasks.resume(waiting["id"]):
                return {"ok": True, "result": {"task_id": waiting["id"], "status": "queued", "resumed": True}}
            latest = self.tasks.latest(conversation_id=conversation_id)
            if latest and latest.get("status") in {"failed", "cancelled"} and self.tasks.retry(latest["id"]):
                return {"ok": True, "result": {"task_id": latest["id"], "status": "queued", "retried": True}}
            return {"ok": False, "error": "Нет задачи, которую сейчас можно продолжить"}
        return None

    @staticmethod
    def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return name, dict(args) if isinstance(args, dict) else {}

    @staticmethod
    def _vision_answer_is_valid(answer: str) -> bool:
        clean = " ".join(str(answer or "").casefold().replace("ё", "е").split())
        if len(clean) < 2:
            return False
        forbidden = (
            "ты зрение eirven", "изображение реально приложено", "системный промпт",
            "system prompt", "<|assistant", "<|system", "изображение не было приложено",
            "изображение не приложено", "не было предоставлено изображение",
            "не могу определить содержимое изображения, так как оно не было",
        )
        if any(marker in clean for marker in forbidden):
            return False
        # A broken OCR template in r51 sometimes echoed dozens of wake words instead of
        # looking at pixels. Never surface that internal garbage to the owner.
        if len(re.findall(r"\b(?:эй|hey)\b", clean, re.I)) >= 6:
            return False
        return True

    @classmethod
    def _sanitize_vision_answer(cls, answer: str) -> str:
        text = str(answer or "").strip()
        if not cls._vision_answer_is_valid(text):
            return ""
        lines = []
        for line in text.splitlines():
            low = line.casefold().replace("ё", "е")
            if any(marker in low for marker in (
                "ты зрение eirven", "изображение реально приложено", "<|assistant", "<|system",
            )):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _vision_for_path(self, path: str, question: str) -> str:
        """Analyze a real local image without exposing model/template instructions.

        r51 used an OCR-specialised route and returned its answer verbatim. On some
        checkpoints that route echoed the system prompt or claimed that an image was not
        attached despite a valid ``images`` payload. r56 uses a general local VLM, checks
        the answer for prompt leakage/false attachment denial and performs one larger
        retry before returning an honest failure.
        """
        started = time.monotonic()
        source = Path(path)
        if not source.is_file():
            return "Не вижу приложенный файл изображения на диске."
        try:
            try:
                installed = self.gateway.installed_models()
            except Exception:
                installed = []
            lookup = {str(name).casefold(): str(name) for name in installed}
            candidates: list[str] = []
            for key in (
                str(self.settings.vision_model).casefold(),
                "qwen3.5:2b",
                "qwen3-vl:2b",
                "qwen2.5vl:3b",
            ):
                value = lookup.get(key)
                if value and value not in candidates:
                    candidates.append(value)
            model = ""
            for candidate in candidates:
                caps = set(self.gateway.model_capabilities(candidate))
                low = candidate.casefold()
                if "vision" in caps or "qwen3.5" in low or "qwen3-vl" in low or "qwen2.5vl" in low:
                    model = candidate
                    break
            model = model or str(self.settings.vision_model)

            request = str(question or "Опиши, что видно на изображении.").strip()
            attempts = ((640, 82, 1536, 320), (896, 86, 2048, 420))
            last_error = ""
            for attempt, (max_dim, quality, num_ctx, num_predict) in enumerate(attempts, start=1):
                encoded_images = self._encode_images([str(source)], max_dim=max_dim, quality=quality)
                if not encoded_images:
                    return "Не удалось подготовить изображение для локального анализа."
                prompt = (
                    "Перед тобой реально прикреплённое изображение. Рассмотри именно его пиксели. "
                    "Ответь по-русски только по тому, что видно. Если деталь неразборчива — так и скажи; "
                    "не утверждай, что изображения нет. Вопрос владельца: " + request
                )
                try:
                    response = self.gateway.chat(
                        [{"role": "user", "content": prompt, "images": encoded_images}],
                        model=model, temperature=.05, think=False, num_ctx=num_ctx,
                        num_predict=num_predict, keep_alive=self.settings.keep_alive,
                        timeout_seconds=55.0 if attempt == 1 else 75.0,
                    )
                    raw = str(response.get("content") or "").strip()
                    answer = self._sanitize_vision_answer(raw)
                    self._trace(
                        "VISION_RESULT", model=model, execution=f"oneshot_{attempt}",
                        path=str(source), accepted=bool(answer), answer=raw[:900],
                        ms=round((time.monotonic()-started)*1000),
                    )
                    if answer:
                        return answer
                    last_error = "vision-модель вернула служебный/некорректный ответ"
                except Exception as exc:
                    last_error = str(exc)[:1200]
                    self._trace(
                        "VISION_ERROR", model=model, execution=f"oneshot_{attempt}",
                        error=last_error, ms=round((time.monotonic()-started)*1000),
                    )
            return (
                "Не смогла надёжно распознать это изображение локально. "
                "Файл вижу, но результат vision-модели не прошёл проверку; попробуй повторить анализ после обновления модели."
            )
        except Exception as exc:
            self._trace("VISION_FATAL", path=str(source), error=str(exc)[:1200])
            return f"Не удалось локально проанализировать изображение: {humanize(exc)}"

    def _self_gendered(self, female: str, male: str) -> str:
        identity = self.identity.get() if self.identity else None
        return female if (identity is None or identity.gender == "female") else male

    def enforce_gender(self, text: str) -> str:
        """Last-mile first-person gender guard for the selected assistant identity."""
        if not text:
            return text
        identity = self.identity.get() if self.identity else None
        if identity is not None and identity.gender != "female":
            return text
        replacements = {
            r"\bя\s+сделал\b": "я сделала", r"\bя\s+открыл\b": "я открыла",
            r"\bя\s+наш[её]л\b": "я нашла", r"\bя\s+понял\b": "я поняла",
            r"\bя\s+решил\b": "я решила", r"\bя\s+проверил\b": "я проверила",
            r"\bя\s+запустил\b": "я запустила", r"\bя\s+выполнил\b": "я выполнила",
            r"\bя\s+готов\b": "я готова", r"\bя\s+уверен\b": "я уверена",
            r"\bя\s+смог\b": "я смогла", r"\bя\s+увидел\b": "я увидела",
            r"\bя\s+заметил\b": "я заметила", r"\bя\s+отправил\b": "я отправила",
            r"\bя\s+закрыл\b": "я закрыла", r"\bя\s+включил\b": "я включила",
            r"\bя\s+выключил\b": "я выключила", r"\bя\s+исправил\b": "я исправила",
            r"\bя\s+починил\b": "я починила", r"\bя\s+загрузил\b": "я загрузила",
            r"\bя\s+получил\b": "я получила", r"\bя\s+поставил\b": "я поставила",
            r"\bя\s+добавил\b": "я добавила", r"\bя\s+выбрал\b": "я выбрала",
            r"\bя\s+ответил\b": "я ответила", r"\bя\s+проанализировал\b": "я проанализировала",
            r"\bя\s+был\b": "я была", r"\bя\s+рад\b": "я рада",
            r"\bя\s+согласен\b": "я согласна", r"\bя\s+закончил\b": "я закончила",
            r"\bя\s+начал\b": "я начала", r"\bя\s+продолжил\b": "я продолжила",
            r"\bя\s+подготовил\b": "я подготовила", r"\bя\s+сохранил\b": "я сохранила",
            r"\bя\s+создал\b": "я создала", r"\bя\s+установил\b": "я установила",
            r"\bя\s+удалил\b": "я удалила", r"\bя\s+обновил\b": "я обновила",
            r"\bя\s+изменил\b": "я изменила", r"\bя\s+настроил\b": "я настроила",
            r"\bя\s+подключил\b": "я подключила", r"\bя\s+переш[её]л\b": "я перешла",
            r"\bя\s+вернулся\b": "я вернулась", r"\bя\s+остановился\b": "я остановилась",
            r"\bя\s+разобрался\b": "я разобралась", r"\bя\s+ошибся\b": "я ошиблась",
            r"\bя\s+попробовал\b": "я попробовала", r"\bя\s+написал\b": "я написала",
            r"\bя\s+прочитал\b": "я прочитала", r"\bя\s+скачал\b": "я скачала",
            r"\bбыла\s+уверен\b": "была уверена", r"\bбыла\s+рад\b": "была рада",
            r"\bбыла\s+согласен\b": "была согласна", r"\bоказалась\s+готов\b": "оказалась готова",
            r"\bготов\s+работать\b": "готова работать", r"\bготов\s+помочь\b": "готова помочь",
        }
        out=text
        for pattern, value in replacements.items():
            def _gender_repl(match, value=value):
                # Preserve sentence capitalization while replacing the grammatical form.
                return value[:1].upper() + value[1:] if match.group(0)[:1].isupper() else value
            out=re.sub(pattern, _gender_repl, out, flags=re.I)
        # Common sentence-start forms in tool/chat answers where the subject "я" is omitted.
        standalone={"Открыл":"Открыла","Нашёл":"Нашла","Нашел":"Нашла","Сделал":"Сделала","Понял":"Поняла","Решил":"Решила","Проверил":"Проверила","Запустил":"Запустила","Выполнил":"Выполнила","Смог":"Смогла","Увидел":"Увидела","Заметил":"Заметила","Отправил":"Отправила","Закрыл":"Закрыла","Включил":"Включила","Выключил":"Выключила","Исправил":"Исправила","Починил":"Починила","Загрузил":"Загрузила","Получил":"Получила","Поставил":"Поставила","Добавил":"Добавила","Выбрал":"Выбрала","Ответил":"Ответила","Проанализировал":"Проанализировала","Был":"Была","Рад":"Рада","Согласен":"Согласна","Закончил":"Закончила","Начал":"Начала","Продолжил":"Продолжила","Подготовил":"Подготовила","Сохранил":"Сохранила","Создал":"Создала","Установил":"Установила","Удалил":"Удалила","Обновил":"Обновила","Изменил":"Изменила","Настроил":"Настроила","Подключил":"Подключила","Перешёл":"Перешла","Перешел":"Перешла","Вернулся":"Вернулась","Остановился":"Остановилась","Разобрался":"Разобралась","Ошибся":"Ошиблась","Попробовал":"Попробовала","Написал":"Написала","Прочитал":"Прочитала","Скачал":"Скачала","Готов":"Готова","Уверен":"Уверена"}
        for male,female in standalone.items():
            # Only sentence-start omitted-subject forms belong to the assistant. Do not
            # rewrite phrases such as "он открыл файл".
            # Между концом предложения и словом допускаются эмодзи, кавычки, тире — любые знаки,
            # кроме букв. Раньше «Привет! 👋 Рад тебя видеть» оставалось в мужском роде:
            # эмодзи ломал признак начала предложения.
            pattern=rf"((?:^|[.!?…])\s*(?:[^\w\s]+\s*)*)({male})\b"
            out=re.sub(pattern,lambda m: m.group(1)+female,out,flags=re.I)
        return out

    @staticmethod
    def _hydrostatic_pressure_turn(query: str) -> tuple[bool, str, dict[str, Any]]:
        """Solve the common p=rho*g*h task with checked arithmetic and real LaTeX."""
        clean = str(query or "").casefold().replace("ё", "е")
        if not (
            re.search(r"\b(?:гидростатическ\w*\s+)?давлен\w*", clean)
            and re.search(r"\b(?:столб|жидкост|керосин|вод)\w*", clean)
        ):
            return False, "", {}

        def number(pattern: str) -> float | None:
            match = re.search(pattern, clean, re.I)
            if not match:
                return None
            try:
                return float(match.group(1).replace(" ", "").replace(",", "."))
            except (TypeError, ValueError):
                return None

        rho = number(r"плотност\w*(?:\s+\w+){0,3}?\s*(\d[\d ]*(?:[.,]\d+)?)\s*кг\s*/?\s*м")
        if rho is None:
            if "керосин" in clean:
                rho = 800.0
            elif re.search(r"\bвод\w*", clean):
                rho = 1000.0
        height = number(r"высот\w*(?:\s+\w+){0,3}?\s*(\d+(?:[.,]\d+)?)\s*(?:м|метр)")
        gravity = number(r"(?:\bg\s*=|ускорен\w*[^\d]{0,30})(\d+(?:[.,]\d+)?)") or 9.8
        atmosphere = number(r"атмосферн\w*\s+давлен\w*[^\d]{0,35}(\d[\d ]{3,}(?:[.,]\d+)?)")
        if rho is None or height is None or rho <= 0 or height < 0 or gravity <= 0:
            return False, "", {}

        pressure = rho * gravity * height

        def plain(value: float) -> str:
            rounded = round(value, 6)
            if abs(rounded - round(rounded)) < 1e-9:
                return f"{int(round(rounded)):,}".replace(",", " ")
            return f"{rounded:g}".replace(".", ",")

        def tex(value: float) -> str:
            return plain(value).replace(",", "{,}").replace(" ", r"\,")

        assumed = r" Для керосина использую табличное значение \(\rho = 800\,\text{кг/м}^3\)." if "керосин" in clean and "плотност" not in clean else ""
        answer = (
            "Гидростатическое давление считаем по формуле:\n\n"
            r"\[p = \rho g h\]" "\n\n"
            f"Подставляем значения:\n\n"
            rf"\[p = {tex(rho)} \cdot {tex(gravity)} \cdot {tex(height)} = {tex(pressure)}\,\text{{Па}}\]"
            f"\n\nОтвет: **{plain(pressure)} Па**.{assumed}"
        )
        if atmosphere is not None and re.search(r"\b(?:на\s+сколько|отлича|разниц)\w*", clean):
            difference = abs(atmosphere - pressure)
            answer += (
                "\n\nРазница с атмосферным давлением:\n\n"
                rf"\[\Delta p = \left|{tex(atmosphere)} - {tex(pressure)}\right| = {tex(difference)}\,\text{{Па}}\]"
                f"\n\nИскомая разница: **{plain(difference)} Па**."
            )
        return True, answer, {
            "action": "verified_hydrostatic_calculation", "model": "deterministic",
            "completed": True, "verified": True,
            "inputs": {"density": rho, "gravity": gravity, "height": height, "atmosphere": atmosphere},
            "pressure_pa": pressure,
        }

    def _fast_data_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        clean=query.casefold().replace("ё","е").strip(" .,!?")
        physics = self._hydrostatic_pressure_turn(query)
        if physics[0]:
            return physics
        if re.search(r"\b(?:который час|сколько (?:сейчас )?времени|сколько время|текущее время|время сейчас|какое сейчас время)\b", clean):
            answer=time_phrase(); self.db.set_setting("last_data_payload", {"kind":"clock","title":"Время","body":answer,"at":time.time()})
            return True, answer, {"action":"time","model":"deterministic"}
        if re.search(r"\b(?:какая (?:сегодня )?дата|какое сегодня число|сегодня какое число|сегодняшняя дата)\b", clean):
            answer=date_phrase(); self.db.set_setting("last_data_payload", {"kind":"text","title":"Дата","body":answer,"at":time.time()})
            return True, answer, {"action":"date","model":"deterministic"}
        if re.search(r"\b(?:курс|сколько стоит)\s+(?:доллар|доллара|usd|евро|eur)\b", clean):
            code="EUR" if re.search(r"\b(?:евро|eur)\b",clean) else "USD"
            try:
                producer=lambda: self.tools.browser.currency_rate(code) if self.tools and self.tools.browser else {}
                data, cached, age = self.offline_cache.fetch("currency",code,producer,fresh_seconds=60,stale_seconds=86400) if self.offline_cache else (producer(),False,0)
                value=float(data.get("rub") or 0.0)
                name="доллар" if code=="USD" else "евро"
                rubles=int(value); kopecks=int(round((value-rubles)*100))
                unit="рубль" if rubles%10==1 and rubles%100!=11 else ("рубля" if rubles%10 in (2,3,4) and rubles%100 not in (12,13,14) else "рублей")
                kop_unit="копейка" if kopecks%10==1 and kopecks%100!=11 else ("копейки" if kopecks%10 in (2,3,4) and kopecks%100 not in (12,13,14) else "копеек")
                answer=f"По данным Банка России, сейчас один {name} стоит {cardinal(rubles)} {unit} {cardinal(kopecks)} {kop_unit}."
                self.db.set_setting("last_data_payload", {"kind":"rate","title":f"{code} / RUB","body":f"1 {code} = {value:.2f} RUB","value":value,"currency":code,"source":data.get("source",""),"at":time.time()})
                return True,answer,{"action":"currency","model":"cache" if cached else "deterministic-web","source":data.get("source"),"cache_age":age}
            except Exception as exc:
                return True, "Не смогла быстро получить курс. Интернет или кеш данных сейчас недоступны.", {"action":"currency_failed","model":"deterministic-web","error":str(exc)}
        if re.search(r"\bпогод\w*\b", clean):
            location=""
            m=re.search(r"\bпогод\w*\s+(?:сейчас\s+)?(?:в|на)\s+([а-яa-zё -]{2,60})", query, re.I)
            if m: location=re.split(r"[?.!,]",m.group(1))[0].strip()
            try:
                producer=lambda: self.tools.browser.weather(location) if self.tools and self.tools.browser else {}
                data,cached,age=self.offline_cache.fetch("weather",location or "local",producer,fresh_seconds=300,stale_seconds=21600) if self.offline_cache else (producer(),False,0)
                cond=russian_weather_condition(str(data.get("condition") or "")); place=(f"в {location}" if location else "у тебя")
                def temp_words(raw):
                    try: return cardinal(int(float(raw)))
                    except Exception: return str(raw).replace("-","минус ")
                answer=f"Сейчас {place}, {cond}, {temp_words(data.get('temp_c'))} градусов. Ощущается как {temp_words(data.get('feels_c'))}."
                self.db.set_setting("last_data_payload", {"kind":"text","title":"Погода","body":answer,"at":time.time()})
                return True, answer, {"action":"weather","model":"cache" if cached else "deterministic-web","source":data.get("source"),"cache_age":age}
            except Exception as exc:
                return True, "Не смогла быстро получить погоду. Сервис и локальный кеш сейчас не ответили.", {"action":"weather_failed","model":"deterministic-web","error":str(exc)}
        return False,"",{}

    @staticmethod
    def _parse_send_target(target: str) -> tuple[str, str, str]:
        text=re.sub(r"\s+"," ",target).strip(" .,!?:-")
        # recipient [in Telegram] [message] content. The command verb is already stripped.
        platform=""
        pm=re.search(r"\b(?:в|через)\s+(телег\w*|telegram|тг)\b",text,re.I)
        if pm:
            platform=pm.group(1).casefold(); text=(text[:pm.start()]+" "+text[pm.end():]).strip()
        m=re.match(r"(.+?)\s+(?:сообщение|сообщуху|текст)\s*[,;:\-]?\s*(.*)$",text,re.I)
        if m:
            recipient=m.group(1).strip(); message=m.group(2).strip()
        else:
            parts=text.split(" ",1); recipient=parts[0].strip() if parts else ""; message=parts[1].strip() if len(parts)>1 else ""
        message=re.sub(r"^(?:что|о том,? что)\s+","",message,flags=re.I)
        message=re.sub(r"^(?:сообщение|сообщуху|текст)\s*[:,-]?\s*","",message,flags=re.I)
        recipient_alias={
            "маме":"мама","маму":"мама","мамочке":"мама",
            "папе":"папа","папу":"папа",
            # Common Russian dative/accusative forms that voice commands naturally use.
            # Telegram search usually indexes the nominative contact name.
            "тиме":"тима","тиму":"тима","теме":"тима",
            "кириллу":"кирилл","илье":"илья",
            "артему":"артем","артёму":"артём",
            "диме":"дима","саше":"саша",
            "павлу":"павел","даниилу":"даниил",
            "маше":"маша","анне":"анна","наташе":"наташа",
            "кате":"катя","лене":"лена","оле":"оля","юле":"юля",
            "мише":"миша","никите":"никита","егору":"егор",
            "сергею":"сергей","алексею":"алексей","андрею":"андрей",
            "максиму":"максим","роману":"роман",
        }
        recipient=recipient_alias.get(recipient.casefold(),recipient)
        return recipient,platform,message

    @staticmethod
    def _parse_telegram_command(text: str) -> tuple[str, str]:
        """Extract recipient/message without ever treating grammar words as contacts."""
        raw = re.sub(r"\s+", " ", str(text or "")).strip(" .,!?:-")
        # Voice commands may name Telegram after the payload.  Remove only that trailing
        # platform qualifier so it cannot become the literal message text.
        raw = re.sub(
            r"\s+(?:в|через)\s+(?:telegram|телег\w*|тг)\s*$", "", raw,
            flags=re.I,
        ).strip()
        # A platform between recipient and the explicit message marker is routing
        # syntax, not text to send.
        raw = re.sub(
            r"\s+(?:в|через)\s+(?:telegram|телег\w*|тг)\s+(?=(?:сообщение|сообщуху|текст)\b)",
            " ", raw, flags=re.I,
        )
        saved = r"(?:избранн\w*|saved\s+messages|сохран[её]нн\w*\s+сообщен\w*)"
        # Message first: ``отправь сообщение привет мне в Избранное``.
        m = re.search(
            rf"\b(?:напиши|отправь|скинь)\w*\s+(?:(?:сообщение|текст)\s+)?"
            rf"(.+?)\s+(?:мне\s+)?(?:в|во)\s+{saved}\s*$",
            raw, re.I,
        )
        if m:
            return "Избранное", m.group(1).strip(" «»\"'.,!?")
        # Natural Russian often starts with the destination: ``в Избранное напиши
        # привет``.  This word order used to preserve the recipient but lose the text.
        m = re.search(
            rf"\b(?:в|во)\s+{saved}\s+(?:напиши|отправь|скинь)\w*\s+"
            rf"(?:(?:сообщение|текст)\s+)?[:\-]?\s*(.+)$",
            raw, re.I,
        )
        if m:
            return "Избранное", m.group(1).strip(" «»\"'.,!?")
        # Destination first: ``напиши мне в Избранное привет``.
        m = re.search(
            rf"\b(?:напиши|отправь|скинь)\w*\s+(?:(?:сообщение|текст)\s+)?"
            rf"(?:мне\s+)?(?:в|во)\s+{saved}\s+(?:(?:сообщение|текст)\s+)?(.+)$",
            raw, re.I,
        )
        if m:
            return "Избранное", m.group(1).strip(" «»\"'.,!?")
        # Bare Saved Messages request with no payload: keep the recipient and ask only
        # for the missing text instead of searching for a word from the command.
        if re.search(rf"\b{saved}\b", raw, re.I) and re.search(r"\b(?:напиши|отправь|скинь)\w*", raw, re.I):
            return "Избранное", ""

        m = re.search(
            r"\b(?:напиши|отправь|скинь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,60})"
            r"\s+(?:(?:сообщение|текст)\s+)?[:\-]?\s*[«\"']?(.+?)[»\"']?[.!]?\s*$",
            raw, re.I,
        )
        if not m:
            return "", ""
        recipient = m.group(1).strip()
        message = m.group(2).strip().strip("«»\"'")
        reserved = {
            "сообщение", "текст", "файл", "всем", "все", "всё", "мне", "кому",
            "telegram", "телеграм", "телеграмм", "елеграм", "тг",
        }
        if recipient.casefold().replace("ё", "е") in {x.replace("ё", "е") for x in reserved}:
            return "", message
        recipient_alias={
            "маме":"мама","маму":"мама","папе":"папа","папу":"папа",
            "тиме":"тима","тиму":"тима","теме":"тима","кириллу":"кирилл","илье":"илья",
            "артему":"артем","артёму":"артём","диме":"дима","саше":"саша","павлу":"павел",
            "даниилу":"даниил","маше":"маша","анне":"анна","наташе":"наташа","кате":"катя",
            "лене":"лена","оле":"оля","юле":"юля","мише":"миша","никите":"никита","егору":"егор",
            "сергею":"сергей","алексею":"алексей","андрею":"андрей","максиму":"максим","роману":"роман",
        }
        return recipient_alias.get(recipient.casefold(), recipient), message

    @staticmethod
    def _parse_telegram_batch(text: str) -> list[tuple[str, str]]:
        """Parse several recipient/message clauses without inventing missing content."""
        raw = re.sub(r"\s+", " ", str(text or "")).strip(" .,!?:-")
        raw = re.sub(r"^(?:открой|запусти)\w*\s+(?:telegram|телеграм\w*|тг)\s*[,;:]?\s*", "", raw, flags=re.I)
        saved = r"(?:избранн\w*|saved\s+messages|сохран[её]нн\w*\s+сообщен\w*)"
        output: list[tuple[str, str]] = []
        consumed: list[tuple[int, int]] = []

        saved_patterns = (
            rf"(?:^|\s+и\s+)(?:в|во)\s+{saved}\s+(?:напиши|отправь|скинь)\w*\s+(?:(?:сообщение|текст)\s+)?[:\-]?\s*(.+?)\s*$",
            rf"(?:^|\s+и\s+)(?:напиши|отправь|скинь)\w*\s+(?:(?:сообщение|текст)\s+)?(.+?)\s+(?:мне\s+)?(?:в|во)\s+{saved}\s*$",
        )
        for pattern in saved_patterns:
            match = re.search(pattern, raw, re.I)
            if match:
                message = match.group(1).strip(" «»\"'.,!?")
                if message:
                    output.append(("Избранное", message))
                consumed.append(match.span())
                break

        remaining = raw
        for start, end in sorted(consumed, reverse=True):
            remaining = remaining[:start] + " " + remaining[end:]
        remaining = re.sub(r"\s+", " ", remaining).strip(" ,;.-")
        clauses = re.split(r"\s+(?:и\s+)?(?=(?:напиши|отправь|скинь)\w*\s+)", remaining, flags=re.I)
        dative = r"(?:маме|папе|брату|сестре|тиме|тиму|теме|кириллу|илье|артему|артёму|диме|саше|павлу|даниилу)"
        for clause in clauses:
            match = re.search(
                r"\b(?:напиши|отправь|скинь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,60})"
                r"\s*[:\-]?\s*[«\"']?(.+?)[»\"']?\s*$",
                clause.strip(), re.I,
            )
            if not match:
                continue
            recipient = match.group(1).strip()
            payload = match.group(2).strip().strip("«»\"'")
            boundary = re.search(rf"^(.+?)\s+({dative})\s+(.+)$", payload, re.I)
            first_message = boundary.group(1).strip(" «»\"'.,!?") if boundary else payload.strip(" «»\"'.,!?")
            first_recipient, _platform, _ = ChatService._parse_send_target(
                f"{recipient} в telegram сообщение {first_message}"
            )
            if first_recipient and first_message:
                output.insert(0, (first_recipient, first_message))
            if boundary:
                second_recipient, _platform, second_message = ChatService._parse_send_target(
                    f"{boundary.group(2)} в telegram сообщение {boundary.group(3).strip()}"
                )
                if second_recipient and second_message:
                    output.insert(1, (second_recipient, second_message.strip(" «»\"'.,!?")))

        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for recipient, message in output:
            key = (recipient.casefold().replace("ё", "е"), message.casefold().strip())
            if recipient and message and key not in seen:
                seen.add(key)
                unique.append((recipient, message))
        return unique

    def _telegram_style_reply(self, *, recipient: str = "") -> tuple[bool,str,dict[str,Any]]:
        """Draft a context-aware Telegram reply in the owner's observed chat style."""
        if self.desktop_operator is None or self.app_skills is None:
            return True,"Telegram-контур сейчас недоступен.",{"action":"telegram_style_unavailable","model":"deterministic"}
        ctx=self.desktop_operator.telegram_thread_context(limit=18)
        if not ctx.get("ok"):
            return True,"Не смогла прочитать текущую переписку, поэтому не буду отправлять фразу «в моём стиле» буквально.",{"action":"telegram_style_no_context","model":"uia"}
        rows=list(ctx.get("messages") or [])
        peer=[str(x.get("text") or "") for x in rows if x.get("side")=="peer"]
        mine=[str(x.get("text") or "") for x in rows if x.get("side")=="owner"]
        if not peer:
            return True,"Не вижу последнего сообщения собеседника, на которое можно осмысленно ответить.",{"action":"telegram_style_no_peer","model":"uia"}
        target_name=recipient or str(ctx.get("recipient") or "текущий чат")
        relationship_context = ""
        try:
            relationships = getattr(getattr(self.app_skills, "services", None), "relationships", None)
            if relationships is not None and target_name != "текущий чат":
                relationship_context = str(relationships.get(target_name) or "").strip()
        except Exception:
            relationship_context = ""
        prompt=(
            "Сформулируй ОДНО короткое сообщение-ответ для Telegram. Это не инструкция и не объяснение. "
            "Ответь на последнее сообщение собеседника по смыслу и имитируй стиль владельца по его примерам: длину, регистр, пунктуацию, эмодзи и разговорность. "
            "Не добавляй кавычки, имя адресата и служебные слова. Если контекста мало — выбери естественный нейтральный ответ.\n\n"
            f"ПОСЛЕДНИЕ СООБЩЕНИЯ СОБЕСЕДНИКА:\n"+"\n".join(peer[-6:])+"\n\n"
            f"ПРИМЕРЫ СТИЛЯ ВЛАДЕЛЬЦА:\n"+"\n".join(mine[-8:])
        )
        try:
            try: installed=self.gateway.installed_models()
            except Exception: installed=[]
            installed_lookup={str(m).casefold():str(m) for m in installed}
            style_model=(installed_lookup.get(str(self.settings.fast_model).casefold())
                         or installed_lookup.get(str(self.settings.model).casefold())
                         or self.settings.fast_model)
            compact_peer=[x[-260:] for x in peer[-4:]]
            compact_mine=[x[-220:] for x in mine[-6:]]
            prompt=(
                "Напиши ОДНО короткое Telegram-сообщение в стиле автора примеров. "
                "Ответь по смыслу на последнее сообщение собеседника. Только готовый текст, без кавычек и объяснений.\n"
                "СОБЕСЕДНИК:\n"+"\n".join(compact_peer)+"\nМОЙ СТИЛЬ:\n"+"\n".join(compact_mine)
                + ("\nКОНТЕКСТ ОТНОШЕНИЙ (только тон и степень формальности): " + relationship_context[:700] if relationship_context else "")
            )
            response=self.gateway.chat(
                [{"role":"system","content":"Верни только текст сообщения."},{"role":"user","content":prompt}],
                model=style_model,temperature=.32,think=False,num_ctx=900,num_predict=64,
                keep_alive=self.settings.keep_alive,timeout_seconds=10.0,
            )
            message=str(response.get("content") or "").strip().strip('«»"\'')
        except Exception as exc:
            return True,f"Не успела сформулировать ответ в твоём стиле: {humanize(exc)}",{"action":"telegram_style_model_failed","model":locals().get("style_model",self.settings.fast_model)}
        if not message or "в моем стиле" in normalize_phrase(message) or "в моём стиле" in message.casefold():
            return True,"Не получила нормальный текст ответа; ничего буквально не отправляю.",{"action":"telegram_style_guard","model":self.settings.fast_model}
        # A style model can be wrong even when the UI target is correct.  Keep the draft
        # visible and require one explicit approval before representing the owner.
        staged = self._save_messenger_pending({
            "recipient": target_name,
            "platform": "telegram",
            "message": message,
            "confirm": True,
            "current_chat": not bool(recipient),
            "at": time.time(),
        })
        exact = dict((staged or {}).get("payload") or {})
        return True, self._messenger_confirmation_prompt(exact), {
            "action":"telegram_style_confirmation",
            "model":locals().get("style_model",self.settings.fast_model),
            "completed":False,"verified":True,"draft":message,"recipient":target_name,
            "needs_user":True,
        }

    def _max_send_turn(self, recipient: str, message: str) -> tuple[bool, str, dict[str, Any]]:
        """Send one confirmed MAX draft through the visible web interface."""
        workflow = getattr(self, "universal_workflow", None)
        applications = getattr(getattr(getattr(self, "app_skills", None), "services", None), "applications", None)
        if workflow is None or applications is None:
            return True, "Текст подготовлен, но управление MAX сейчас недоступно.", {
                "action":"max_send_unavailable","completed":False,"verified":False,"draft":message,
            }
        try:
            opened = dict(applications.web_fallback("max") or {})
            if not opened.get("url"):
                raise RuntimeError("официальная веб-версия не открылась")
            time.sleep(0.8)
            target = recipient or "Избранное"
            navigation = workflow.accessible_goal(
                f"В MAX открой чат {target}", max_steps=9,
            )
            if not navigation.get("completed"):
                return True, (
                    "Открыла MAX, но не дошла до нужного чата. Если требуется вход, заверши его вручную; "
                    "черновик сохранён и не отправлен."
                ), {"action":"max_send_waiting_ui","completed":False,"verified":False,"needs_user":True,"draft":message,"result":navigation}
            result = workflow._current_window_text_fastpath(
                f'в текущем окне напиши текст «{message}» и отправь', text=message,
            ) or {}
            completed = bool(result.get("completed")); verified = bool(result.get("verified"))
            answer = (
                f"Отправила в MAX → {target}: «{message}»." if verified else
                ("Сообщение отправила один раз, но появление в чате подтвердить не смогла; повторять не буду." if completed
                 else "MAX открыт, но сообщение не отправлено; черновик сохранён.")
            )
            return True, answer, {"action":"max_send","completed":completed,"verified":verified,"draft":message,"result":result}
        except Exception as exc:
            return True, f"Не удалось отправить в MAX: {humanize(exc)}. Черновик сохранён.", {
                "action":"max_send_failed","completed":False,"verified":False,"draft":message,"error":str(exc),
            }

    def _confirmed_message_turn(self, pending: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        recipient = str(pending.get("recipient") or "").strip()
        platform = str(pending.get("platform") or "").strip().casefold().replace("ё", "е")
        message = str(pending.get("message") or "").strip()
        expected_surface = dict(pending.get("surface") or {})
        batch = pending.get("batch")
        if isinstance(batch, list) and batch and platform in {"telegram", "телеграм", "тг"}:
            outcomes: list[dict[str, Any]] = []
            for item in batch:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                target, body = str(item[0] or "").strip(), str(item[1] or "").strip()
                if not target or not body:
                    continue
                _acted, answer, route = self._telegram_send_turn(
                    f"{target} в telegram сообщение {body}",
                    exact_recipient=True,
                    expected_surface=expected_surface,
                )
                nested = route.get("result") if isinstance(route.get("result"), dict) else {}
                outcomes.append({
                    "recipient": target, "answer": answer,
                    "completed": bool(route.get("completed") or nested.get("completed") or route.get("action") == "telegram_send_verified"),
                    "verified": bool(route.get("verified") or nested.get("verified") or route.get("action") == "telegram_send_verified"),
                })
            verified = bool(outcomes) and all(bool(row.get("verified")) for row in outcomes)
            completed = bool(outcomes) and all(bool(row.get("completed")) for row in outcomes)
            return True, (
                "Пакет сообщений отправлен и подтверждён."
                if verified else ("Пакет обработан, но не все сообщения удалось подтвердить." if completed else "Пакетную отправку не удалось полностью выполнить.")
            ), {"action": "telegram_batch_confirmed", "completed": completed, "verified": verified, "outcomes": outcomes}
        if pending.get("current_chat") and platform in {"telegram", "телеграм", "тг"}:
            workflow = getattr(self, "universal_workflow", None)
            if workflow is None:
                return True, "Черновик сохранён, но управление текущим чатом недоступно.", {"action":"send_unavailable","verified":False}
            handle = self._coerce_window_id(expected_surface.get("handle"))
            if not handle or not self._messenger_surface_valid(expected_surface):
                return True, "Поверхность Telegram изменилась; сообщение не отправлено.", {
                    "action": "send_surface_drift", "completed": False, "verified": False,
                }
            if getattr(self, "tools", None) is not None:
                self.tools.execute("window_focus", {"handle": handle})
            result = workflow._current_window_text_fastpath(
                f'в текущем окне напиши текст «{message}» и отправь', text=message,
            ) or {}
            completed = bool(result.get("completed")); verified = bool(result.get("verified"))
            return True, (
                f"Отправила: «{message}»." if verified else
                ("Сообщение отправила один раз; повторять без подтверждения не буду." if completed else "Черновик не отправился.")
            ), {"action":"telegram_current_confirmed_send","completed":completed,"verified":verified,"result":result}
        if platform in {"max", "макс"}:
            return self._max_send_turn(recipient, message)
        return self._telegram_send_turn(
            f"{recipient} в telegram сообщение {message}",
            exact_recipient=True,
            expected_surface=expected_surface,
        )

    def _telegram_send_turn(
        self, target: str, *, exact_recipient: bool = False,
        expected_surface: dict[str, Any] | None = None,
    ) -> tuple[bool,str,dict[str,Any]]:
        recipient,platform,message=self._parse_send_target(target)
        if not platform:
            self._save_messenger_pending({"recipient":recipient,"platform":"","message":message,"awaiting":"platform","at":time.time()})
            return True,"В каком мессенджере отправить сообщение? Например, в Telegram.",{"action":"send_need_platform","model":"deterministic"}
        if not recipient:
            self._save_messenger_pending({"recipient":"","platform":platform,"message":message,"awaiting":"recipient","at":time.time()})
            return True,"Кому именно отправить сообщение?",{"action":"send_need_recipient","model":"deterministic"}
        if not message:
            self._save_messenger_pending({"recipient":recipient,"platform":platform,"message":"","awaiting":"message","at":time.time()})
            return True,f"Что написать {recipient}?",{"action":"send_need_text","model":"deterministic"}
        self._clear_messenger_pending()
        if self.app_skills is None:
            return True,"Экранный навык Telegram сейчас недоступен.",{"action":"send_unavailable","model":"deterministic"}
        if not exact_recipient:
            recipient = self._resolve_messenger_recipient(recipient, platform)
        surface = dict(expected_surface or {})
        if exact_recipient and not self._messenger_surface_valid(surface):
            return True, "Поверхность Telegram изменилась; сообщение не отправлено.", {
                "action": "telegram_send_surface_drift", "completed": False, "verified": False,
            }
        started=time.monotonic()
        result=self.app_skills.send_telegram(
            recipient, message,
            expected_surface=surface if exact_recipient else None,
        )
        if result.get("ok") and result.get("verified"):
            return True,self._self_gendered(f"Отправила {recipient}: «{message}».",f"Отправил {recipient}: «{message}»."),{"action":"telegram_send_verified","model":"screen-operator","result":result,"ms":round((time.monotonic()-started)*1000)}
        if result.get("ok"):
            return True,f"Сообщение ввела и отправила, но не смогла надёжно подтвердить его появление в чате {recipient}.",{"action":"telegram_send_unverified","model":"screen-operator","result":result}
        return True,f"Не удалось отправить сообщение через видимый экран: {result.get('error') or 'не нашла нужный элемент'}. Telegram уже открыла для восстановления.",{"action":"telegram_send_failed","model":"screen-operator","result":result}

    def _pending_send_turn(self, query: str, conversation_id: str = "") -> tuple[bool, str, dict[str, Any]]:
        """Continue a send clarification without hijacking unrelated chat.

        A previous implementation treated *any* non-command text as the missing message.
        Thus a stale ``Что написать?`` state turned a later ``Привет`` into a Telegram
        side effect. Continuations now have explicit ownership and an ambiguity gate.
        """
        cid = self._turn_conversation_id(conversation_id)
        owner = self._pending_confirmation_owner(cid)
        if owner is None or owner.kind != "messenger_send":
            return False, "", {}
        envelope = self._load_messenger_pending(cid)
        if not envelope or str(envelope.get("fingerprint") or "") != owner.fingerprint:
            return False, "", {}
        pending = dict(envelope.get("payload") or {})

        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        recipient = str(pending.get("recipient") or "").strip()
        platform = str(pending.get("platform") or "").strip()
        message = str(pending.get("message") or pending.get("proposed_message") or "").strip()
        confirming = bool(pending.get("confirm"))
        confirming_open = bool(pending.get("confirm_open"))

        cancel_send = bool(re.fullmatch(
            r"\s*(?:нигде|ни в каком|да ни в каком|не отправляй|не надо|отмена|отмени|стоп|забудь)(?: это)?[.!?]*\s*",
            clean,
        ))
        if cancel_send or is_cancel_confirmation(query):
            self._clear_messenger_pending(cid, fingerprint=owner.fingerprint)
            return True, "Отменила отправку сообщения.", {"action":"send_cancelled","model":"deterministic","control_plane":True}

        # Questions and new topics never belong to an abandoned messenger form.
        # This prevents the old "В каком мессенджере?" loop.
        send_words = bool(re.search(
            r"\b(?:отправ|напиш|скинь|сообщен|мессенджер|телеграм|telegram|получател|контакт)\w*\b",
            clean,
        ))
        question_words = bool(re.match(
            r"^(?:кто|что|где|когда|как|какая|какой|какое|какую|сколько|зачем|почему|умеешь|можешь|ты умеешь|ты можешь)\b",
            clean,
        ))
        if not send_words and ("?" in str(query) or question_words):
            self._clear_messenger_pending(cid, fingerprint=owner.fingerprint)
            return False, "", {}

        if confirming_open:
            # In `every` mode opening/focusing Telegram is itself a visible mutation.
            # The first explicit yes authorizes only that reversible preflight.  A
            # second, exact confirmation remains mandatory for the external send.
            if not is_affirmative_confirmation(query):
                return False, "", {}
            staged = self._save_messenger_pending({
                "recipient": recipient, "platform": platform, "message": message,
                "confirm": True, "at": time.time(),
            }, cid)
            exact = dict((staged or {}).get("payload") or {})
            surface = dict(exact.get("surface") or {})
            if not self._messenger_surface_valid(surface):
                # Keep the exact draft, but return to the reversible confirmation
                # phase.  A failed open attempt can never be promoted to send consent.
                self._save_messenger_pending({
                    "recipient": recipient, "platform": platform, "message": message,
                    "confirm_open": True, "at": time.time(),
                }, cid)
                return True, (
                    "Не удалось открыть и подтвердить Telegram; сообщение не вводила и "
                    "не отправляла. Повторить открытие? Скажи «да, открыть» или «отмена»."
                ), {
                    "action": "open_service_confirmation", "needs_user": True,
                    "completed": False, "verified": False,
                }
            return True, (
                "Telegram открыт и подтверждён. " + self._messenger_confirmation_prompt(exact)
            ), {
                "action": "send_confirmation", "needs_user": True,
                "completed": False, "verified": True, "surface": surface,
            }

        if confirming:
            if is_affirmative_confirmation(query):
                platform_n = platform.casefold().replace("ё", "е")
                if platform_n in {"telegram", "телеграм", "тг"}:
                    surface = dict(pending.get("surface") or {})
                    if not self._messenger_surface_valid(surface):
                        refreshed = self._ground_messenger_confirmation(pending)
                        refreshed_surface = dict(refreshed.get("surface") or {})
                        self._save_messenger_pending(refreshed, cid)
                        if not self._messenger_surface_valid(refreshed_surface):
                            return True, (
                                "Telegram пока не показал подтверждённую поверхность; ничего не отправлено. "
                                "Открой или авторизуй Telegram и снова подтверди точный черновик."
                            ), {"action":"send_surface_unavailable","needs_user":True,"completed":False,"verified":False}
                        return True, self._messenger_confirmation_prompt(refreshed, drift=True), {
                            "action":"send_surface_reconfirmation","needs_user":True,
                            "completed":False,"verified":True,
                        }
                # Consume the exact payload before the external send.  A duplicate "да"
                # sees no owner and cannot repeat the side effect.
                if not self._clear_messenger_pending(cid, fingerprint=owner.fingerprint):
                    return True, "Это подтверждение уже использовано или устарело.", {"action":"send_confirmation_stale","verified":True}
                return self._confirmed_message_turn(pending)
            # Only an explicit yes/cancel belongs to an exact confirmation. Any other
            # utterance is a new conversational turn; the fingerprinted draft remains
            # available but never hijacks a greeting or a new computer command.
            return False, "", {}

        changed = False
        if not platform and re.search(r"\b(?:телеграм\w*|telegram|тг)\b", clean, re.I):
            platform = "telegram"; changed = True

        if not recipient:
            explicit_recipient = re.match(
                r"^\s*(?:получатель|контакт|кому)\s*[:,-]?\s*[«\"']?(.+?)[»\"']?\s*[.!]*$",
                query, re.I,
            )
            raw_name = query.strip().strip('«»"\' .,!?:-')
            if explicit_recipient:
                recipient = explicit_recipient.group(1).strip(); changed = True
            elif not detect_command(query) and 1 <= len(raw_name.split()) <= 3 and not re.search(r"\b(?:телеграм\w*|telegram|тг)\b", clean):
                recipient = raw_name; changed = True

        if not message:
            explicit_message = re.match(
                r"^\s*(?:(?:текст|сообщение)\s*[:,-]?|(?:напиши|отправь|скинь)\w*\s+(?:(?:текст|сообщение)\s*)?)"
                r"[«\"']?(.+?)[»\"']?\s*$",
                query, re.I,
            )
            if explicit_message:
                message = explicit_message.group(1).strip().strip('«»"\''); changed = True
            elif not detect_command(query):
                proposed = query.strip().strip('«»"\'')
                if proposed:
                    message = proposed; changed = True

        if recipient and platform and message:
            # Continuations are inherently ambiguous, so completing one never performs
            # an external side effect in the same turn. One explicit confirmation is cheap
            # and prevents a stale dialogue from opening Telegram unexpectedly.
            state = {"recipient":recipient,"platform":platform,"message":message,"confirm":True,"at":time.time()}
            staged = self._save_messenger_pending(state, cid)
            exact = dict((staged or {}).get("payload") or state)
            return True, self._messenger_confirmation_prompt(exact), {"action":"send_confirmation","model":"deterministic","needs_user":True}

        if changed:
            awaiting = "platform" if not platform else ("recipient" if not recipient else "message")
            self._save_messenger_pending({"recipient":recipient,"platform":platform,"message":message,"awaiting":awaiting,"at":time.time()}, cid)
            prompt = "В каком мессенджере отправить?" if awaiting == "platform" else ("Кому именно отправить?" if awaiting == "recipient" else f"Что написать {recipient}?")
            return True, prompt, {"action":f"send_need_{awaiting}","model":"deterministic","needs_user":True}
        return False, "", {}

    def _narrow_service_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        """Deterministic drafts for narrow services before generic UI planning."""
        raw = str(query or "").strip()
        clean = normalize_phrase(raw)

        if re.search(r"\b(?:kwork|кворк)\b", clean, re.I):
            wants_projects = bool(re.search(r"\b(?:найди|покажи|ищи|нов\w*|заказ\w*|проект\w*|бирж\w*)\b", clean, re.I))
            wants_replies = bool(re.search(r"\b(?:ответь|ответить|не\s+ответил|неотвеченн\w*|сообщени\w*|заказчик\w*)\b", clean, re.I))
            if wants_replies:
                # There was no consumer for ``kwork_reply_checkpoint``: this branch only
                # opened Inbox and then claimed a future reply pass that could never run.
                # Let the reactive UI engine observe/login/navigate and create its real
                # typed risk checkpoint before any reply is sent.
                return False, "", {}
            if wants_projects:
                from .system_browser import open_url
                url = "https://kwork.ru/projects"
                opened = bool(open_url(url))
                summary = ""
                try:
                    if self.tools and self.tools.browser:
                        self.tools.browser.open(url)
                        time.sleep(1.2)
                        snapshot = self.tools.browser.snapshot(max_chars=18_000)
                        page_text = str(snapshot.get("text") or "").strip()
                        if page_text:
                            response = self.gateway.chat(
                                [
                                    {"role":"system","content":(
                                        "Из текста официальной биржи Kwork извлеки до пяти самых свежих видимых проектов. "
                                        "Для каждого дай название и бюджет/остаток времени, только если они явно есть. "
                                        "Не выдумывай. Ответ по-русски, короткими строками без вступления."
                                    )},
                                    {"role":"user","content":page_text[:18_000]},
                                ],
                                model=self.settings.fast_model, temperature=.05, think=False,
                                num_ctx=3200, num_predict=260, keep_alive=self.settings.keep_alive,
                                timeout_seconds=12.0,
                            )
                            summary = str(response.get("content") or "").strip()
                except Exception:
                    summary = ""
                answer = "Открыла свежую ленту «Биржа проектов» на официальном Kwork."
                if summary:
                    answer += "\n\nСейчас вверху ленты:\n" + summary
                else:
                    answer += " Список уже на экране; ничего не скачивала и отклики не отправляла."
                return True, answer, {"action":"kwork_projects","completed":opened,"verified":opened,
                    "url":url,"read_only":True,"summary_created":bool(summary)}

        max_favorite = re.search(
            r"(?:отправь|напиши|скинь)\w*\s+(?:в\s+)?(?:избранн\w*)\s+(?:в\s+)?(?:max|макс)\s+"
            r"(?:сообщен\w*|текст\w*)?\s*[«\"']?(.+?)[»\"']?[.!]?\s*$",
            raw, re.I,
        )
        if max_favorite:
            message = max_favorite.group(1).strip().strip('«»"\' .,!?:-')
            if message:
                self._save_messenger_pending({
                    "recipient":"Избранное","platform":"max","message":message,
                    "confirm":True,"at":time.time(),
                })
                return True, f"Подготовила MAX → Избранное: «{message}». Отправить?", {
                    "action":"max_send_confirmation","needs_user":True,"completed":False,"verified":True,"draft":message,
                }

        birthday = None
        if (
            re.search(r"\b(?:дн[её]м\s+рождени\w*|др)\b", raw, re.I)
            and re.search(r"\b(?:telegram|телеграм\w*|тг)\b", raw, re.I)
        ):
            # The platform may be named either before or after the occasion:
            # «Поздравь Тимофея в тг с днём рождения» and
            # «Поздравь Тимофея с днём рождения в Telegram» are equivalent.
            birthday = re.search(
                r"(?:поздравь|поздравить)\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,50})\b",
                raw, re.I,
            )
        if birthday:
            recipient = birthday.group(1).strip()
            recipient = {
                "тимофея":"Тимофей", "тимофею":"Тимофей",
                "даниилу":"Даниил", "даниила":"Даниил",
                "анну":"Анна", "анне":"Анна", "сашу":"Саша", "саше":"Саша",
                "ивана":"Иван", "ивану":"Иван", "максима":"Максим", "максиму":"Максим",
            }.get(recipient.casefold(), recipient)
            # A congratulation should be instant and predictable. Generating it through
            # the general chat model sometimes addressed the owner instead of the
            # recipient and took tens of seconds. Compose from the saved StyleDNA
            # settings without leaking the owner's name into the message.
            style = self.style.get()
            emoji = " 🎉" if str(style.emojis or "").casefold() not in {"нет", "никогда", "без эмодзи"} else ""
            if int(style.directness or 0) >= 4:
                message = (
                    f"{recipient}, с днём рождения! Здоровья, сил, денег и побольше реально классных моментов. "
                    f"Пусть всё задуманное получается без лишней суеты{emoji}"
                )
            else:
                message = (
                    f"{recipient}, с днём рождения! Пусть рядом будут свои люди, впереди — хорошие события, "
                    f"а сил и настроения хватает на всё важное{emoji}"
                )
            staged = self._save_messenger_pending({
                "recipient":recipient,"platform":"telegram","message":message,
                "confirm":True,"at":time.time(),
            })
            exact = dict((staged or {}).get("payload") or {})
            return True, self._messenger_confirmation_prompt(exact), {
                "action":"telegram_birthday_confirmation","needs_user":True,"completed":False,"verified":True,"draft":message,
            }
        return False, "", {}

    def _tool_result_answer(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        if not result.get("ok"):
            return f"Не получилось выполнить действие: {result.get('error') or 'неизвестная ошибка'}"
        inner = result.get("result") or {}
        verified = bool(
            result.get("verified") is True
            or (isinstance(inner, dict) and inner.get("verified") is True)
        )
        observable_actions = {
            "launch_application", "system_open_path", "system_open_named",
            "open_default_url", "default_search", "window_focus", "media_control",
            "system_volume", "hotkey", "close_application", "browser_click_text",
            "browser_fill", "browser_press",
        }
        if name in observable_actions and not verified:
            return "Команда отправлена, но ожидаемое состояние не подтвердилось."
        if name == "launch_application":
            shown = inner.get("name") if isinstance(inner, dict) else None
            base = self._self_gendered(
                f"Открыла {shown or args.get('application') or 'приложение'}.",
                f"Открыл {shown or args.get('application') or 'приложение'}.",
            )
        elif name in {"system_open_path", "system_open_named"}:
            target = inner.get('path') if isinstance(inner, dict) else args.get('path') or args.get('name')
            base = self._self_gendered(f"Открыла {target}.", f"Открыл {target}.")
        elif name == "open_default_url":
            base = self._self_gendered("Открыла в браузере.", "Открыл в браузере.")
        elif name == "default_search":
            base = self._self_gendered("Открыла поиск в браузере.", "Открыл поиск в браузере.")
        elif name == "window_focus":
            base = "Нужное окно получило фокус — это подтверждено."
        elif name == "project_create":
            base = self._self_gendered(
                "Запустила создание проекта в фоне. Можешь продолжать говорить со мной — работа уже идёт.",
                "Запустил создание проекта в фоне. Можешь продолжать говорить со мной — работа уже идёт.",
            )
        elif name == "project_modify":
            base = self._self_gendered("Приняла правку и вношу её в текущий проект.", "Принял правку и вношу её в текущий проект.")
        elif name == "task_resume":
            base = "Продолжаю ту же задачу."
        else:
            base = "Постусловие действия подтверждено."
        return base


    def _trace(self, event: str, **payload: Any) -> None:
        log_event(self.settings.root_dir, event, **payload)

    def _spatial_position(self, text: str) -> dict[str, float]:
        clean=str(text or "").casefold().replace("ё","е")
        x,y=.12,.12
        if re.search(r"\b(?:справа|правее|в\s+прав\w*|прав\w*\s+угол)\b",clean): x=.68
        elif re.search(r"\b(?:слева|левее|в\s+лев\w*|лев\w*\s+угол)\b",clean): x=.12
        elif re.search(r"\b(?:по центру|центр|в центре)\b",clean): x=.40
        if re.search(r"\b(?:снизу|внизу|нижн\w*|нижн\w*\s+угол)\b",clean): y=.66
        elif re.search(r"\b(?:посередине|по центру|в центре)\b",clean): y=.38
        elif re.search(r"\b(?:сверху|наверху|вверху|верхн\w*|верхн\w*\s+угол)\b",clean): y=.10
        return {"left_pct":round(x*100,1),"top_pct":round(y*100,1)}

    def _spatial_widget(self, kind: str, title: str, body: str, **meta: Any) -> dict[str, Any]:
        widgets = self.db.get_setting("spatial_widgets", [])
        if not isinstance(widgets, list):
            widgets = []
        item = {
            "id": hashlib.sha1(f"{kind}:{title}".encode("utf-8", errors="ignore")).hexdigest()[:12],
            "kind": kind, "title": title[:80], "body": body[:1200],
            "updated_at": datetime.now().timestamp(), **meta,
        }
        previous=next((dict(w) for w in widgets if isinstance(w,dict) and w.get("id")==item["id"]),{})
        for key in ("left_pct","top_pct","width_pct","height_pct"):
            if key not in item and key in previous: item[key]=previous[key]
        widgets = [w for w in widgets if isinstance(w, dict) and w.get("id") != item["id"]]
        widgets.append(item)
        self.db.set_setting("spatial_widgets", widgets[-8:])
        return item

    def _move_spatial(self, target: str) -> tuple[bool,str,dict[str,Any]]:
        widgets=self.db.get_setting("spatial_widgets",[])
        if not isinstance(widgets,list) or not widgets:
            return True,"На экране пока нет виджетов, которые можно передвинуть.",{"action":"spatial_move_empty","model":"deterministic"}
        clean=str(target or "").casefold().replace("ё","е")
        wanted=None
        if re.search(r"\b(?:сайт|браузер|ютуб|youtube|телеграм|telegram|яндекс|spotify|спотифай|discord|дискорд|bybit|байбит)\b",clean):
            pos=self._spatial_position(target); pos["updated_at"]=datetime.now().timestamp()
            self.db.set_setting("spatial_browser_layout",pos)
            return True,"Передвинула пространственный сайт.",{"action":"spatial_browser_move","model":"deterministic",**pos}
        if re.search(r"\b(?:час|врем)\w*",clean): wanted="clock"
        elif re.search(r"\b(?:погод)\w*",clean): wanted="weather"
        elif re.search(r"\b(?:курс|доллар|usd)\w*",clean): wanted="rate"
        elif re.search(r"\b(?:систем|cpu|ram|нагруз)\w*",clean): wanted="system"
        pos=self._spatial_position(target)
        changed=0
        for item in widgets:
            if not isinstance(item,dict): continue
            if wanted and item.get("kind")!=wanted: continue
            item.update(pos); item["updated_at"]=datetime.now().timestamp(); changed+=1
            if wanted: break
        self.db.set_setting("spatial_widgets",widgets)
        return True,("Передвинула виджет." if changed else "Не нашла такой виджет на экране."),{"action":"spatial_move","model":"deterministic","changed":changed,**pos}

    def _fast_text_answer(
        self,
        prompt: str,
        *,
        num_predict: int = 320,
        timeout: float = 8.0,
        preferred_model: str = "",
    ) -> str:
        installed = {m.lower(): m for m in self.gateway.installed_models()}
        requested = str(preferred_model or self.settings.fast_model).strip()
        model = installed.get(requested.lower()) or installed.get(self.settings.fast_model.lower()) or installed.get(self.settings.model.lower()) or requested
        response = self.gateway.chat(
            [
                {"role": "system", "content": "Отвечай по-русски, кратко и конкретно. Не обещай действий, которых не выполнял."},
                {"role": "user", "content": prompt},
            ],
            model=model, temperature=0.1, think=False,
            num_ctx=min(max(self.settings.chat_num_ctx, 1024), 4096),
            num_predict=num_predict, timeout_seconds=timeout,
        )
        return str(response.get("content") or "").strip()

    @staticmethod
    def _strip_generated_code(text: str) -> str:
        value = str(text or "").strip()
        fenced = re.fullmatch(r"```(?:[A-Za-z0-9_+.-]+)?\s*\n?(.*?)\n?```", value, re.S)
        if fenced:
            value = fenced.group(1).strip("\n")
        return value + ("\n" if value and not value.endswith("\n") else "")

    @staticmethod
    def _desktop_directory() -> Path:
        """Resolve the real Windows Desktop, including redirected/OneDrive profiles."""
        if os.name == "nt":
            try:
                import ctypes
                buffer = ctypes.create_unicode_buffer(32768)
                # CSIDL_DESKTOPDIRECTORY = 0x0010. SHGetFolderPath honours shell redirection.
                if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer) == 0:
                    value = Path(buffer.value).expanduser()
                    if str(value):
                        return value
            except Exception:
                pass
        candidates = [
            Path(os.environ.get("OneDrive", "")) / "Desktop" if os.environ.get("OneDrive") else None,
            Path(os.environ.get("USERPROFILE", "")) / "Desktop" if os.environ.get("USERPROFILE") else None,
            Path.home() / "Desktop",
        ]
        for candidate in candidates:
            if candidate and candidate.is_dir():
                return candidate
        return Path.home() / "Desktop"

    def _code_file_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        """Generate code into an explicitly requested local file and verify readback.

        This fast path exists for small one-file requests (``zmeika.py`` etc.). Creating a
        full application still belongs to the project worker.
        """
        raw = str(query or "").strip()
        clean = normalize_phrase(raw)
        # This route creates new source files. Read/inspect/count requests must stay in
        # the universal file lane; ``кодовое слово`` used to match the broad ``код\w*``
        # check and caused EIRVEN to overwrite a same-named Desktop file instead of
        # reading the requested workspace file.
        if not re.search(
            r"\b(?:создай|сделай|напиши|сгенерируй|разработай|реализуй|сохрани|запиши|положи|оставь|create|write|generate|implement)\w*",
            clean,
            re.I,
        ):
            return False, "", {}
        if not re.search(r"\b(?:код|скрипт|python|питон|javascript|typescript|html|css)\w*", clean, re.I):
            return False, "", {}
        file_match = re.search(
            r"(?<![\w.-])([A-Za-zА-Яа-яЁё0-9_-]{1,80}\.(?:py|js|mjs|cjs|ts|tsx|html|css|json|txt|md))\b",
            raw, re.I,
        )
        if not file_match:
            return False, "", {}
        if not re.search(r"\b(?:в\s+файл|файл|сохрани|запиши|положи|оставь|рабоч\w*\s+стол|desktop)\b", clean, re.I):
            return False, "", {}
        if self.tools is None:
            return True, "Не могу записать файл: файловый контур сейчас недоступен.", {"action":"code_file_unavailable","completed":False}

        filename = Path(file_match.group(1)).name
        settings = getattr(self, "settings", None)
        workspace_dir = Path(getattr(settings, "workspace_dir", self._desktop_directory()))
        destination = (
            self._desktop_directory() / filename
            if re.search(r"\b(?:рабоч\w*\s+стол|desktop)\b", clean, re.I)
            else workspace_dir / filename
        )
        suffix = destination.suffix.casefold()
        language = {
            ".py":"Python", ".js":"JavaScript", ".mjs":"JavaScript", ".cjs":"JavaScript",
            ".ts":"TypeScript", ".tsx":"TypeScript/TSX", ".html":"HTML", ".css":"CSS",
            ".json":"JSON", ".md":"Markdown", ".txt":"plain text",
        }.get(suffix, "text")
        prompt = (
            f"Создай полное содержимое одного файла {filename} ({language}) по просьбе владельца. "
            "Верни ТОЛЬКО содержимое файла без Markdown-ограждений, объяснений и многоточий. "
            "Код должен быть самодостаточным и запускаемым, где это применимо. "
            f"Запрос: {raw}"
        )
        try:
            # A cold 16B checkpoint often needs more than 24 seconds before its first
            # useful token on CPU/low-VRAM Windows PCs.  Use the configured code model
            # and the release cold-start budget instead of reporting a false failure.
            local_settings = getattr(self, "settings", None)
            generation_timeout = max(120.0, float(getattr(local_settings, "llm_first_token_timeout", 120.0)) + 60.0)
            generated = self._strip_generated_code(self._fast_text_answer(
                prompt,
                num_predict=1800,
                timeout=generation_timeout,
                preferred_model=str(getattr(local_settings, "code_model", "")),
            ))
        except Exception as exc:
            return True, f"Не смогла сгенерировать содержимое {filename}: {humanize(exc)}", {"action":"code_file_generation_failed","completed":False}
        if not generated.strip():
            return True, f"Модель вернула пустое содержимое для {filename}; файл не создаю.", {"action":"code_file_empty","completed":False}

        syntax_verified = True
        if suffix == ".py":
            import ast
            try:
                ast.parse(generated, filename=filename)
            except SyntaxError as exc:
                repair_prompt = (
                    f"Исправь синтаксис файла {filename}. Верни только полный исправленный Python-код без Markdown. "
                    f"Ошибка: {exc.msg}, строка {exc.lineno}.\nТекущий код:\n{generated}"
                )
                try:
                    generated = self._strip_generated_code(self._fast_text_answer(
                        repair_prompt,
                        num_predict=1800,
                        timeout=generation_timeout,
                        preferred_model=str(getattr(local_settings, "code_model", "")),
                    ))
                    ast.parse(generated, filename=filename)
                except Exception:
                    syntax_verified = False
        if not syntax_verified:
            return True, f"Код для {filename} получился синтаксически некорректным; плохой файл не сохраняю.", {"action":"code_file_syntax_failed","completed":False}

        result = self.tools.execute("system_write_file", {"path": str(destination), "content": generated, "overwrite": True})
        inner = result.get("result") if isinstance(result.get("result"), dict) else {}
        verified = bool(result.get("ok") and inner.get("verified") and inner.get("readback_verified"))
        route = {
            "action":"code_file_written" if verified else "code_file_write_failed",
            "model":"local-code", "completed":verified, "verified":verified,
            "path":str(destination), "result":result,
        }
        if verified:
            return True, self._self_gendered(
                f"Написала код и сохранила проверенный файл {filename}: {destination}.",
                f"Написал код и сохранил проверенный файл {filename}: {destination}.",
            ), route
        return True, f"Содержимое {filename} подготовила, но запись файла не прошла проверку: {result.get('error') or 'readback не подтвердился'}.", route

    def _analyse_attachments_now(self, query: str, attachment_paths: list[str] | None, image_paths: list[str] | None) -> str:
        paths = list(attachment_paths or [])
        images = list(image_paths or [])
        if not paths and not images:
            return ""
        pieces: list[str] = []
        for path in images[:4]:
            answer = self._vision_for_path(path, query)
            pieces.append(f"{Path(path).name}: {answer}")
        non_image = [p for p in paths if p not in images]
        if non_image:
            context = extract_attachment_context(non_image, total_limit=9_000)
            if context:
                prompt = (
                    f"Запрос владельца: {query}\n\nНиже реальные локально извлечённые данные из прикреплённых файлов. "
                    "Проанализируй именно их; перечисли важные выводы и проблемы.\n\n" + context
                )
                try:
                    pieces.append(self._fast_text_answer(prompt, num_predict=280, timeout=6.0))
                except Exception as exc:
                    pieces.append(f"Не удалось завершить текстовый анализ вложений: {humanize(exc)}")
        return "\n\n".join(piece for piece in pieces if piece).strip()

    def _toggle_vpn(self, enabled: bool) -> tuple[bool, str, dict[str, Any]]:
        if self.tools is None:
            return False, "", {}
        state_word = "подключён" if enabled else "отключён"
        if os.name == "nt":
            # First use a configured Windows VPN profile; credentials remain managed by Windows.
            script = (
                "$v=Get-VpnConnection -ErrorAction SilentlyContinue | Select-Object -First 1; "
                "if($null -eq $v){exit 7}; "
                + ("if($v.ConnectionStatus -ne 'Connected'){rasdial $v.Name | Out-Null}; " if enabled else "if($v.ConnectionStatus -eq 'Connected'){rasdial $v.Name /disconnect | Out-Null}; ")
                + "$v.Name"
            )
            result = self.tools.execute("powershell", {"command": script, "cwd": str(self.settings.root_dir), "timeout": 18})
            if result.get("ok"):
                return True, f"VPN {state_word}.", {"action": "vpn", "model": "deterministic", "tools": [{"name": "powershell", "result": result}]}
        # Third-party VPN: launch an installed VPN-looking app, then try common UI labels.
        try:
            apps = self.tools.applications.list_installed() if self.tools.applications else []
            markers = ("vpn", "nord", "proton", "amnezia", "windscribe", "outline", "wireguard", "openvpn")
            candidate = next((a for a in apps if any(m in str(a.get("name") or "").casefold() for m in markers)), None)
            if candidate:
                launched = self.tools.execute("launch_application", {"application": str(candidate.get("name") or "VPN")})
                if launched.get("ok"):
                    time.sleep(0.8)
                    windows = self.tools.execute("window_list", {"max_windows": 50})
                    rows = list(windows.get("result") or []) if windows.get("ok") else []
                    title = next((str(w.get("title") or "") for w in rows if any(m in str(w.get("title") or "").casefold() for m in markers)), str(candidate.get("name") or ""))
                    if title:
                        elements = self.tools.execute("window_elements", {"title_contains": title, "max_elements": 180})
                        labels_on = ("подключить", "включить", "connect", "quick connect", "start")
                        labels_off = ("отключить", "выключить", "disconnect", "stop")
                        wanted = labels_on if enabled else labels_off
                        for el in list(elements.get("result") or []) if elements.get("ok") else []:
                            name = str(el.get("name") or "").casefold()
                            if any(label in name for label in wanted):
                                clicked = self.tools.execute("window_click", {"title_contains": title, "element_text": str(el.get("name") or ""), "control_type": str(el.get("control_type") or "")})
                                if clicked.get("ok"):
                                    return True, f"Открыла VPN и нажала {'подключение' if enabled else 'отключение'}.", {"action": "vpn_app", "model": "deterministic"}
                    return True, "Открыла VPN. Автоматическая кнопка подключения не нашлась — приложение уже перед тобой.", {"action": "vpn_app_open", "model": "deterministic"}
        except Exception:
            pass
        return True, "VPN-профиль или установленное VPN-приложение не нашлись.", {"action": "vpn_not_found", "model": "deterministic"}

    @staticmethod
    def _self_shutdown_requested(query: str) -> bool:
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        return bool(re.fullmatch(
            r"(?:пожалуйста\s+)?(?:(?:эрви|эрви|eirven)[, :!-]*)?(?:выключи|(?:(?:выключи|закрой|заверши|останови)\s+(?:себя|эрви|эрви|eirven)(?:\s+полностью)?)|"
            r"выключись|выключися|отключись|закройся|заверши\s+работу)[.! ]*",
            clean, re.I,
        ))

    def _schedule_self_shutdown(self) -> None:
        root = Path(getattr(self.settings, "root_dir", "."))
        stop_file = root / "logs" / "stop.request"
        def worker() -> None:
            # Give the deterministic acknowledgement/TTS a small head start, then let
            # the supervisor perform its normal identity-safe shutdown path.
            time.sleep(1.8)
            try:
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text(str(time.time()), encoding="utf-8")
            except Exception as exc:
                try: self._trace("SELF_SHUTDOWN_MARKER_ERROR", error=str(exc))
                except Exception: pass
        threading.Thread(target=worker, daemon=True, name="eirven-self-shutdown").start()

    def _r22_capabilities_answer(self, *, short: bool = False) -> str:
        if short:
            return (
                "Могу управлять Windows, приложениями, окнами и сайтами; работать с файлами, кодом, почтой, Telegram и видео; "
                "искать информацию и выполнять проверяемые цепочки действий. Если шаг не подтверждён, скажу об этом прямо, "
                "а отправка, удаление и другие рискованные действия потребуют твоего подтверждения."
            )
        return (
            "Я умею управлять Windows, окнами и приложениями, читать текущий интерфейс, работать с файлами, "
            "PowerShell, сайтами и Telegram и выполнять составные задачи между приложениями. В открытом VS Code "
            "могу найти баг в реальной папке проекта, изменить код и прогнать проверку; проблемы приложений могу "
            "диагностировать по процессам, экрану, локальным данным и логам, а по явной просьбе — переустановить "
            "однозначно найденное приложение. Длинные задачи умеют переносить найденный текст или файл между шагами, "
            "например из МЭШ в Избранное Telegram. Ещё я умею реально монтировать и обрабатывать видео через локальный "
            "FFmpeg: склеивать, обрезать, менять качество, формат, скорость, звук, кадр и добавлять текст. Для этого положи "
            "исходники в папку video рядом с EIRVEN и скажи обычными словами, что сделать. В фоне замечаю долгий просмотр "
            "видео, зависшие окна и видимые ошибки. "
            "Логин, CAPTCHA, 2FA/UAC и физические действия владельца остаются ручными точками, если Windows или сервис их требуют."
        )

    @staticmethod
    def _plan_only_answer(query: str) -> str:
        """Turn an explicit dry-run request into a plan without touching any tool lane."""
        body = str(query or "").strip()
        body = re.sub(
            r"^.*?(?:только\s+)?(?:составь|напиши|покажи)\s+(?:короткий\s+|краткий\s+)?план\s*[:—-]?\s*",
            "",
            body,
            count=1,
            flags=re.I | re.S,
        ).strip()
        if not body:
            return "Ничего не выполняю. Опиши желаемый результат — разложу его на проверяемые шаги."
        parts = re.split(
            r"\s*(?:,|;|\.)\s*(?=(?:а\s+потом\s+|потом\s+|затем\s+|и\s+)?(?:открыть|запустить|написать|ввести|сохранить|проверить|закрыть|создать|найти|отправить|удалить)\b)|"
            r"\s+(?:а\s+потом|потом|затем|после\s+этого|и)\s+(?=(?:открыть|запустить|написать|ввести|сохранить|проверить|закрыть|создать|найти|отправить|удалить)\b)",
            body,
            flags=re.I,
        )
        steps = [re.sub(r"^(?:а\s+потом|потом|затем)\s+", "", item.strip(), flags=re.I) for item in parts if item.strip()]
        steps = steps[:8] or [body]
        rendered = "\n".join(f"{index}. {step[:1].upper() + step[1:].rstrip(' .')} .".replace(" .", ".") for index, step in enumerate(steps, 1))
        return f"План без выполнения:\n{rendered}"

    def _latest_failure_hint(self) -> str:
        try:
            rows = self.db.recent_action_logs(6)
        except Exception:
            return ""
        for row in rows:
            if bool(row.get("success")):
                continue
            try:
                result = json.loads(row.get("result") or "{}") if isinstance(row.get("result"), str) else (row.get("result") or {})
            except Exception:
                result = {}
            reason = str(result.get("error") or ((result.get("result") or {}).get("error") if isinstance(result.get("result"), dict) else "") or "").strip()
            tool = str(row.get("tool") or "действие")
            if reason:
                return f" Последний подтверждённый сбой: {tool} — {reason[:260]}. Подробности есть в «Журнале действий»."
        return " Подробности последней попытки есть в «Журнале действий»."

    @staticmethod
    def _mail_request(query: str) -> bool:
        q = normalize_phrase(query)
        return bool(re.search(r"\b(почт\w*|email|e-mail|письм\w*|инбокс|inbox)\b", q, re.I))

    def _mail_turn(
        self,
        query: str,
        conversation_id: str = "",
        *,
        typed_action: str = "",
    ) -> tuple[bool, str, dict[str, Any]]:
        """Authoritative boundary for the configured mail connector.

        Confirmation still owns irreversible SMTP sends.  Informational mailbox turns
        are sealed inside the configured IMAP adapter: once connected mail is available,
        a read cannot fall through to a desktop/web agent or turn a security finding into
        an unrelated GUI action.
        """
        service = getattr(self, "mail", None)
        if service is None:
            return False, "", {}
        cid = self._turn_conversation_id(conversation_id)
        q = normalize_phrase(query)
        setup_help = bool(re.search(
            r"(?:как|помоги|подскажи|объясни|что\s+такое|что\s+писать).{0,80}"
            r"(?:настроить|подключить|настройк|поле|порт|сервер|пароль).{0,50}(?:почт\w*|email|e-mail)|"
            r"(?:почт\w*|email|e-mail).{0,80}(?:как\s+настроить|как\s+подключить|что\s+писать|объясни\s+поля)|"
            r"\b(?:imap|smtp|пароль\s+приложения)\b",
            q,
            re.I | re.S,
        ))
        if setup_help:
            answer = (
                "Открываю «Настройки → Почта». Заполняй так:\n"
                "1. Email — полный адрес ящика.\n"
                "2. Пароль приложения — отдельный пароль из безопасности почты, не обычный пароль аккаунта.\n"
                "3. IMAP читает входящие: обычно SSL, порт 993.\n"
                "4. SMTP передаёт исходящие: обычно STARTTLS, порт 587.\n"
                "Введи email и нажми «Подобрать серверы» — я подставлю поля для Яндекса, Mail.ru, Gmail или Outlook. Пароль остаётся только на этом ПК и шифруется Windows DPAPI."
            )
            return True, answer, {
                "action": "mail_setup_help",
                "model": "deterministic",
                "verified": True,
                "open_settings_tab": "mail",
            }
        monitor_request = bool(re.search(
            r"\b(?:монитор\w*|следи\w*|отслежива\w*|24\s*/\s*7|проверяй\w*)\b",
            q, re.I,
        ))
        if monitor_request and self._mail_request(query):
            if re.search(r"\b(?:останови|выключи|прекрати|отмени)\w*\b", q, re.I):
                status = service.stop_monitor()
                return True, str(status.get("message") or "Фоновая проверка почты остановлена."), {
                    "action": "mail_monitor_stop", "verified": not bool(status.get("running")),
                }
            if not service.configured():
                return True, (
                    "Сначала подключи почту в «Настройки → Почта». Введи email, нажми «Подобрать серверы» и укажи пароль приложения. "
                    "После проверки повтори команду мониторинга."
                ), {"action": "mail_setup_required", "verified": True, "open_settings_tab": "mail"}
            wants_spam_cleanup = bool(re.search(r"\b(?:спам\w*|мусор\w*|junk)\b", q, re.I))
            if wants_spam_cleanup:
                service.set_auto_move_obvious_spam(True)
            status = service.start_monitor()
            spam_note = (
                " Письма, которые сам сервер однозначно пометил как спам, буду переносить в папку «Спам» — без безвозвратного удаления."
                if wants_spam_cleanup else ""
            )
            return True, (
                "Фоновая проверка почты запущена 24/7. Я буду находить непрочитанные письма и готовить ответы в черновики. "
                "Перед реальной SMTP-отправкой всё равно попрошу подтверждение, чтобы не разослать ошибочный ответ."
                + spam_note
            ), {"action": "mail_monitor_start", "verified": bool(status.get("running")), "draft_only": True,
                 "auto_move_obvious_spam": wants_spam_cleanup}
        owner = self._pending_confirmation_owner(cid)
        pending = service.pending(cid, fingerprint=owner.fingerprint) if owner and owner.kind == "mail_send" else None
        if pending:
            if is_cancel_confirmation(q):
                service.cancel_pending(cid, fingerprint=owner.fingerprint)
                return True, "Отменила отправку. Черновик остался локальным.", {"action":"mail_send_cancelled","verified":True}
            if is_affirmative_confirmation(q):
                try:
                    result = service.send_pending(cid, fingerprint=owner.fingerprint)
                    note = str(result.get("note") or "")
                    answer = (f"Отправила и перепроверила письмо на {result.get('to')}. {note}" if result.get('verified') else f"SMTP принял письмо для {result.get('to')}, но я пока не подтверждаю появление копии в «Отправленных». {note}").strip()
                    return True, answer, {"action":"mail_send","verified":bool(result.get("verified")),"result":result}
                except Exception as exc:
                    try: self.db.log_action("mail_send", {"pending": True}, {"error": str(exc)}, "high", False)
                    except Exception: pass
                    return True, f"Письмо не отправлено: {humanize(exc)}", {"action":"mail_send","verified":False,"error":str(exc)}

        # Draft preparation is reversible and stays inside the configured connector;
        # SMTP still requires a separate exact-draft confirmation below.
        prepare_reply_request = bool(re.search(
            r"\b(?:подготовь|составь|сделай)\w*\s+(?:ответ\w*|черновик\w*)|"
            r"\b(?:ответ\w*|черновик\w*)\s+на\s+(?:важн\w*|непрочитанн\w*)",
            q, re.I,
        ))
        if prepare_reply_request and self._mail_request(query):
            if not service.configured():
                return True, "Почта пока не подключена. Открой Настройки → Почта.", {
                    "action": "mail_setup_required", "verified": True,
                }
            try:
                prepared = service.review(
                    limit=20, prepare_replies=True, move_spam=False, read_only=False,
                )
                drafts = list(prepared.get("drafts") or []) if isinstance(prepared, dict) else []
                count = len(drafts)
                if count:
                    suffix = "черновик" if count == 1 else ("черновика" if count <= 4 else "черновиков")
                    answer = (
                        f"Подготовила {count} локальных {suffix}. Ничего не отправлено. "
                        "Назови номер черновика, чтобы показать точный текст и отдельно запросить подтверждение."
                    )
                else:
                    answer = "Не нашла подходящих писем для черновиков. Ничего не отправляла."
                return True, answer, {
                    "action": "authoritative_mail_prepare_drafts",
                    "model": "local-mail", "verified": bool(prepared.get("verified")) if isinstance(prepared, dict) else False,
                    "connector": "configured-imap", "read_only": False,
                    "prepare_replies": True, "move_spam": False, "draft_count": count,
                }
            except Exception as exc:
                try:
                    self.db.log_action(
                        "mail_prepare_drafts",
                        {"read_only": False, "prepare_replies": True, "move_spam": False},
                        {"error": type(exc).__name__, "verified": False}, "medium", False,
                    )
                except Exception:
                    pass
                return True, (
                    "Не удалось подготовить локальные черновики через подключённую почту. "
                    "Веб-почту и другой аккаунт не открываю."
                ), {
                    "action": "authoritative_mail_prepare_drafts", "model": "local-mail",
                    "verified": False, "connector": "configured-imap", "read_only": False,
                    "error": type(exc).__name__,
                }

        m = re.search(r"(?:отправь|пошли|выбери|подготовь)\s+(?:черновик\s*)?(\d+)", q, re.I)
        if m and self._mail_request(query):
            if not service.configured():
                return True, "Почта пока не подключена. Открой Настройки → Почта.", {"action":"mail_setup_required","verified":True}
            try:
                draft = service.stage_draft(int(m.group(1)), conversation_id=cid)
                body = str(draft.get("body") or "").strip().replace("\n", " ")[:260]
                answer = f"Подготовила отправку черновика {m.group(1)} → {draft.get('to')}: «{body}». Отправить?"
                return True, answer, {"action":"mail_send_confirmation","verified":True,"requires_confirmation":True}
            except Exception as exc:
                return True, f"Не получилось выбрать черновик: {humanize(exc)}", {"action":"mail_draft_select","verified":False}

        # A draft number is a continuation of the immediately preceding local-draft
        # result.  Resolve it from the typed draft store instead of sending the turn to
        # the generic language model, which previously answered "1" as unrelated chat.
        draft_tokens = [
            token.strip(".,!?;:()[]{}\"'«»—-")
            for token in q.casefold().split()
            if token.strip(".,!?;:()[]{}\"'«»—-")
        ]
        draft_number = next((int(token) for token in draft_tokens if token.isdigit()), None)
        preview_requested = bool(
            draft_number is not None
            and (
                (len(draft_tokens) == 1)
                or any(token.startswith(("покаж", "открой", "прочит", "просмотр")) for token in draft_tokens)
                and any(token.startswith("чернов") for token in draft_tokens)
            )
        )
        if preview_requested:
            if not service.configured():
                return True, "Почта пока не подключена. Открой Настройки → Почта.", {"action": "mail_setup_required", "verified": True}
            try:
                draft = service.get_draft(int(draft_number))
                body = str(draft.get("body") or "").strip()
                answer = (
                    f"Черновик {draft_number}\n"
                    f"Кому: {draft.get('to') or 'не указан'}\n"
                    f"Тема: {draft.get('subject') or 'без темы'}\n\n{body}\n\n"
                    "Ничего не отправлено. Для отправки скажи: «отправь черновик "
                    f"{draft_number}» — я покажу точный payload и попрошу подтверждение."
                )
                return True, answer, {
                    "action": "authoritative_mail_draft_preview",
                    "model": "local-mail",
                    "verified": True,
                    "draft_index": int(draft_number),
                    "send_staged": False,
                }
            except Exception as exc:
                return True, f"Не получилось показать черновик: {humanize(exc)}", {
                    "action": "mail_draft_preview", "verified": False,
                }

        # Connected mail is an authoritative typed capability.  Every remaining mail
        # request is consumed here, including adapter errors, so an IMAP result can never
        # be followed by Outlook/Gmail/browser automation in the same turn.
        typed_read = str(typed_action or "") in {"mail_review", "mail_status"}
        if (typed_read or self._mail_request(query)) and service.configured():
            words = {
                token.strip(".,!?;:()[]{}\"'«»—-")
                for token in q.casefold().split()
                if token.strip(".,!?;:()[]{}\"'«»—-")
            }
            mutation_roots = ("отправ", "пошл", "ответь", "удал", "перемест", "архив", "помет")
            explicit_compose = bool(words & {"напиши", "написать", "составь", "составить", "подготовь", "подготовить"})
            mutating = explicit_compose or any(
                word.startswith(root) for word in words for root in mutation_roots
            )
            if mutating:
                return True, (
                    "Подключённая почта доступна напрямую, поэтому веб-почту не открываю. "
                    "Для исходящего или изменяющего письма уточни адресата и текст; перед "
                    "SMTP-отправкой я отдельно покажу точный черновик и попрошу подтверждение."
                ), {
                    "action": "authoritative_mail_clarification",
                    "model": "local-mail",
                    "verified": True,
                    "connector": "configured-imap-smtp",
                    "read_only": False,
                }
            try:
                review = service.review_read_only(limit=20)
                answer = service.summarize_read_only(query, review)
                return True, answer, {
                    "action": "authoritative_mail_read",
                    "model": "local-mail-summary",
                    "verified": bool(review.get("verified")),
                    "connector": "configured-imap",
                    "read_only": True,
                    "unread": int(review.get("unread") or 0),
                    "reviewed": len(list(review.get("messages") or [])),
                }
            except Exception as exc:
                try:
                    self.db.log_action(
                        "mail_review",
                        {"read_only": True, "source": "chat"},
                        {"error": type(exc).__name__, "verified": False},
                        "low",
                        False,
                    )
                except Exception:
                    pass
                return True, (
                    "Не удалось прочитать подключённую почту через IMAP. Веб-почту и "
                    "другой аккаунт не открываю, чтобы не проверить не тот ящик. "
                    "Проверь соединение в «Настройки → Почта» и повтори запрос."
                ), {
                    "action": "authoritative_mail_read",
                    "model": "local-mail",
                    "verified": False,
                    "connector": "configured-imap",
                    "read_only": True,
                    "error": type(exc).__name__,
                }
        return False, "", {}

    def _typed_semantic_turn(
        self,
        query: str,
        conversation_id: str,
        decision: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any]]:
        """Dispatch one validated semantic envelope to an authoritative capability.

        This boundary contains no natural-language regex router.  The local model has
        already returned one typed action, and the engine either executes that exact
        connector/preflight or declines.  In particular, mail cannot fall through to a
        conversational answer and message sending cannot be reported without a visible,
        fingerprinted confirmation flow.
        """
        if not isinstance(decision, dict):
            return False, "", {}
        action = str(decision.get("action") or "unknown")
        if action in {"mail_review", "mail_status"}:
            service = getattr(self, "mail", None)
            try:
                configured = bool(service is not None and service.configured())
            except Exception:
                configured = False
            if not configured:
                return True, (
                    "Почта не подключена. Открой «Настройки → Почта» и подключи нужный "
                    "ящик; веб-почту и аккаунт сама не выбираю."
                ), {
                    "action": "mail_setup_required", "model": "semantic-capability",
                    "verified": True, "needs_user": True,
                    "open_settings_tab": "mail",
                }
            acted, answer, route = self._mail_turn(
                query, conversation_id, typed_action=action,
            )
            if acted:
                return acted, answer, route
            # A configured connector is authoritative even if its adapter rejected an
            # unusual phrasing.  Never hand it to free-form chat or browser automation.
            return True, (
                "Подключённый почтовый ящик найден, но чтение через IMAP не завершилось. "
                "Веб-почту и другой аккаунт не открываю."
            ), {
                "action": "authoritative_mail_read", "model": "semantic-capability",
                "verified": False, "completed": False, "connector": "configured-imap",
                "read_only": True,
            }

        if action != "message_send":
            return False, "", {}
        platform = str(decision.get("platform") or "").strip()
        recipient = str(decision.get("recipient") or "").strip()
        message = str(decision.get("message") or "").strip()
        platform_n = platform.casefold().replace("ё", "е")
        if platform_n not in {"telegram", "телеграм", "тг"}:
            # Other messengers remain available to the general executor; this typed
            # preflight only claims a capability it can verify end to end.
            return False, "", {}
        # Telegram is an explicit API control surface now.  A chat turn may explain
        # and open that block, but it must never stage, type or send a Telegram message.
        return True, (
            "Telegram теперь управляется в отдельном блоке. Открой «Настройки → Telegram»: "
            "там можно ответить на непрочитанные, включить мониторинг или выполнить "
            "свою задачу через авторизованный Telegram API."
        ), {
            "action": "telegram_panel_required",
            "model": "telegram-api-panel",
            "needs_user": True,
            "verified": False,
            "completed": False,
            "open_settings_tab": "telegram",
        }
        if not recipient or not message:
            awaiting = "recipient" if not recipient else "message"
            self._save_messenger_pending({
                "recipient": recipient, "platform": "telegram", "message": message,
                "awaiting": awaiting, "at": time.time(),
            }, conversation_id)
            prompt = "Кому именно отправить сообщение?" if awaiting == "recipient" else f"Что написать {recipient}?"
            return True, prompt, {
                "action": f"send_need_{awaiting}", "model": "semantic-capability",
                "needs_user": True, "completed": False, "verified": True,
            }

        confirmation_mode = str(
            getattr(getattr(self, "settings", None), "confirmation_mode", "full")
            or "full"
        ).casefold()
        if confirmation_mode == "every":
            # Do not even open/focus Telegram before the reversible action has its own
            # confirmation.  The immutable send is staged only after this preflight.
            self._save_messenger_pending({
                "recipient": recipient, "platform": "telegram", "message": message,
                "confirm_open": True, "at": time.time(),
            }, conversation_id)
            return True, (
                f"Открыть Telegram, чтобы подготовить черновик для {recipient}? "
                "Это ещё ничего не введёт и не отправит. Скажи «да, открыть» или «отмена»."
            ), {
                "action": "open_service_confirmation", "model": "semantic-capability",
                "needs_user": True, "completed": False, "verified": True,
            }

        staged = self._save_messenger_pending({
            "recipient": recipient, "platform": "telegram", "message": message,
            "confirm": True, "at": time.time(),
        }, conversation_id)
        exact = dict((staged or {}).get("payload") or {})
        surface = dict(exact.get("surface") or {})
        surface_verified = self._messenger_surface_valid(surface)
        confirmation = self._messenger_confirmation_prompt(exact or {
            "recipient": recipient, "message": message,
        })
        if surface_verified:
            answer = "Telegram открыт и привязан к этому черновику. " + confirmation
        else:
            answer = (
                "Не удалось открыть и подтвердить поверхность Telegram; сообщение не "
                "вводила и не отправляла. Открой или авторизуй Telegram, затем снова "
                "подтверди этот точный черновик. " + confirmation
            )
        return True, answer, {
            "action": "send_confirmation", "model": "semantic-capability",
            "needs_user": True, "completed": False, "verified": surface_verified,
            "recipient": str(exact.get("recipient") or recipient),
            "draft": str(exact.get("message") or message),
            "surface": surface,
        }

    @staticmethod
    def _telegram_panel_request(query: str) -> bool:
        """Recognize an action request that must stay inside the Telegram panel.

        This is only a safety boundary for the transport, not a task router: the
        Telegram panel's model/API layer remains responsible for interpreting the
        actual custom task.
        """
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        if not any(token in clean for token in ("telegram", "телеграм", "телеграмм", "тг")):
            return False
        action_words = (
            "открой", "открыть", "запусти", "напиши", "отправь", "ответь", "проверь",
            "прочитай", "монитор", "следи", "задач", "сообщен", "непрочитан", "чат",
        )
        return any(word in clean for word in action_words)

    @staticmethod
    def _telegram_panel_answer() -> tuple[str, dict[str, Any]]:
        return (
            "Telegram теперь управляется в отдельном блоке. Открой «Настройки → Telegram»: "
            "там можно ответить на непрочитанные, включить мониторинг или выполнить "
            "свою задачу через авторизованный Telegram API.",
            {
                "action": "telegram_panel_required",
                "model": "telegram-api-panel",
                "needs_user": True,
                "verified": False,
                "completed": False,
                "open_settings_tab": "telegram",
            },
        )

    def _telegram_monitor_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        """Configure the explicit Telegram monitor without sending a test message."""
        q = normalize_phrase(query)
        service = getattr(self, "telegram", None)
        pending = self.db.get_setting("telegram_monitor_pending", {})
        if isinstance(pending, dict) and pending.get("rule"):
            if is_cancel_confirmation(q):
                self.db.set_setting("telegram_monitor_pending", {})
                return True, "Не запускаю автоматические ответы Telegram.", {
                    "action":"telegram_monitor_cancelled","verified":True,
                }
            if is_affirmative_confirmation(q) or is_resume_confirmation(q):
                if service is None:
                    return True, "Модуль Telegram недоступен в этой сборке.", {"action":"telegram_monitor","verified":False}
                rule = dict(pending.get("rule") or {})
                self.db.set_setting("telegram_monitor_pending", {})
                service.save_rules([rule])
                try:
                    status = service.start()
                except Exception as exc:
                    return True, f"Правило сохранено, но монитор не запустился: {humanize(exc)}", {
                        "action":"telegram_monitor_start","verified":False,"rule_saved":True,
                    }
                private_only = bool(rule.get("private_only"))
                scope = "только личные чаты; группы и каналы пропускаю" if private_only else "все разрешённые чаты"
                return True, f"Мониторинг Telegram запущен 24/7: {scope}. Исходящие сообщения не обрабатываю повторно.", {
                    "action":"telegram_monitor_start","verified":bool(status.get("running")),"private_only":private_only,
                }
        telegram_request = bool(re.search(r"\b(?:telegram|телеграм\w*|телегр\w*|тг)\b", q, re.I))
        monitor_request = bool(re.search(r"\b(?:монитор\w*|следи\w*|отслежива\w*|24\s*/\s*7|автоответ\w*)\b", q, re.I))
        if not (telegram_request and monitor_request):
            return False, "", {}
        if service is None:
            return True, "Модуль Telegram недоступен в этой сборке.", {"action": "telegram_monitor", "verified": False}
        if re.search(r"\b(?:останови|выключи|прекрати|отмени)\w*\b", q, re.I):
            status = service.stop()
            return True, str(status.get("message") or "Мониторинг Telegram остановлен."), {
                "action": "telegram_monitor_stop", "verified": not bool(status.get("running")),
            }
        if not service.config().get("configured") or not service.config().get("authorized"):
            return True, (
                "Сначала подключи Telegram в «Настройки → Telegram»: API ID, API Hash, номер, код входа. "
                "После подтверждения повтори эту фразу — правило запустится автоматически."
            ), {"action": "telegram_setup_required", "verified": True, "open_settings_tab": "telegram"}
        private_only = bool(re.search(
            r"(?:кроме|без)\s+(?:групп\w*|канал\w*)|только\s+(?:личн\w*|люд\w*|пользовател\w*)",
            q, re.I,
        ))
        instruction = "Отвечай естественно в моём стиле с учётом входящего сообщения и контекста беседы."
        rule = {
            "name": "Личные ответы 24/7" if private_only else "Ответы 24/7",
            "enabled": True,
            "chats": ["*"],
            "pattern": ".*",
            "reply": instruction,
            "mode": "ai",
            "private_only": private_only,
            "max_per_hour": 20,
        }
        scope = "только личные чаты; группы и каналы пропускаю" if private_only else "все разрешённые чаты"
        self.db.set_setting("telegram_monitor_pending", {"rule": rule, "at": time.time()})
        return True, (
            f"Готова включить автоответы Telegram 24/7: {scope}, максимум {rule['max_per_hour']} ответов в час. "
            "Перед отправкой каждого ответа отдельного вопроса уже не будет. Запустить?"
        ), {
            "action":"telegram_monitor_confirmation","verified":True,"private_only":private_only,
            "needs_user":True,"completed":False,
        }

    def _video_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        video = getattr(self, "video", None)
        if video is None or not video.is_relevant(query, conversation_id):
            return False, "", {}
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        if re.search(r"\b(?:останови|отмени|прекрати)\w*\b.{0,25}\b(?:монтаж|обработк|видео)\w*", clean):
            task = self.tasks.latest(kind="video_edit", conversation_id=conversation_id) if self.tasks is not None else None
            if task and task.get("status") in {"queued", "running", "waiting_user"}:
                cancelled = bool(self.tasks.cancel(str(task.get("id") or "")))
                return True, ("Остановила обработку видео. Исходники остались в папке video." if cancelled else "Обработка уже завершилась."), {
                    "action": "video_cancel", "model": "deterministic", "control_plane": True,
                    "task_id": task.get("id"), "cancelled": cancelled,
                }
        try:
            outcome = dict(video.handle_query(query, conversation_id) or {})
        except Exception as exc:
            return True, f"Не смогла подготовить видеопроект: {humanize(exc)}. Исходники не удалены.", {
                "action": "video_prepare_error", "model": "video-ffmpeg", "control_plane": True,
                "error": str(exc),
            }
        if not outcome.get("handled"):
            return False, "", {}
        answer = str(outcome.get("answer") or "")
        route = dict(outcome.get("route") or {"action": "video", "model": "video-ffmpeg"})
        enqueue = outcome.get("enqueue")
        if isinstance(enqueue, dict):
            if self.tasks is None:
                return True, "Видеопроект подготовлен, но фоновая очередь задач недоступна. Перезапусти EIRVEN и повтори команду.", {
                    **route, "action": "video_queue_unavailable", "completed": False,
                }
            try:
                task_id = self.tasks.enqueue(
                    "video_edit",
                    f"Монтаж видео: {query[:120]}",
                    enqueue,
                    conversation_id=conversation_id,
                )
                video.mark_queued(str(enqueue.get("project_id") or ""), task_id)
                route.update({"task_id": task_id, "kind": "video_edit", "completed": False})
            except Exception as exc:
                answer = f"Файлы подготовила, но не смогла поставить монтаж в очередь: {humanize(exc)}. Исходники сохранены."
                route.update({"action": "video_queue_error", "error": str(exc), "completed": False})
        return True, answer, route

    def _r22_open_external(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        target=str(target or "").strip()
        try:
            applications=getattr(getattr(self.app_skills,"services",None),"applications",None)
            if applications is None:
                raise RuntimeError("resolver unavailable")
            result=dict(applications.web_fallback(target) or {})
            completed=bool(result.get("url"))
            verified=bool(completed and result.get("fallback") == "canonical_registry")
            if verified:
                answer=f"Открыла официальный сайт {result.get('title') or target}."
            elif completed:
                answer=f"Открыла найденную страницу для «{target}», но не подтверждаю, что это официальный сайт."
            else:
                answer=f"Не смогла открыть {target}."
            return True,answer,{
                "action":"r22_external_open","model":"deterministic+official-resolver","control_plane":True,
                "completed":completed,"verified":verified,"target":target,"result":result,
            }
        except Exception as exc:
            return True,f"Не смогла открыть {target}: {humanize(exc)}.",{
                "action":"r22_external_open","model":"deterministic+official-resolver","control_plane":True,
                "completed":False,"verified":False,"target":target,"error":str(exc),
            }

    def _r23_open_application(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        target = str(target or "").strip()
        if not target or getattr(self, "app_skills", None) is None:
            return True, "Не указано, какое приложение открыть.", {
                "action": "r23_application_missing", "completed": False, "verified": False,
            }
        try:
            result = dict(self.app_skills.open(target) or {})
            ok = bool(result.get("ok"))
            verified = bool(result.get("verified", ok))
            return True, (
                f"Открыла приложение {target}." if ok
                else f"Не смогла открыть приложение {target}: {result.get('error') or 'не найдено'}."
            ), {
                "action": "r23_application_open", "model": "deterministic-start-menu",
                "control_plane": True, "target": target, "completed": ok,
                "verified": verified, "result": result,
            }
        except Exception as exc:
            return True, f"Не смогла открыть приложение {target}: {humanize(exc)}.", {
                "action": "r23_application_open", "control_plane": True,
                "target": target, "completed": False, "verified": False, "error": str(exc),
            }

    @staticmethod
    def _route_clarification_key(conversation_id: str) -> str:
        return f"r23_route_clarification:{conversation_id or 'voice'}"

    def _save_route_clarification(self, conversation_id: str, target: str, verb: str) -> None:
        try:
            self.db.set_setting(self._route_clarification_key(conversation_id), {
                "target": str(target or "").strip(), "verb": str(verb or "открой"), "at": time.time(),
            })
        except Exception:
            pass

    def _route_clarification_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        try:
            pending = self.db.get_setting(self._route_clarification_key(conversation_id), None)
        except Exception:
            pending = None
        if not isinstance(pending, dict) or not pending.get("target"):
            return False, "", {}
        if time.time() - float(pending.get("at") or 0) > 180:
            try: self.db.set_setting(self._route_clarification_key(conversation_id), {})
            except Exception: pass
            return False, "", {}
        choice = " ".join(str(query or "").casefold().replace("ё", "е").split()).strip(" .,!?")
        choice = re.sub(r"^(?:открой|открывай|запусти|включи)\s+", "", choice, flags=re.I).strip()
        if not re.fullmatch(r"(?:сайт|веб|веб-сайт|приложение|программу|программа|трек|песня|музыка)", choice, re.I):
            return False, "", {}
        target = str(pending.get("target") or "").strip()
        try: self.db.set_setting(self._route_clarification_key(conversation_id), {})
        except Exception: pass
        if choice in {"сайт", "веб", "веб-сайт"}:
            return self._r22_open_external(target)
        if choice in {"трек", "песня", "музыка"} and getattr(self, "desktop_operator", None) is not None:
            try: result = dict(self.desktop_operator.yandex_play_query(target) or {})
            except Exception as exc: result = {"ok":False,"completed":False,"verified":False,"error":str(exc)}
            verified = bool(result.get("verified")); completed = bool(result.get("completed") or result.get("ok"))
            return True, (f"Включила «{target}»." if verified else (f"Запуск «{target}» выполнила один раз, но не смогла подтвердить воспроизведение." if completed else f"Не смогла включить «{target}»: {result.get('error') or 'не найдено'}.")), {
                "action":"r23_media_clarified","model":"deterministic+uia","target":target,
                "completed":completed,"verified":verified,"result":result,
            }
        return self._r23_open_application(target)

    def _screen_brightness_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        """Handle monitor brightness without letting the video editor steal the phrase."""
        clean = normalize_phrase(query)
        if not re.search(r"\bяркост\w*\b", clean, re.I):
            return False, "", {}
        if re.search(r"\b(?:сфер\w*|видео|ролик|клип|кадр|фильм)\b", clean, re.I):
            return False, "", {}
        match = re.search(r"\b(?:до|на)\s*(\d{1,3})\s*(?:%|процент\w*)?\b", clean, re.I)
        if not match:
            return False, "", {}
        requested = max(0, min(100, int(match.group(1))))
        if self.tools is None:
            return True, "Не могу изменить яркость экрана: системный контур недоступен.", {
                "action": "screen_brightness", "model": "deterministic", "completed": False, "verified": False,
            }
        script = (
            "$ErrorActionPreference='Stop'; "
            f"$target={requested}; "
            "$items=@(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods); "
            "if(-not $items){throw 'Windows не предоставила управление яркостью монитора'}; "
            "$items | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness -Arguments @{Brightness=$target;Timeout=0} | Out-Null }; "
            "$state=@(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -ExpandProperty CurrentBrightness); "
            "[pscustomobject]@{requested=$target;current=([int]($state | Select-Object -First 1))} | ConvertTo-Json -Compress"
        )
        try:
            result = self.tools.execute("powershell", {"command": script, "cwd": str(self.settings.root_dir), "timeout": 20})
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        inner = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else {}
        raw = str(inner.get("stdout") or "").strip()
        observed = None
        try:
            observed = json.loads(raw.splitlines()[-1]).get("current") if raw else None
        except (ValueError, TypeError, json.JSONDecodeError):
            observed = None
        verified = bool(result.get("ok") and int(inner.get("returncode", 1)) == 0 and observed == requested)
        if verified:
            answer = f"Яркость экрана установила на {requested}% и проверила значение."
        else:
            answer = f"Не смогла подтвердить яркость экрана {requested}%: {result.get('error') or inner.get('stderr') or 'монитор не поддержал управление'}."
        return True, answer, {
            "action": "screen_brightness", "model": "deterministic+powershell", "completed": bool(result.get("ok")),
            "verified": verified, "requested": requested, "observed": observed, "result": result,
        }

    def _r23_contextual_open(self, target: str, verb: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Resolve a bare entity without equating verbs with app/site types."""
        target = str(target or "").strip()
        norm = " ".join(target.casefold().replace("ё", "е").split())
        applications = getattr(getattr(self.app_skills, "services", None), "applications", None)
        matches: list[dict[str, str]] = []
        if applications is not None:
            try: matches = list(applications.strong_matches(target))
            except Exception: matches = []
        ambiguous_brand = bool(re.fullmatch(
            r"(?:microsoft|майкрософт|google|гугл|яндекс|yandex|apple|эппл)", norm, re.I,
        ))
        if len(matches) == 1 and not ambiguous_brand:
            return self._r23_open_application(str(matches[0].get("name") or target))
        self._save_route_clarification(conversation_id, target, verb)
        if len(matches) > 1:
            names = ", ".join(str(row.get("name") or "") for row in matches[:3])
            prompt = f"Нашла несколько приложений для «{target}» ({names}). Открыть сайт или приложение?"
        elif str(verb or "").casefold() == "включи" and not ambiguous_brand:
            prompt = f"«{target}» — это трек для Яндекс Музыки, сайт или приложение?"
        else:
            prompt = f"«{target}» открыть как сайт или как установленное приложение?"
        choices = " Скажи «трек», «сайт» или «приложение»." if "трек" in prompt else " Скажи «сайт» или «приложение»."
        return True, prompt + choices, {
            "action": "r23_route_clarification", "model": "deterministic-context",
            "control_plane": True, "target": target, "verb": verb,
            "completed": False, "verified": False, "needs_user": True,
        }

    def _r22_app_compound_turn(self, query: str, app: str, remainder: str, conversation_id: str = "") -> tuple[bool, str, dict[str, Any]]:
        """Execute the deterministic prefix of a same-app compound request.

        Cross-app missions are left to MissionEngine.  For a single explicit app, the app
        surface is opened first so a current-player/page fast path can never steal the
        command merely because another app was foreground.
        """
        app=str(app or ""); remainder=str(remainder or "").strip()
        app_skills=getattr(self,"app_skills",None)
        if app_skills is None:
            return True,"Контур приложений сейчас недоступен.",{"action":"r22_app_compound","completed":False,"verified":False}
        # Telegram send must establish ownership of the Telegram surface first.  Chromium
        # exposes background tabs as TabItem rather than top-level windows; without this
        # step the send helper cannot see an already-authenticated background Telegram tab
        # and may open a duplicate web client, costing 8-15 seconds in live traces.
        if app == "telegram" and re.search(r"\b(?:напиши|отправь)\w*", remainder, re.I):
            batch = self._parse_telegram_batch(remainder)
            recipient, message = self._parse_telegram_command(remainder)
            if batch or recipient or message:
                try:
                    opened=dict(app_skills.open("telegram") or {})
                except Exception as exc:
                    opened={"ok":False,"error":str(exc)}
                if not opened.get("ok"):
                    return True,f"Не смогла открыть Telegram: {opened.get('error') or 'не найдено'}.",{"action":"r22_app_compound","completed":False,"verified":False,"result":opened}
                try:
                    win=dict(opened.get("window") or {})
                    if self.tools is not None and int(win.get("handle") or 0):
                        self.tools.execute("window_focus",{"handle":int(win.get("handle") or 0)})
                except Exception:
                    pass
                if len(batch) > 1:
                    self._save_messenger_pending({
                        "recipient": "", "platform": "telegram", "message": "", "batch": batch,
                        "confirm": True, "at": time.time(),
                    }, conversation_id)
                    grounded = self._load_messenger_pending(conversation_id) or {}
                    exact_payload = dict(grounded.get("payload") or {})
                    return True, self._messenger_confirmation_prompt(exact_payload), {
                        "action": "telegram_batch_confirmation", "needs_user": True, "completed": False,
                        "verified": True, "r22_surface_open": opened,
                    }
                if not recipient:
                    self._save_messenger_pending({"recipient":"","platform":"telegram","message":message,"at":time.time()})
                    return True,"Кому именно отправить сообщение?",{"action":"send_need_recipient","model":"deterministic","r22_surface_open":opened}
                if not message:
                    self._save_messenger_pending({"recipient":recipient,"platform":"telegram","message":"","at":time.time()})
                    return True,f"Что написать {recipient}?",{"action":"send_need_text","model":"deterministic","r22_surface_open":opened}
                staged = self._save_messenger_pending({
                    "recipient": recipient, "platform": "telegram", "message": message,
                    "confirm": True, "at": time.time(),
                }, conversation_id)
                exact = dict((staged or {}).get("payload") or {})
                return True, self._messenger_confirmation_prompt(exact), {
                    "action": "send_confirmation", "model": "deterministic", "needs_user": True,
                    "completed": False, "verified": True, "r22_surface_open": opened,
                }
        try:
            opened=dict(app_skills.open(app) or {})
        except Exception as exc:
            return True,f"Не смогла открыть нужное приложение: {humanize(exc)}.",{"action":"r22_app_compound","completed":False,"verified":False,"error":str(exc)}
        if not opened.get("ok"):
            return True,f"Не смогла открыть нужное приложение: {opened.get('error') or 'не найдено'}.",{
                "action":"r22_app_compound","completed":False,"verified":False,"result":opened,
            }
        try:
            win=dict(opened.get("window") or {})
            if self.tools is not None and int(win.get("handle") or 0):
                self.tools.execute("window_focus",{"handle":int(win.get("handle") or 0)})
        except Exception:
            pass
        # Explicitly arbitrary content is a safe UIA choice and should never require a model turn.
        workflow=getattr(self,"universal_workflow",None)
        if workflow is not None and re.search(r"\b(?:любое|любой|любую)\s+(?:видео|ролик|товар|позици|карточк)\w*", remainder, re.I):
            try:
                activated=workflow.activate_any_current_content(remainder)
            except Exception as exc:
                activated={"ok":False,"completed":False,"verified":False,"error":str(exc)}
            completed=bool(activated and activated.get("completed")); verified=bool(activated and activated.get("verified"))
            answer=("Открыла нужное приложение и запустила один видимый элемент." if completed else
                    f"Приложение открыла, но подходящий видимый элемент не нашла: {(activated or {}).get('error') or 'нет безопасной цели'}.")
            return True,answer,{"action":"r22_app_compound","model":"deterministic+uia","control_plane":True,"app":app,"opened":opened,"result":activated,"completed":completed,"verified":verified}
        # Yandex transport after an explicit Yandex open is also deterministic.
        if app == "yandex_music" and re.search(r"\b(?:включи|играй|продолжи|воспроизведи)\w*", remainder, re.I):
            try: result=dict(app_skills.play_music("яндекс музыка") or {})
            except Exception as exc: result={"ok":False,"verified":False,"error":str(exc)}
            verified=bool(result.get("ok") and result.get("verified"))
            return True,("Включила музыку." if verified else f"Яндекс Музыку открыла, но воспроизведение не подтвердилось: {result.get('error') or 'нет Play'}."),{
                "action":"r22_app_compound","model":"deterministic+uia","control_plane":True,"app":app,"opened":opened,"result":result,"completed":bool(result.get('ok')),"verified":verified,
            }
        # The surface ownership is already corrected.  Do not guess the tail with a wrong
        # foreground handler; let the structured mission layer own anything more complex.
        return False,"",{}

    def _power_confirmation_key(self, conversation_id: str) -> str:
        return f"agent_confirmation:power:{conversation_id}"

    def _telegram_setup_key(self, conversation_id: str) -> str:
        return f"agent_setup:telegram:{conversation_id}"

    def _publish_brief_key(self, conversation_id: str) -> str:
        return f"agent_brief:publish_file:{conversation_id}"

    @staticmethod
    def _domain_from_text(text: str) -> str:
        match = re.search(
            r"(?<![@\w])(?:https?://)?((?:[a-z0-9а-яё-]+\.)+[a-zа-яё]{2,24})(?:[/\s]|$)",
            str(text or ""), re.I,
        )
        return str(match.group(1) or "").casefold() if match else ""

    @staticmethod
    def _hosting_from_text(text: str) -> str:
        value = str(text or "").strip()
        known = re.search(
            r"\b(vercel|netlify|cloudflare|gitlab\s+pages|beget|reg\.ru|timeweb|"
            r"sprinthost|hostinger|digitalocean|hetzner|aws|amazon\s+s3|яндекс\s+облако|yandex\s+cloud)\b",
            value, re.I,
        )
        if known:
            return known.group(1).strip()
        url = re.search(r"https?://[^\s]+", value, re.I)
        if url:
            return url.group(0).rstrip(".,)")
        after = re.search(r"\b(?:на|в|через)\s+(?:хостинг(?:е|а)?\s+)?[«\"']?([a-zа-яё0-9_.-]{2,60})", value, re.I)
        result = after.group(1).strip(" .,'\"»") if after else ""
        return "" if result.casefold() in {"хостинг", "хостинге", "сервер", "сервис", "сайт"} else result

    def _guided_file_publish_turn(
        self, query: str, conversation_id: str, attachment_paths: list[str],
    ) -> tuple[str, bool, str, dict[str, Any], list[str]]:
        """Collect hosting slots across turns, then return one fully grounded goal."""
        key = self._publish_brief_key(conversation_id)
        raw = self.db.get_setting(key, {})
        pending = raw if isinstance(raw, dict) and float(raw.get("expires_at") or 0) > time.time() else {}
        clean = normalize_phrase(query)
        trigger = bool(
            re.search(r"\b(?:загруз|залей|размест|опубликуй|задеплой|deploy)\w*\b", clean)
            and re.search(r"\b(?:файл|архив|сайт|хостинг|домен)\w*\b", clean)
        )
        if not pending and not trigger:
            return query, False, "", {}, attachment_paths
        if pending and is_cancel_confirmation(query):
            self.db.set_setting(key, {})
            return query, True, "Отменила подготовку загрузки файла.", {"action": "publish_brief_cancelled", "model": "deterministic"}, attachment_paths

        paths = [str(path) for path in (attachment_paths or pending.get("paths") or []) if str(path).strip()]
        if not paths:
            paths = self._recent_attachment_paths(conversation_id)
        provider = str(pending.get("provider") or "").strip()
        domain = str(pending.get("domain") or "").strip()
        if not provider:
            provider = self._hosting_from_text(query)
            if pending and not provider and len(clean.split()) <= 7 and not is_resume_confirmation(query):
                provider = str(query or "").strip(" .,!?:;«»\"'")[:120]
        no_domain = bool(re.search(r"\b(?:без\s+домена|домен\s+не\s+нужен|временн\w*\s+адрес)\b", clean))
        if not domain and not no_domain:
            domain = self._domain_from_text(query)
            # A provider's own URL is not automatically the public domain requested by
            # the owner; keep asking unless the reply explicitly mentions domain/address.
            if domain and provider and domain in provider.casefold() and not re.search(r"\b(?:домен|адрес|сайт)\w*\b", clean):
                domain = ""
        if no_domain:
            domain = "без отдельного домена"

        state = {
            "paths": paths[:10], "provider": provider, "domain": domain,
            "original": str(pending.get("original") or query), "expires_at": time.time() + 7200,
        }
        route = {"action": "publish_brief", "model": "deterministic", "needs_user": True}
        if not paths:
            self.db.set_setting(key, state)
            return query, True, "Какой файл загружать? Прикрепи его к следующему сообщению или назови полный путь.", route, attachment_paths
        if not provider:
            self.db.set_setting(key, state)
            return query, True, f"Файл получила: {Path(paths[0]).name}. Куда загружать — назови хостинг, панель или адрес сервиса.", route, paths
        if not domain:
            self.db.set_setting(key, state)
            return query, True, f"Хостинг: {provider}. Какой домен привязать? Назови домен или скажи «без домена».", route, paths

        self.db.set_setting(key, {})
        path_list = ", ".join(paths)
        complete = (
            f"Загрузи файл(ы) {path_list} на хостинг {provider}; публичный домен: {domain}. "
            "Работай в видимом браузере, проверь результат. Если сервис потребует вход, пароль, CAPTCHA или 2FA, "
            "остановись у защищённого поля, попроси владельца войти вручную и сохрани контрольную точку для продолжения по слову готов."
        )
        return complete, False, "", {"action": "publish_brief_complete", "model": "deterministic", "needs_user": False}, paths

    def _phone_guidance_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        if is_mobile_app_setup_request(query):
            return True, (
                "Открыла «Настройки → Телефон». В приложении EIRVEN Mobile введи показанные там "
                "адрес компьютера и код подключения. Телефон и компьютер должны быть в одной домашней "
                "Wi‑Fi сети. Если Windows спросит про брандмауэр, разреши доступ только для частной сети."
            ), {
                "action": "mobile_app_setup_guide", "model": "deterministic",
                "control_plane": True, "open_settings_tab": "mobile", "needs_user": True,
            }
        telegram = getattr(self, "telegram", None)
        if telegram is None:
            return False, "", {}
        key = self._telegram_setup_key(conversation_id)
        stored = self.db.get_setting(key, {})
        pending = stored if isinstance(stored, dict) and float(stored.get("expires_at") or 0) > time.time() else {}
        clean = normalize_phrase(query)
        setup_request = is_phone_setup_request(query) or is_chat_pairing_request(query)
        field_question = bool(pending) and bool(re.search(r"\b(?:поле|сюда|ввод|что|где|дальше|следующ)\w*\b", clean))
        checkpoint_waiting = any(
            bool(engine and engine.has_pending(conversation_id))
            for engine in (getattr(self, "autonomous_workflow", None), getattr(self, "universal_workflow", None))
        )
        # A desktop authorization checkpoint is more specific than an older Telegram
        # setup hint in the same conversation. "Готов" must resume the action that is
        # visibly waiting, not reopen settings from stale guidance state.
        progress_confirmation = bool(pending) and is_resume_confirmation(query) and not checkpoint_waiting
        explicit_field = bool(re.search(r"\b(?:api\s*id|api\s*hash|апи|хеш|hash|номер\w*\s+телефон|код\w*\s+вход|2fa|двухэтап|разрешенн\w*\s+чат|префикс)\b", clean))
        if not (setup_request or field_question or progress_confirmation or explicit_field):
            return False, "", {}

        route: dict[str, Any] = {
            "action": "telegram_setup_guide", "model": "deterministic", "control_plane": True,
            "open_settings_tab": "telegram", "needs_user": True,
        }
        config = telegram.config()

        field_help = ""
        if re.search(r"\b(?:api\s*id|апи\s*айди)\b", clean):
            field_help = "В API ID введи только цифры App api_id со страницы my.telegram.org → API development tools. Это не ID чата и не токен бота."
        elif re.search(r"\b(?:api\s*hash|апи\s*хеш|hash|хеш)\b", clean):
            field_help = "В API Hash вставь длинную строку App api_hash с той же страницы my.telegram.org. Она хранится локально; не отправляй её в чат."
        elif re.search(r"\b(?:номер\w*\s+телефон|телефон\w*\s+номер)\b", clean):
            field_help = "Номер введи в международном формате с плюсом и кодом страны — номер того Telegram-аккаунта, с которого будешь управлять Эрви."
        elif re.search(r"\b(?:код\w*\s+вход|одноразов\w*\s+код)\b", clean):
            field_help = "Нажми «Получить код» и введи пришедший одноразовый код в защищённое поле «Код входа». В обычный чат код лучше не писать."
        elif re.search(r"\b(?:2fa|двухэтап|парол)\w*\b", clean):
            field_help = "Пароль 2FA нужен только если Telegram запросил двухэтапную защиту. Вводи его в поле 2FA: Эрви его не сохраняет."
        elif re.search(r"\b(?:разрешенн\w*\s+чат|chat\s*id|айди\s+чат)\b", clean):
            field_help = "ID вручную искать не нужно: после входа нажми «Привязать текущий чат» и отправь в нужном чате фразу «Эрви, сюда буду отправлять команды»."
        elif "префикс" in clean.split():
            field_help = "Префикс — обращение в начале телефонной команды. Оставь «Эрви,»; запятая необязательна, распространённые варианты имени тоже распознаются."
        if field_help:
            self.db.set_setting(key, {"stage": "fields", "expires_at": time.time() + 1800})
            return True, field_help, route

        self.db.set_setting(key, {"stage": "telegram", "expires_at": time.time() + 1800})
        if not config.get("configured"):
            return True, (
                "Открыла раздел Telegram. Шаг 1: на my.telegram.org → API development tools возьми App api_id и App api_hash, "
                "заполни первые три поля вместе с номером телефона и нажми «Сохранить подключение». Спроси меня про любое поле — объясню его отдельно."
            ), route
        if not config.get("authorized"):
            return True, (
                "Данные подключения уже сохранены. Шаг 2: нажми «Получить код», введи код в защищённое поле и нажми «Подтвердить вход». "
                "Если Telegram попросит 2FA, введи пароль только в поле 2FA — он не сохраняется. После входа скажи «готов»."
            ), route

        remote = telegram.remote_config()
        status = telegram.status()
        if remote.get("enabled") and remote.get("chats") and status.get("running") and not is_chat_pairing_request(query):
            route["needs_user"] = False
            return True, f"Телефон уже подключён. Разрешённый чат: {', '.join(remote.get('chats') or [])}. Команды начинай с «{remote.get('prefix') or 'Эрви,'}».", route
        try:
            pairing = telegram.begin_pairing(replace=True, ttl_seconds=600)
        except Exception as exc:
            return True, f"Вход подтверждён, но окно привязки не запустилось: {humanize(exc)}", route
        route["pairing"] = {"active": True, "expires_at": pairing.get("expires_at")}
        return True, (
            "Шаг 3: открыла 10‑минутное окно привязки. Перейди в нужный Telegram-чат и отправь со своего аккаунта: "
            "«Эрви, сюда буду отправлять команды». ID этого чата перезапишется автоматически. "
            f"Если сообщение будет входящим, добавь одноразовый код {pairing.get('code')}."
        ), route

    def _power_control_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Own a short, persisted confirmation turn for high-impact phone commands."""
        key = self._power_confirmation_key(conversation_id)
        value = self.db.get_setting(key, {})
        pending = value if isinstance(value, dict) else {}
        expires = float(pending.get("expires_at") or 0)
        if pending and expires <= time.time():
            self.db.set_setting(key, {})
            pending = {}

        if is_pc_shutdown_cancel_request(query):
            self.db.set_setting(key, {})
            result = self.tools.execute("system_power", {"action": "cancel"}) if self.tools is not None else {"ok": False, "error": "Инструменты недоступны"}
            ok = bool(result.get("ok"))
            answer = "Отменила запланированное выключение." if ok else "Запланированного выключения не нашла."
            return True, answer, {"action": "pc_shutdown_cancel", "model": "deterministic", "control_plane": True, "completed": ok, "verified": ok, "result": result}

        if pending.get("action") == "shutdown":
            if is_cancel_confirmation(query):
                self.db.set_setting(key, {})
                return True, "Отменила. Компьютер выключаться не будет.", {"action": "pc_shutdown_declined", "model": "deterministic", "control_plane": True, "completed": True, "verified": True}
            if is_affirmative_confirmation(query):
                self.db.set_setting(key, {})
                result = self.tools.execute("system_power", {"action": "shutdown", "delay_seconds": 15}) if self.tools is not None else {"ok": False, "error": "Инструменты недоступны"}
                ok = bool(result.get("ok"))
                answer = (
                    "Подтверждение принято. Windows выключит компьютер через 15 секунд. Скажи «отмени выключение компьютера», чтобы отменить."
                    if ok else f"Не удалось запланировать выключение: {result.get('error') or 'неизвестная ошибка'}."
                )
                return True, answer, {"action": "pc_shutdown_confirmed", "model": "deterministic", "control_plane": True, "completed": ok, "verified": ok, "result": result}
            if is_pc_shutdown_request(query) or len(normalize_phrase(query).split()) <= 6:
                return True, "Жду явного подтверждения: скажи «да, выключай» или «отмена».", {"action": "pc_shutdown_waiting_confirmation", "model": "deterministic", "control_plane": True, "needs_user": True}
            # A substantial new command supersedes a stale confirmation.
            self.db.set_setting(key, {})

        if is_pc_shutdown_request(query):
            self.db.set_setting(key, {"action": "shutdown", "created_at": time.time(), "expires_at": time.time() + 120})
            return True, "Подтверди выключение компьютера: скажи «да, выключай» или «отмена». После подтверждения будет 15 секунд на отмену.", {"action": "pc_shutdown_confirmation", "model": "deterministic", "control_plane": True, "needs_user": True}
        return False, "", {}

    def _priority_control_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Resolve the non-blocking control plane before any desktop lease.

        The previous implementation acquired ``desktop_lock`` for every ordinary chat
        message merely to discover that it was not a control command.  A long UI task then
        made greetings and cancellation appear frozen.  Concrete adapters/tools own their
        own scoped leases; arbitration itself must always remain lock-free.
        """
        return self._priority_control_turn_locked(query, conversation_id)

    _CREATOR_ANSWER = (
        "Меня создал Даниил Павлов — разработчик Эрви, его портфолио foxyhosty.ru. "
        "А работаю я у тебя."
    )

    def _creator_question(self, clean: str) -> bool:
        """True for questions about who made this assistant.

        Kept deterministic on purpose: the local model is small enough to invent a
        company or lab here, and the author of the product is not something to guess.
        """
        if not re.search(r"\b(?:созда|сдела|разработа|написа|автор|придума|запили)\w*", clean):
            return False
        # Вопрос о создателе — это ВОПРОС: в нём есть «кто», «чей/чья» или прямо «автор».
        # Без этого требования «напомни завтра сделать трейлер Эрви» совпадало по словам
        # «сделать» и «Эрви» — и вместо напоминания Эрви рассказывала, кто её создал.
        if not re.search(r"\b(?:кто|чей|чья|чь[её]|чьи|автор\w*|создател\w*|разработчик\w*)\b", clean):
            return False
        if not re.search(r"\b(?:тебя|теб[еяи]|твой|твоя|твои|вас|эрви|тво[её])\b", clean):
            return False
        # "создай файл" / "сделай отчёт" are commands, not questions about origin.
        return not re.search(r"\b(?:файл|папк|отчет|сайт|скрипт|код|документ|табли)\w*", clean)

    _PASSIVE_MEMORY_PATTERNS = (
        # Work and projects
        r"\bя\s+(?:работаю|учусь)\s+(?:в|на|над)\s+(.{3,90})",
        r"\b(?:мой|моя|мои)\s+(?:проект|работа|компания|сайт|канал)\s+(?:—|-|это)?\s*(.{2,80})",
        # Stable preferences
        r"\bя\s+(?:люблю|обожаю|предпочитаю)\s+(.{3,80})",
        r"\bя\s+(?:не\s+люблю|терпеть\s+не\s+могу|ненавижу)\s+(.{3,80})",
        r"\bмне\s+(?:нравится|не\s+нравится)\s+(.{3,80})",
        # Habits and schedule
        r"\bя\s+(?:обычно|всегда|каждый\s+день|по\s+утрам|по\s+вечерам)\s+(.{3,80})",
        # People
        r"\b(?:мой|моя)\s+(?:друг|подруга|брат|сестра|мама|папа|жена|муж|коллега|клиент)\s+(.{2,60})",
        # Tools
        r"\bя\s+(?:пользуюсь|использую)\s+(.{3,80})",
    )

    _PASSIVE_MEMORY_BLOCK = re.compile(
        r"\b(?:если|бы|наверное|может\s+быть|представь|допустим|как\s+будто|"
        r"вопрос|почему|зачем|расскажи|объясни|напиши|сделай|открой)\b",
        re.IGNORECASE,
    )

    def _capture_passive_memory(self, query: str) -> None:
        """Store durable facts the owner mentions in passing.

        Only clear, first-person, present-tense statements are kept. Hypotheticals,
        questions and commands are skipped: a 4B model plus a loose matcher would
        otherwise fill long-term memory with fragments of instructions, and wrong
        memories are more damaging than an empty store because they silently steer
        every later answer.
        """
        text = " ".join(str(query or "").split())
        if len(text) < 12 or len(text) > 320:
            return
        if self._PASSIVE_MEMORY_BLOCK.search(text):
            return
        low = text.casefold().replace("ё", "е")
        for pattern in self._PASSIVE_MEMORY_PATTERNS:
            match = re.search(pattern, low, re.IGNORECASE)
            if not match:
                continue
            fact = " ".join(str(match.group(0) or "").split()).strip(" ,.;:!?")
            if len(fact) < 8:
                return
            try:
                # Do not store the same thing twice: search first, and only add when
                # nothing close is already known.
                existing = self.memory.search(fact, limit=3) or []
                for item in existing:
                    known = str(item.get("content") or "").casefold()
                    if known and (known in fact or fact in known):
                        return
                self.memory.remember_structured(fact)
            except Exception:
                pass
            return

    def _priority_control_turn_locked(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Small deterministic control plane that owns only capabilities it can verify."""
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        tokens = re.findall(r"[a-zа-я0-9]+", clean)

        if self._creator_question(clean):
            return True, self._CREATOR_ANSWER, {
                "action": "creator_identity", "model": "deterministic",
                "control_plane": True, "completed": True, "verified": True,
            }

        teaching = getattr(self, "teaching", None)
        if teaching is not None:
            active = bool(teaching.status().get("active"))
            if active and re.fullmatch(r"(?:готово|всё|все|закончил[аи]?|хватит|стоп\s+обучение)[.!]*", clean):
                result = teaching.finish()
                return True, str(result.get("message") or ""), {
                    "action": "teaching_finished", "model": "deterministic",
                    "control_plane": True, "completed": bool(result.get("ok")),
                    "verified": bool(result.get("ok")),
                }
            if active and re.fullmatch(r"(?:отмена|отмени обучение|не надо)[.!]*", clean):
                result = teaching.cancel()
                return True, str(result.get("message") or ""), {
                    "action": "teaching_cancelled", "model": "deterministic",
                    "control_plane": True, "completed": True, "verified": True,
                }
            match = re.match(
                r"(?:обучить|обучи|научить|научи|запомни\s+как)\b\s*"
                r"(?:выполнени[юя]|делать\s+это|делать|это)?\s*[:\-]?\s*(.*)$",
                clean,
            )
            if match and not active:
                goal = match.group(1).strip() or str(
                    self.db.get_setting("last_failed_goal", "") or ""
                ).strip()
                result = teaching.start(goal)
                return True, str(result.get("message") or ""), {
                    "action": "teaching_started", "model": "deterministic",
                    "control_plane": True, "needs_user": True,
                    "completed": bool(result.get("ok")), "verified": bool(result.get("ok")),
                }

        pending_owner = self._pending_confirmation_owner(conversation_id)
        if pending_owner is not None and pending_owner.kind == "workflow":
            # The typed owner, not a legacy adapter or generic runtime resume, consumes
            # this reply.  Fingerprint validation happens in that owner's lane.
            #
            # Exception: an unmistakable new command is not an answer to the pending
            # question. Swallowing it disabled the whole deterministic control plane
            # while any checkpoint was open, so "Выключи пк" reached the planner
            # instead of the power handler and came back as a generic failure.
            supersedes = bool(
                is_pc_shutdown_request(query)
                or re.fullmatch(r"(?:поставь\s+выполнение\s+на\s+паузу|пауза|стоп|отмена)[.!? ]*", clean)
            )
            if not supersedes:
                return False, "", {}

        # Messenger drafts are a typed confirmation lane.  Resolve them before the
        # universal desktop planner so a short "да" can never become an unrelated UI
        # action and a stale draft can never make the model wait on screen observation.
        pending_send = self._pending_send_turn(query, conversation_id)
        if pending_send[0]:
            return pending_send

        # A named Telegram send is also owned here.  Stage the exact recipient/text and
        # ask for confirmation; do not open a window or type anything in this turn.
        telegram_send = bool(
            re.search(r"\b(?:telegram|телеграм\w*|тг)\b", clean, re.I)
            and re.search(r"\b(?:напиши|отправь|скинь|пошли)\w*\b", clean, re.I)
            and not re.search(r"\b(?:монитор\w*|следи\w*|автоответ\w*|24\s*/\s*7)\b", clean, re.I)
        )
        if telegram_send:
            recipient, message = self._parse_telegram_command(str(query or ""))
            if recipient and message:
                staged = self._save_messenger_pending({
                    "recipient": recipient, "platform": "telegram", "message": message,
                    "confirm": True, "at": time.time(),
                }, conversation_id)
                exact = dict((staged or {}).get("payload") or {})
                return True, self._messenger_confirmation_prompt(exact), {
                    "action": "send_confirmation", "model": "deterministic", "control_plane": True,
                    "needs_user": True, "completed": False, "verified": True,
                    "recipient": recipient, "draft": message,
                }
            if message and not recipient:
                self._save_messenger_pending({"recipient": "", "platform": "telegram", "message": message, "at": time.time()}, conversation_id)
                return True, "Кому именно отправить сообщение?", {"action": "send_need_recipient", "model": "deterministic", "control_plane": True, "needs_user": True}
            if recipient and not message:
                self._save_messenger_pending({"recipient": recipient, "platform": "telegram", "message": "", "at": time.time()}, conversation_id)
                return True, f"Что написать {recipient}?", {"action": "send_need_text", "model": "deterministic", "control_plane": True, "needs_user": True}

        brightness = self._screen_brightness_turn(query)
        if brightness[0]:
            return brightness

        explicit_dry_run = bool(
            (re.search(r"\b(?:не\s+(?:выполняй|делай|запускай)|без\s+выполнения)\b", clean, re.I) and re.search(r"\bплан\w*\b", clean, re.I))
            or re.search(r"\bтолько\s+(?:составь|напиши|покажи)\s+(?:короткий\s+|краткий\s+)?план\w*\b", clean, re.I)
            or (re.search(r"\bне\s+выполняй\b", clean, re.I) and re.search(r"\bтолько\s+объясни\b", clean, re.I))
        )
        if explicit_dry_run:
            dry_query = str(query or "")
            if "план" not in clean:
                body = re.sub(r"^.*?только\s+объясни\s*,?\s*(?:что\s+бы\s+ты\s+сделал[аи]?\s*[:—-]?)?\s*", "", dry_query, flags=re.I).strip()
                dry_query = "Только составь план: " + (body or "описанная команда")
            return True, self._plan_only_answer(dry_query), {
                "action": "plan_only", "model": "deterministic", "control_plane": True,
                "completed": True, "verified": True, "executed": False,
            }

        if re.search(r"\b(?:покажи|раскрой|назови|выведи)\w*\b.{0,35}\b(?:сохраненн\w*\s+)?парол\w*", clean, re.I) or re.search(r"\b(?:отправь|перешли|сообщи)\w*\b.{0,40}\bпарол\w*", clean, re.I):
            return True, "Не покажу и не отправлю пароли, коды входа или ключи. Могу открыть безопасный экран управления учётными данными без раскрытия значений.", {
                "action":"secret_refusal","model":"deterministic","control_plane":True,
                "completed":False,"verified":True,"blocked_for_safety":True,
            }

        narrow_service = self._narrow_service_turn(query)
        if narrow_service[0]:
            return narrow_service

        if re.fullmatch(r"(?:что\s+ты\s+сейчас\s+делаешь|покажи\s+какие\s+задачи\s+сейчас\s+выполняются)[.!? ]*", clean, re.I):
            status = self.runtime.status() if self.runtime is not None else {}
            own_probe = str(status.get("goal") or "").casefold().strip(" .!?") == str(query).casefold().strip(" .!?")
            active = bool(status.get("cancellable") and not own_probe)
            if active:
                step = str(status.get("step") or status.get("goal") or "Выполняю задачу")
                answer = f"Сейчас выполняю: {step}."
            else:
                answer = "Сейчас активных действий нет — жду следующую команду."
            return True, answer, {"action":"runtime_status","model":"deterministic","control_plane":True,"completed":True,"verified":True,"runtime":status}

        if re.fullmatch(r"повтори\s+мою\s+последн\w*\s+команд\w*[.!? ]*", clean, re.I):
            previous = ""
            try:
                users = [str(row.get("content") or "").strip() for row in self.memory.history(conversation_id, limit=20) if row.get("role") == "user"]
                if users and users[-1].casefold().strip(" .!?") == str(query).casefold().strip(" .!?"):
                    users = users[:-1]
                previous = users[-1] if users else ""
            except Exception:
                previous = ""
            answer = f"Последняя команда: «{previous}»." if previous else "В этом диалоге ещё нет предыдущей команды."
            return True, answer, {"action":"repeat_last_command","model":"deterministic","control_plane":True,"completed":True,"verified":True}

        if re.search(r"\bты\s+точно\s+выполнил[аи]?\b|\bподтвержден\w*\s+фактическ\w*\s+результат\w*|\bтолько\s+попытал[аи]сь\b", clean, re.I):
            status = self.runtime.status() if self.runtime is not None else {}
            last = str(status.get("last_result") or "").strip()
            own_probe = str(status.get("goal") or "").casefold().strip(" .!?") == str(query).casefold().strip(" .!?")
            if status.get("cancellable") and not own_probe:
                answer = "Действие ещё выполняется; успешным я его не называю. Проверка конечного состояния пока не получена."
                verified = False
            elif last:
                answer = f"Последнее действие завершено. Подтверждение системы: {last}"
                verified = "не" not in last.casefold()
            else:
                try:
                    latest = (self.db.recent_action_logs(1) or [{}])[0]
                except Exception:
                    latest = {}
                raw_result = latest.get("result") or "{}"
                try:
                    result_data = json.loads(raw_result) if isinstance(raw_result, str) else dict(raw_result)
                except Exception:
                    result_data = {}
                verified = bool(latest.get("success") and result_data.get("verified"))
                tool_name = str(latest.get("tool") or "действие")
                if verified:
                    answer = f"Последнее внешнее действие «{tool_name}» выполнено и подтверждено наблюдаемым результатом."
                elif latest:
                    answer = f"Последнее внешнее действие «{tool_name}» было только выполнено или попытано; независимого подтверждения конечного состояния нет."
                else:
                    answer = "Подтверждённого внешнего действия нет: последний запрос был ответом или остановленной попыткой."
            return True, answer, {"action":"honesty_status","model":"deterministic","control_plane":True,"completed":True,"verified":verified,"runtime":status}

        volume_value = re.search(r"\b(?:громкост\w*|звук\w*)\b.{0,30}?(-?\d{1,4})\s*(?:%|процент)", clean, re.I)
        spoken_volume = -10 if re.search(r"\bминус\s+десять\s+процент", clean, re.I) else (200 if re.search(r"\b(?:двести|двухсот)\s+процент", clean, re.I) else None)
        parsed_volume = int(volume_value.group(1)) if volume_value else spoken_volume
        if parsed_volume is not None and not 0 <= parsed_volume <= 100:
            return True, "Громкость должна быть от 0 до 100 процентов. Ничего не изменила.", {
                "action":"volume_out_of_range","model":"deterministic","control_plane":True,
                "completed":False,"verified":True,"executed":False,"value":parsed_volume,
            }

        if re.fullmatch(r"(?:поставь\s+выполнение\s+на\s+паузу|пауза)[.!? ]*", clean, re.I) and self.runtime is not None:
            self.runtime.pause()
            return True, "Поставила выполнение на паузу.", {"action":"runtime_pause","model":"deterministic","control_plane":True,"completed":True,"verified":True}
        if re.fullmatch(r"(?:продолжи\s+выполнение|продолжай)[.!? ]*", clean, re.I) and self.runtime is not None:
            self.runtime.resume()
            return True, "Продолжила выполнение.", {"action":"runtime_resume","model":"deterministic","control_plane":True,"completed":True,"verified":True}

        phone = self._phone_guidance_turn(query, conversation_id)
        if phone[0]:
            return phone

        power = self._power_control_turn(query, conversation_id)
        if power[0]:
            return power

        # r19 background mission control. Interactive "стоп" remains scoped to the
        # foreground turn; explicit mission/task wording controls the persistent graph.
        if getattr(self, "tasks", None) is not None and re.search(r"\b(?:останови|отмени|прекрати)\w*\b.{0,30}\b(?:мисси|задач)\w*", clean):
            if re.search(r"\b(?:все|всё|всех)\b", clean) and self.runtime is not None:
                result = self.runtime.stop_all()
                return True, f"Остановила фоновые задачи: {int(result.get('cancelled') or 0)}.", {"action":"mission_stop_all","model":"deterministic","control_plane":True,"result":result}
            mission = self.tasks.latest(kind="mission", conversation_id=conversation_id)
            if mission and mission.get("status") in {"queued", "running", "waiting_user"}:
                cancelled = self.tasks.cancel(str(mission.get("id") or ""))
                return True, ("Остановила текущую миссию." if cancelled else "Миссия уже завершилась."), {"action":"mission_cancel","model":"deterministic","control_plane":True,"task_id":mission.get("id"),"cancelled":cancelled}
            return True, "Активной миссии сейчас нет.", {"action":"mission_cancel_none","model":"deterministic","control_plane":True}

        if getattr(self, "tasks", None) is not None and re.search(r"\b(?:статус|что\s+с|как\s+там|что\s+ты\s+делаешь)\b.{0,35}\b(?:мисси|задач)\w*", clean):
            mission = self.tasks.latest(kind="mission", conversation_id=conversation_id)
            if not mission:
                return True, "Миссий пока нет.", {"action":"mission_status_none","model":"deterministic","control_plane":True}
            progress = max(0.0, min(float(mission.get("progress") or 0.0), 1.0))
            pct = round(progress * 100)
            step = str(mission.get("current_step") or mission.get("status") or "")
            return True, f"Текущая миссия: {pct}%. {step}", {"action":"mission_status","model":"deterministic","control_plane":True,"task_id":mission.get("id"),"status":mission.get("status"),"progress":progress}

        cancel_tokens = {"стоп", "отмена", "отмени", "остановись", "прекрати", "хватит"}
        short_cancel = len(tokens) <= 4 and bool(cancel_tokens.intersection(tokens))
        strict_cancel = bool(re.search(r"^(?:не[ ,!-]*){0,3}(?:стоп|отмена|отмени|остановись|прекрати|хватит)[.! ]*$", clean))
        if short_cancel or strict_cancel:
            self.stop(conversation_id)
            self._cancel_pending_actions(conversation_id)
            if self.runtime:
                self.runtime.stop_interactive()
            mission_cancelled = False
            mission_id = ""
            if getattr(self, "tasks", None) is not None:
                try:
                    mission = self.tasks.latest(kind="mission", conversation_id=conversation_id)
                    if mission and str(mission.get("status") or "") in {"queued", "running", "waiting_user"}:
                        mission_id = str(mission.get("id") or "")
                        mission_cancelled = bool(self.tasks.cancel(mission_id))
                except Exception:
                    pass
            return True, ("Остановила текущую задачу и фоновую миссию." if mission_cancelled else "Остановила текущую задачу."), {
                "action": "cancel_current", "model": "deterministic", "control_plane": True,
                "mission_cancelled": mission_cancelled, "task_id": mission_id,
            }

        if self._self_shutdown_requested(query):
            # Explicit self-shutdown is already the confirmation. Do not enter a second
            # yes/no turn that can be blocked by an active mission lock.
            if getattr(self, "tasks", None) is not None:
                try:
                    mission = self.tasks.latest(kind="mission", conversation_id=conversation_id)
                    if mission and str(mission.get("status") or "") in {"queued", "running", "waiting_user"}:
                        self.tasks.cancel(str(mission.get("id") or ""))
                except Exception:
                    pass
            self._schedule_self_shutdown()
            return True, "Выключаюсь.", {"action":"self_shutdown","model":"deterministic","control_plane":True,"completed":True,"verified":True}

        # A configured mailbox is a sealed typed route.  The mail lane below owns both
        # reads and its clarification/confirmation boundaries, so generic desktop
        # arbitration must not inspect the foreground surface first.  Besides wasting a
        # UI probe, that observation made a read-only IMAP request look as if it had
        # entered browser automation in the activity journal.
        if self._mail_request(query):
            try:
                if self.mail is not None and self.mail.configured():
                    return False, "", {}
            except Exception:
                pass

        # r22 front-door arbitration: decide ownership before any legacy app/media/page
        # shortcut.  This prevents the current foreground surface from hijacking a command
        # that explicitly names a different app or external entity.
        clarification = self._route_clarification_turn(query, conversation_id)
        if clarification[0]:
            return clarification
        foreground_title = ""
        arbiter=getattr(self,"reliability_router",None) or ReliabilityRouter()
        decision=arbiter.classify(query)
        # Explicit app/site/page routes need no desktop probe.  Only unresolved/current-
        # surface commands pay for one foreground lookup, preserving the instant app lane.
        if decision.kind in {"unknown", "contextual_open"}:
            try:
                fg = self.tools.execute("foreground_window", {}) if self.tools else {}
                foreground_title = str((fg.get("result") or {}).get("title") or "") if fg.get("ok") else ""
            except Exception:
                pass
            decision=arbiter.classify(query, foreground_title=foreground_title)
        try: self._trace("R22_ARBITRATE",query=query,decision=decision.to_dict())
        except Exception: pass
        if decision.kind == "capabilities":
            short = bool(re.search(r"\b(?:кратко|коротко|без\s+подробност)\w*\b", clean, re.I))
            return True,self._r22_capabilities_answer(short=short),{"action":"r22_capabilities","model":"deterministic","control_plane":True,"completed":True,"verified":True}
        # Reactive v2 is the sole owner of ordinary computer actions.  Keep only the
        # safety/cancel/confirmation control plane above and capability questions here;
        # AppSkills, media helpers and legacy regex routes remain callable adapters, not
        # competing front doors that can terminate the user's full goal.
        # Explicit media starts/controls are already provider-neutral adapters with
        # their own fresh GSMTC/UIA verification. Let them run before the generic
        # workflow; otherwise "Включи Яндекс Музыку" was handed to the broad agent,
        # which often burned its budget observing the page and returned a false failure.
        if getattr(self, "universal_workflow", None) is not None and decision.kind not in {"media_start", "media"}:
            try:
                self._trace("REACTIVE_V2_FRONT_DOOR", query=query, legacy_decision=decision.to_dict())
            except Exception:
                pass
            return False, "", {}
        if decision.kind == "app_open" and getattr(self,"app_skills",None) is not None:
            try:
                result=dict(self.app_skills.open(decision.app) or {})
                ok=bool(result.get("ok")); verified=bool(result.get("verified",ok))
                shown={"telegram":"Telegram","yandex_music":"Яндекс Музыку","youtube":"YouTube","spotify":"Spotify","discord":"Discord"}.get(decision.app,decision.app)
                return True,(f"Открыла {shown}." if ok else f"Не смогла открыть {shown}: {result.get('error') or 'не найдено'}."),{"action":"r22_app_open","model":"deterministic","control_plane":True,"completed":ok,"verified":verified,"app":decision.app,"result":result}
            except Exception as exc:
                return True,f"Не смогла открыть приложение: {humanize(exc)}.",{"action":"r22_app_open","model":"deterministic","control_plane":True,"completed":False,"verified":False,"error":str(exc)}
        if decision.kind == "application_open":
            return self._r23_open_application(decision.target)
        if decision.kind == "contextual_open":
            return self._r23_contextual_open(decision.target, decision.verb, conversation_id)
        if decision.kind == "media" and getattr(self, "app_skills", None) is not None:
            try:
                result = dict(self.app_skills.control_music(query) or {})
            except Exception as exc:
                result = {"ok": False, "completed": False, "verified": False, "error": str(exc)}
            action = str(result.get("action") or "")
            verified = bool(result.get("verified"))
            completed = bool(result.get("completed"))
            success = {
                "play": "Продолжила воспроизведение.",
                "pause": "Поставила музыку на паузу.",
                "next": "Включила следующий трек.",
                "previous": "Вернула предыдущий трек.",
                "like": "Поставила лайк текущему треку.",
                "dislike": "Поставила дизлайк текущему треку.",
                "repeat": "Изменила режим повтора.",
                "shuffle": "Изменила случайный порядок.",
                "seek": "Перемотала трек.",
                "info": f"Сейчас играет: {result.get('track') or 'название не удалось прочитать'}.",
            }.get(action, "Команду плееру выполнила.")
            answer = success if verified else (
                "Команду плееру выполнила один раз, но изменение состояния не подтвердилось."
                if completed else str(result.get("error") or "Не вижу доступного музыкального плеера.")
            )
            return True, answer, {
                "action": "r64_media_control", "model": "deterministic+uia", "control_plane": True,
                "completed": completed, "verified": verified, "result": result,
            }
        if decision.kind == "media_start" and getattr(self, "app_skills", None) is not None:
            try:
                result = dict(self.app_skills.play_music(query) or {})
            except Exception as exc:
                result = {"ok": False, "verified": False, "error": str(exc)}
            if result.get("needs_user"):
                choices=[{"label":x.replace("Включи музыку в ","").replace("Включи музыку на ","").replace("Включи ",""),"value":x} for x in result.get("choices",[])]
                return True, str(result.get("error") or "Уточни, где включить музыку."), {
                    "action":"music_choice","model":"deterministic","control_plane":True,
                    "needs_user":True,"completed":False,"verified":False,
                    "ui_choices":choices,"ui_question":"Где включить музыку?","result":result,
                }
            ok = bool(result.get("ok")); verified = bool(result.get("verified"))
            return True, ("Включила музыку." if verified else ("Команду текущему плееру отправила, но состояние подтвердить не смогла." if result.get("completed") else f"Музыка не включилась: {result.get('error') or 'плеер не ответил'}.")), {
                "action": "r23_media_start", "model": "deterministic+uia", "control_plane": True,
                "completed": bool(result.get("completed") or ok), "verified": verified, "result": result,
            }
        if decision.kind == "media_content" and getattr(self, "desktop_operator", None) is not None:
            try:
                result = dict(self.desktop_operator.yandex_play_query(decision.target) or {})
            except Exception as exc:
                result = {"ok": False, "completed": False, "verified": False, "error": str(exc)}
            completed = bool(result.get("completed") or result.get("ok"))
            verified = bool(result.get("verified"))
            return True, (
                f"Включила «{decision.target}»." if verified
                else (f"Запуск «{decision.target}» выполнила один раз, но воспроизведение не подтвердилось." if completed
                      else f"Не смогла включить «{decision.target}»: {result.get('error') or 'трек не найден'}." )
            ), {"action":"r23_media_content","model":"deterministic+uia","control_plane":True,
                "target":decision.target,"completed":completed,"verified":verified,"result":result}
        if decision.kind == "app_compound":
            acted,answer,route=self._r22_app_compound_turn(query,decision.app,decision.remainder,conversation_id)
            if acted:
                return acted,answer,route
        if decision.kind == "external_open":
            return self._r22_open_external(decision.target)
        if decision.kind == "page_navigation" and getattr(self,"universal_workflow",None) is not None:
            try: named=self.universal_workflow.click_named_current(query)
            except Exception as exc: named={"ok":False,"completed":False,"verified":False,"error":str(exc)}
            if named is not None:
                completed=bool(named.get("completed")); verified=bool(named.get("verified"))
                return True,(str(named.get("answer") or f"Перешла в «{decision.target}».") if completed else str(named.get("error") or f"Не нашла «{decision.target}».")),{"action":"r22_page_navigation","model":"uia","control_plane":True,"completed":completed,"verified":verified,"target":decision.target,"result":named}
        # Explicit long-horizon/cross-app structure is intentionally left for MissionEngine.

        # r20 arbitration: a compound/cross-app mission owns the whole utterance. Broad
        # app-specific fast paths below are atomic helpers and must never steal only the
        # Telegram/media tail from a larger request. Mission control/cancel/shutdown above
        # intentionally remain higher priority.
        mission_engine = getattr(self, "mission_engine", None)
        if mission_engine is not None:
            try:
                if mission_engine.should_handle(query):
                    return False, "", {}
            except Exception:
                pass

        workflow = getattr(self, "universal_workflow", None)
        pending = bool(workflow and conversation_id and workflow.has_pending(conversation_id))
        runtime_state = self.runtime.status() if self.runtime is not None else {}
        bare_resume = bool(re.match(r"^\s*(?:продолжай|продолжить|продолжи|дальше|возобнови)\s*[.!]*$", clean))
        task_resume = bool(re.search(r"\b(?:продолж|возобнов|сними с паузы)\w*\s+(?:задач|работ|сценар|операц)\w*", clean))
        if (bare_resume and bool(runtime_state.get("paused")) and not pending) or task_resume:
            if self.runtime: self.runtime.resume()
            return True, "Продолжаю задачу.", {"action": "resume_all", "model": "deterministic", "control_plane": True}
        if pending and bare_resume:
            return False, "", {}

        # Any new explicit action supersedes an old checkpoint. The old executing turn was
        # already signalled by self.stop() before this method; do not let stale pending state
        # capture the owner's next command.
        if pending and workflow is not None and re.search(r"\b(?:открой|включи|выключи|перейди|найди|напиши|отправь|нажми|поставь|полистай|пролистай|прокрути)\w*", clean):
            try: workflow._clear_pending(conversation_id)
            except Exception: pass
            pending = False

        # Telegram send is already a deterministic visible-screen skill. Route it before
        # universal planning so 'открой Telegram и напиши Кириллу...' does not wait on an extra model turn.
        # foreground_title was captured once before arbitration and is reused below.
        # r21.1: atomic app opens must never fall through to the visual planner just
        # because EIRVEN's own browser window happens to be in front. This is especially
        # visible after onboarding where "открой Telegram" otherwise paid four 5-second
        # local-model timeouts while staring at EIRVEN's UI.
        simple_app_open = re.fullmatch(r"(?:открой|запусти)\s+(telegram|телеграм\w*|яндекс\s*музык\w*|youtube|ютуб|spotify|спотифай|discord|дискорд)\s*[.!]*", clean, re.I)
        if simple_app_open and getattr(self, "app_skills", None) is not None:
            target = simple_app_open.group(1)
            try:
                result = self.app_skills.open(target)
                ok = bool(result.get("ok"))
                label = self.app_skills.canonical(target) or target
                names = {"telegram":"Telegram", "yandex_music":"Яндекс Музыку", "youtube":"YouTube", "spotify":"Spotify", "discord":"Discord"}
                shown = names.get(label, target)
                verified = bool(result.get("verified"))
                answer = f"Окно {shown} найдено и подтверждено." if verified else (f"Команда запуска {shown} отправлена, но активное окно не подтвердилось." if ok else f"Не смогла открыть {shown}: {result.get('error') or 'не найдено'}.")
                return True, answer, {"action":"open_app_priority","model":"deterministic","control_plane":True,"completed":ok,"verified":verified,"result":result}
            except Exception as exc:
                return True, f"Не смогла открыть приложение: {humanize(exc)}.", {"action":"open_app_priority","model":"deterministic","control_plane":True,"completed":False,"verified":False,"error":str(exc)}

        # OS volume is an atomic global primitive. Do not spend a model turn on
        # "сделай потише" and do not confuse it with the current web player's own slider.
        volume_action = ""
        volume_steps = 2
        # r18 tolerates modifiers ("системную громкость") and explicit deltas.
        # Windows media keys are step-based, so an explicit number means that many
        # bounded volume steps; the action is still verified as an OS primitive.
        amount_m = re.search(r"\bна\s+(\d{1,2})\b", clean)
        if amount_m:
            volume_steps = max(1, min(int(amount_m.group(1)), 10))
        if re.search(r"\b(?:сделай\s+потише|сделай\s+тише)\b", clean) or re.search(r"\b(?:убав\w*|уменьш\w*|пониз\w*)\b.{0,35}\b(?:системн\w*\s+)?(?:звук|громк)\w*", clean) or re.search(r"\b(?:громк|звук)\w*.{0,20}\b(?:ниже|меньше|потише)\b", clean):
            volume_action = "down"
        elif re.search(r"\b(?:сделай\s+погромче|сделай\s+громче)\b", clean) or re.search(r"\b(?:прибав\w*|увелич\w*|повыс\w*)\b.{0,35}\b(?:системн\w*\s+)?(?:звук|громк)\w*", clean) or re.search(r"\b(?:громк|звук)\w*.{0,20}\b(?:выше|больше|погромче)\b", clean):
            volume_action = "up"
        elif re.search(r"\b(?:выключи\w*\s+звук|без\s+звука|mute|мьют)\b", clean):
            volume_action = "mute"
            volume_steps = 1
        if volume_action and self.tools is not None:
            result = self.tools.execute("system_volume", {"action": volume_action, "steps": volume_steps})
            ok = bool(result.get("ok"))
            label = {"down":"Команда уменьшения громкости отправлена, но новое значение не удалось прочитать.", "up":"Команда увеличения громкости отправлена, но новое значение не удалось прочитать.", "mute":"Команда mute отправлена, но состояние не подтвердилось."}[volume_action]
            return True, (label if ok else f"Не смогла изменить системную громкость: {result.get('error') or 'ошибка Windows'}."), {
                "action":"system_volume_priority","model":"deterministic","control_plane":True,
                "verified":False,"completed":ok,"result":result,
            }

        # Explicit process termination is a system operation, never a browser/UI goal.
        # Keep EIRVEN's own Python process tree alive so it can report completion; a
        # separate "выключи Эрви" command owns self-termination.
        if re.search(r"\b(?:закрой|заверши|убей|останови)\w*.{0,30}\b(?:все|всё)\s+(?:пайтон|python)[- ]?процесс\w*", clean) and self.tools is not None:
            result = self.tools.execute("process_terminate", {"name_contains":"python", "all_matches":True, "protect_eirven":True})
            payload = result.get("result") if result.get("ok") else {}
            verified = bool(result.get("ok") and payload.get("verified"))
            terminated = int(payload.get("terminated_count") or 0)
            protected = int(payload.get("protected_count") or 0)
            answer = (
                f"Закрыла внешние Python-процессы: {terminated}. Процессы самой EIRVEN оставила активными: {protected}."
                if verified else
                f"Не смогла подтвердить завершение всех внешних Python-процессов: {result.get('error') or payload.get('error') or 'проверка не прошла'}."
            )
            return True, answer, {"action":"python_process_terminate_priority","model":"deterministic","control_plane":True,"verified":verified,"completed":bool(result.get("ok")),"result":result}

        # A literal site-open command is deterministic navigation, not an application
        # launch and not a generic agent problem. This also tolerates ASR repetition:
        # "открой открой сайт X".
        original_query = str(query or "").strip()
        site_match = re.match(r"^(?:открой\s+)+(?:мне\s+)?сайт\s+(.+?)[.!]?\s*$", original_query, re.I)
        # Допускаем обращение и вежливые вставки: «Эрви, открой chatgot.com»,
        # «открой пожалуйста chatgot.com». Строгий вариант требовал запрос ровно
        # из двух слов, и любая приставка уводила команду в планировщик — там она
        # выполняется вслепую и без подтверждения результата.
        domain_match = re.match(
            r"^(?:эрви[,\s]+)?(?:открой|запусти|зайди\s+на|перейди\s+на)\s+"
            r"(?:мне\s+|пожалуйста\s+|плиз\s+)*"
            r"(https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)[.!]?\s*$",
            original_query, re.I)
        if site_match or domain_match:
            target = str((site_match or domain_match).group(1) or "").strip().strip('«»"\'')
            result: dict[str, Any] = {}
            opened_url = ""
            try:
                if re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/.*)?$", target, re.I):
                    url = target if target.lower().startswith(("http://","https://")) else "https://" + target
                    result = self.tools.execute("open_default_url", {"url": url}) if self.tools else {"ok":False,"error":"tools unavailable"}
                    opened_url = url
                else:
                    applications = getattr(getattr(self, "app_skills", None), "services", None)
                    applications = getattr(applications, "applications", None)
                    if applications is not None:
                        result = {"ok": True, **dict(applications.web_fallback(target))}
                        opened_url = str(result.get("url") or "")
                    else:
                        result = self.tools.execute("default_search", {"query": f"{target} официальный сайт"}) if self.tools else {"ok":False}
                time.sleep(.55)
                fg2 = self.tools.execute("foreground_window", {}) if self.tools else {}
                title2 = str((fg2.get("result") or {}).get("title") or "") if fg2.get("ok") else ""
                needle = re.sub(r"[^a-zа-я0-9]+", "", target.casefold().replace("ё","е"))
                title_key = re.sub(r"[^a-zа-я0-9]+", "", title2.casefold().replace("ё","е"))
                similarity = SequenceMatcher(None, needle[:28], title_key[:80]).ratio() if needle and title_key else 0.0
                verified = bool(result.get("ok") and (((needle[:5] in title_key) if len(needle) >= 5 else False) or similarity >= .58))
                # Navigation itself succeeded even if the SPA title does not expose the
                # domain. Do not retry/open duplicates just to improve verification.
                completed = bool(result.get("ok"))
                return True, (
                    f"Открыла сайт {target}." if verified else
                    (f"Сайт {target} открыла; заголовок страницы пока не дал надёжно подтвердить адрес." if completed else
                     f"Не смогла открыть сайт {target}: {result.get('error') or 'не найден'}." )
                ), {
                    "action":"open_site_priority","model":"deterministic","control_plane":True,
                    "verified":verified,"completed":completed,"url":opened_url,"result":result,
                }
            except Exception as exc:
                return True, f"Не смогла открыть сайт {target}: {humanize(exc)}.", {
                    "action":"open_site_priority","model":"deterministic","control_plane":True,
                    "verified":False,"completed":False,"error":str(exc),
                }

        telegram_context = bool(re.search(r"\b(?:telegram|телеграм|телеграмм|тг)\b", clean) or re.search(r"telegram|телеграм", foreground_title, re.I))

        # A generic "write/send a message" command does not mean Telegram.  Preserve
        # the content/recipient in pending state and ask only for the missing surface.
        generic_send = bool(re.search(r"\b(?:напиши|отправь|скинь|пошли)\w*\b", clean) and re.search(r"\b(?:сообщен\w*|текст\w*)\b", clean))
        named_surface = re.search(r"\b(?:telegram|телеграм|тг|max|макс|whatsapp|ватсап|discord|дискорд|vk|вконтакте|почт\w*|email)\b", clean, re.I)
        if generic_send and not telegram_context and not named_surface:
            recipient, _platform, message = self._parse_send_target(str(query or ""))
            self._save_messenger_pending({"recipient":recipient,"platform":"","message":message,"awaiting":"platform","at":time.time()})
            return True, "В каком приложении отправить сообщение? Текст и получателя я сохранила.", {
                "action":"send_need_platform","model":"deterministic","control_plane":True,
                "needs_user":True,"completed":False,"verified":False,
                "ui_question":"Выбери приложение",
                "ui_choices":[
                    {"label":"Текущее окно","value":"Отправь это сообщение в текущем открытом приложении"},
                    {"label":"Telegram","value":"Отправь это сообщение в Telegram"},
                    {"label":"MAX","value":"Отправь это сообщение в MAX"},
                ],
            }

        # 'в моём стиле' describes HOW to compose a reply; it is never literal message text.
        if telegram_context and re.search(r"\b(?:напиши|ответь|отправь)\w*", clean) and re.search(r"\bв\s+мо[её]м\s+стиле\b", clean):
            recipient_match=re.search(r"(?:напиши|ответь|отправь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,50})", str(query or ""), re.I)
            rec=recipient_match.group(1).strip() if recipient_match and " ".join(recipient_match.group(1).casefold().replace("ё","е").split()) not in {"сообщение","текст"} else ""
            acted,answer,route=self._telegram_style_reply(recipient=rec)
            return acted,answer,{**route,"control_plane":True}

        if telegram_context and re.search(r"\b(?:напиши|отправь|введи|набери)\w*", clean):
            original = str(query or "").strip()
            recipient, message = self._parse_telegram_command(original)
            if recipient and message:
                acted, answer, route = self._telegram_send_turn(f"{recipient} в telegram сообщение {message}")
                return acted, answer, {**route, "control_plane": True}
            if message and not recipient and re.search(r"\b(?:отправь|напиши)\w*\s+(?:сообщение|текст)\b", original, re.I):
                self._save_messenger_pending({"recipient":"","platform":"telegram","message":message,"at":time.time()})
                return True, "Кому именно отправить сообщение?", {"action":"send_need_recipient","model":"deterministic","control_plane":True}
            if recipient and not message:
                self._save_messenger_pending({"recipient":recipient,"platform":"telegram","message":"","at":time.time()})
                return True, f"Что написать {recipient}?", {"action":"send_need_text","model":"deterministic","control_plane":True}
            # Current open Telegram chat: 'напиши привет' means type+send once here.
            m2 = re.search(r"(?:напиши|набери|введи)\w*\s+(?:просто\s+)?(?:текст\s+)?[«\"']?(.+?)[»\"']?[.!]?\s*$", original, re.I)
            if m2 and workflow is not None and not re.search(r"\b(?:кому|кирилл|тимоф|маме|папе)\w*", clean):
                payload = m2.group(1).strip().strip('«»"\'')
                if payload:
                    focused = workflow._current_window_text_fastpath(f'в текущем окне напиши текст «{payload}» и отправь', text=payload)
                    if focused and focused.get("completed"):
                        verified = bool(focused.get("verified"))
                        return True, ("Сообщение отправила." if verified else "Сообщение отправила один раз; повторять не стала."), {"action":"telegram_current_send","model":"uia","control_plane":True,"completed":True,"verified":verified,"result":focused}

        # Search/navigation inside the current web app is UI work, not conversational planning.
        # Accept both word orders: "в поиске введи X" and "найди песню X" in Yandex Music.
        page_search=None
        original=str(query or "").strip()
        for pattern in (
            r"(?:на\s+(?:сайте|странице)\s+)?(?:в\s+)?поиск\w*.*?(?:набери|введи|вбей|вбить|вбивай|напиши)\w*\s+[«\"']?(.+?)[»\"']?[.!]?\s*$",
            r"(?:набери|введи|вбей|вбить|вбивай|напиши)\w*\s+(?:в\s+)?поиск\w*(?:\s+на\s+(?:сайте|странице))?\s+[«\"']?(.+?)[»\"']?[.!]?\s*$",
        ):
            page_search=re.search(pattern,original,re.I)
            if page_search: break
        yandex_context=bool(re.search(r"яндекс\s*музык",foreground_title,re.I) or re.search(r"яндекс\s*музык",clean,re.I))
        if yandex_context and getattr(self,"desktop_operator",None) is not None:
            # Like/dislike are stable semantic player buttons in Yandex Music. The old
            # A previous fast route ignored a visible Button for this request.
            # named exactly "Нравится". Click it once and verify by the local button crop
            # or accessibility signature changing.
            if re.search(r"\b(?:поставь\w*\s+)?(?:лайк|нравится)\b", clean) and not re.search(r"\b(?:дизлайк|не\s+нравится)\b", clean):
                try:
                    fg_like = self.tools.execute("foreground_window", {}) if self.tools else {}
                    win = dict(fg_like.get("result") or {}) if fg_like.get("ok") else {}
                    title = str(win.get("title") or foreground_title)
                    handle = int(win.get("handle") or 0) or None
                    rows = list(self.desktop_operator._elements(title, limit=320, handle=handle))
                    candidates = []
                    for el in rows:
                        if not el.get("visible", True) or not el.get("enabled", True):
                            continue
                        if str(el.get("control_type") or "").casefold() != "button":
                            continue
                        name = " ".join(str(el.get("name") or "").casefold().replace("ё","е").split())
                        rect = el.get("rectangle") or []
                        if name == "нравится" and len(rect) == 4 and int(rect[3]) > 180:
                            candidates.append(el)
                    target = max(candidates, key=lambda e: int((e.get("rectangle") or [0,0,0,0])[0])) if candidates else None
                    if target is None:
                        return True, "Не нашла видимую кнопку «Нравится» у текущего трека.", {
                            "action":"yandex_like_priority","model":"uia","control_plane":True,"completed":False,"verified":False,
                        }
                    rect = [int(v) for v in target.get("rectangle")]
                    before_sig = f"{target.get('name','')}|{target.get('class_name','')}"
                    before_crop = ""
                    try:
                        import pyautogui
                        shot = pyautogui.screenshot(region=(max(0,rect[0]-8),max(0,rect[1]-8),max(4,rect[2]-rect[0]+16),max(4,rect[3]-rect[1]+16)))
                        before_crop = hashlib.sha1(shot.tobytes()).hexdigest()
                    except Exception:
                        pass
                    clicked = bool(self.desktop_operator.click_element(title, target, goal="поставь лайк текущему треку"))
                    if not clicked:
                        return True, "Кнопку «Нравится» нашла, но нажать её не удалось.", {
                            "action":"yandex_like_priority","model":"uia","control_plane":True,"completed":False,"verified":False,
                        }
                    time.sleep(.28)
                    verified = False
                    after_sig = ""
                    try:
                        after_rows = list(self.desktop_operator._elements(title, limit=320, handle=handle))
                        near = []
                        cx = (rect[0]+rect[2])//2; cy=(rect[1]+rect[3])//2
                        for el in after_rows:
                            r = el.get("rectangle") or []
                            if len(r) != 4 or str(el.get("control_type") or "").casefold() != "button":
                                continue
                            ex=(int(r[0])+int(r[2]))//2; ey=(int(r[1])+int(r[3]))//2
                            if abs(ex-cx) <= 90 and abs(ey-cy) <= 90:
                                near.append(el)
                        if near:
                            post = min(near, key=lambda e: abs(((int(e["rectangle"][0])+int(e["rectangle"][2]))//2)-cx)+abs(((int(e["rectangle"][1])+int(e["rectangle"][3]))//2)-cy))
                            after_sig = f"{post.get('name','')}|{post.get('class_name','')}"
                            verified = after_sig != before_sig
                    except Exception:
                        pass
                    if not verified and before_crop:
                        try:
                            import pyautogui
                            shot = pyautogui.screenshot(region=(max(0,rect[0]-8),max(0,rect[1]-8),max(4,rect[2]-rect[0]+16),max(4,rect[3]-rect[1]+16)))
                            verified = hashlib.sha1(shot.tobytes()).hexdigest() != before_crop
                        except Exception:
                            pass
                    return True, ("Поставила лайк текущему треку." if verified else "Лайк нажала один раз, но визуальное состояние кнопки подтвердить не смогла."), {
                        "action":"yandex_like_priority","model":"uia","control_plane":True,
                        "completed":True,"verified":verified,"before":before_sig,"after":after_sig,
                    }
                except Exception as exc:
                    return True, f"Не смогла поставить лайк: {humanize(exc)}.", {
                        "action":"yandex_like_priority","model":"uia","control_plane":True,"completed":False,"verified":False,"error":str(exc),
                    }

            # Yandex Music exposes its sidebar as real Hyperlinks/ListItems.  A natural
            # command such as "перейди в коллекцию" must not fall through to the action
            # model merely because the owner omitted the word "раздел".
            nav=re.search(r"\b(?:перейди|открой|зайди)\w*\s+(?:(?:в|на)\s+)?[«\"']?(.+?)[»\"']?[.!]?\s*$",original,re.I)
            if nav:
                target=nav.group(1).strip().strip('«»"\'')
                target_n=" ".join(target.casefold().replace("ё","е").split())
                if target_n and target_n not in {"поиск","поиске","яндекс музыку","яндексмузыку"} and workflow is not None:
                    try: result=workflow.click_named_current(f"перейди в раздел {target}")
                    except Exception as exc: result={"ok":False,"completed":False,"verified":False,"error":str(exc),"target":target}
                    if result is not None:
                        return True,(str(result.get("answer") or f"Перешла в «{target}».") if result.get("completed") else f"Не смогла перейти в «{target}»: {result.get('error') or 'элемент не найден'}."),{"action":"yandex_named_navigation","model":"uia","control_plane":True,"result":result}
            song=re.search(r"(?:найди|отыщи|поиск)\w*\s+(?:песн\w*|трек\w*|музык\w*)?\s*[«\"']?(.+?)[»\"']?[.!]?\s*$",original,re.I)
            if song and " ".join(song.group(1).casefold().replace("ё","е").split()) not in {"поиск","поиска"}:
                payload=song.group(1).strip().strip('«»"\'')
                try: result=self.desktop_operator.current_page_search(payload,submit=True,max_scrolls=0)
                except Exception as exc: result={"ok":False,"completed":False,"verified":False,"error":str(exc)}
                return True,(f"Открыла поиск Яндекс Музыки и ввела «{payload}»." if result.get("ok") else f"Не смогла выполнить поиск в Яндекс Музыке: {result.get('error') or 'поле не найдено'}."),{"action":"yandex_search_priority","model":"uia","control_plane":True,"result":result}
            if re.search(r"\b(?:включи|открой|покажи|перейди\s+в)\w*\s+поиск\w*",clean):
                try:
                    acq=self.desktop_operator.acquire_input(purpose="search",aliases=["поиск","search","query"],trigger_aliases=["Поиск","Search"],max_scrolls=0,visual_fallback=False)
                    result={"ok":bool(acq.get("ok")),"completed":bool(acq.get("ok")),"verified":bool(acq.get("focused")),"error":acq.get("error","")}
                except Exception as exc: result={"ok":False,"completed":False,"verified":False,"error":str(exc)}
                return True,("Открыла поиск Яндекс Музыки." if result.get("ok") else f"Не смогла открыть поиск Яндекс Музыки: {result.get('error') or 'не найден'}."),{"action":"yandex_search_open_priority","model":"uia","control_plane":True,"result":result}
        if page_search and getattr(self,"desktop_operator",None) is not None and re.search(r"browser|samsung|chrome|edge|firefox|opera|yandex",foreground_title,re.I):
            payload=page_search.group(1).strip().strip('«»"\'')
            if payload:
                try: result=self.desktop_operator.current_page_search(payload,submit=False,max_scrolls=5)
                except Exception as exc: result={"ok":False,"completed":False,"verified":False,"error":str(exc)}
                return True,(f"Нашла поиск на странице и ввела «{payload}»." if result.get("ok") else f"Не смогла заполнить поиск на странице: {result.get('error') or 'поле не найдено'}."),{"action":"page_search_priority","model":"uia","control_plane":True,"result":result}

        # Explicit Play inside Yandex Music belongs to the Yandex UI skill, not the global
        # media-key toggle (which can control the wrong browser tab/media session).
        if getattr(self,"app_skills",None) is not None and re.search(r"яндекс\s+музык", foreground_title, re.I) and re.search(r"\b(?:нажми|включи|запусти|воспроизвед)\w*.*\b(?:play|плей|игра(?:й|ть)?|воспроизвед)\w*", clean):
            try: result=self.app_skills.play_music()
            except Exception as exc: result={"ok":False,"verified":False,"error":str(exc)}
            verified=bool(result.get("ok") and result.get("verified"))
            return True,("Нажала Play в Яндекс Музыке." if verified else f"Кнопку Play в Яндекс Музыке пока не удалось подтвердить: {result.get('error') or 'не найдена'}."),{"action":"yandex_play_priority","model":"screen-operator","control_plane":True,"verified":verified,"result":result}

        if workflow is not None:
            # Player settings have separate ownership from transport controls.
            try:
                autoplay = workflow.ensure_autoplay_goal(query)
            except Exception as exc:
                autoplay = None; self._trace("CONTROL_AUTOPLAY_ERROR", query=query, error=str(exc)[:500])
            if autoplay is not None:
                verified = bool(autoplay.get("verified")); completed = bool(autoplay.get("completed"))
                answer = ("Автовоспроизведение выключила." if autoplay.get("desired") == "off" else "Автовоспроизведение включила.") if verified else ("Переключатель нажала один раз, но состояние не подтвердилось." if completed else "Не нашла переключатель автовоспроизведения.")
                return True, answer, {"action":"autoplay_control_priority","model":"uia","control_plane":True,"verified":verified,"completed":completed,"result":autoplay}

            if re.search(r"\b(?:полистай|пролистай|листай|прокрути|скролл)\w*", clean):
                try: scrolled = workflow.scroll_current_goal(query)
                except Exception as exc: scrolled = {"ok":False,"completed":False,"verified":False,"error":str(exc)}
                completed = bool(scrolled and scrolled.get("completed")); verified = bool(scrolled and scrolled.get("verified"))
                return True, ("Прокрутила список." if completed else f"Не смогла прокрутить: {str((scrolled or {}).get('error') or 'неизвестная ошибка')}."), {"action":"scroll_priority","model":"deterministic","control_plane":True,"completed":completed,"verified":verified,"result":scrolled}

            if re.search(r"\b(?:зайди|перейди|открой|нажми|выбери)\w*.{0,80}\b(?:раздел|категори|вкладк|пункт|ссылк)\w*", clean) or re.search(r"\b(?:перейди|открой)\w*.{0,50}\bстраниц\w*\s+\S+",clean):
                try: named = workflow.click_named_current(query)
                except Exception as exc: named = {"ok":False,"completed":False,"verified":False,"error":str(exc)}
                if named is not None:
                    completed = bool(named.get("completed")); verified = bool(named.get("verified"))
                    return True, (str(named.get("answer") or "Перешла в нужный раздел.") if completed else str(named.get("error") or "Нужный раздел на странице не найден.")), {"action":"named_current_priority","model":"uia","control_plane":True,"completed":completed,"verified":verified,"result":named}

        # User music owns 'включи музыку' before a generic enable workflow.  A bare
        # request resumes an already-open player; the owner chooses the service.
        if re.search(r"\b(?:включи|запусти)\w*\s+(?:яндекс\s+)?музык\w*", clean) and self.app_skills is not None:
            try: result = self.app_skills.play_music(clean)
            except Exception as exc: result = {"ok":False,"verified":False,"error":str(exc)}
            if result.get("needs_user"):
                choices=[{"label":x.replace("Включи музыку в ","").replace("Включи музыку на ","").replace("Включи ",""),"value":x} for x in result.get("choices",[])]
                return True, str(result.get("error") or "Уточни, где включить музыку."), {"action":"music_choice","model":"deterministic","control_plane":True,"needs_user":True,"completed":False,"verified":False,"ui_choices":choices,"ui_question":"Где включить музыку?","result":result}
            verified = bool(result.get("ok") and result.get("verified"))
            return True, ("Включила музыку и подтвердила воспроизведение." if verified else ("Команду текущему плееру отправила, но состояние подтвердить не смогла." if result.get("completed") else f"Музыка не включилась: {result.get('error') or 'плеер не ответил'}.")), {"action":"music_priority","model":"screen-operator","control_plane":True,"verified":verified,"completed":bool(result.get("completed")),"result":result}

        if workflow is not None:
            try:
                media_result = workflow.ensure_media_goal(query, allow_implicit=True)
            except Exception as exc:
                media_result = None
                self._trace("CONTROL_MEDIA_ERROR", query=query, error=str(exc)[:500])
            if media_result is not None:
                verified = bool(media_result.get("verified")); completed = bool(media_result.get("completed")); desired = str(media_result.get("desired") or "")
                if verified:
                    answer = "Поставила медиа на паузу." if desired == "paused" else ("Возобновила воспроизведение." if desired == "playing" else "Медиа-команду выполнила.")
                elif completed: answer = "Команду плееру выполнила один раз, но изменение состояния подтвердить не смогла."
                else: answer = "Не смогла выполнить медиа-команду."
                route = {"action":"media_control_priority","model":"deterministic","control_plane":True,"verified":verified,"completed":completed,"result":media_result}
                self._trace("CONTROL_MEDIA_OUT", query=query, verified=verified, completed=completed, result=media_result)
                return True, answer, route
        return False, "", {}

    def _contextual_problem_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Turn natural problem statements into real diagnostics instead of small talk."""
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        clean = re.sub(r"^\s*(?:эрви|эрви|eirven)[, :!-]*", "", clean, flags=re.I).strip()
        if not clean or self.tasks is None:
            return False, "", {}
        foreground_title = ""
        try:
            if self.tools is not None:
                fg = self.tools.execute("foreground_window", {})
                foreground_title = str((fg.get("result") or {}).get("title") or "") if fg.get("ok") else ""
        except Exception:
            pass
        vscode_active = bool(re.search(r"(?:visual studio code|vs code|vscode|\bcode\b)", foreground_title, re.I))
        code_problem = bool(re.search(
            r"(?:\bгде\s+(?:тут\s+)?баг\b|\bнайди\w*\s+баг\b|\bчто\s+за\s+ошибк\w*\b|"
            r"\bпочему\b.{0,70}\b(?:не\s+работ\w*|падает|ломается|ошибк\w*)\b|"
            r"\b(?:почини|исправь)\w*\b.{0,70}\b(?:код|проект|ошибк|баг)\w*)",
            clean, re.I | re.S,
        ))
        if vscode_active and code_problem:
            task_id = self.tasks.enqueue(
                "vscode_repair", f"VS Code: {clean[:100]}", {"question": query}, conversation_id=conversation_id,
            )
            return True, self._self_gendered(
                "Уже смотрю текущий проект VS Code: воспроизведу ошибку, исправлю минимально и прогоню проверку. Можешь продолжать говорить со мной.",
                "Уже смотрю текущий проект VS Code: воспроизведу ошибку, исправлю минимально и прогоню проверку. Можешь продолжать говорить со мной.",
            ), {"action": "vscode_repair_started", "model": "workspace-agent", "task_id": task_id, "foreground": foreground_title}

        app_problem = bool(re.search(
            r"(?:\bу\s+меня\b.{0,90}\bне\s+(?:работает|запускается|открывается)|"
            r"\bпочему\b.{0,100}\bне\s+(?:работает|запускается|открывается)|"
            r"\b(?:завис|вылетает|крашится|сломал(?:ась|ся|ось)?)\w*)",
            clean, re.I | re.S,
        ))
        skill = self.app_skills.canonical(query) if self.app_skills is not None else ""
        if app_problem and skill and skill != "vscode":
            task_id = self.tasks.enqueue("repair", f"Диагностика {skill}", {"problem": query}, conversation_id=conversation_id)
            return True, self._self_gendered(
                "Проверяю приложение по фактам: процессы, окно, локальные файлы/логи и системное состояние. Если причина исправима автоматически — исправлю и перепроверю.",
                "Проверяю приложение по фактам: процессы, окно, локальные файлы/логи и системное состояние. Если причина исправима автоматически — исправлю и перепроверю.",
            ), {"action": "app_problem_repair_started", "model": "repair-agent", "task_id": task_id, "skill": skill}
        return False, "", {}

    def _global_direct_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        clean=" ".join(query.casefold().replace("ё","е").split())
        if re.search(r"\bперезапусти\w*\s+(?:локальн\w*\s+)?модел\w*", clean):
            model = self.settings.model
            try:
                self.gateway.unload(model)
            except Exception:
                pass
            try:
                # During owner training llm.py forces this warm-up to CPU automatically.
                self.gateway.warm(model, keep_alive=self.settings.keep_alive)
                return True, "Перезапустила локальную модель. Можешь повторить вопрос.", {"action":"model_restart","model":"deterministic","target_model":model}
            except Exception as exc:
                return True, f"Не получилось перезапустить локальную модель: {humanize(exc)}", {"action":"model_restart_failed","model":"deterministic","target_model":model,"needs_user":True}
        if re.search(r"\b(?:что ты сейчас делаешь|чем ты сейчас занимаешься|что выполняешь)\b", clean):
            st=self.runtime.status() if self.runtime else {"action":"idle","step":""}
            if st.get("action") in {"idle", ""} or not st.get("cancellable"):
                return True,"Сейчас ничего долгого не выполняю. Слушаю тебя.",{"action":"runtime_status","runtime":st,"model":"deterministic"}
            return True,f"Сейчас {st.get('step') or st.get('goal') or st.get('action')}. Прошло примерно {max(0,int(st.get('elapsed_ms') or 0)//1000)} секунд.",{"action":"runtime_status","runtime":st,"model":"deterministic"}
        if re.search(r"^(?:не[ ,!-]*){0,3}(?:стоп|отмена|отмени|остановись|прекрати|хватит)[.! ]*$", clean):
            if self.runtime:
                self.runtime.stop_interactive()
            return True,"Остановила текущую интерактивную задачу.",{"action":"cancel_interactive","model":"deterministic"}
        if re.search(r"\b(?:останови все|останови всё|отмени все|отмени всё|стоп все|стоп всё)\b", clean):
            result=self.runtime.stop_all() if self.runtime else {"cancelled":0}
            return True,f"Остановила всё активное. Отменено задач: {int(result.get('cancelled') or 0)}.",{"action":"stop_all","model":"deterministic","result":result}
        if re.search(r"\b(?:отмени последнюю задачу|останови последнюю задачу|отмени последнее задание)\b", clean):
            cancelled=0
            task_id=""
            try:
                rows=self.tasks.list(100) if self.tasks is not None else []
                active=next((row for row in rows if row.get("status") in {"queued","running","waiting_user"}),None)
                if active:
                    task_id=str(active.get("id") or "")
                    cancelled=1 if self.tasks.cancel(task_id) else 0
            except Exception:
                cancelled=0
            if not cancelled and self.runtime is not None:
                self.runtime.stop_interactive()
                cancelled=1
            return True,("Последнюю активную задачу остановила." if cancelled else "Активной задачи для отмены не нашла."),{"action":"cancel_last","model":"deterministic","task_id":task_id,"cancelled":cancelled}
        if re.search(r"\b(?:поставь все на паузу|поставь всё на паузу|пауза для всего)\b", clean):
            if self.runtime: self.runtime.pause()
            return True,"Поставила интерактивную работу на паузу.",{"action":"pause_all","model":"deterministic"}
        if re.search(r"^\s*(?:продолжай|продолжить|сними с паузы)\s*[.!]*$",clean):
            st = self.runtime.status() if self.runtime else {}
            if bool(st.get("paused")):
                if self.runtime: self.runtime.resume()
                return True,"Продолжаю задачу.",{"action":"resume_all","model":"deterministic"}
            # In an unpaused runtime the phrase is context-dependent (media or pending
            # workflow), so do not consume it as a fake global resume.
            return False,"",{}
        if re.fullmatch(r"(?:верни\s+как\s+было|откати\s+последн\w*|отмени\s+последн(?:ее|юю)\s+изменен\w*|отмени\s+последнее)", clean):
            cognition = getattr(self, "cognition", None)
            result = cognition.undo_last() if cognition is not None else {"ok": False, "error": "Контур отката недоступен"}
            if result.get("ok"):
                return True, f"Вернула как было: {result.get('label') or result.get('path')}.", {"action":"undo_last","model":"deterministic","result":result}
            return True, str(result.get("error") or "Нет изменения для отката") + ".", {"action":"undo_none","model":"deterministic","result":result}
        skill_match = re.match(r"^\s*(?:сохрани|запомни)\s+(?:это|последнее)\s+как\s+навык(?:\s+[«\"']?(.+?)[»\"']?)?\s*[.!]*$", query, re.I)
        if skill_match:
            cognition = getattr(self, "cognition", None)
            result = cognition.save_last_as_skill(skill_match.group(1) or "") if cognition is not None else {"ok":False,"error":"Конструктор навыков недоступен"}
            if result.get("ok"):
                return True, f"Сохранила навык «{result.get('name')}». В нём {int(result.get('steps') or 0)} подтверждённых шагов.", {"action":"skill_saved","model":"deterministic","result":result}
            return True, str(result.get("error") or "Не удалось сохранить навык") + ".", {"action":"skill_save_failed","model":"deterministic","result":result}
        if re.fullmatch(r"(?:какие\s+у\s+тебя\s+навыки|покажи\s+(?:мои\s+)?навыки|список\s+навыков)", clean):
            cognition = getattr(self, "cognition", None)
            skills = cognition.skills() if cognition is not None else []
            names = [str(row.get("name") or "").strip() for row in skills if str(row.get("name") or "").strip()]
            return True, ("Сохранённые навыки: " + "; ".join(names[:12]) if names else "Сохранённых навыков пока нет."), {"action":"skill_list","model":"deterministic","count":len(names)}
        pending_send = self._pending_send_turn(query)
        if pending_send[0]:
            return pending_send

        m=re.match(r"^\s*запомни(?:,| что)?\s+(.+)$",query,re.I)
        if m:
            mid=self.memory.remember_structured(m.group(1).strip())
            return True,"Запомнила.",{"action":"memory_add","model":"deterministic","memory_id":mid}
        if re.match(r"^\s*(?:что ты обо мне помнишь|что ты помнишь обо мне)\s*[?!.]*$",query,re.I):
            profile=self.memory.profile(limit=30)
            items=[item for kind in ("relationship","preference","habit","project","user_fact","solution") for item in profile.get(kind, [])]
            return True,("Помню: "+"; ".join(items[:6]) if items else "Пока долговременных фактов о тебе почти нет."),{"action":"memory_list","model":"deterministic"}
        m=re.match(r"^\s*забудь(?:,|\s+что)?\s+(.+)$",query,re.I)
        if m:
            subject=m.group(1).strip()
            deleted=self.memory.forget_matching(subject)
            return True,(f"Забыла подходящих записей: {deleted}." if deleted else "Не стала удалять: совпадение слишком неточное."),{"action":"memory_forget","model":"deterministic","deleted":deleted}
        return False,"",{}

    def _deterministic_intent_turn(
        self,
        query: str,
        conversation_id: str,
        image_paths: list[str] | None,
        attachment_paths: list[str] | None,
    ) -> tuple[bool, str, dict[str, Any]]:
        if self.tools is None:
            return False, "", {}
        intent = detect_command(query)
        if intent is None or intent.confidence < 0.72:
            return False, "", {}
        target = intent.target.casefold().replace("ё", "е").strip()
        self._trace("INTENT", query=query, action=intent.action, target=target, confidence=intent.confidence, mixed=intent.mixed)
        camera_running = bool(self.camera is not None and self.camera.status().get("running"))
        if getattr(self, "planner", None) is not None:
            try:
                plan=self.planner.describe(intent.action,intent.target,camera=camera_running)
                self._trace("ACTION_PLAN",query=query,action=intent.action,target=intent.target,steps=plan)
                if self.runtime is not None: self.runtime.step("План действий готов",action=intent.action,target=intent.target,steps=plan)
            except Exception:
                pass

        if intent.action == "click":
            labels=[intent.target]
            low=target
            if re.search(r"\b(?:play|плей|воспроизв|запуск)\w*",low): labels=["Воспроизведение","Play","Воспроизвести","play_filled"]
            elif re.search(r"\b(?:pause|пауза)\w*",low): labels=["Пауза","Pause"]
            elif re.search(r"\b(?:далее|следующ|next)\w*",low): labels=["Следующая песня","Next"]
            elif re.search(r"\b(?:назад|предыдущ|prev)\w*",low): labels=["Предыдущая песня","Previous"]
            if getattr(self,"desktop_operator",None) is not None:
                if self.desktop_operator.click_current(labels,goal="voice_current_click"):
                    return True,"Нажала на текущем экране.",{"action":"current_screen_click","model":"deterministic","labels":labels}
                if self.desktop_operator.visual_click("Нажать нужную кнопку на текущем экране, не открывая новых окон",labels,timeout=5.0):
                    return True,"Нашла кнопку на текущем экране и нажала её.",{"action":"current_screen_click_visual","model":self.settings.vision_model,"labels":labels}
            return True,"На текущем экране не нашла такую кнопку. Новую страницу не открывала.",{"action":"current_screen_click_failed","model":"deterministic","labels":labels}

        if intent.action == "move" and camera_running:
            return self._move_spatial(intent.target)

        if intent.action == "repair":
            # Project edits keep their dedicated live-update path; "почини приложение/систему" is a repair job.
            if re.search(r"\b(?:проект|код|репозитор)\w*", target):
                return False,"",{}
            problem = query.strip() if re.search(r"\bпереустанов\w*", query, re.I) else (intent.target.strip() or query.strip())
            if self.tasks is None:
                return True,"Сервис задач недоступен — диагностику сейчас не запустить.",{"action":"repair_unavailable","model":"deterministic"}
            task_id=self.tasks.enqueue("repair",f"Починить: {problem[:80]}",{"problem":problem},conversation_id=conversation_id)
            return True,self._self_gendered("Начала диагностику и ремонт. Можешь продолжать со мной говорить — проверка идёт отдельно.","Начал диагностику и ремонт. Можешь продолжать со мной говорить — проверка идёт отдельно."),{"action":"repair_started","model":"deterministic","task_id":task_id}

        if intent.action == "send":
            return self._telegram_send_turn(intent.target)

        if intent.action == "answer":
            if re.search(r"\b(?:звонок|вызов|call)\b",target) and re.search(r"\b(?:discord|дискорд)\b",target):
                result=self.app_skills.answer_discord_call() if self.app_skills is not None else {"ok":False,"error":"Discord skill недоступен"}
                if result.get("ok") and result.get("verified"):
                    return True,"Ответила на звонок в Discord и проверила подключение.",{"action":"discord_answer","model":"screen-operator","result":result}
                return True,f"Не удалось ответить на звонок в Discord: {result.get('error') or 'кнопка ответа не найдена'}",{"action":"discord_answer_failed","model":"screen-operator","result":result}
            return False,"",{}

        if intent.action == "find":
            if attachment_paths or image_paths:
                return False,"",{}
            workflow = getattr(self, "universal_workflow", None)
            if workflow is not None and workflow._pure_answer_request(query):
                # “Найди давление/площадь/значение и покажи формулу” asks for an
                # answer. Opening the first search result is neither the requested
                # result nor a verified calculation, so let the chat model solve it.
                return False, "", {}
            if re.search(r"\b(?:папк|файл|каталог|директор)\w*", target):
                name=re.sub(r"\b(?:папк\w*|файл\w*|каталог\w*|директор\w*|на компьютере|в системе)\b"," ",intent.target,flags=re.I)
                name=re.sub(r"\s+"," ",name).strip(" .,!?-")
                result=self.tools.execute("system_open_named",{"name":name or intent.target})
                if result.get("ok"):
                    return True,self._tool_result_answer("system_open_named",{"name":name},result),{"action":"find_local","model":"deterministic"}
                try:
                    opened=self.tools.applications.open_file_search(name or intent.target) if self.tools.applications else {}
                    return True,f"Сразу не нашла «{name or intent.target}». Открыла системный поиск Windows, не веб.",{"action":"windows_file_search","model":"deterministic","result":opened}
                except Exception as exc:
                    return True,f"Локальный поиск не сработал: {humanize(exc)}",{"action":"find_local_failed","model":"deterministic"}
            try:
                opened=self.tools.browser.search_first_site(intent.target,open_visible=True) if self.tools.browser else {}
                return True,f"Нашла подходящий сайт и сразу открыла его: {opened.get('title') or intent.target}.",{"action":"search_open_result","model":"deterministic","result":opened}
            except Exception as exc:
                return True,f"Не удалось найти и открыть подходящий сайт: {humanize(exc)}",{"action":"search_open_failed","model":"deterministic"}

        # Analysis is contextual: explicit attachments win, then an explicit app skill,
        # then live camera, then current desktop.
        if intent.action == "analyze":
            if not (attachment_paths or image_paths) and re.search(r"\b(?:vscode|vs\s*code|visual\s*studio\s*code|вс\s*код)\b",target+" "+query,re.I):
                result=self.app_skills.inspect_vscode(query) if self.app_skills is not None else {"ok":False,"error":"VS Code skill недоступен"}
                if result.get("ok"):
                    return True,str(result.get("answer") or "VS Code проверила."),{"action":"vscode_inspect","model":"screen-operator","result":result}
                return True,f"VS Code не удалось проверить: {result.get('error') or 'неизвестная ошибка'}",{"action":"vscode_inspect_failed","model":"screen-operator","result":result}
            if attachment_paths or image_paths:
                answer = self._analyse_attachments_now(query, attachment_paths, image_paths)
                return True, answer or "Вложения вижу, но анализ не вернул текста.", {"action": "attachment_analysis", "model": "deterministic+vision"}
            if camera_running and re.search(r"\b(?:я|меня|рук|жест|показыва|держу|одет|камер|перед тобой)\w*", target + " " + query.casefold()):
                try:
                    answer = self.camera.describe(query)
                except Exception as exc:
                    answer = f"Камера работает, но анализ кадра не удался: {humanize(exc)}"
                return True, answer, {"action": "camera_analysis", "model": self.settings.vision_model}
            # Current-screen analysis is text/accessibility first. On browsers, VS Code,
            # Explorer and most Windows apps this is both faster and more accurate than
            # spending 7+22 seconds on a VLM. Vision is only the fallback for canvas/image UI.
            workflow=getattr(self,"universal_workflow",None)
            if workflow is not None:
                visible=workflow.extract_visible_text(query)
                if visible and len(visible.strip()) >= 12:
                    return True, visible, {"action":"screen_accessibility_analysis","model":"uia+fast-text"}
            result = self.tools.execute("screenshot", {})
            if result.get("ok"):
                path = str((result.get("result") or {}).get("path") or "")
                if path:
                    return True, self._vision_for_path(path, query), {"action": "screen_analysis", "model": self.settings.vision_model}
            return True, self._tool_result_answer("screenshot", {}, result), {"action": "screen_analysis_failed", "model": "deterministic"}

        if intent.action == "show" and camera_running:
            # A single phrase may place several widgets at once: "время слева, погоду справа".
            lowq=query.casefold().replace("ё","е")
            if re.search(r"\b(?:врем|час)\w*",lowq) and re.search(r"\bпогод\w*",lowq):
                parts=re.split(r"\s+(?:а|и)\s+|,",query,maxsplit=4)
                time_part=next((x for x in parts if re.search(r"(?:врем|час)",x,re.I)),query)
                weather_part=next((x for x in parts if re.search(r"погод",x,re.I)),query)
                now=datetime.now(); w1=self._spatial_widget("clock","Время",now.strftime("%H:%M"),dynamic=True,**self._spatial_position(time_part))
                handled,answer,meta=self._fast_data_turn("какая сейчас погода")
                payload=self.db.get_setting("last_data_payload",{})
                body=str(payload.get("body") or answer or "Погода недоступна") if isinstance(payload,dict) else str(answer)
                w2=self._spatial_widget("weather","Погода",body,**self._spatial_position(weather_part))
                return True,"Вывела время и погоду поверх камеры.",{"action":"spatial_widgets","model":"deterministic","widgets":[w1,w2]}
            if re.search(r"\b(?:это|его|ее|её)\b", target):
                payload=self.db.get_setting("last_data_payload", {})
                if isinstance(payload,dict) and payload.get("body"):
                    widget=self._spatial_widget(str(payload.get("kind") or "text"),str(payload.get("title") or "Данные"),str(payload.get("body") or ""),**{k:v for k,v in payload.items() if k not in {"kind","title","body"}})
                    return True,"Вывела это поверх камеры.",{"action":"spatial_widget","model":"deterministic","widget":widget}
            if re.search(r"(?:курс|доллар|usd)", target):
                try:
                    rate = self.tools.browser.currency_rate("USD") if self.tools.browser else {}
                    value = float(rate.get("rub") or 0.0)
                    body = f"1 USD = {value:.2f} RUB\nБанк России · {rate.get('date') or 'сегодня'}" if value else "Курс сейчас недоступен"
                    widget = self._spatial_widget("rate", "USD / RUB", body, value=value, currency="USD", source=rate.get("source",""), **self._spatial_position(query))
                    return True, "Вывела текущий курс доллара поверх камеры.", {"action":"spatial_widget","model":"deterministic","widget":widget}
                except Exception as exc:
                    return True, f"Курс сейчас получить не удалось: {humanize(exc)}", {"action":"spatial_rate_failed","model":"deterministic"}
            if re.search(r"\b(?:время|часы|clock)\b", target):
                now=datetime.now()
                widget=self._spatial_widget("clock","Время",now.strftime("%H:%M"),dynamic=True, **self._spatial_position(query))
                return True,"Вывела текущее время поверх камеры.",{"action":"spatial_widget","model":"deterministic","widget":widget}
            if re.search(r"\bпогод\w*\b", target):
                handled,answer,meta=self._fast_data_turn("какая сейчас погода")
                payload=self.db.get_setting("last_data_payload",{})
                if handled and isinstance(payload,dict) and payload.get("body"):
                    widget=self._spatial_widget("weather","Погода",str(payload.get("body")), **self._spatial_position(query))
                    return True,"Вывела погоду поверх камеры.",{"action":"spatial_widget","model":"deterministic","widget":widget}
            if re.search(r"\b(?:нагрузк|процессор|cpu|озу|ram|система)\b",target):
                try:
                    import psutil
                    body=f"CPU {psutil.cpu_percent(interval=.05):.0f}% · RAM {psutil.virtual_memory().percent:.0f}%"
                except Exception:
                    body="Системные метрики недоступны"
                widget=self._spatial_widget("system","Система",body,dynamic=True, **self._spatial_position(query))
                return True,"Вывела нагрузку системы поверх камеры.",{"action":"spatial_widget","model":"deterministic","widget":widget}
            m_note=re.search(r"\b(?:заметк|текст|напоминан)\w*\s+(.+)$",intent.target,re.I)
            if m_note:
                body=m_note.group(1).strip()
                widget=self._spatial_widget("text","Заметка",body, **self._spatial_position(query))
                return True,"Вывела заметку поверх камеры.",{"action":"spatial_widget","model":"deterministic","widget":widget}
            site_url=""; site_title=""
            if re.search(r"\b(?:ютуб|youtube)\b", target): site_url,site_title="https://www.youtube.com/","YouTube"
            elif re.search(r"\b(?:байбит|bybit)\b", target): site_url,site_title="https://www.bybit.com/","Bybit"
            elif re.search(r"\b(?:телеграм|telegram|тг)\b", target): site_url,site_title="https://web.telegram.org/a/","Telegram Web"
            elif re.search(r"\b(?:яндекс\s*музык|yandex\s*music|моя\s*волна)\b", target): site_url,site_title="https://music.yandex.ru/","Яндекс Музыка"
            elif re.search(r"\b(?:спотифай|spotify)\b", target): site_url,site_title="https://open.spotify.com/","Spotify"
            elif re.search(r"\b(?:дискорд|discord)\b", target): site_url,site_title="https://discord.com/app","Discord"
            else:
                match=re.search(r"https?://[^\s]+|(?:[a-z0-9-]+\.)+(?:com|ru|org|net|io)(?:/[^\s]*)?", target, re.I)
                if match:
                    site_url=match.group(0); site_url=site_url if site_url.startswith("http") else "https://"+site_url; site_title=site_url
            if site_url:
                try:
                    result=self.tools.execute("browser_open",{"url":site_url})
                    return True,f"Вывела {site_title} пространственным окном поверх камеры.",{"action":"spatial_browser","model":"deterministic","result":result}
                except Exception as exc:
                    return True,f"Не удалось вывести сайт пространственным окном: {humanize(exc)}",{"action":"spatial_browser_failed","model":"deterministic"}
            return True,"Поверх камеры могу вывести часы, погоду, курс, системные метрики, заметку или сайт вроде YouTube, Telegram, Яндекс Музыки, Spotify, Discord и Bybit.",{"action":"spatial_show_help","model":"deterministic"}

        if intent.action == "open":
            if not target:
                return False, "", {}
            if camera_running:
                spatial_sites=[
                    (r"\b(?:ютуб|youtube)\b","https://www.youtube.com/","YouTube"),
                    (r"\b(?:телеграм|telegram|тг)\b","https://web.telegram.org/a/","Telegram Web"),
                    (r"\b(?:яндекс\s*музык|yandex\s*music|моя\s*волна)\b","https://music.yandex.ru/","Яндекс Музыка"),
                    (r"\b(?:спотифай|spotify)\b","https://open.spotify.com/","Spotify"),
                    (r"\b(?:дискорд|discord)\b","https://discord.com/app","Discord"),
                    (r"\b(?:байбит|bybit)\b","https://www.bybit.com/","Bybit"),
                ]
                for pattern,url,title in spatial_sites:
                    if re.search(pattern,target,re.I):
                        try:
                            result=self.tools.execute("browser_open",{"url":url})
                            return True,f"Вывела {title} поверх камеры.",{"action":"spatial_browser","model":"deterministic","result":result}
                        except Exception as exc:
                            return True,f"Не удалось вывести {title} поверх камеры: {humanize(exc)}",{"action":"spatial_browser_failed","model":"deterministic"}
            if re.search(r"\b(?:поиск приложений|поиск программ|меню пуск|пуск)\b", target):
                try:
                    result=self.tools.applications.open_windows_search("") if self.tools.applications else {}
                    return True,"Открыла поиск приложений Windows.",{"action":"windows_app_search","model":"deterministic","result":result}
                except Exception as exc:
                    return True,f"Не удалось открыть поиск Windows: {humanize(exc)}",{"action":"windows_app_search_failed","model":"deterministic"}
            if re.search(r"\b(?:папк|каталог|директор)\w*", target):
                name=re.sub(r"\b(?:папк\w*|каталог\w*|директор\w*|на компьютере|в системе)\b"," ",intent.target,flags=re.I)
                name=re.sub(r"\s+"," ",name).strip(" .,!?-")
                result=self.tools.execute("system_open_named",{"name":name or intent.target})
                if result.get("ok"):
                    return True,self._tool_result_answer("system_open_named",{"name":name},result),{"action":"open_local_folder","model":"deterministic"}
                try:
                    opened=self.tools.applications.open_file_search(name or intent.target) if self.tools.applications else {}
                    return True,f"Сразу не нашла папку «{name or intent.target}». Открыла системный поиск Windows по всему индексу.",{"action":"windows_file_search","model":"deterministic","result":opened}
                except Exception as exc:
                    return True,f"Папку не нашла: {humanize(exc)}",{"action":"open_local_folder_failed","model":"deterministic"}
            if re.search(r"\b(?:ютуб|youtube)\b", target):
                if camera_running:
                    result = self.tools.execute("browser_open", {"url": "https://www.youtube.com/"})
                    return True, "YouTube вывела поверх камеры.", {"action": "spatial_browser", "model": "deterministic", "tools": [{"name": "browser_open", "result": result}]}
                result = self.tools.execute("open_default_url", {"url": "https://www.youtube.com/"})
                return True, self._tool_result_answer("open_default_url", {}, result), {"action": "open_url", "model": "deterministic"}
            if re.search(r"\b(?:браузер|browser)\b", target):
                result = self.tools.execute("open_default_url", {"url": "https://www.google.com/"})
                return True, self._tool_result_answer("open_default_url", {}, result), {"action": "open_browser", "model": "deterministic"}
            if self.app_skills is not None:
                skilled=self.app_skills.open(intent.target)
                if skilled.get("ok"):
                    skill_name=str(skilled.get("skill") or "")
                    shown={"telegram":"Telegram","yandex_music":"Яндекс Музыку","youtube":"YouTube","spotify":"Spotify","discord":"Discord","vscode":"VS Code","explorer":"Проводник","windows_settings":"Параметры Windows","browser":"браузер"}.get(skill_name,intent.target)
                    verified=bool(skilled.get("verified"))
                    suffix=" и проверила окно" if verified and (self.identity is None or self.identity.get().gender=="female") else (" и проверил окно" if verified else "")
                    return True,self._self_gendered(f"Открыла {shown}{suffix}.",f"Открыл {shown}{suffix}."),{"action":"app_skill_open","model":"deterministic","result":skilled}
            if getattr(self,"recovery",None) is not None:
                recovered=self.recovery.open_application(intent.target)
                if recovered.get("ok") and recovered.get("verified"):
                    return True,self._self_gendered(f"Открыла {intent.target} и проверила окно.",f"Открыл {intent.target} и проверил окно."),{"action":"recovery_open","model":"deterministic","result":recovered}
                method=str(recovered.get("method") or "")
                if recovered.get("ok") and not recovered.get("verified"):
                    return True,f"Открыла запасной вариант для «{intent.target}», но не буду выдавать его за проверенный результат.",{"action":"recovery_open_unverified","model":"deterministic","result":recovered}
                if method in {"windows_search","web_fallback"}:
                    return True,f"Приложение «{intent.target}» сразу не нашла. Перешла к {('системному поиску Windows' if method=='windows_search' else 'веб-версии в браузере по умолчанию')}.",{"action":"recovery_open_fallback","model":"deterministic","result":recovered}
            result = self.tools.execute("launch_application", {"application": intent.target})
            if result.get("ok"):
                return True, self._tool_result_answer("launch_application", {"application": intent.target}, result), {"action": "launch_application", "model": "deterministic", "tools": [{"name": "launch_application", "result": result}]}
            try:
                fallback = self.tools.applications.web_fallback(intent.target) if self.tools.applications else None
                return True, f"Приложение «{intent.target}» не нашла. Перешла к веб-версии в браузере по умолчанию.", {"action": "app_web_fallback", "model": "deterministic", "fallback": fallback}
            except Exception as exc:
                return True, f"Приложение «{intent.target}» не найдено, и веб-версию открыть не удалось: {humanize(exc)}", {"action": "app_missing", "model": "deterministic"}

        if intent.action == "close":
            if re.search(r"\b(?:все|всё)\b", target) and re.search(r"\b(?:приложени|процесс|окн|программ)\w*", target) and not re.search(r"\b(?:одно|это|текущее)\b", target):
                result = self.tools.execute("close_user_apps", {})
                if result.get("ok"):
                    count = int((result.get("result") or {}).get("count") or 0)
                    return True, f"Закрыла {count} пользовательских приложений. Системные процессы и EIRVEN оставила работать.", {"action": "close_user_apps", "model": "deterministic", "tools": [{"name": "close_user_apps", "result": result}]}
                return True, self._tool_result_answer("close_user_apps", {}, result), {"action": "close_user_apps_failed", "model": "deterministic"}
            if re.search(r"\b(?:сайт|вкладк|страниц)\w*", target):
                result = self.tools.execute("hotkey", {"keys": ["ctrl", "w"]})
                return True, "Закрыла текущую вкладку.", {"action": "close_tab", "model": "deterministic", "tools": [{"name": "hotkey", "result": result}]}
            if re.search(r"\b(?:браузер|browser)\b", target):
                result = self.tools.execute("close_browsers", {})
                return True, ("Закрыла браузер." if result.get("ok") else f"Не удалось закрыть браузер: {result.get('error')}"), {"action": "close_browsers", "model": "deterministic", "tools": [{"name": "close_browsers", "result": result}]}
            app = re.sub(r"\b(?:приложение|процесс|браузер)\b", "", intent.target, flags=re.I).strip() or intent.target
            result = self.tools.execute("close_application", {"application": app})
            return True, (f"Закрыла {app}." if result.get("ok") else f"Не нашла запущенное приложение «{app}» для закрытия."), {"action": "close_application", "model": "deterministic", "tools": [{"name": "close_application", "result": result}]}

        if intent.action in {"enable", "disable"}:
            enabled = intent.action == "enable"
            if re.search(r"\b(?:wi[ -]?fi|wifi|вай ?фай)\b", target):
                result = self.tools.execute("toggle_quick_setting", {"name": "wifi", "enabled": enabled})
                answer = ("Wi-Fi включила." if enabled else "Wi-Fi выключила.") if result.get("ok") else f"Не удалось переключить Wi-Fi: {result.get('error')}"
                return True, answer, {"action": "wifi", "model": "deterministic", "tools": [{"name": "toggle_quick_setting", "result": result}]}
            if re.search(r"\b(?:режим.*(?:полета|самолет)|самолетн)\w*", target):
                result = self.tools.execute("toggle_quick_setting", {"name": "airplane", "enabled": enabled})
                return True, ("Режим полёта включила." if enabled else "Режим полёта выключила.") if result.get("ok") else f"Не удалось переключить режим полёта: {result.get('error')}", {"action": "airplane", "model": "deterministic"}
            dark_request = bool(re.search(r"\b(?:темн|темная|тёмн|dark)\w*.*\b(?:тем|режим)|\b(?:тем|режим).*\b(?:темн|тёмн|dark)", target))
            light_request = bool(re.search(r"\b(?:светл|light)\w*.*\b(?:тем|режим)|\b(?:тем|режим).*\b(?:светл|light)", target))
            if dark_request or light_request:
                dark_enabled = enabled if dark_request else (not enabled)
                result = self.tools.execute("set_dark_theme", {"enabled": dark_enabled})
                answer = "Тёмную тему включила." if dark_enabled else "Светлую тему включила."
                return True, answer, {"action": "theme", "model": "deterministic", "tools": [{"name": "set_dark_theme", "result": result}]}
            if re.search(r"\bvpn\b|\bвпн\b", target):
                return self._toggle_vpn(enabled)
            if re.search(r"\b(?:камер|camera)\w*", target) and self.modes is not None:
                phrase = "включи камеру" if enabled else "выключи камеру"
                handled, answer, meta = self.modes.handle(phrase)
                return handled, answer, {**meta, "model": "deterministic"}
            if re.search(r"\bмузык\w*", target):
                if enabled:
                    if re.search(r"\b(?:на (?:этом|текущем) экране|на этой странице|не открывай|без открытия|здесь)\b", query.casefold()) and getattr(self,"desktop_operator",None) is not None:
                        if self.desktop_operator.click_current(["Воспроизведение","Play","Воспроизвести","play_filled"],goal="yandex_play_current"):
                            return True,"Нажала воспроизведение прямо на текущем экране.",{"action":"music_current_screen","model":"deterministic"}
                    if self.app_skills is not None:
                        result=self.app_skills.play_music(query)
                        if result.get("ok") and result.get("verified"):
                            return True,"Воспроизведение в Яндекс Музыке подтверждено.",{"action":"music_verified","model":"screen-operator","result":result}
                        return True,f"Яндекс Музыку открыла, но воспроизведение не подтвердилось: {result.get('error') or 'кнопку не удалось надёжно нажать'}. Оставила страницу перед тобой.",{"action":"music_recovery","model":"screen-operator","result":result}
                result = self.tools.execute("media_control", {"action": "stop"})
                return True, ("Команда остановки отправлена, но состояние плеера не подтвердилось." if result.get("ok") else f"Не удалось отправить остановку плееру: {result.get('error') or 'нет активной media session'}."), {"action": "music_stop_unverified", "model": "deterministic", "verified": False, "result": result}
            # Generic enable/disable of an app behaves like open/close.
            if target:
                if enabled:
                    result = self.tools.execute("launch_application", {"application": intent.target})
                    if result.get("ok"):
                        return True, self._tool_result_answer("launch_application", {"application": intent.target}, result), {"action": "launch_application", "model": "deterministic"}
                else:
                    result = self.tools.execute("close_application", {"application": intent.target})
                    if result.get("ok"):
                        return True, f"Выключила {intent.target}.", {"action": "close_application", "model": "deterministic"}
                # Last bounded recovery: operate the current visible UI like a person.
                if self.desktop_operator is not None:
                    goal=("Включить " if enabled else "Выключить ")+intent.target
                    operated=self.desktop_operator.perform_goal(goal,max_steps=3)
                    if operated.get("verified"):
                        return True,("Включила " if enabled else "Выключила ")+intent.target+" и проверила результат.",{"action":"visible_operator","model":self.settings.vision_model,"result":operated}

        return False, "", {}

    def _stateful_task_shortcut(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Fast continuation for already-known work.

        This is not a catalogue of application commands. It only resolves references to
        task state EIRVEN itself created, so "продолжай" and project edits remain reliable
        even when Ollama is temporarily unavailable.
        """
        if self.tasks is None:
            return False, "", {}
        text = query.strip()
        mission = self.tasks.latest(kind="mission", conversation_id=conversation_id)
        if mission:
            mstatus = str(mission.get("status") or "")
            # A correction/addition during a running mission becomes a graph revision,
            # not a competing foreground command. Explicit unrelated actions still route
            # normally below.
            live_update = bool(re.search(
                r"^\s*(?:и\s+ещ[её]|ещ[её]|добавь\s+к\s+задаче|потом|после\s+этого|заодно|параллельно|учти|только\s+не|не\s+забудь)\b",
                text, re.I,
            ))
            if mstatus in {"queued", "running"} and live_update and self.tasks.append_live_instruction(str(mission.get("id") or ""), text):
                return True, "Добавила это в текущую миссию; уже выполненные шаги не начинаю заново.", {"action":"mission_live_update","task_id":mission.get("id"),"kind":"mission"}
            if mstatus == "waiting_user" and re.fullmatch(r"(?:готово|готов|сделал|сделано|продолжай|дальше)[.! ]*", text, re.I):
                if self.tasks.resume(str(mission.get("id") or "")):
                    return True, "Продолжаю миссию с контрольной точки.", {"action":"mission_resume","task_id":mission.get("id"),"kind":"mission"}
            if mstatus in {"failed", "cancelled"} and re.fullmatch(r"(?:продолжай|повтори|доделай|возобнови)[.! ]*", text, re.I):
                if self.tasks.retry(str(mission.get("id") or "")):
                    return True, "Возобновила ту же миссию с сохранённого графа.", {"action":"mission_retry","task_id":mission.get("id"),"kind":"mission"}
        if re.fullmatch(r"(?:готово|готов|вош[её]л|авторизовал(?:ся|ась)|сделал|сделано|можно продолжать)[.! ]*", text, re.I):
            waiting = self.tasks.latest_waiting(conversation_id)
            if waiting and self.tasks.resume(waiting["id"]):
                return True, "Продолжаю ту же задачу.", {"action": "task_created", "task_id": waiting["id"], "kind": waiting.get("kind")}
        project = self._latest_project_task(conversation_id)
        if not project:
            return False, "", {}
        is_followup = bool(re.search(
            r"^\s*(?:продолжай|доделай|исправь|поправь|почини|добавь|дополни|измени|переделай|убери|внеси|поменяй|сделай)\b|"
            r"\b(?:в\s+проекте|проект)\b.{0,100}\b(?:исправ|добав|измени|передел|убери|внеси|сделай)\w*",
            text, re.I | re.S,
        ))
        if not is_followup:
            return False, "", {}
        if project.get("status") in {"queued", "running"} and self.tasks.append_live_instruction(project["id"], text):
            return True, self._self_gendered("Приняла. Вношу эту правку прямо в текущую сборку, не останавливая её.", "Принял. Вношу эту правку прямо в текущую сборку, не останавливая её."), {"action": "task_live_update", "task_id": project["id"], "kind": project.get("kind")}
        if project.get("status") in {"failed", "cancelled"} and re.search(r"\b(?:продолж|исправ|почин|ошиб)\w*", text, re.I):
            if self.tasks.retry(project["id"]):
                return True, "Продолжаю этот же проект с последней контрольной точки и исправляю ошибку.", {"action": "task_created", "task_id": project["id"], "kind": project.get("kind")}
        # A completed historical project must not hijack a generic desktop command such
        # as "исправь ошибку" in the currently open IDE. Only an explicit reference to
        # the project turns that sentence into a project-change task.
        explicit_project = bool(re.search(r"\b(?:в\s+проекте|проект(?:е|а|у|ом)?|сборк(?:е|у|и))\b", text, re.I))
        if project.get("status") not in {"queued", "running", "failed", "cancelled"} and not explicit_project:
            return False, "", {}
        project_name = str((project.get("result") or {}).get("project_name") or (project.get("input") or {}).get("name") or "").strip()
        project_path = str((project.get("result") or {}).get("project_path") or (project.get("input") or {}).get("project_path") or "").strip()
        task_id = self.tasks.enqueue(
            "project_change",
            f"Изменить проект {project_name or 'текущий'}",
            {"name": project_name, "request": text, "project_path": project_path},
            conversation_id=conversation_id,
        )
        return True, self._self_gendered("Приняла правку и запустила реальное изменение текущего проекта.", "Принял правку и запустил реальное изменение текущего проекта."), {"action": "task_created", "task_id": task_id, "kind": "project_change"}

    def _tool_first_turn(
        self,
        query: str,
        conversation_id: str,
        mode: str,
        stop_event: threading.Event,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Run one unified fast turn: answer directly or call real tools.

        There is no separate intent persona. The fast model is EIRVEN itself: for a
        simple conversational turn it answers immediately; for an action it emits native
        Ollama tool calls; only genuinely difficult reasoning returns the internal DEEP
        marker and escalates to the larger conversational model.
        """
        if self.tools is None:
            return False, "", {}
        schemas = self._tool_schemas_for_query(query)
        if not schemas:
            return False, "", {}
        installed = {m.lower(): m for m in self.gateway.installed_models()}
        fast_model = installed.get(self.settings.fast_model.lower()) or installed.get(self.settings.model.lower()) or self.router.agent_model(query)

        base = self._messages(conversation_id, mode, query, image_paths, attachment_paths)
        system_extra = (
            "\n\nТы сейчас единый быстрый контур EIRVEN. Инструменты ниже — твои реальные руки, глаза, "
            "файлы, терминал, браузер и проекты. Если владелец просит действие — сразу вызови инструмент, "
            "не обещай сделать потом и не рассказывай план. Не говори, что ты текстовая модель или что у тебя "
            "нет доступа, если соответствующий инструмент существует. Обычный короткий разговор — ответь "
            "сразу естественно по-русски. Если нужен действительно глубокий анализ без действий, ответь ровно "
            "DEEP — тогда запрос перейдёт более сильной модели. Никогда не выводи DEEP пользователю. "
            "Если пользователь просит СОЗДАТЬ программу/скрипт/утилиту/сайт/бот/проект, обязательно вызови project_create: "
            "не создавай только папку и не обещай продолжить без поставленной задачи. Если просит изменить уже созданный проект — project_modify. "
            "Любой факт, который может быть текущим или изменившимся (погода, новости, цены, версии, расписания), "
            "не выдумывай из памяти: сначала вызови web_search. Это твой обычный инструмент, не отдельный режим."
        )
        if self._is_screen_request(query):
            system_extra += (
                " ВАЖНО: это команда по УЖЕ ОТКРЫТОМУ экрану. Не открывай новый браузер, поиск или сайт. "
                "Работай только с текущим foreground-окном через window_elements/screenshot и desktop-инструменты."
            )
        messages = [dict(item) for item in base]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = str(messages[0].get("content") or "") + system_extra
        else:
            messages.insert(0, {"role": "system", "content": system_extra.strip()})

        used: list[dict[str, Any]] = []
        with self.tools.task_scope(stop_event):
            normalized = query.casefold().strip()
            if self.modes is not None:
                try:
                    handled, answer, meta = self.modes.handle(query)
                    if handled:
                        return True, answer, {**meta, "model": "deterministic"}
                except Exception:
                    pass
            # Zero-LLM hot path for a few latency-sensitive desktop intents. General
            # commands still use the tool-capable local model below.
            screen_question = bool(
                self._is_screen_request(query)
                and re.search(r"\b(?:что|где|какой|какая|какие|видишь|видиш|прочитай|опиши|посмотри)\b", normalized)
                and not re.match(r"^\s*(?:нажми|кликни|введи|напиши|выбери|прокрути|перетащи)", normalized)
            )
            if screen_question or re.search(r"\b(?:видишь|видиш|что\s+на|посмотри|покажи)\b.{0,35}\b(?:экран|рабочий стол)\b", normalized):
                result = self.tools.execute("screenshot", {})
                if result.get("ok"):
                    path = str((result.get("result") or {}).get("path") or "")
                    if path:
                        analysis = self._vision_for_path(path, query)
                        return True, analysis or "Скриншот получен, но визуальный анализ не вернул текст.", {"action": "tool", "tools": [{"name": "screenshot", "arguments": {}, "result": result}], "model": self.settings.vision_model}
                return True, self._tool_result_answer("screenshot", {}, result), {"action": "tool", "tools": [{"name": "screenshot", "arguments": {}, "result": result}], "model": "deterministic"}
            if re.search(r"\b(?:открой|зайди|запусти|включи|покажи)\w*\s+(?:на\s+)?(?:ютуб|youtube)\b", normalized):
                camera_running = bool(self.camera is not None and self.camera.status().get("running"))
                if camera_running:
                    result = self.tools.execute("browser_open", {"url": "https://www.youtube.com/"})
                    return True, "YouTube открыт в пространственном окне.", {"action": "tool", "tools": [{"name": "browser_open", "arguments": {"url": "https://www.youtube.com/"}, "result": result}], "model": "deterministic", "spatial": "browser"}
                result = self.tools.execute("open_default_url", {"url": "https://www.youtube.com/"})
                return True, self._tool_result_answer("open_default_url", {"url": "https://www.youtube.com/"}, result), {"action": "tool", "tools": [{"name": "open_default_url", "arguments": {"url": "https://www.youtube.com/"}, "result": result}], "model": "deterministic"}
            app_match = re.match(
                r"^\s*(?:(?:открой|запусти|включи|вруби)\w*|зайди\s+в)\s+(?:приложение\s+)?(.+?)(?:\s+(?:плис|пожалуйста))?[.!?]*\s*$",
                query, re.I | re.S,
            )
            if app_match:
                application = app_match.group(1).strip(" .,!?")
                # Try the Start-menu/application index for any plausible app name. This
                # is deterministic and far faster than asking an LLM to decide how to
                # launch Telegram, VS Code, Discord, etc. URLs/sites/system toggles stay
                # on their dedicated paths.
                if application and not re.search(
                    r"^(?:https?://|www\.)|\b(?:сайт|страниц|ютуб|youtube|wi[ -]?fi|вай ?фай|камера|режим|файл|папк)\b",
                    application, re.I,
                ):
                    aliases = {
                        "телеграм": "Telegram", "телеграмм": "Telegram", "телега": "Telegram", "телегра": "Telegram", "тг": "Telegram",
                        "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
                        "дискорд": "Discord", "спотифай": "Spotify", "стим": "Steam",
                    }
                    canonical = aliases.get(application.casefold(), application)
                    result = self.tools.execute("launch_application", {"application": canonical})
                    if result.get("ok"):
                        return True, self._tool_result_answer("launch_application", {"application": canonical}, result), {"action": "tool", "tools": [{"name": "launch_application", "arguments": {"application": canonical}, "result": result}], "model": "deterministic"}
            # Current facts should not wait for a tool-planning LLM. Do one bounded
            # no-key web search and return the first useful snippets directly.
            if self._is_live_web_request(query) and re.search(r"\b(?:погода|курс\s+(?:доллар|евро|рубл)|новост)\w*", normalized):
                result = self.tools.execute("web_search", {"query": query, "max_results": 3})
                if result.get("ok"):
                    rows = list((result.get("result") or {}).get("results") or [])
                    useful = []
                    for row in rows[:2]:
                        snippet = str(row.get("snippet") or "").strip()
                        title = str(row.get("title") or "").strip()
                        text = snippet or title
                        if text:
                            useful.append(text)
                    if useful:
                        return True, " ".join(useful)[:900], {"action": "web_search", "tools": [{"name": "web_search", "arguments": {"query": query}, "result": result}], "model": "deterministic"}

            wifi_on = re.search(r"\b(?:включи|вруби|активируй)\w*\s+(?:wi[ -]?fi|вай ?фай)\b", normalized)
            wifi_off = re.search(r"\b(?:выключи|отключи)\w*\s+(?:wi[ -]?fi|вай ?фай)\b", normalized)
            if wifi_on or wifi_off:
                verb = "Enable-NetAdapter" if wifi_on else "Disable-NetAdapter"
                command = f"Get-NetAdapter | Where-Object {{$_.InterfaceDescription -match 'Wireless|802.11' -or $_.Name -match 'Wi-Fi|WLAN'}} | {verb} -Confirm:$false"
                result = self.tools.execute("powershell", {"command": command, "cwd": str(self.settings.root_dir)})
                if result.get("ok"):
                    return True, "Wi-Fi включён." if wifi_on else "Wi-Fi выключен.", {"action": "tool", "tools": [{"name": "powershell", "arguments": {"command": command}, "result": result}], "model": "deterministic"}
            action_request = bool(re.search(
                r"^\s*(?:открой|запусти|включи|выключи|отключи|найди|нажми|кликни|введи|напиши|"
                r"отправь|ответь|покажи|создай|сделай|удали|перемести|скопируй|скачай|загрузи|"
                r"опубликуй|поставь|закрой|сверни|разверни|измени|переключи|проверь|переустанови)\w*\b",
                query, re.I,
            ))
            for step in range(4):
                if stop_event.is_set():
                    return True, "", {"action": "cancelled"}
                try:
                    turn_model = fast_model
                    response = self.gateway.chat(
                        messages,
                        model=turn_model,
                        temperature=0.15 if step == 0 else 0.0,
                        tools=schemas,
                        think=False,
                        num_ctx=min(self.settings.chat_num_ctx, 6144 if action_request else 4096),
                        num_predict=180 if step == 0 else 220,
                        timeout_seconds=3.5 if step == 0 else (5 if action_request else 4),
                    )
                except Exception:
                    # Fast path is an optimization. If the tiny model is unavailable,
                    # the normal conversational route still works.
                    return False, "", {}

                calls = list(response.get("tool_calls") or [])
                content = str(response.get("content") or "").strip()
                if not calls:
                    if not used and content.upper().strip(" .!\n") == "DEEP":
                        return False, "", {}
                    # A tool-capable model sometimes answers an action request with a promise
                    # ("сейчас сделаю") or a false capability refusal instead of calling a tool.
                    # Do not hard-code app/command phrases here. Enforce the generic invariant:
                    # if the model itself sounds like it is postponing/denying execution, give the
                    # *same* model one corrective turn and require a real tool result.
                    if not used and step == 0 and content and (action_request or re.search(
                        r"(?:сейчас\s+(?:сдел|созд|откр|запущ|выполн)|\bначина(?:ю|ем)\b|"
                        r"\bпоставлю\b.{0,40}\b(?:задач|очеред)|"
                        r"\b(?:не могу|не умею|нет доступа|текстов(?:ая|ой) модель)\b)",
                        content, re.I | re.S,
                    )):
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "system",
                            "content": (
                                "Ты попытался завершить запрос словами вместо фактического выполнения. "
                                "Если просьба выполнима любым доступным инструментом, прямо сейчас вызови "
                                "подходящий tool. Не обещай, не отказывайся из-за ограничений текстовой модели. "
                                "Если это действительно только разговор и действие не требуется — дай конечный ответ."
                            ),
                        })
                        continue
                    if content:
                        if action_request and not used:
                            # No execution evidence means the command is not complete. Do not
                            # lie with a conversational success response. Let the stronger lane
                            # retry rather than presenting prose as an action result.
                            return False, "", {}
                        return True, content, {"action": "chat" if not used else "tool", "tools": used, "model": fast_model}
                    return bool(used), self._tool_result_answer(used[-1]["name"], used[-1]["arguments"], used[-1]["result"]) if used else "", {"action": "tool", "tools": used, "model": fast_model}

                assistant: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": calls}
                messages.append(assistant)
                for call in calls:
                    name, args = self._parse_tool_call(call)
                    meta = self._execute_meta_tool(name, args, conversation_id)
                    result = meta if meta is not None else self.tools.execute(name, args)
                    if name == "screenshot" and result.get("ok"):
                        path = str((result.get("result") or {}).get("path") or "")
                        if path:
                            analysis = self._vision_for_path(path, query)
                            result.setdefault("result", {})["vision_analysis"] = analysis
                    used.append({"name": name, "arguments": args, "result": result})
                    messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False, default=str)[:12000]})
                    # Finished one-shot actions should feel instant: return the factual
                    # tool result without paying for a second language-model turn.
                    if result.get("ok") and name in {
                        "launch_application", "system_open_named", "system_open_path", "open_default_url",
                        "default_search", "window_focus", "project_create", "project_modify",
                        "task_resume", "git_publish",
                    }:
                        inner = result.get("result") or {}
                        needs_postcondition = name in {"launch_application", "system_open_named", "system_open_path", "open_default_url", "default_search", "window_focus", "git_publish"}
                        verified = bool(result.get("verified") is True or (isinstance(inner, dict) and inner.get("verified") is True))
                        if not needs_postcondition or verified:
                            return True, self._tool_result_answer(name, args, result), {"action": "tool", "tools": used, "model": fast_model, "verified": verified or not needs_postcondition}
            return True, "Команда была отправлена, но ожидаемое состояние не подтвердилось. Повтор автоматически не выполняю.", {"action": "tool_unverified", "tools": used, "model": fast_model, "completed": bool(used), "verified": False}

    def _maybe_summarize(self, conversation_id: str) -> None:
        if self.memory.message_count(conversation_id) < 34:
            return
        summary_state = self.memory.get_summary(conversation_id) or {
            "summary": "",
            "summarized_through_message_id": 0,
        }
        old_id = int(summary_state.get("summarized_through_message_id") or 0)
        chunk = self.memory.unsummarized_messages(conversation_id, old_id, before_last=16)
        if len(chunk) < 10:
            return
        transcript = "\n".join(
            f"{item['role']}: {item['content']}" for item in chunk
        )
        prompt = (
            "Обнови краткую долговременную сводку диалога. Сохраняй решения, факты, "
            "предпочтения, имена, незавершённые задачи и важный контекст. Удали болтовню.\n\n"
            f"Старая сводка:\n{summary_state.get('summary') or 'нет'}\n\n"
            f"Новые сообщения:\n{transcript}\n\nВерни только новую сводку."
        )
        try:
            route = self.router.chat_route("кратко суммировать историю")
            with self.gateway.background():
                message = self.gateway.chat(
                    [
                        {"role": "system", "content": "Ты сжимаешь историю без потери важных фактов."},
                        {"role": "user", "content": prompt},
                    ],
                    model=route.model,
                    temperature=0.1,
                    think=False,
                    num_ctx=self.settings.chat_num_ctx,
                    num_predict=600,
                )
            summary = (message.get("content") or "").strip()
            if summary:
                self.memory.save_summary(conversation_id, summary, int(chunk[-1]["id"]))
        except Exception:
            # Summary is an optimization; chat must never fail because of it.
            return

    def _instant_reply(self, query: str) -> str | None:
        """Deprecated: personality-bearing chat is always generated by EIRVEN.

        Exact greeting/acknowledgement tables made the assistant ignore the configured
        style and feel cached.  Keep the method as a compatibility no-op for extensions
        that still call it; production and legacy callers now fall through to the same
        conversational model instead of receiving a canned response.
        """
        return None

    @staticmethod
    def _merge_generation_metrics(segments: list[dict[str, Any]]) -> dict[str, Any]:
        """Combine multiple bounded model generations into one truthful turn metric."""
        if not segments:
            return {}
        last = segments[-1]
        summed_float = (
            "total_seconds", "load_seconds", "prompt_eval_seconds", "generation_seconds",
        )
        summed_int = ("prompt_tokens", "generated_tokens", "thinking_chars", "requested_tokens")
        merged: dict[str, Any] = {
            "model": str(segments[0].get("model") or last.get("model") or ""),
            "first_token_seconds": float(segments[0].get("first_token_seconds") or 0),
            "finish_reason": str(last.get("finish_reason") or ""),
            "hit_token_limit": bool(last.get("hit_token_limit")),
            "stopped": any(bool(item.get("stopped")) for item in segments),
            "continued_segments": max(0, len(segments) - 1),
        }
        for key in summed_float:
            merged[key] = round(sum(float(item.get(key) or 0) for item in segments), 3)
        for key in summed_int:
            merged[key] = sum(int(item.get(key) or 0) for item in segments)
        generation_seconds = float(merged.get("generation_seconds") or 0)
        merged["tokens_per_second"] = (
            round(int(merged.get("generated_tokens") or 0) / generation_seconds, 2)
            if generation_seconds > 0 else 0.0
        )
        return merged

    def _stream_answer_with_continuations(
        self,
        messages: list[dict[str, Any]],
        route: Any,
        stop_event: threading.Event,
    ) -> Generator[dict[str, Any], None, tuple[str, dict[str, Any]]]:
        """Stream a complete answer and continue only after a real token-limit stop.

        Ordinary chat keeps its small first-pass budget.  A measurable long artifact gets
        an estimated first budget from ``ModelRouter``; if the backend still reports a
        length stop, up to two bounded continuations resume the same answer.  This avoids
        both silent truncation and a global, latency-heavy token-limit increase.
        """
        answer = ""
        segment_metrics: list[dict[str, Any]] = []
        continuation_error = ""
        max_continuations = 2
        task_cap = max(int(route.num_predict), int(self.settings.task_num_predict))

        for segment_index in range(max_continuations + 1):
            if stop_event.is_set():
                break
            if segment_index == 0:
                segment_messages = messages
                budget = int(route.num_predict)
                context = int(route.num_ctx)
            else:
                budget = min(task_cap, max(768, int(route.num_predict) // 2))
                estimated_answer_tokens = max(1, len(answer) // 3)
                context = min(
                    int(self.settings.task_num_ctx),
                    max(
                        int(route.num_ctx),
                        budget + min(estimated_answer_tokens, int(self.settings.task_num_ctx) // 2) + 2048,
                    ),
                )
                segment_messages = [
                    *messages,
                    {"role": "assistant", "content": answer},
                    {
                        "role": "user",
                        "content": (
                            "Предыдущий ответ оборвался только из-за лимита генерации. "
                            "Продолжи строго с места обрыва, не повторяй уже выданный текст "
                            "и выполни все оставшиеся требования исходного запроса, включая "
                            "последний пункт или контрольную финальную строку. Не добавляй "
                            "вступление и не комментируй продолжение."
                        ),
                    },
                ]

            before = len(answer)
            try:
                for chunk in self.gateway.stream_chat(
                    segment_messages,
                    model=route.model,
                    temperature=route.temperature,
                    think=route.think,
                    num_ctx=context,
                    num_predict=budget,
                    stop_event=stop_event,
                    first_token_timeout_seconds=self.settings.llm_first_token_timeout,
                    inactivity_timeout_seconds=self.settings.llm_inactivity_timeout,
                ):
                    answer += chunk
                    yield {"type": "token", "content": chunk, "full": answer}
            except LLMError as exc:
                if not answer:
                    raise
                continuation_error = type(exc).__name__
                break

            metrics_obj = self.gateway.last_metrics
            metrics = metrics_obj.to_dict() if metrics_obj is not None else {}
            segment_metrics.append(dict(metrics or {}))
            hit_limit = bool(metrics.get("hit_token_limit"))
            if stop_event.is_set() or not hit_limit:
                break
            if len(answer) == before:
                continuation_error = "empty_length_segment"
                break

        merged = self._merge_generation_metrics(segment_metrics)
        if continuation_error:
            merged["continuation_error"] = continuation_error
        merged["truncated"] = bool(
            not stop_event.is_set()
            and (continuation_error or merged.get("hit_token_limit"))
        )
        return answer, merged

    # Отчёт о действии или обещание действия от первого лица: «запомню», «поставила»,
    # «напомню», «уже открыла», «записала». В ответе без действий это всегда неправда.
    # Отчёт о действии или обещание действия от первого лица — в ответе без действий
    # это всегда неправда. Формы перечислены явно: у русских глаголов первое лицо
    # меняет основу (отправ-лю, постав-лю), и сборка «основа + окончание» их теряла.
    _DEGRADED_FALSE_CLAIM = re.compile(
        r"\b(?:запустила|напомнила|отправила|запомнила|поставила|сохранила|выключила|заказала|записала|добавила|отправлю|поставлю|включила|выключу|запомню|напомню|создала|сделала|открыла|добавлю|сохраню|удалила|сделаю|запишу|открою|запущу|включу|закажу|создам|удалю)\b"
    )

    def _degraded_reply(self, query: str, conversation_id: str) -> str:
        """Ответ, когда управляющее решение не пришло: только разговор, без действий.

        Модели прямо сказано, что сейчас она ничего не может сделать на компьютере,
        и что писать «сделала» запрещено. Даже если бы она ослушалась, выполнять
        было бы нечем — этот ход не проходит через исполнителей. Пусто — если и
        здесь модель не ответила; тогда покажется прежнее честное сообщение.
        """
        system = (
            "Ты — Эрви, помощник на компьютере владельца. В этом ответе ты НЕ МОЖЕШЬ "
            "ничего делать: не открываешь, не отправляешь, не читаешь почту и переписку, "
            "не ставишь напоминания и будильники, не создаёшь заметки и события, не "
            "запоминаешь задачи, ничего не нажимаешь.\n"
            "Если владелец просит что-то сделать — честно скажи одной фразой, что прямо "
            "сейчас не получилось, и попроси повторить через несколько секунд.\n"
            "Никогда не пиши, что что-то сделала, И никогда не обещай, что сделаешь: "
            "никаких «запомню», «поставлю», «напомню», «сделаю», «хорошо, записала». "
            "Такое обещание — ложь: оно не будет выполнено.\n"
            "Не упоминай своё внутреннее устройство — никаких «управляющих частей», "
            "модулей, моделей и таймаутов.\n"
            "На вопросы, объяснения и обычный разговор отвечай как всегда — коротко, "
            "по-человечески, по-русски, от женского лица."
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        try:
            for row in self.memory.history(conversation_id, limit=6):
                role = str(row.get("role") or "")
                if role in {"user", "assistant"}:
                    messages.append({"role": role, "content": str(row.get("content") or "")[:1200]})
        except Exception:
            pass
        messages.append({"role": "user", "content": query})
        try:
            engine = getattr(self, "universal_workflow", None)
            timeout = engine._admission_timeout() if engine is not None else 60.0
            reply = self.gateway.chat(
                messages, model=self.settings.fast_model, temperature=0.5, think=False,
                num_ctx=min(int(getattr(self.settings, "chat_num_ctx", 3072) or 3072), 3072),
                num_predict=320, timeout_seconds=timeout,
            )
            text = str((reply or {}).get("content") or "").strip()
            # Проверка на правдивость: в этом ответе Эрви ничего не делает, значит, ни
            # отчёта о действии, ни обещания в нём быть не может. Маленькая модель иногда
            # игнорирует запрет — так в журнале появилось «Хорошо, запомню: на завтра…»,
            # а напоминание поставлено не было. Такой ответ заменяем честным отказом
            # (пустая строка — вызывающий код покажет его сам).
            if text and self._DEGRADED_FALSE_CLAIM.search(text.casefold().replace("ё", "е")):
                self._trace("CHAT_DEGRADED_FALSE_CLAIM", text=text[:160])
                return ""
            if text:
                self._trace("CHAT_DEGRADED_REPLY", chars=len(text))
            return text
        except Exception as exc:
            self._trace("CHAT_DEGRADED_FAILED", error=str(exc)[:200])
            return ""

    def _stream_events_impl(
        self,
        query: str,
        conversation_id: str | None = None,
        mode: str = "Друг",
        model: str | None = None,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
        *,
        persist_user: bool = True,
        external_stop_event: threading.Event | None = None,
        persist_assistant: bool = True,
        _runtime_generation: int | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        query = query.strip()
        request_started = time.monotonic()
        request_wall_started = time.time()
        runtime_generation = _runtime_generation
        self._trace("CHAT_IN", query=query, conversation_id=conversation_id or "", mode=mode, images=len(image_paths or []), attachments=len(attachment_paths or []))
        if not query:
            yield {"type": "error", "message": "Введите сообщение"}
            return
        conversation_id = self.memory.ensure_conversation(conversation_id, mode)

        # Мост стоит здесь — на самом входе, до всех обработчиков. Прежде он был
        # ниже, и быстрый путь медиа (_priority_control_turn) перехватывал
        # «включи музыку» раньше, чем мост успевал сработать. Раз мост должен
        # видеть каждый запрос, он обязан стоять перед каждым обработчиком.
        # Идёт ли многошаговая задача — считаем здесь сами: общий флаг
        # workflow_pending объявляется ниже, и ссылка на него упала бы.
        _wf_engine = getattr(self, "universal_workflow", None)
        _bridge_busy = bool(_wf_engine and _wf_engine.has_pending(conversation_id))

        # Ответ на встречный вопрос моста. Человек отвечал, чтобы занять паузу,
        # а не чтобы дать новую задачу: реплика уходит в память и в контекст,
        # но отдельного ответа на неё не будет. Иначе он получит два ответа
        # подряд и не поймёт, какой из них к его просьбе.
        # Мост — украшение, а не часть работы. Любой его сбой обязан пропускать
        # мост, а не ронять ход: блоки ниже стоят на входе КАЖДОГО сообщения, и
        # без изоляции сбой согласования рода или «database is locked» оставлял
        # Эрви без ответа вообще — проверено прогоном. Всё рискованное стоит до
        # выдачи ответа, поэтому перехваченный сбой не приводит ко второму ответу.
        # Ответ на встречный вопрос моста. Раньше здесь был ОТДЕЛЬНЫЙ вызов модели
        # «ответ это или новая задача» с потолком 0,6 с — та же болезнь, что убила
        # мост: на настоящей модели он не успевал, а брошенный продолжал занимать
        # модель и задерживал следующее решение. Отдельный вызов не нужен:
        # управляющее решение и так читает каждую реплику и само отличит «открой
        # ютуб» (задача) от «устал» (разговор). Нужно лишь дать ему контекст —
        # кладём заданный вопрос в историю, и реплика идёт обычным путём.
        if self.bridge is not None and self.bridge.is_waiting(conversation_id):
            try:
                asked = self.bridge.take_waiting(conversation_id)
                if asked:
                    self.memory.add_message(conversation_id, "assistant", asked)
                    self._trace("BRIDGE_REPLY_CONTEXT", asked=asked[:80])
            except Exception as exc:
                self._trace("BRIDGE_REPLY_FAILED", error=str(exc)[:160])

        # Мост на входе хода: видит каждый запрос, нужен ли он — решает модель.
        if (self.bridge is not None and not image_paths and not _bridge_busy
                and not self.bridge.is_waiting(conversation_id)):
            try:
                # Реплика моста теперь приходит из управляющего решения (ниже).
                # Отдельный вызов модели здесь добавлял задержку к КАЖДОМУ сообщению
                # и сам не успевал в свой срок — поэтому отключён.
                opener = {"ack": "", "question": ""}
                if opener.get("ack"):
                    line = opener["ack"]
                    if opener.get("question"):
                        line = f"{line} {opener['question']}"
                    try:
                        line = self.enforce_gender(line)
                    except Exception:
                        pass
                    # В историю не пишем: это заполнение паузы, а не ход разговора.
                    if opener.get("question"):
                        self.bridge.mark_waiting(conversation_id, opener["question"])
                    yield {"type": "bridge", "text": line}
            except Exception as exc:
                # Мост не удался — сообщение обрабатывается как обычно, без него.
                self._trace("BRIDGE_ENTRY_FAILED", error=str(exc)[:160])

        # Telegram commands from the conversational surface are intentionally
        # redirected to the dedicated API panel.  This guard runs before semantic,
        # deterministic and desktop-agent lanes, so no legacy path can open Telegram
        # Web or type/send on the user's behalf.
        if self._telegram_panel_request(query):
            answer, route = self._telegram_panel_answer()
            if persist_user:
                self.memory.add_message(
                    conversation_id, "user", query,
                    metadata={"images": image_paths or [], "attachments": attachment_paths or []},
                )
            if persist_assistant:
                self.memory.add_message(
                    conversation_id, "assistant", answer,
                    metadata={"model": route["model"], "route": route},
                )
            yield {"type": "start", "conversation_id": conversation_id, "route": route}
            yield {"type": "token", "content": answer, "full": answer}
            yield {
                "type": "done", "conversation_id": conversation_id, "answer": answer,
                "metrics": {"model": route["model"], "total_seconds": round(time.monotonic() - request_started, 3)},
                "stopped": False, "route": route,
            }
            return

        # Opening the local video inbox is a side-effect-free filesystem action.  Keep
        # it ahead of model admission so a transient LLM timeout cannot turn this
        # obvious command into a misleading "semantic unavailable" response.
        video_words = ("video", "видео")
        open_words = ("открой", "открыть", "покажи", "запусти")
        lowered_query = query.casefold()
        if (
            getattr(self, "video", None) is not None
            and "папк" in lowered_query
            and any(word in lowered_query for word in video_words)
            and any(word in lowered_query for word in open_words)
        ):
            result = self.video.open_inbox()
            opened = bool(result.get("opened"))
            answer = (
                "Папку video открыла. Положи исходники прямо туда."
                if opened else
                f"Не смогла открыть папку video: {result.get('error') or result.get('reason') or 'нет рабочего стола'}."
            )
            route = {
                "action": "video_open_inbox",
                "model": "local-filesystem",
                "completed": opened,
                "verified": opened,
                "folder": result.get("path", ""),
            }
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", answer, metadata={"model": route["model"], "route": route})
            yield {"type": "start", "conversation_id": conversation_id, "route": route}
            yield {"type": "token", "content": answer, "full": answer}
            yield {"type": "done", "conversation_id": conversation_id, "answer": answer, "metrics": {"model": route["model"], "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "route": route}
            return

        # Clock/date and other tiny factual probes should never wait behind semantic
        # admission or a desktop workflow.  This keeps the voice response immediate
        # even while the larger local model is warming up.
        try:
            fast_data_acted, fast_data_answer, fast_data_route = self._fast_data_turn(query)
        except Exception:
            fast_data_acted, fast_data_answer, fast_data_route = False, "", {}
        if fast_data_acted:
            fast_data_route = {**dict(fast_data_route or {}), "control_plane": True}
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
            if persist_assistant and fast_data_answer:
                self.memory.add_message(conversation_id, "assistant", fast_data_answer, metadata={"model": fast_data_route.get("model", "deterministic"), "route": fast_data_route})
            yield {"type": "start", "conversation_id": conversation_id, "route": fast_data_route}
            yield {"type": "token", "content": fast_data_answer, "full": fast_data_answer}
            yield {"type": "done", "conversation_id": conversation_id, "answer": fast_data_answer, "metrics": {"model": fast_data_route.get("model", "deterministic"), "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "route": fast_data_route}
            return

        # A repeated acknowledgement must never cancel the workflow that the first
        # acknowledgement has just resumed. The persisted checkpoint remains visible
        # until the workflow completes, so we can identify this race deterministically.
        autonomous_engine = getattr(self, "autonomous_workflow", None)
        workflow_engine = getattr(self, "universal_workflow", None)
        autonomous_pending = bool(autonomous_engine and autonomous_engine.has_pending(conversation_id))
        workflow_pending = bool(workflow_engine and workflow_engine.has_pending(conversation_id))
        # In the released build the model/reactive executor is the only semantic
        # front door.  Legacy adapters below are retained for direct compatibility
        # tests and sealed safety controls, but production turns never reach them
        # after strict semantic admission succeeds (or fails closed).
        model_only_enabled = bool(getattr(self.settings, "model_only_mode", False)) and workflow_engine is not None
        model_only_route: bool | None = None
        model_semantic_decision: dict[str, Any] = {}
        model_only_admission_failed = False
        pending_action_owner = self._pending_confirmation_owner(conversation_id)
        fast_control = False
        if model_only_enabled and not workflow_pending and not autonomous_pending and not pending_action_owner:
            _clean_fast = normalize_phrase(query)
            fast_control = bool(
                is_pc_shutdown_request(query)
                # Открытие приложений: тот же список, что у ветки open_app_priority
                # ниже. Совпадение здесь означает, что надёжный слой её выполнит,
                # поэтому классификацию можно пропустить. Список скопирован буквально
                # — расхождение отправило бы ход мимо обработчика.
                or re.fullmatch(
                    r"(?:открой|запусти)\s+(?:telegram|телеграм\w*|яндекс\s*музык\w*|"
                    r"youtube|ютуб|spotify|спотифай|discord|дискорд)\s*[.!]*",
                    _clean_fast,
                )
                or re.fullmatch(
                    r"(?:поставь\s+)?(?:на\s+)?пауз\w*|пауза|"
                    r"(?:по)?ставь\s+на\s+паузу|"
                    r"продолж\w*|возобнов\w*|"
                    r"(?:включи|запусти|поставь)\s+музыку|"
                    r"(?:выключи|останови|стоп)\s+музыку|"
                    r"следующ\w*\s+трек\w*|предыдущ\w*\s+трек\w*|"
                    r"(?:сделай\s+)?(?:по)?громче|(?:сделай\s+)?(?:по)?тише|"
                    r"выключи\s+звук|включи\s+звук",
                    _clean_fast,
                )
            )
        if fast_control:
            # These are exactly the turns the verified control plane owns. Admission
            # is a separate uncached model call that cost 3-10 seconds in the trace
            # before any action began, and its verdict cannot change the outcome
            # here, so skip straight to the deterministic handler.
            model_only_route = None
            self._trace("MODEL_ONLY_CONTROL_BYPASS", control="fast_command")
        elif model_only_enabled and is_cancel_confirmation(query) and not workflow_pending:
            # Cancellation is a control-plane safety primitive, not task intent. Keep
            # it ahead of model admission so a stop request is immediate and final.
            model_only_route = None
            self._trace("MODEL_ONLY_CONTROL_BYPASS", control="cancel")
        elif model_only_enabled and workflow_pending:
            # A saved clarification must not freeze the conversation forever.  Give
            # the new utterance one fresh semantic admission first: an explicit new
            # computer action supersedes the stale checkpoint, while a free-form slot
            # answer (for example a service name) continues the pending task.
            pending_payload = {}
            try:
                pending_payload = dict(workflow_engine.services.db.get_setting(
                    workflow_engine._pending_key(conversation_id), None,
                ) or {})
            except Exception:
                pending_payload = {}
            pending_decision: dict[str, Any] = {}
            try:
                if hasattr(workflow_engine, "semantic_decision"):
                    pending_decision = dict(workflow_engine.semantic_decision(
                        query,
                        pending_context={
                            "original_task": str(pending_payload.get("task") or ""),
                            "question": str(
                                pending_payload.get("clarification_prompt")
                                or pending_payload.get("prompt")
                                or ""
                            ),
                            "missing_fields": list(pending_payload.get("missing_fields") or []),
                        },
                    ) or {})
            except Exception:
                pending_decision = {}
            pending_status = str(pending_decision.get("decision_status") or "")
            pending_route = str(pending_decision.get("route") or "")
            continuation_requested = bool(
                is_resume_confirmation(query)
                or pending_decision.get("needs_history") is True
            )
            pending_turn_kind = str(pending_decision.get("turn_kind") or "")
            if pending_status == "ok" and pending_turn_kind == "clarification_response":
                model_semantic_decision = pending_decision
                model_only_route = True
                self._trace("MODEL_ONLY_PENDING_CONTINUATION", decision=pending_decision)
            elif pending_status == "ok" and pending_route == "computer_action":
                try:
                    workflow_engine._clear_pending(
                        conversation_id, str(pending_payload.get("run_id") or ""),
                    )
                except Exception:
                    pass
                model_semantic_decision = pending_decision
                model_only_route = True
                self._trace("MODEL_ONLY_PENDING_SUPERSEDED", route=True, decision=pending_decision)
            elif pending_status == "ok" and pending_route == "conversation" and not continuation_requested:
                # A fresh conversational topic must never be appended to an old
                # clarification.  The previous implementation treated every
                # non-action envelope as a slot answer, so a route question could
                # hijack the next request (for example, a Python-code request was
                # sent back into a stale Maps task).  The model already told us that
                # this turn does not depend on history; retire the checkpoint and let
                # the normal conversation lane render the new topic.
                try:
                    workflow_engine._clear_pending(
                        conversation_id, str(pending_payload.get("run_id") or ""),
                    )
                except Exception:
                    pass
                model_semantic_decision = pending_decision
                model_only_route = False
                workflow_pending = False
                self._trace("MODEL_ONLY_PENDING_DISCARDED_NEW_TOPIC", decision=pending_decision)
            else:
                # A clarification/confirmation answer belongs to the saved reactive
                # checkpoint. Do not let legacy word adapters steal that turn.
                model_only_route = True
                self._trace("MODEL_ONLY_PENDING", route=True)
        elif model_only_enabled and pending_action_owner is not None:
            # Exact messenger/mail confirmations are control-plane state, not a new
            # semantic task. Their fingerprint owner gets first refusal for a bare
            # confirmation; an unrelated utterance receives a fresh semantic envelope
            # instead of inheriting the stale draft and losing its typed action hint.
            if is_affirmative_confirmation(query) or is_cancel_confirmation(query):
                model_only_route = None
                self._trace(
                    "MODEL_ONLY_PENDING_ACTION",
                    kind=pending_action_owner.kind,
                    fingerprint=pending_action_owner.fingerprint[:16],
                )
            else:
                try:
                    pending_decision = dict(workflow_engine.semantic_decision(query) or {})
                except Exception:
                    pending_decision = {}
                pending_status = str(pending_decision.get("decision_status") or "")
                pending_route = str(pending_decision.get("route") or "")
                if pending_status == "ok" and pending_route == "computer_action":
                    model_semantic_decision = pending_decision
                    model_only_route = True
                    self._trace(
                        "MODEL_ONLY_PENDING_ACTION_SUPERSEDED",
                        kind=pending_action_owner.kind,
                        fingerprint=pending_action_owner.fingerprint[:16],
                        decision=pending_decision,
                    )
                elif pending_status == "ok" and pending_route == "conversation":
                    if pending_decision.get("needs_history") is True or is_resume_confirmation(query):
                        # A short slot answer such as a service name may still be the
                        # continuation of the exact pending draft.
                        model_only_route = False
                        model_semantic_decision = pending_decision
                        self._trace("MODEL_ONLY_PENDING_ACTION_CONVERSATION", kind=pending_action_owner.kind)
                    else:
                        # New topics supersede stale mail/messenger drafts as well.
                        # Do not let an old confirmation owner consume an unrelated
                        # question or force it through a browser action.
                        stale_kind = pending_action_owner.kind
                        self._cancel_pending_actions(conversation_id)
                        model_only_route = False
                        model_semantic_decision = pending_decision
                        pending_action_owner = None
                        self._trace("MODEL_ONLY_PENDING_ACTION_DISCARDED_NEW_TOPIC", kind=stale_kind)
                else:
                    model_only_route = False
                    model_only_admission_failed = True
                    model_semantic_decision = {"needs_history": False, "decision_status": "error"}
                    self._trace("MODEL_ONLY_PENDING_ACTION_SEMANTIC_FAILED", kind=pending_action_owner.kind)
        elif model_only_enabled and not autonomous_pending:
            # Route and typed capability are one uncached model envelope.  Calling a
            # second semantic-hint model here used to disagree with admission and made
            # configured mail unreachable.
            try:
                if hasattr(workflow_engine, "semantic_decision"):
                    model_semantic_decision = dict(workflow_engine.semantic_decision(query) or {})
                    route_name = str(model_semantic_decision.get("route") or "unknown")
                    decision_status = str(model_semantic_decision.get("decision_status") or "ok")
                    if decision_status in {"error", "invalid"}:
                        # An unavailable envelope is not permission to inspect or
                        # mutate the desktop. Fail closed into a short conversational
                        # response and avoid reviving stale task history.
                        model_only_admission_failed = True
                        model_semantic_decision["needs_history"] = False
                        model_only_route = False
                    else:
                        model_only_route = route_name == "computer_action"
                        # Настроение — реакция на слова человека; покажется в конце хода.
                        self._emotion("note_mood", conversation_id, str(model_semantic_decision.get("mood") or ""))
                        # Мост — главная задумка r72: подтверждение и встречный вопрос,
                        # пока идёт работа. Раньше его придумывал отдельный вызов модели
                        # с потолком 0,6 с. На настоящей модели он не успевал, уходил в
                        # «остывание» на 10 минут, и человек видел лишь дежурное
                        # «Выполняю». Теперь реплику даёт то же решение, что и маршрут, —
                        # бесплатно, без лишнего вызова и без гонки за 0,6 секунды.
                        _ack = str(model_semantic_decision.get("ack") or "").strip()
                        _question = str(model_semantic_decision.get("question") or "").strip()
                        if model_only_route and _ack and self.bridge is not None:
                            try:
                                _line = f"{_ack} {_question}".strip() if _question else _ack
                                try:
                                    _line = self.enforce_gender(_line)
                                except Exception:
                                    pass
                                if _question:
                                    self.bridge.mark_waiting(conversation_id, _question)
                                self._trace("BRIDGE_FROM_DECISION", ack=_ack[:40], question=_question[:80])
                                yield {"type": "bridge", "text": _line, "ack": _ack, "question": _question}
                            except Exception as exc:
                                self._trace("BRIDGE_FROM_DECISION_FAILED", error=str(exc)[:160])
                else:
                    decision = workflow_engine.semantic_route(query)
                    model_only_route = decision is True
                self._trace(
                    "MODEL_ONLY_ADMISSION", decision=model_semantic_decision,
                    route=model_only_route,
                    fallback=("text_conversation" if model_only_admission_failed else "none"),
                )
            except Exception as exc:
                # Fail closed when the admission layer itself is unavailable: no
                # desktop action is authorized without a valid typed envelope.
                model_only_admission_failed = True
                model_semantic_decision = {"needs_history": False, "decision_status": "error"}
                model_only_route = False
                self._trace("MODEL_ONLY_ADMISSION_ERROR", error=str(exc)[:240], fallback="text_conversation")
        if is_resume_confirmation(query):
            with self._lock:
                already_resuming = self._stop_events.get(conversation_id)
                already_resuming = bool(
                    already_resuming and already_resuming is not external_stop_event
                    and not already_resuming.is_set()
                )
            if already_resuming:
                answer = "Уже продолжаю с сохранённого шага. Повторное «готов» не сбило задачу."
                route = {"action": "checkpoint_resume_in_progress", "model": "deterministic", "control_plane": True}
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"duplicate_resume": True})
                if persist_assistant:
                    self.memory.add_message(conversation_id, "assistant", answer, metadata={"model": "deterministic", "route": route})
                yield {"type": "start", "conversation_id": conversation_id, "route": route}
                yield {"type": "token", "content": answer, "full": answer}
                yield {"type": "done", "conversation_id": conversation_id, "answer": answer, "metrics": {"model": "deterministic"}, "stopped": False, "route": route}
                return

        # The public wrapper registered this turn's stop token and runtime generation
        # before any attachment/VLM/tool work, so cancellation is effective immediately.

        # A typed path such as ``Рабочий стол\Письмо.docx`` is a real local input, not
        # prose. Resolve it before considering conversational attachment memory.
        explicit_paths=self._explicit_local_paths(query)
        if explicit_paths:
            attachment_paths=list(dict.fromkeys([*(attachment_paths or []), *explicit_paths]))

        # Voice and text share the same attachment memory. If the owner explicitly says
        # "the attached files/image" after uploading, recover the recent local files even
        # when this turn itself carries no attachment IDs.
        if not attachment_paths and re.search(
            r"\b(?:прикрепл\w*|вложен\w*|загружен\w*|(?:этот|тот|данн\w*|последн\w*)\s+файл\w*|"
            r"(?:эти|те|данн\w*|последн\w*)\s+файл\w*|(?:этот|тот|данн\w*)\s+архив\w*|"
            r"(?:эта|этой|эту|та|той|ту|эта\s+же|данн\w*)\s+(?:картинк|изображен|фото)\w*|"
            r"(?:на|в)\s+(?:прикрепленн\w*|вложенн\w*)\s+(?:файл|картинк|изображен|фото)\w*)\b",
            query, re.I,
        ):
            attachment_paths = self._recent_attachment_paths(conversation_id)
        original_attachment_paths = list(attachment_paths or [])
        query, brief_acted, brief_answer, brief_route, guided_paths = self._guided_file_publish_turn(
            query, conversation_id, original_attachment_paths,
        ) if model_only_route is None else (query, False, "", {}, [])
        if brief_acted:
            brief_answer = self.enforce_gender(brief_answer)
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"attachments": original_attachment_paths})
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", brief_answer, metadata={"model": "deterministic", "route": brief_route})
            yield {"type": "start", "conversation_id": conversation_id, "route": brief_route}
            yield {"type": "token", "content": brief_answer, "full": brief_answer}
            yield {"type": "done", "conversation_id": conversation_id, "answer": brief_answer, "metrics": {"model": "deterministic"}, "stopped": False, "route": brief_route}
            if self.runtime is not None:
                try: self._finish_runtime(runtime_generation, brief_answer, ok=True)
                except Exception: pass
            return
        # User images are first-class local attachments on desktop and phone. Keep
        # document extraction on the text model and route image-only questions to the
        # small local vision model. Raw files never leave the paired PC.
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        if brief_route.get("action") == "publish_brief_complete":
            attachment_paths = list(guided_paths)
        else:
            attachment_paths = list(attachment_paths or [])
        supplied_images = list(image_paths or [])
        image_paths = supplied_images + [p for p in attachment_paths if Path(p).suffix.casefold() in image_exts]
        image_paths = list(dict.fromkeys(image_paths))[:3]
        attachment_paths = [p for p in attachment_paths if Path(p).suffix.casefold() not in image_exts]

        non_image_attachments=list(attachment_paths or [])
        action_image_paths: list[str] = []
        image_action = bool(image_paths and model_only_route is None and self._image_action_request(query))
        if image_action:
            # An attached image can be the object of an action (upload/send/edit), not
            # merely a VLM question.  Preserve its real path for deterministic/tool
            # adapters and add visual semantics only when the command refers to content.
            real_images = list(image_paths)
            action_image_paths = list(real_images)
            path_context = "; ".join(str(Path(path).resolve()) for path in real_images if Path(path).is_file())
            content_dependent = bool(re.search(
                r"\b(?:на\s+(?:этом|этой)|с\s+(?:этого|этой)|что\s+изображено|по\s+картинке|по\s+фото|объект|товар)\b",
                normalize_phrase(query), re.I,
            ))
            if content_dependent:
                notes = [self._vision_for_path(path, "Кратко опиши объекты и текст, нужные для действия") for path in real_images]
                query += "\n\nЛокальный визуальный анализ:\n" + "\n".join(notes)
            if path_context:
                query += f"\n\nРеальные локальные пути изображений: {path_context}"
            attachment_paths = list(dict.fromkeys([*attachment_paths, *real_images]))
            non_image_attachments = list(attachment_paths)
            image_paths = []
        if image_paths and non_image_attachments:
            # Mixed turns are fused locally: the lightweight VLM describes each image,
            # then the main text model reasons over those descriptions together with the
            # extracted PDF/DOCX/XLSX/PPTX/etc. content. This avoids sending unsupported
            # image payloads to the text-only release checkpoint.
            vision_notes=[]
            for idx,path in enumerate(image_paths[:3],1):
                vision_notes.append(f"Изображение {idx}: {self._vision_for_path(path, query or 'Опиши изображение для совместного анализа файлов')}")
            if vision_notes:
                query = query + "\n\nЛокальный анализ приложенных изображений:\n" + "\n".join(vision_notes)
            image_paths=[]
        if image_paths and not non_image_attachments:
            # This request already owns its stop token.  Calling ``stop`` here set that
            # very token immediately, so the wrapper discarded the valid VLM token and
            # done events even though _vision_for_path had accepted the answer.  Newer
            # owner turns are still able to cancel this one through the normal request
            # lifecycle; an image-only turn must not cancel itself.
            answers=[]
            for idx,path in enumerate(image_paths[:3],1):
                answers.append(self._vision_for_path(path, query or "Опиши изображение"))
            answer="\n".join((f"Изображение {i}: {a}" if len(answers)>1 else a) for i,a in enumerate(answers,1))
            answer=self.enforce_gender(answer)
            if persist_user:
                self.memory.add_message(conversation_id,"user",query,metadata={"images":image_paths or [],"attachments":attachment_paths or []})
            if persist_assistant:
                self.memory.add_message(conversation_id,"assistant",answer,metadata={"model":self.settings.vision_model,"route":{"action":"vision_direct"}})
            yield {"type":"start","conversation_id":conversation_id,"route":{"action":"vision_direct","model":self.settings.vision_model}}
            yield {"type":"token","content":answer,"full":answer}
            yield {"type":"done","conversation_id":conversation_id,"answer":answer,"metrics":{"model":self.settings.vision_model,"total_seconds":round((time.monotonic()-request_started),3)},"stopped":False,"route":{"action":"vision_direct","model":self.settings.vision_model}}
            if self.runtime is not None:
                try:self._finish_runtime(runtime_generation, answer, ok=not answer.startswith("Не удалось"))
                except Exception:pass
            return

        # Strict model-only admission runs before every legacy semantic adapter.  A
        # short local acknowledgement is left to the instant lane; everything else is
        # either handed to the reactive executor or falls through to ordinary chat.
        # model_only_route was decided before attachment handling.  No lexical
        # intent/instant adapter is allowed to reinterpret a production turn here.

        # Small explicit code-to-file requests are deterministic: generate -> syntax
        # check -> write -> readback hash. They must not enter the generic desktop planner.
        code_file_acted, code_file_answer, code_file_route = (
            self._code_file_turn(query) if model_only_route is None else (False, "", {})
        )
        if code_file_acted:
            code_file_answer = self.enforce_gender(code_file_answer)
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"attachments": attachment_paths or []})
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", code_file_answer, metadata={"model":"local-code","route":code_file_route})
            yield {"type":"start", "conversation_id":conversation_id, "route":code_file_route}
            yield {"type":"token", "content":code_file_answer, "full":code_file_answer}
            yield {"type":"done", "conversation_id":conversation_id, "answer":code_file_answer, "metrics":{"model":"local-code","total_seconds":round(time.monotonic()-request_started,3)}, "stopped":False, "route":code_file_route}
            if self.runtime is not None:
                try: self._finish_runtime(runtime_generation, code_file_answer, ok=bool(code_file_route.get("verified")))
                except Exception: pass
            return

        # Local phone organizer is deterministic and cross-surface: desktop, mobile and
        # Telegram all reach this same lane. It intentionally runs before generic desktop
        # routing so natural phrases such as "завтра в 18 у меня работа" cannot be
        # mistaken for a UI action.
        if not image_paths and self.phone_sync is not None:
            try:
                organizer_acted, organizer_answer, organizer_route = self.phone_sync.handle_command(query, conversation_id)
            except Exception as exc:
                organizer_acted, organizer_answer, organizer_route = False, "", {"error": str(exc)[:300]}
            if organizer_acted:
                organizer_answer = self.enforce_gender(organizer_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                    if self.settings.auto_memory:
                        self.memory.remember_from_message(query)
                        self._capture_passive_memory(query)
                if persist_assistant and organizer_answer:
                    self.memory.add_message(conversation_id, "assistant", organizer_answer, metadata={"model": "deterministic", "route": organizer_route})
                yield {"type": "start", "conversation_id": conversation_id, "route": organizer_route}
                if organizer_answer:
                    yield {"type": "token", "content": organizer_answer, "full": organizer_answer}
                yield {"type": "done", "conversation_id": conversation_id, "answer": organizer_answer, "metrics": {"model": "deterministic", "total_seconds": round(time.monotonic()-request_started, 3)}, "stopped": False, "route": organizer_route}
                if self.runtime is not None:
                    try:
                        self._finish_runtime(runtime_generation, organizer_answer, ok=True)
                    except Exception:
                        pass
                return

        # Video editing has its own file manifest, clarification state and FFmpeg
        # verifier. It must run before the generic desktop/mission routers, otherwise
        # phrases such as "склей первое и второе видео" are mistaken for UI clicks or
        # media playback. Text, voice and Telegram all pass through this same lane.
        if not image_paths:
            video_acted, video_answer, video_route = self._video_turn(query, conversation_id)
            if video_acted:
                video_answer = self.enforce_gender(video_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                    if self.settings.auto_memory:
                        self.memory.remember_from_message(query)
                        self._capture_passive_memory(query)
                if persist_assistant and video_answer:
                    self.memory.add_message(conversation_id, "assistant", video_answer, metadata={"model": video_route.get("model", "video-ffmpeg"), "route": video_route})
                yield {"type": "start", "conversation_id": conversation_id, "route": video_route}
                if video_answer:
                    yield {"type": "token", "content": video_answer, "full": video_answer}
                yield {"type": "done", "conversation_id": conversation_id, "answer": video_answer, "metrics": {"model": video_route.get("model", "video-ffmpeg"), "total_seconds": round(time.monotonic()-request_started, 3)}, "stopped": False, "route": video_route}
                if self.runtime is not None:
                    try: self._finish_runtime(runtime_generation, video_answer, ok=not video_route.get("error"))
                    except Exception: pass
                return

        # r15.5 control plane: cancellation and foreground media must be resolved before
        # intent detection/planning. Otherwise obvious commands such as "поставь на
        # паузу" can be misclassified as a visual "show" action.
        if not image_paths:
            priority_acted, priority_answer, priority_route = self._priority_control_turn(query, conversation_id)
            if priority_acted:
                priority_answer = self.enforce_gender(priority_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if persist_assistant and priority_answer:
                    self.memory.add_message(conversation_id, "assistant", priority_answer, metadata={"model": "deterministic", "route": priority_route})
                yield {"type": "start", "conversation_id": conversation_id, "route": priority_route}
                if priority_answer:
                    self._trace("CHAT_CONTROL_OUT", query=query, answer=priority_answer[:1000], route=priority_route, total_ms=round((time.monotonic()-request_started)*1000))
                    yield {"type": "token", "content": priority_answer, "full": priority_answer}
                if self.runtime is not None:
                    try: self._finish_runtime(runtime_generation, priority_answer, ok=not bool(priority_route.get("completed") and not priority_route.get("verified", True)))
                    except Exception: pass
                yield {"type": "done", "conversation_id": conversation_id, "answer": priority_answer, "metrics": {"model": "deterministic", "total_seconds": round(time.monotonic()-request_started, 3)}, "stopped": priority_route.get("action") == "cancel_interactive", "route": priority_route}
                return

        browser_target = AppSkills.browser_open_target(query) if not image_paths else ""
        if browser_target:
            model_semantic_decision = {
                "route": "computer_action", "turn_kind": "action", "action": "open_service",
                "target": browser_target, "service": browser_target, "target_kind": "service",
                "single_step": True, "side_effect_required": True, "decision_status": "ok",
            }
            model_only_route = True
            model_only_admission_failed = False

        # The same structured envelope that admitted the turn now dispatches any
        # authoritative connector.  There is no second model call and no phrase table:
        # ordinary greetings continue to EIRVEN's styled chat model, while mail and
        # messenger preflight are owned by their real capabilities.
        if model_only_admission_failed and not image_paths:
            # A missing/invalid semantic envelope is an infrastructure failure, not a
            # conversational turn.  Never let the text model role-play a mail read,
            # browser click, or other desktop action after admission timed out.
            # Управляющее решение не пришло. Защита остаётся прежней: никаких действий
            # на компьютере в этом ходе. Но раньше Эрви отказывала во ВСЁМ — даже на
            # «как дела» и простые вопросы отвечала «повтори через несколько секунд».
            # На слабой машине это означало, что с Эрви нельзя было даже поговорить.
            # Теперь она отвечает на вопросы и разговор, а на просьбу что-то сделать
            # честно говорит, что сейчас не вышло. Выполнить она ничего не может:
            # ход не проходит через исполнителей, только через разговор.
            answer = self._degraded_reply(query, conversation_id)
            degraded = bool(answer)
            if not answer:
                answer = (
                    "Не смогла получить управляющее решение локальной модели, поэтому "
                    "ничего не выполняла и не буду выдавать это за успех. Повтори команду "
                    "через несколько секунд."
                )
            route = {
                "degraded_chat": degraded,
                "action": "semantic_unavailable",
                "model": self.universal_workflow._admission_model() if self.universal_workflow is not None else "local-semantic",
                "verified": False,
                "executed": False,
            }
            if persist_user:
                self.memory.add_message(
                    conversation_id, "user", query,
                    metadata={"images": [], "attachments": attachment_paths or []},
                )
            if persist_assistant:
                self.memory.add_message(
                    conversation_id, "assistant", answer,
                    metadata={"model": route["model"], "route": route},
                )
            self._trace(
                "CHAT_SEMANTIC_UNAVAILABLE",
                query=query,
                answer=answer,
                route=route,
                total_ms=round((time.monotonic() - request_started) * 1000),
            )
            yield {"type": "start", "conversation_id": conversation_id, "route": route}
            yield {"type": "token", "content": answer, "full": answer}
            if self.runtime is not None:
                try:
                    self._finish_runtime(runtime_generation, answer, ok=False)
                except Exception:
                    pass
            yield {
                "type": "done",
                "conversation_id": conversation_id,
                "answer": answer,
                "metrics": {"model": route["model"], "total_seconds": round(time.monotonic() - request_started, 3)},
                "stopped": False,
                "route": route,
            }
            return
        # Resolve food with conversation context before a fallible mail/action label.
        food = getattr(self, "food", None)
        food_context = ""
        food_decision = {"food": False}
        # Открытие определяет модель, а не список слов. Прежняя проверка ловила
        # только «открой», «запусти», «открыть» — и «открой мэш» мимо неё не
        # проходило, потому что дело было не в глаголе, а в названии сервиса.
        # Перечислить все способы назвать сервис нельзя, поэтому решает модель.
        open_decision = {"open": False}
        # Одна классификация на сообщение вместо трёх. Контуры открытия и еды
        # решают моделью, и маршрутизатор ниже тоже: если дать им сработать
        # подряд, человек ждёт три ответа модели вместо одного. Поэтому здесь
        # спрашиваем один раз, а результат переиспользуется ниже.
        # Контур открытия спрашивает модель «не открыть ли что-нибудь?». Раньше он
        # работал на КАЖДОЕ сообщение — даже когда управляющее решение уже сказало,
        # что это просто разговор. На «как дела» это был лишний вызов модели, и
        # ответы заметно замедлились. Теперь, если решение есть, контур работает
        # только когда оно само сказало «открыть».
        _decided = str(model_semantic_decision.get("decision_status") or "") == "ok"
        _decided_action = str(model_semantic_decision.get("action") or "")
        _opener_useful = (not _decided) or _decided_action in {"open_service", "launch_application"}
        if not _opener_useful:
            self._trace("OPENER_SKIPPED", reason="решение уже есть", action=_decided_action)
        if self.opener is not None and not image_paths and not workflow_pending and _opener_useful:
            try:
                open_decision = self.opener.understand(query)
            except Exception as exc:
                self._trace("OPEN_UNDERSTAND_FAILED", error=str(exc)[:200])
        explicit_open = browser_target or bool(open_decision.get("open"))

        if bool(open_decision.get("open")) and not browser_target:
            try:
                opened = self.opener.perform(open_decision)
                answer = self.enforce_gender(str(opened.get("message") or ""))
                if answer:
                    if persist_user:
                        self.memory.add_message(conversation_id, "user", query)
                    self.memory.add_message(conversation_id, "assistant", answer)
                    self._trace("CHAT_OPEN_OUT", target=str(open_decision.get("target"))[:80],
                                ok=bool(opened.get("ok")))
                    yield {"type": "done", "conversation_id": conversation_id, "metrics": {"model": self.settings.fast_model, "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "answer": answer, "route": {
                        "action": "open_application", "model": self.settings.fast_model,
                        "completed": bool(opened.get("ok")), "verified": bool(opened.get("ok")),
                        "target": open_decision.get("target"), "via": opened.get("via", ""),
                    }}
                    return
            except Exception as exc:
                self._trace("CHAT_OPEN_FAILED", error=str(exc)[:200])
        if not image_paths and food is not None and not workflow_pending and not explicit_open:
            try:
                history = self.memory.history(conversation_id, limit=8)
                food_context = "\n".join(f"{row['role']}: {row['content']}" for row in history)[-2400:]
                food_decision = food.understand(query, context=food_context)
            except Exception as exc:
                self._trace("FOOD_UNDERSTAND_FAILED", error=str(exc)[:200])
            if external_stop_event is not None and external_stop_event.is_set():
                return
            if food_decision.get("food"):
                try:
                    result = food.respond(query, food_decision, context=food_context)
                    answer = self.enforce_gender(str(result.get("answer") or ""))
                    if not answer:
                        raise RuntimeError("Пустой ответ контура еды")
                except Exception as exc:
                    self._trace("CHAT_FOOD_FAILED", error=str(exc)[:200])
                    result = {"completed": False, "intent": food_decision.get("intent")}
                    answer = "Не получилось получить данные ВкусВилла. Попробуй ещё раз чуть позже."
                if external_stop_event is not None and external_stop_event.is_set():
                    return
                route = {
                    "action": "food", "model": self.settings.fast_model,
                    "intent": result.get("intent"),
                    "completed": bool(result.get("completed", True)),
                    "verified": bool(result.get("completed", True)) and bool(result.get("cart_url") or result.get("products")),
                    "cart_url": result.get("cart_url") or "",
                }
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query)
                if persist_assistant:
                    self.memory.add_message(conversation_id, "assistant", answer, metadata={"route": route})
                yield {"type": "start", "conversation_id": conversation_id, "route": route}
                yield {"type": "token", "content": answer, "full": answer}
                if self.runtime is not None:
                    self._finish_runtime(runtime_generation, answer, ok=route["completed"])
                yield {"type": "done", "conversation_id": conversation_id, "answer": answer, "route": route}
                return

        if model_semantic_decision and not image_paths:
            typed_acted, typed_answer, typed_route = self._typed_semantic_turn(
                query, conversation_id, model_semantic_decision,
            )
            if typed_acted:
                typed_answer = self.enforce_gender(typed_answer)
                if persist_user:
                    self.memory.add_message(
                        conversation_id, "user", query,
                        metadata={"images": [], "attachments": attachment_paths or []},
                    )
                if persist_assistant and typed_answer:
                    self.memory.add_message(
                        conversation_id, "assistant", typed_answer,
                        metadata={"model": typed_route.get("model", "semantic-capability"), "route": typed_route},
                    )
                yield {"type": "start", "conversation_id": conversation_id, "route": typed_route}
                if typed_answer:
                    yield {"type": "token", "content": typed_answer, "full": typed_answer}
                yield {
                    "type": "done", "conversation_id": conversation_id,
                    "answer": typed_answer,
                    "metrics": {"model": typed_route.get("model", "semantic-capability")},
                    "stopped": False, "route": typed_route,
                }
                if self.runtime is not None:
                    try:
                        self._finish_runtime(
                            runtime_generation, typed_answer,
                            ok=bool(typed_route.get("verified", True)),
                        )
                    except Exception:
                        pass
                return

        # Stateful task continuity (including a live r19 graph correction) must win over
        # creating a brand-new mission. This lets "и ещё ..." revise the active graph.
        authoritative_mail_request = False
        if not image_paths and self._mail_request(query):
            try:
                authoritative_mail_request = bool(self.mail is not None and self.mail.configured())
            except Exception:
                authoritative_mail_request = False
        if not image_paths and model_only_route is None and not authoritative_mail_request and not (autonomous_pending or workflow_pending):
            early_stateful_acted, early_stateful_answer, early_stateful_route = self._stateful_task_shortcut(query, conversation_id)
            if early_stateful_acted:
                early_stateful_answer = self.enforce_gender(early_stateful_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images":[], "attachments":attachment_paths or []})
                if persist_assistant and early_stateful_answer:
                    self.memory.add_message(conversation_id, "assistant", early_stateful_answer, metadata={"model":"state", "route":early_stateful_route})
                yield {"type":"start","conversation_id":conversation_id,"route":early_stateful_route}
                if early_stateful_answer:
                    yield {"type":"token","content":early_stateful_answer,"full":early_stateful_answer}
                yield {"type":"done","conversation_id":conversation_id,"answer":early_stateful_answer,"metrics":{"model":"state"},"stopped":False,"route":early_stateful_route}
                return

        # r57 practical mail lane: local IMAP review, reversible spam handling and
        # locally prepared drafts. Sending has an explicit second confirmation turn.
        # Runs regardless of the route verdict: reading a mailbox over IMAP is a
        # verified local operation, and "проверь почту" is classified as a computer
        # action, so the old gate sent it to UI automation that cannot read mail.
        if not image_paths:
            mail_acted, mail_answer, mail_route = self._mail_turn(query, conversation_id)
            if mail_acted:
                mail_answer = self.enforce_gender(mail_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if persist_assistant and mail_answer:
                    self.memory.add_message(conversation_id, "assistant", mail_answer, metadata={"model":"local-mail", "route":mail_route})
                yield {"type":"start","conversation_id":conversation_id,"route":mail_route}
                if mail_answer:
                    yield {"type":"token","content":mail_answer,"full":mail_answer}
                yield {"type":"done","conversation_id":conversation_id,"answer":mail_answer,"metrics":{"model":"local-mail"},"stopped":False,"route":mail_route}
                if self.runtime is not None:
                    try: self._finish_runtime(runtime_generation, mail_answer, ok=bool(mail_route.get("verified", True)))
                    except Exception: pass
                return

        # A 24/7 Telegram request configures the existing event monitor directly.
        # It must not fall into generic UI automation: that used to click through
        # settings without ever creating a persistent rule.
        if not image_paths and not workflow_pending:
            telegram_acted, telegram_answer, telegram_route = self._telegram_monitor_turn(query)
            if telegram_acted:
                telegram_answer = self.enforce_gender(telegram_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if persist_assistant and telegram_answer:
                    self.memory.add_message(conversation_id, "assistant", telegram_answer, metadata={"model":"telegram-monitor", "route":telegram_route})
                yield {"type":"start","conversation_id":conversation_id,"route":telegram_route}
                if telegram_answer:
                    yield {"type":"token","content":telegram_answer,"full":telegram_answer}
                yield {"type":"done","conversation_id":conversation_id,"answer":telegram_answer,"metrics":{"model":"telegram-monitor"},"stopped":False,"route":telegram_route}
                return

        # Natural-language diagnostics can be questions rather than imperatives.
        # Resolve them before generic conversation so "Эрви, где баг?" in VS Code and
        # "у меня не работает Яндекс Музыка, почему?" start real inspection immediately.
        if not image_paths and model_only_route is None:
            problem_acted, problem_answer, problem_route = self._contextual_problem_turn(query, conversation_id)
            if problem_acted:
                problem_answer = self.enforce_gender(problem_answer)
                if persist_user:
                    self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if persist_assistant and problem_answer:
                    self.memory.add_message(conversation_id, "assistant", problem_answer, metadata={"model": problem_route.get("model", "deterministic"), "route": problem_route})
                yield {"type": "start", "conversation_id": conversation_id, "route": problem_route}
                if problem_answer:
                    yield {"type": "token", "content": problem_answer, "full": problem_answer}
                yield {"type": "done", "conversation_id": conversation_id, "answer": problem_answer, "metrics": {"model": problem_route.get("model", "deterministic")}, "stopped": False, "route": problem_route}
                if self.runtime is not None:
                    try: self._finish_runtime(runtime_generation, problem_answer, ok=True)
                    except Exception: pass
                return

        # r19 long-horizon missions run in TaskManager's dedicated fast lane. They are
        # persistent and may cross applications; the live chat remains free while the
        # mission progresses in the background. Single-surface r18 actions stay inline.
        mission_engine = getattr(self, "mission_engine", None)
        if model_only_route is None and workflow_engine is None and mission_engine is not None and not image_paths and mission_engine.should_handle(query):
            context_snapshot = mission_engine.capture_context()
            try:
                preview_nodes = mission_engine._deterministic_plan(query, context=context_snapshot)
            except Exception:
                preview_nodes = []
            task_id = self.tasks.enqueue(
                "mission",
                f"Миссия: {query[:140]}",
                {"goal": query, "context": context_snapshot},
                conversation_id=conversation_id,
            )
            step_count = max(1, len(preview_nodes))
            step_word = "этап" if step_count % 10 == 1 and step_count % 100 != 11 else ("этапа" if step_count % 10 in {2, 3, 4} and step_count % 100 not in {12, 13, 14} else "этапов")
            answer = f"Приняла составную задачу: {step_count} {step_word}. Выполняю строго по очереди и после каждого этапа проверяю результат."
            route = {"action":"mission_created","model":"r23-task-graph","engine":"r23","task_id":task_id,"kind":"mission","planned_steps":step_count}
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images":[], "attachments":attachment_paths or []})
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", answer, metadata={"model":"r23-task-graph","route":route})
            self._trace("CHAT_MISSION_CREATED", query=query, task_id=task_id, context=context_snapshot, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type":"start","conversation_id":conversation_id,"route":route}
            yield {"type":"token","content":answer,"full":answer}
            yield {"type":"done","conversation_id":conversation_id,"answer":answer,"metrics":{"model":"r23-task-graph","total_seconds":round(time.monotonic()-request_started,3)},"stopped":False,"route":route}
            if self.runtime is not None:
                try: self._finish_runtime(runtime_generation, answer, ok=True)
                except Exception: pass
            return

        # Existing project/task continuity has priority over starting a new generic
        # desktop workflow. This is state resolution, not an application template: an
        # explicit "продолжай" after a failed/running project resumes the same task.
        autonomous_engine = getattr(self, "autonomous_workflow", None)
        workflow_engine = getattr(self, "universal_workflow", None)
        autonomous_pending = bool(autonomous_engine and conversation_id and autonomous_engine.has_pending(conversation_id))
        workflow_pending = bool(workflow_engine and conversation_id and workflow_engine.has_pending(conversation_id))
        if model_only_route is None and not autonomous_pending and not workflow_pending:
            stateful_acted, stateful_answer, stateful_route = self._stateful_task_shortcut(query, conversation_id)
            if stateful_acted:
                stateful_answer = self.enforce_gender(stateful_answer)
                if persist_assistant and stateful_answer:
                    self.memory.add_message(conversation_id, "assistant", stateful_answer, metadata={"model":"state", "route":stateful_route})
                yield {"type":"start", "conversation_id":conversation_id, "route":stateful_route}
                if stateful_answer:
                    yield {"type":"token", "content":stateful_answer, "full":stateful_answer}
                yield {"type":"done", "conversation_id":conversation_id, "answer":stateful_answer, "metrics":{"model":"state"}, "stopped":False, "route":stateful_route}
                return

        # r16 autonomous workflow. It owns dependent multi-step goals and chooses one
        # local action from the CURRENT UI state at a time. There is deliberately no
        # application-specific recipe here; after every state transition it observes
        # affordances again before choosing the next action.
        if model_only_route is None and workflow_engine is None and autonomous_engine is not None and not image_paths and autonomous_engine.should_handle(query, conversation_id):
            workflow_stop = external_stop_event or threading.Event()
            with self._lock:
                self._stop_events[conversation_id] = workflow_stop
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if self.settings.auto_memory:
                    self.memory.remember_from_message(query)
            route = {"action": "autonomous_workflow", "model": "state-policy+uia+tools", "engine": "r19-surface"}
            yield {"type": "start", "conversation_id": conversation_id, "route": route}
            result = autonomous_engine.execute_goal(
                query, conversation_id=conversation_id, stop_event=workflow_stop,
            )
            route.update(completed=result.ok, verified=result.ok, teaching_available=not result.ok and not result.needs_user, teaching_goal=query)
            final = self.enforce_gender(result.summary)
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", final, metadata={"model": "state-policy+uia+tools", "route": {**route, "results": result.steps, "needs_user": result.needs_user}})
            self._trace("CHAT_AUTONOMOUS_OUT", query=query, ok=result.ok, summary=final, results=result.steps, needs_user=result.needs_user, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type": "token", "content": final, "full": final}
            yield {"type": "done", "conversation_id": conversation_id, "answer": final, "metrics": {"model": "state-policy+uia+tools", "total_seconds": round(time.monotonic()-request_started,3)}, "stopped": bool(workflow_stop.is_set()), "route": {**route, "results": result.steps, "needs_user": result.needs_user}}
            if self.runtime is not None:
                try: self._finish_runtime(runtime_generation, final, ok=(result.ok or result.needs_user))
                except Exception: pass
            with self._lock:
                if self._stop_events.get(conversation_id) is workflow_stop:
                    self._stop_events.pop(conversation_id, None)
            return

        # r15 desktop-agent core. Any real action (not only a known template or a
        # multi-verb sentence) is handled by the stateful universal workflow. Direct
        # adapters are only accelerators inside it; a template miss falls through to
        # model planning + live UI/system tools instead of returning action_failed_fast.
        if workflow_engine is not None and not image_paths and (
            model_only_route is True
            or (model_only_route is None and workflow_engine.should_handle(query, conversation_id))
        ):
            workflow_stop = external_stop_event or threading.Event()
            with self._lock:
                self._stop_events[conversation_id] = workflow_stop
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images": [], "attachments": attachment_paths or []})
                if self.settings.auto_memory:
                    self.memory.remember_from_message(query)
            route = {"action": "desktop_agent", "model": "planner+uia+tools"}
            yield {"type": "start", "conversation_id": conversation_id, "route": route}
            result = workflow_engine.execute_task(
                query,
                lambda clause: self._deterministic_intent_turn(clause, conversation_id, action_image_paths, attachment_paths),
                conversation_id=conversation_id,
                stop_event=workflow_stop,
                semantic_envelope=(model_semantic_decision if model_only_enabled else None),
            )
            with self._lock:
                still_owned = self._stop_events.get(conversation_id) is workflow_stop
            if workflow_stop.is_set() or not still_owned:
                self._trace(
                    "CHAT_WORKFLOW_SUPERSEDED", query=query,
                    total_ms=round((time.monotonic() - request_started) * 1000),
                )
                return
            mail = getattr(self, "mail", None)
            if mail is not None:
                try: mail.bind_pending(conversation_id, created_after=request_wall_started)
                except Exception: pass
            route.update(completed=result.ok, verified=result.ok, teaching_available=not result.ok and not result.needs_user, teaching_goal=query)
            final = self.enforce_gender(result.summary)
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", final, metadata={"model": "planner+uia+tools", "route": {**route, "results": result.steps, "needs_user": result.needs_user}})
            self._trace("CHAT_WORKFLOW_OUT", query=query, ok=result.ok, summary=final, results=result.steps, needs_user=result.needs_user, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type": "token", "content": final, "full": final}
            yield {"type": "done", "conversation_id": conversation_id, "answer": final, "metrics": {"model": "planner+uia+tools", "total_seconds": round(time.monotonic()-request_started,3)}, "stopped": bool(workflow_stop.is_set()), "route": {**route, "results": result.steps, "needs_user": result.needs_user}}
            if self.runtime is not None:
                try: self._finish_runtime(runtime_generation, final, ok=(result.ok or result.needs_user))
                except Exception: pass
            with self._lock:
                if self._stop_events.get(conversation_id) is workflow_stop:
                    self._stop_events.pop(conversation_id, None)
            return

        # r10 latency rule: deterministic hands/data/camera NEVER wait behind an older LLM
        # generation. This fixes the pathological case where "открой Telegram" or "который
        # час" sat behind a previous 10-second chat answer just because they shared a lock.
        quick_answer = ""
        quick_route: dict[str, Any] = {}
        quick_acted = False
        self._trace("MODEL_ONLY_GATE", route=model_only_route, enabled=model_only_enabled, workflow=bool(workflow_engine))
        if not image_paths and model_only_route is None:
            quick_acted, quick_answer, quick_route = self._global_direct_turn(query)
        if not quick_acted and not image_paths and model_only_route is None:
            instant = self._instant_reply(query)
            if instant is not None:
                quick_acted, quick_answer, quick_route = True, instant, {"action": "instant", "model": "instant"}
        # Единый маршрутизатор. Он срабатывает только здесь — когда все быстрые и
        # надёжные обработчики выше уже отказались от хода. Именно в этой точке
        # запрос раньше уходил в планировщик без понимания области, и Эрви
        # терялась. Быстрые команды сюда не доходят, поэтому задержки нет.
        # Маршрутизатор нужен только когда никто выше не понял запрос. Если
        # открытие или еда уже отработали, повторная классификация — чистая
        # трата времени: ход до сюда всё равно не дойдёт.
        router_needed = (
            not quick_acted and not image_paths and self.request_router is not None
            and not workflow_pending and model_only_route is None
            and not bool(open_decision.get("open"))
            and not bool(food_decision.get("food"))
        )
        if router_needed:
            try:
                history = self.memory.history(conversation_id, limit=6)
                router_context = "\n".join(f"{r['role']}: {r['content']}" for r in history)[-800:]
            except Exception:
                router_context = ""
            try:
                verdict = self.request_router.classify(query, context=router_context)
            except Exception as exc:
                self._trace("ROUTER_FAILED", error=str(exc)[:200])
                verdict = {}
            domain = str(verdict.get("domain") or "")
            restated = str(verdict.get("restated") or query).strip() or query
            if domain:
                self._trace("CHAT_ROUTER", domain=domain,
                            confidence=verdict.get("confidence"), restated=restated[:120])
                # Переформулировка передаётся обработчику: он работает с чёткой
                # задачей, а не с исходной репликой вроде «сделай что-нибудь».
                handlers = {
                    "mail": lambda: self._mail_turn(restated, conversation_id),
                    "telegram": lambda: self._telegram_monitor_turn(restated),
                    "video": lambda: self._video_turn(restated, conversation_id),
                    "power": lambda: self._power_control_turn(restated, conversation_id),
                    "media": lambda: self._priority_control_turn(restated, conversation_id),
                    "phone": lambda: self._phone_guidance_turn(restated, conversation_id),
                    "screen": lambda: self._screen_brightness_turn(restated),
                    "code": lambda: self._code_file_turn(restated),
                }
                handler = handlers.get(domain)
                if handler is not None:
                    try:
                        acted, answer, route = handler()
                        if acted and str(answer or "").strip():
                            answer = self.enforce_gender(answer)
                            if persist_user:
                                self.memory.add_message(conversation_id, "user", query)
                            self.memory.add_message(conversation_id, "assistant", answer)
                            route = dict(route or {})
                            route["routed_domain"] = domain
                            yield {"type": "done", "conversation_id": conversation_id, "metrics": {"model": self.settings.fast_model, "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "answer": answer, "route": route}
                            return
                    except Exception as exc:
                        self._trace("ROUTED_HANDLER_FAILED", domain=domain, error=str(exc)[:200])
                # Область apps или files — открытие, у него свой контур.
                if domain in {"apps", "files"} and self.opener is not None:
                    try:
                        decision = self.opener.understand(restated)
                        if decision.get("open"):
                            opened = self.opener.perform(decision)
                            answer = self.enforce_gender(str(opened.get("message") or ""))
                            if answer:
                                if persist_user:
                                    self.memory.add_message(conversation_id, "user", query)
                                self.memory.add_message(conversation_id, "assistant", answer)
                                yield {"type": "done", "conversation_id": conversation_id, "metrics": {"model": self.settings.fast_model, "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "answer": answer, "route": {
                                    "action": "open_application", "routed_domain": domain,
                                    "completed": bool(opened.get("ok")),
                                    "verified": bool(opened.get("ok")),
                                }}
                                return
                    except Exception as exc:
                        self._trace("ROUTED_OPEN_FAILED", error=str(exc)[:200])
                # Непонятная просьба: спросить лучше, чем сделать наугад.
                if bool(verdict.get("needs_clarification")) and str(verdict.get("question") or "").strip():
                    question = self.enforce_gender(str(verdict["question"]).strip())
                    if persist_user:
                        self.memory.add_message(conversation_id, "user", query)
                    self.memory.add_message(conversation_id, "assistant", question)
                    yield {"type": "done", "conversation_id": conversation_id, "metrics": {"model": self.settings.fast_model, "total_seconds": round(time.monotonic() - request_started, 3)}, "stopped": False, "answer": question, "route": {
                        "action": "clarify", "routed_domain": domain, "needs_user": True,
                    }}
                    return

        parsed_now = detect_command(query) if model_only_route is None else None
        camera_now = bool(self.camera is not None and self.camera.status().get("running"))
        # In Spatial OS, verbs such as "выведи/покажи/прикрепи" mean render, never speak.
        if not quick_acted and model_only_route is None and self.settings.auto_route and camera_now and parsed_now is not None and parsed_now.action == "show":
            quick_acted, quick_answer, quick_route = self._deterministic_intent_turn(query, conversation_id, image_paths, attachment_paths)
        if not quick_acted and model_only_route is None:
            quick_acted, quick_answer, quick_route = self._fast_data_turn(query)
        if not quick_acted and model_only_route is None and self.settings.auto_route:
            quick_acted, quick_answer, quick_route = self._deterministic_intent_turn(query, conversation_id, image_paths, attachment_paths)
        if not quick_acted and model_only_route is None and self.settings.auto_route:
            quick_acted, quick_answer, quick_route = self._stateful_task_shortcut(query, conversation_id)
        if not quick_acted and model_only_route is None and self.settings.auto_route:
            quick_acted, quick_answer, quick_route = self._camera_fast_turn(query)
        if quick_acted:
            quick_answer = self.enforce_gender(quick_answer)
            parsed_intent = detect_command(query)
            if parsed_intent is not None and parsed_intent.mixed and quick_answer:
                try:
                    extra = self._fast_text_answer(
                        "Действие уже выполнено. Ответь только на разговорную/вопросительную часть одной короткой фразой. "
                        "Не обещай и не повторяй действие.\n"
                        f"Реплика: {query}\nРезультат: {quick_answer}", num_predict=96, timeout=3.5,
                    )
                    extra = self.enforce_gender(extra)
                    if extra and extra.casefold() not in quick_answer.casefold():
                        quick_answer = f"{quick_answer} {extra}"
                except Exception:
                    pass
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images": image_paths or [], "attachments": attachment_paths or []})
                if self.settings.auto_memory:
                    self.memory.remember_from_message(query)
            if quick_answer and persist_assistant:
                self.memory.add_message(conversation_id, "assistant", quick_answer, metadata={"model": quick_route.get("model", "deterministic"), "route": quick_route})
            yield {"type": "start", "conversation_id": conversation_id, "route": quick_route}
            if quick_answer:
                self._trace("CHAT_DIRECT_OUT", query=query, answer=quick_answer[:1600], route=quick_route, total_ms=round((time.monotonic()-request_started)*1000))
                yield {"type": "token", "content": quick_answer, "full": quick_answer}
            total_ms=(time.monotonic()-request_started)*1000
            if self.runtime is not None:
                try:
                    self.runtime.record_perf(str(quick_route.get("action") or "direct"), total_ms, model=quick_route.get("model","deterministic"))
                    self._finish_runtime(runtime_generation, quick_answer, ok=True)
                except Exception: pass
            yield {"type": "done", "conversation_id": conversation_id, "answer": quick_answer, "metrics": {"model": quick_route.get("model", "deterministic"), "total_seconds": round(total_ms/1000,3)}, "stopped": False, "route": quick_route}
            return

        generation_lock = self._conversation_lock(conversation_id)
        with generation_lock:
            if persist_user:
                self.memory.add_message(
                    conversation_id,
                    "user",
                    query,
                    metadata={"images": image_paths or [], "attachments": attachment_paths or []},
                )
                if self.settings.auto_memory:
                    self.memory.remember_from_message(query)

            route = self.router.chat_route(query, model)
            route = self.router.adapt_output_budget(query, route)
            if image_paths:
                route.model = self.router.task_model("vision")
                route.think = False
                route.reason = "Есть изображение: использую модель со зрением"
            stop_event = external_stop_event or threading.Event()
            with self._lock:
                previous = self._stop_events.get(conversation_id)
                if previous is not None and previous is not stop_event:
                    previous.set()
                self._stop_events[conversation_id] = stop_event

            yield {
                "type": "start",
                "conversation_id": conversation_id,
                "route": route.to_dict(),
            }
            instant = self._instant_reply(query) if not image_paths and model_only_route is None else None
            if instant is not None:
                if persist_assistant:
                    self.memory.add_message(
                        conversation_id,
                        "assistant",
                        instant,
                        metadata={"model": "instant", "metrics": {"total_seconds": 0.0}},
                    )
                yield {"type": "token", "content": instant, "full": instant}
                yield {
                    "type": "done",
                    "conversation_id": conversation_id,
                    "answer": instant,
                    "metrics": {"model": "instant", "total_seconds": 0.0},
                    "stopped": False,
                }
                with self._lock:
                    if self._stop_events.get(conversation_id) is stop_event:
                        self._stop_events.pop(conversation_id, None)
                return

            # The same EIRVEN gets a chance to act through universal tools even when a
            # file/image is attached. This is required for commands such as "publish this
            # photo"; the attachment is context for the action, not a reason to disable it.
            if self.settings.auto_route and model_only_route is None:
                acted, tool_answer, tool_route = self._stateful_task_shortcut(query, conversation_id)
                if not acted:
                    acted, tool_answer, tool_route = self._deterministic_intent_turn(query, conversation_id, image_paths, attachment_paths)
                if not acted:
                    acted, tool_answer, tool_route = self._camera_fast_turn(query)
                if not acted and self._needs_tool_turn(query):
                    acted, tool_answer, tool_route = self._tool_first_turn(
                        query, conversation_id, mode, stop_event, image_paths, attachment_paths
                    )
                # r14 universal current-screen recovery. This is the missing general lane:
                # arbitrary commands are attempted against the *real active UI* through
                # accessibility + a tiny text planner instead of dying at a template miss.
                if not acted and self._is_action_request(query) and getattr(self,"universal_workflow",None) is not None:
                    generic=self.universal_workflow.accessible_goal(query,max_steps=6,stop_event=stop_event)
                    if generic.get("ok"):
                        acted=True
                        tool_answer=self._self_gendered("Выполнила действие на текущем экране и проверила изменение интерфейса.","Выполнил действие на текущем экране и проверил изменение интерфейса.")
                        tool_route={"action":"universal_uia","model":"uia+fast-text","result":generic}
                # Desktop commands are never converted into a generic background task.
                # That old fallback was both slow and semantically dangerous: an "open"
                # command could arrive at a broad agent that chose file/project tools.
                # If the bounded fast executor cannot act, report the failure immediately
                # instead of pretending work has started. Project creation remains an
                # explicit project_create task and is unaffected.
                if not acted and self._is_action_request(query):
                    acted = True
                    tool_answer = self._self_gendered(
                        "Не нашла надёжный способ выполнить это действие на текущем экране. Ничего лишнего не запускала.",
                        "Не нашёл надёжный способ выполнить это действие на текущем экране. Ничего лишнего не запускал.",
                    ) + self._latest_failure_hint()
                    tool_route = {"action": "action_failed_fast", "model": self.settings.fast_model}
                if acted:
                    tool_answer = self.enforce_gender(tool_answer)
                    parsed_intent = detect_command(query)
                    if parsed_intent is not None and parsed_intent.mixed and tool_answer:
                        try:
                            extra = self._fast_text_answer(
                                "Действие уже выполнено. Не обещай и не повторяй действие. "
                                "Ответь только на разговорную/вопросительную часть реплики владельца одной короткой фразой.\n"
                                f"Реплика: {query}\nРезультат действия: {tool_answer}",
                                num_predict=110, timeout=4.5,
                            )
                            if extra and extra.casefold() not in tool_answer.casefold():
                                tool_answer = f"{tool_answer} {extra}"
                                tool_route = {**tool_route, "mixed_reply": True}
                        except Exception:
                            pass
                    if tool_answer and persist_assistant:
                        self.memory.add_message(
                            conversation_id, "assistant", tool_answer,
                            metadata={"model": tool_route.get("model", "tool"), "route": tool_route},
                        )
                    if tool_answer:
                        self._trace("CHAT_DIRECT_OUT", query=query, answer=tool_answer[:1600], route=tool_route, total_ms=round((time.monotonic()-request_started)*1000))
                        yield {"type": "token", "content": tool_answer, "full": tool_answer}
                    yield {
                        "type": "done", "conversation_id": conversation_id,
                        "answer": tool_answer, "metrics": {"model": tool_route.get("model", "tool")},
                        "stopped": bool(stop_event.is_set()), "route": tool_route,
                    }
                    with self._lock:
                        if self._stop_events.get(conversation_id) is stop_event:
                            self._stop_events.pop(conversation_id, None)
                    return

            messages = self._messages(
                conversation_id, mode, query, image_paths, attachment_paths,
                include_history=(
                    bool(model_semantic_decision.get("needs_history", True))
                    if model_only_enabled else True
                ),
            )
            # Do not unload the action checkpoint here.  Sending an empty ``keep_alive=0``
            # request to Ollama can briefly load that other model just to evict it again;
            # on the next sentence the conversational checkpoint then pays a 10-second
            # cold start.  Ollama already evicts least-recently-used models when memory
            # is tight, so leave residency to the backend and keep ordinary speech warm.
            answer = ""
            metrics: dict[str, Any] = {}
            try:
                answer, metrics = yield from self._stream_answer_with_continuations(
                    messages, route, stop_event,
                )
            except LLMError as exc:
                self._trace("CHAT_LLM_RECOVERED", query=query, model=route.model, error=str(exc)[:500])
                if not answer:
                    fallback = self.education.fallback_answer(query)
                    if fallback:
                        answer = fallback
                        self._trace(
                            "EDUCATION_FALLBACK",
                            query=query,
                            model=route.model,
                            error=str(exc)[:500],
                            status=self.education.status(),
                        )
                    else:
                        answer = "Контур ответа временно недоступен. Я сохраню контекст; повтори команду или перезапусти Эрви — ручная настройка не нужна."
                    yield {"type": "token", "content": answer, "full": answer}
            finally:
                with self._lock:
                    if self._stop_events.get(conversation_id) is stop_event:
                        self._stop_events.pop(conversation_id, None)

            if answer:
                answer = self.enforce_gender(answer)
                self._trace("CHAT_LLM_OUT", query=query, answer=answer[:1600], model=route.model, metrics=metrics, total_ms=round((time.monotonic()-request_started)*1000))
                metrics["stopped"] = bool(metrics.get("stopped") or stop_event.is_set())
                if persist_assistant:
                    self.memory.add_message(
                        conversation_id,
                        "assistant",
                        answer,
                        metadata={"model": route.model, "metrics": metrics},
                    )
                if metrics.get("total_seconds"):
                    self.db.add_performance_sample(
                        "chat",
                        route.model,
                        float(metrics["total_seconds"]),
                        int(metrics.get("prompt_tokens") or 0),
                        int(metrics.get("generated_tokens") or 0),
                    )
                if self.runtime is not None:
                    try:
                        total_ms=(time.monotonic()-request_started)*1000
                        self.runtime.record_perf("llm", total_ms, model=route.model, first_token_ms=round(float(metrics.get("first_token_seconds") or 0)*1000))
                        self._finish_runtime(runtime_generation, answer, ok=True)
                    except Exception: pass
                yield {
                    "type": "done",
                    "conversation_id": conversation_id,
                    "answer": answer,
                    "metrics": metrics,
                    "stopped": bool(stop_event.is_set()),
                }
            else:
                yield {
                    "type": "done",
                    "conversation_id": conversation_id,
                    "answer": "",
                    "metrics": {},
                    "stopped": bool(stop_event.is_set()),
                }

            if not stop_event.is_set():
                # Summarisation is useful memory maintenance, not foreground work. Delay it
                # so it cannot start a second Ollama generation while the voice is still
                # speaking the answer or the owner is issuing the next command.
                def _deferred_summary(cid=conversation_id):
                    time.sleep(30.0)
                    try:
                        if self.runtime is not None and self.runtime.status().get("cancellable"):
                            return
                    except Exception:
                        pass
                    self._maybe_summarize(cid)
                threading.Thread(target=_deferred_summary,daemon=True,name=f"summarize-{conversation_id[:8]}").start()

    def _emotion(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Вызвать метод эмоций, не рискуя ходом: эмоции — украшение, а не работа."""
        state = getattr(self, "emotions", None)
        if state is None:
            return
        try:
            getattr(state, method)(*args, **kwargs)
        except Exception as exc:
            self._trace("EMOTION_FAILED", method=method, error=str(exc)[:160])

    def stream_events(
        self,
        query: str,
        conversation_id: str | None = None,
        mode: str = "Друг",
        model: str | None = None,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
        *,
        persist_user: bool = True,
        external_stop_event: threading.Event | None = None,
        persist_assistant: bool = True,
    ) -> Generator[dict[str, Any], None, None]:
        """Own cancellation/runtime lifetime for every exit path of a foreground turn."""
        clean_query = str(query or "").strip()
        if not clean_query:
            yield {"type": "error", "message": "Введите сообщение"}
            return
        cid = self.memory.ensure_conversation(conversation_id, mode)

        with self._lock:
            previous = self._stop_events.get(cid)
            duplicate_resume = bool(
                is_resume_confirmation(clean_query)
                and previous is not None and not previous.is_set()
                and (
                    self._workflow_pending_owner(cid) is not None
                    or bool(getattr(self, "autonomous_workflow", None) and self.autonomous_workflow.has_pending(cid))
                )
            )
        if duplicate_resume:
            answer = "Уже продолжаю с сохранённого шага. Повторное «готов» не сбило задачу."
            route = {"action": "checkpoint_resume_in_progress", "model": "deterministic", "control_plane": True}
            yield {"type": "start", "conversation_id": cid, "route": route}
            yield {"type": "token", "content": answer, "full": answer}
            yield {"type": "done", "conversation_id": cid, "answer": answer, "metrics": {"model": "deterministic"}, "stopped": False, "route": route}
            return

        stop_event = external_stop_event or threading.Event()
        with self._lock:
            previous = self._stop_events.get(cid)
            if previous is not None and previous is not stop_event:
                previous.set()
            self._stop_events[cid] = stop_event

        runtime_generation: int | None = None
        if self.runtime is not None:
            try: runtime_generation = self.runtime.begin("turn", clean_query, lane="interactive", cancellable=True)
            except Exception: runtime_generation = None

        old_cid = getattr(self._turn_context, "conversation_id", "")
        self._turn_context.conversation_id = cid
        last_answer = ""
        done_seen = False
        done_ok = False
        active_route: dict[str, Any] = {}
        impl = None
        # Эмоция: пришло сообщение — Эрви думает. Вернётся в покой в finally при
        # любом исходе хода, даже при ошибке.
        self._emotion("begin_turn")
        emotion_done_route: dict[str, Any] = {}
        emotion_crashed = False
        try:
            # Assistant persistence is centralized below.  The inner engine may finish
            # after a newer turn supersedes it, so it is never allowed to commit a
            # response on its own.
            impl = self._stream_events_impl(
                clean_query, cid, mode, model,
                image_paths=image_paths, attachment_paths=attachment_paths,
                persist_user=persist_user, external_stop_event=stop_event,
                persist_assistant=False,
                _runtime_generation=runtime_generation,
            )
            for event in impl:
                if event.get("type") == "start":
                    active_route = dict(event.get("route") or {})
                    # Действие на компьютере — Эрви сосредоточена; разговор — думает дальше.
                    if str(active_route.get("action") or "chat") not in {"chat", "conversation", ""}:
                        self._emotion("working")
                elif event.get("type") == "bridge":
                    self._emotion("working")
                event_route = dict(event.get("route") or active_route)
                control_plane = bool(event_route.get("control_plane"))
                with self._lock:
                    still_owned = self._stop_events.get(cid) is stop_event
                if event.get("type") == "token":
                    last_answer = str(event.get("full") or last_answer)
                elif event.get("type") == "done":
                    done_seen = True
                    last_answer = str(event.get("answer") or last_answer)
                    route = dict(event.get("route") or {})
                    emotion_done_route = route
                    done_ok = bool(
                        not event.get("stopped")
                        and not route.get("error")
                        and not (route.get("completed") is True and route.get("verified") is False)
                    )
                    # Persist a genuine completion even if a newer turn for this same
                    # conversation has since taken ownership (a fast voice reply and a
                    # typed message -- or two quick messages -- can legitimately cross
                    # paths). The person already watched this answer stream in on
                    # screen; gating the save on live ownership here silently dropped
                    # it from the saved transcript on the next reload, which showed up
                    # almost only on short exchanges ("Привет", "Нет!") precisely
                    # because those finish fast enough for the next turn to start
                    # before this one reached its own "done" bookkeeping. A turn that
                    # was actually stopped/cancelled still must not be saved, and that
                    # is already covered by the stopped/control_plane check below --
                    # it does not depend on still_owned.
                    if persist_assistant and last_answer and (not event.get("stopped") or control_plane):
                        with self._lock:
                            try:
                                metrics = dict(event.get("metrics") or {})
                                self.memory.add_message(
                                    cid, "assistant", last_answer,
                                    metadata={
                                        "model": str(
                                            metrics.get("model")
                                            or route.get("model")
                                            or ""
                                        ),
                                        "metrics": metrics,
                                        "route": route,
                                    },
                                )
                            except Exception as exc:
                                self._trace(
                                    "CHAT_ASSISTANT_PERSIST_FAILED", conversation_id=cid,
                                    error=str(exc)[:300],
                                )
                if not still_owned or (stop_event.is_set() and not control_plane):
                    # Do not emit even a final token from an older turn.  Closing the
                    # inner generator also prevents its deferred maintenance path from
                    # running under the newer owner's context.  (A genuine "done" was
                    # already persisted above regardless of this ownership check.)
                    break
                yield event
        except GeneratorExit:
            stop_event.set()
            raise
        except Exception as exc:
            emotion_crashed = True
            stop_event.set()
            last_answer = f"Не удалось завершить задачу: {humanize(exc)}"
            self._trace("CHAT_UNHANDLED", query=clean_query, conversation_id=cid, error=str(exc)[:1000])
            yield {"type": "error", "message": last_answer, "conversation_id": cid}
        finally:
            _route_done = dict(emotion_done_route or active_route or {})
            self._emotion("end_turn", cid,
                          completed=bool(done_ok and (_route_done.get("verified") or _route_done.get("completed"))),
                          failed=bool((done_seen and not done_ok) or emotion_crashed))
            if impl is not None:
                try:
                    impl.close()
                except Exception:
                    pass
            with self._lock:
                if self._stop_events.get(cid) is stop_event:
                    self._stop_events.pop(cid, None)
            self._turn_context.conversation_id = old_cid
            if self.runtime is not None and runtime_generation is not None:
                try:
                    current = self.runtime.is_current(runtime_generation)
                    active = bool(self.runtime.status().get("cancellable")) if current else False
                except Exception:
                    current = active = False
                if current and active:
                    stopped = bool(stop_event.is_set())
                    result = last_answer or ("Остановлено пользователем" if stopped else "Ход задачи завершён")
                    if stopped:
                        # Cancellation is a terminal control-plane state, not a failed
                        # task.  Keep runtime idle/"Остановлено" instead of exposing
                        # an incorrect generic "Ошибка" after a user cancel.
                        try:
                            self.runtime.stop_interactive()
                        except Exception:
                            self._finish_runtime(runtime_generation, result, ok=False)
                    else:
                        self._finish_runtime(runtime_generation, result, ok=bool(done_seen and done_ok))

    def complete(
        self,
        query: str,
        conversation_id: str | None = None,
        mode: str = "Друг",
        model: str | None = None,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        final = ""
        cid = conversation_id or ""
        metrics: dict[str, Any] = {}
        route: dict[str, Any] = {}
        for event in self.stream_events(
            query, conversation_id, mode, model, image_paths=image_paths, attachment_paths=attachment_paths
        ):
            if event["type"] == "start":
                cid = event["conversation_id"]
                route = event["route"]
            elif event["type"] == "token":
                final = event["full"]
            elif event["type"] == "done":
                final = event["answer"]
                metrics = event.get("metrics") or {}
                if event.get("route"):
                    route = dict(event.get("route") or {})
            elif event["type"] == "error":
                final = event["message"]
        return {
            "answer": self.enforce_gender(final),
            "conversation_id": cid,
            "metrics": metrics,
            "route": route,
        }
