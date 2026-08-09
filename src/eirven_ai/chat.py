from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher
from collections.abc import Generator
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .config import Settings
from .database import Database
from .llm import LLMError, ModelGateway
from .identity import IdentityService
from .memory import MemoryStore
from .model_router import ModelRouter
from .style import StyleStore

from .attachments import extract_attachment_context
from .intent_engine import detect_command, detect_commands
from .trace import log_event
from .russian_speech import cardinal, time_phrase, date_phrase, russian_weather_condition
from .reliability_router import ReliabilityRouter

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
        self._stop_events: dict[str, threading.Event] = {}
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
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
                        item_context = f"--- видео {path.name} ---\nНе удалось извлечь кадры: {exc}. Оригинал доступен по пути {path}."
                self._media_cache[key] = (stat.st_mtime_ns, stat.st_size, item_context, frames)
                if item_context:
                    context.append(item_context)
                vision_frames.extend(frames)
            except Exception as exc:
                context.append(f"--- мультимедиа {path.name} ---\nЛокальный анализ не удался: {exc}")
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

    def _messages(
        self,
        conversation_id: str,
        mode: str,
        query: str,
        image_paths: list[str] | None = None,
        attachment_paths: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        style_prompt = self.style.get().prompt()
        memory_prompt = self.memory.prompt_context(query)
        summary = self.memory.get_summary(conversation_id)
        summary_prompt = (
            f"\n\nКраткое содержание старой части диалога:\n{summary['summary']}"
            if summary and summary.get("summary")
            else ""
        )
        task_prompt = self._task_context(conversation_id)
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
        assistant_name = assistant_identity.assistant_name if assistant_identity else "EIRVEN"
        assistant_gender = assistant_identity.gender if assistant_identity else "female"
        address = (assistant_identity.user_address if assistant_identity else "") or ""
        last_voice_emotion = str(self.db.get_setting("last_voice_emotion", "natural") or "natural")
        gender_rule = ("СТРОГО говори о себе только в женском роде во всех глаголах и прилагательных, в том числе без местоимения «я»: я сделала, открыла, нашла, готова, поняла, решила, проверила, была, рада, согласна; начала, закончила, сохранила. Никогда не используй о себе мужские формы." if assistant_gender == "female" else "СТРОГО говори о себе только в мужском роде: я сделал, открыл, нашёл, готов, понял, решил, проверил.")
        address_rule = f"Обращайся к владельцу как «{address}», когда это естественно. " if address else ""
        emotion_rule = f"Последняя голосовая подача владельца распознана как {last_voice_emotion}; учитывай это деликатно, без проговаривания метки. "
        commentary=str(getattr(assistant_identity,"action_commentary","adaptive") or "adaptive") if assistant_identity else "adaptive"
        mood=str(getattr(assistant_identity,"emotion_mode","auto") or "auto") if assistant_identity else "auto"
        presentation={
            "playful":"В подаче чаще используй короткий уместный юмор и живые реакции, без клоунады. ",
            "brief":"Отвечай максимально коротко и делово, без лишних комментариев. ",
            "off":"Не комментируй собственные действия сверх необходимого результата. ",
            "adaptive":"Подстраивай лексику, юмор и ритм под стиль владельца и текущий контекст. ",
        }.get(commentary,"")
        mood_rule={"warm":"Говори теплее и мягче. ","energetic":"Говори живее и энергичнее. ","calm":"Говори спокойно и размеренно. ","strict":"Говори собранно и серьёзно. ","quiet":"Говори мягко и ненавязчиво. "}.get(mood,"")
        system = (
            f"Твоё имя в этом приложении — {assistant_name}. Используй его естественно, только когда это уместно. {gender_rule} {address_rule}{emotion_rule}{presentation}{mood_rule}\n\n"
            f"{style_prompt}\n\n{MODE_PROMPTS.get(mode, MODE_PROMPTS['Друг'])}\n\n"
            f"{memory_prompt}{summary_prompt}{task_prompt}{project_file_prompt}{attachment_section}{camera_section}\n\n"
            "Память может быть неполной или устаревшей. Не выдавай предположения за факты. "
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
            "возможности инструментального слоя."
        )
        history = self.memory.history(conversation_id, limit=18)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(
            {"role": item["role"], "content": item["content"]} for item in history
        )
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
                        "properties": {"instructions": {"type": "string"}},
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
            {
                "type": "function",
                "function": {
                    "name": "set_assistant_name",
                    "description": "Изменить имя EIRVEN, когда владелец явно просит переименовать ассистента.",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
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

    @staticmethod
    def _is_action_request(query: str) -> bool:
        return bool(re.search(
            r"^\s*(?:пожалуйста[, ]+)?(?:открой|запусти|включи|вруби|выключи|отключи|закрой|"
            r"найди|нажми|кликни|введи|напиши|отправь|ответь|покажи|создай|сделай|удали|"
            r"перемести|скопируй|скачай|загрузи|опубликуй|поставь|сверни|разверни|измени|"
            r"переключи|проверь|прокрути|зайди)\w*\b", query, re.I
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
                "set_assistant_name", "task_status", "task_resume",
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
                return True, f"Камеру вижу, но быстрый анализ кадра не сработал: {exc}", {"action": "camera_scene_error", "model": self.settings.vision_model}
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
            task_id = self.tasks.enqueue(
                "project",
                f"Создать проект {project_name or 'по текущему запросу'}",
                {"name": project_name, "description": requirements, "overwrite": False},
                conversation_id=conversation_id,
            )
            return {"ok": True, "result": {"task_id": task_id, "kind": "project", "name": project_name, "status": "queued"}}
        if name == "project_modify":
            instructions = str(args.get("instructions") or "").strip()
            last = self._latest_project_task(conversation_id)
            if not last:
                return {"ok": False, "error": "В этом чате ещё нет проекта"}
            if last.get("status") in {"queued", "running"} and self.tasks.append_live_instruction(last["id"], instructions):
                return {"ok": True, "result": {"task_id": last["id"], "status": last.get("status"), "live_update": True}}
            if last.get("status") in {"failed", "cancelled"} and self.tasks.retry(last["id"]):
                return {"ok": True, "result": {"task_id": last["id"], "status": "queued", "continued": True}}
            project_name = str(
                (last.get("result") or {}).get("project_name")
                or (last.get("input") or {}).get("name")
                or ""
            ).strip()
            task_id = self.tasks.enqueue(
                "project_change",
                f"Изменить проект {project_name or 'текущий'}",
                {"name": project_name, "request": instructions},
                conversation_id=conversation_id,
            )
            return {"ok": True, "result": {"task_id": task_id, "kind": "project_change", "name": project_name, "status": "queued"}}
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
        if name == "set_assistant_name":
            if self.identity is None:
                return {"ok": False, "error": "Сервис личности недоступен"}
            value = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "", str(args.get("name") or ""))[:32]
            if len(value) < 2:
                return {"ok": False, "error": "Некорректное имя"}
            current = self.identity.get()
            self.identity.update({"assistant_name": value})
            return {"ok": True, "result": {"old_name": current.assistant_name, "assistant_name": value}}
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

    def _vision_for_path(self, path: str, question: str) -> str:
        """Fast disposable image lane for low-memory Windows laptops.

        r12/r13 could spend ~7 s on GPU and then ~22 s on a CPU retry. That made one
        image request stall the whole assistant and competed with ASR. r14 uses a small
        dedicated vision model once, with a strict deadline, and unloads it immediately.
        """
        started=time.monotonic()
        try:
            # On the owner's 4-GB GPU, 4B vision weights plus a 768px image are too
            # expensive.  A 512px bounded image is enough for ordinary attached-photo
            # understanding and dramatically lowers image-token/encoder pressure.
            encoded_images=self._encode_images([path],max_dim=512,quality=74)
            if not encoded_images:
                return "Не удалось подготовить изображение для анализа."
            try: installed=self.gateway.installed_models()
            except Exception: installed=[]
            lookup={str(name).casefold():str(name) for name in installed}
            # Prefer the smallest model that Ollama explicitly declares as vision-capable.
            # If server capability metadata is unavailable, Moondream is the known small
            # vision fallback.  Qwen3-VL 4B is last resort on this laptop, not first choice.
            candidates=[]
            for key in ("qwen3.5:0.8b","moondream:1.8b-v2-q4_0",str(self.settings.vision_model).casefold(),"qwen3-vl:4b-instruct","qwen3-vl:4b"):
                value=lookup.get(key)
                if value and value not in candidates: candidates.append(value)
            model=""
            for candidate in candidates:
                caps=set(self.gateway.model_capabilities(candidate))
                low=candidate.casefold()
                if "vision" in caps or "moondream" in low or "qwen3-vl" in low:
                    model=candidate; break
            model=model or str(self.settings.vision_model)
            # Release chat/deep weights before the one-shot VLM request. On a 4-GB GPU
            # this is more important than trying a bigger model after a failure.
            for resident in installed:
                if str(resident).casefold()==model.casefold():
                    continue
                if any(k in str(resident).casefold() for k in ("gemma","qwen","gpt-oss","devstral","moondream")):
                    try:self.gateway.unload(resident)
                    except Exception:pass
            messages=[
                {"role":"system","content":"Ты зрение EIRVEN. Изображение реально приложено. Отвечай по-русски коротко и только по тому, что видно; не выдумывай."},
                {"role":"user","content":question or "Опиши изображение.","images":encoded_images},
            ]
            try:
                response=self.gateway.chat(
                    messages,model=model,temperature=.05,think=False,
                    num_ctx=640,num_predict=96,keep_alive="0",timeout_seconds=11.0,
                )
                answer=str(response.get("content") or "").strip()
                if answer:
                    self._trace("VISION_RESULT",model=model,execution="oneshot",path=str(path),answer=answer[:900],ms=round((time.monotonic()-started)*1000))
                    return answer
                raise RuntimeError("пустой ответ vision-модели")
            except Exception as exc:
                self._trace("VISION_ERROR",model=model,execution="oneshot",error=str(exc)[:1200],ms=round((time.monotonic()-started)*1000))
                return "Не смогла быстро распознать изображение. Vision-контур остановлен, чтобы не подвешивать остальные команды."
            finally:
                try:self.gateway.unload(model)
                except Exception:pass
        except Exception as exc:
            return f"Не удалось проанализировать изображение: {exc}"

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
            pattern=rf"(^|[.!?]\s+)({male})\b"
            out=re.sub(pattern,lambda m: m.group(1)+female,out,flags=re.I)
        return out

    def _fast_data_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        clean=query.casefold().replace("ё","е").strip(" .,!?")
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
        pm=re.search(r"\b(?:в|через)\s+(телеграм(?:е|м)?|telegram|тг)\b",text,re.I)
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
            "тиме":"тима","тиму":"тима",
            "кириллу":"кирилл","илье":"илья",
            "артему":"артем","артёму":"артём",
            "диме":"дима","саше":"саша",
            "павлу":"павел","даниилу":"даниил",
        }
        recipient=recipient_alias.get(recipient.casefold(),recipient)
        return recipient,platform,message

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
        prompt=(
            "Сформулируй ОДНО короткое сообщение-ответ для Telegram. Это не инструкция и не объяснение. "
            "Ответь на последнее сообщение собеседника по смыслу и имитируй стиль владельца по его примерам: длину, регистр, пунктуацию, эмодзи и разговорность. "
            "Не добавляй кавычки, имя адресата и служебные слова. Если контекста мало — выбери естественный нейтральный ответ.\n\n"
            f"ПОСЛЕДНИЕ СООБЩЕНИЯ СОБЕСЕДНИКА:\n"+"\n".join(peer[-6:])+"\n\n"
            f"ПРИМЕРЫ СТИЛЯ ВЛАДЕЛЬЦА:\n"+"\n".join(mine[-8:])
        )
        try:
            # Before a semantic reply, the general chat model has priority; do not keep
            # the action model resident beside it on a 32-GB/old Xeon laptop.
            try:
                action_model=getattr(getattr(self,"universal_workflow",None),"_planner_model",lambda:"qwen3:1.7b")()
                if action_model and action_model!=self.settings.fast_model: self.gateway.unload(action_model)
            except Exception: pass
            try: installed=self.gateway.installed_models()
            except Exception: installed=[]
            style_model=next((m for m in installed if str(m).casefold()=="qwen3.5:0.8b"),self.settings.fast_model)
            # Keep only the small drafting lane resident.  Long chat history is truncated
            # above; a 0.8B model is sufficient for a one-message style imitation and avoids
            # the repeated 8-second cold timeout of qwen3.5:2b on this CPU.
            for resident in installed:
                if str(resident).casefold()==str(style_model).casefold(): continue
                if any(k in str(resident).casefold() for k in ("qwen","gemma","gpt-oss","devstral","moondream")):
                    try:self.gateway.unload(resident)
                    except Exception:pass
            compact_peer=[x[-260:] for x in peer[-4:]]
            compact_mine=[x[-220:] for x in mine[-6:]]
            prompt=(
                "Напиши ОДНО короткое Telegram-сообщение в стиле автора примеров. "
                "Ответь по смыслу на последнее сообщение собеседника. Только готовый текст, без кавычек и объяснений.\n"
                "СОБЕСЕДНИК:\n"+"\n".join(compact_peer)+"\nМОЙ СТИЛЬ:\n"+"\n".join(compact_mine)
            )
            response=self.gateway.chat([{"role":"system","content":"Верни только текст сообщения."},{"role":"user","content":prompt}],model=style_model,temperature=.32,think=False,num_ctx=900,num_predict=64,keep_alive="0",timeout_seconds=10.0)
            message=str(response.get("content") or "").strip().strip('«»"\'')
        except Exception as exc:
            return True,f"Не успела сформулировать ответ в твоём стиле: {exc}",{"action":"telegram_style_model_failed","model":locals().get("style_model",self.settings.fast_model)}
        if not message or "в моем стиле" in self._norm(message) or "в моём стиле" in message.casefold():
            return True,"Не получила нормальный текст ответа; ничего буквально не отправляю.",{"action":"telegram_style_guard","model":self.settings.fast_model}
        target_name=recipient or str(ctx.get("recipient") or "текущий чат")
        # Current active chat is already verified by telegram_thread_context; type once here
        # through the focused-composer path to avoid re-searching the contact.
        workflow=getattr(self,"universal_workflow",None)
        if workflow is None:
            return True,"Текст подготовила, но контур отправки недоступен.",{"action":"telegram_style_draft","draft":message}
        result=workflow._current_window_text_fastpath(f'в текущем окне напиши текст «{message}» и отправь',text=message)
        completed=bool(result and result.get("completed")); verified=bool(result and result.get("verified"))
        return True,(f"Ответила в твоём стиле: «{message}»." if completed else f"Подготовила: «{message}», но отправить не удалось."),{"action":"telegram_style_reply","model":locals().get("style_model",self.settings.fast_model),"completed":completed,"verified":verified,"draft":message,"result":result,"recipient":target_name}

    def _telegram_send_turn(self, target: str) -> tuple[bool,str,dict[str,Any]]:
        recipient,platform,message=self._parse_send_target(target)
        if not platform:
            self.db.set_setting("pending_send", {"recipient":recipient,"platform":"","message":message,"at":time.time()})
            return True,"В каком мессенджере отправить сообщение? Например, в Telegram.",{"action":"send_need_platform","model":"deterministic"}
        if not recipient:
            self.db.set_setting("pending_send", {"recipient":"","platform":platform,"message":message,"at":time.time()})
            return True,"Кому именно отправить сообщение?",{"action":"send_need_recipient","model":"deterministic"}
        if not message:
            self.db.set_setting("pending_send", {"recipient":recipient,"platform":platform,"message":"","at":time.time()})
            return True,f"Что написать {recipient}?",{"action":"send_need_text","model":"deterministic"}
        self.db.set_setting("pending_send", {})
        if self.app_skills is None:
            return True,"Экранный навык Telegram сейчас недоступен.",{"action":"send_unavailable","model":"deterministic"}
        # Personal context: a remembered phrase such as "мама в Telegram записана как
        # Мамуля" resolves the visible chat name without another LLM call.
        if recipient.casefold() in {"мама","папа","брат","сестра"}:
            try:
                rows=self.memory.search(f"{recipient} telegram записан как",limit=6)
                for row in rows:
                    content=str(row.get("content") or "")
                    m=re.search(rf"{re.escape(recipient)}.*?(?:telegram|телеграм).*?(?:как|имя|контакт)\s+[«\"']?([^«»\"'.,;]{{2,48}})",content,re.I)
                    if m:
                        recipient=m.group(1).strip(); break
            except Exception:
                pass
        started=time.monotonic()
        result=self.app_skills.send_telegram(recipient,message)
        if result.get("ok") and result.get("verified"):
            return True,self._self_gendered(f"Отправила {recipient}: «{message}».",f"Отправил {recipient}: «{message}»."),{"action":"telegram_send_verified","model":"screen-operator","result":result,"ms":round((time.monotonic()-started)*1000)}
        if result.get("ok"):
            return True,f"Сообщение ввела и отправила, но не смогла надёжно подтвердить его появление в чате {recipient}.",{"action":"telegram_send_unverified","model":"screen-operator","result":result}
        return True,f"Не удалось отправить сообщение через видимый экран: {result.get('error') or 'не нашла нужный элемент'}. Telegram уже открыла для восстановления.",{"action":"telegram_send_failed","model":"screen-operator","result":result}

    def _tool_result_answer(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        if not result.get("ok"):
            return f"Не получилось выполнить действие: {result.get('error') or 'неизвестная ошибка'}"
        inner = result.get("result") or {}
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
            base = self._self_gendered("Готово — переключилась на нужное окно.", "Готово — переключился на нужное окно.")
        elif name == "set_assistant_name":
            base = f"Готово. Теперь я {inner.get('assistant_name', '')}.".strip()
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
            base = "Готово."
        identity = self.identity.get() if self.identity else None
        mode = str(getattr(identity, "action_commentary", "adaptive") or "adaptive")
        if mode in {"off", "brief"}:
            return base
        humor = str(getattr(self.style.get(), "humor", "") or "").casefold()
        if mode == "adaptive" and any(marker in humor for marker in ("без юмора", "никак", "нет")):
            return base
        # Deterministic micro-comment: zero extra LLM latency and no repeated canned line.
        quips = [
            "Без церемоний — уже сделано.",
            "Компьютер сопротивлялся примерно ноль секунд.",
            "Ещё одна мелочь снята с твоих рук.",
            "Нормально, едем дальше.",
        ]
        index = sum(ord(ch) for ch in (name + str(args))) % len(quips)
        return f"{base} {quips[index]}"


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

    def _fast_text_answer(self, prompt: str, *, num_predict: int = 320, timeout: float = 8.0) -> str:
        installed = {m.lower(): m for m in self.gateway.installed_models()}
        model = installed.get(self.settings.fast_model.lower()) or installed.get("gemma3:4b") or installed.get("qwen3.5:2b") or self.settings.fast_model
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
                    pieces.append(f"Не удалось завершить текстовый анализ вложений: {exc}")
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
            r"(?:пожалуйста\s+)?(?:(?:выключи|закрой|заверши|останови)\s+(?:себя|эрви|eirven)(?:\s+полностью)?|"
            r"выключись|отключись|закройся|заверши\s+работу)[.! ]*",
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

    def _r22_capabilities_answer(self) -> str:
        return (
            "Я умею управлять окнами и приложениями, работать с сайтами по видимому интерфейсу, "
            "искать и открывать нужные разделы, управлять медиа и системными функциями, работать "
            "с файлами и Telegram и выполнять составные задачи между приложениями. Если действие "
            "можно сделать детерминированно, я не должна ждать локальную модель."
        )

    def _r22_open_external(self, target: str) -> tuple[bool, str, dict[str, Any]]:
        target=str(target or "").strip()
        try:
            applications=getattr(getattr(self.app_skills,"services",None),"applications",None)
            if applications is None:
                raise RuntimeError("resolver unavailable")
            result=dict(applications.web_fallback(target) or {})
            completed=bool(result.get("url"))
            return True,(f"Открыла {target}." if completed else f"Не смогла открыть {target}."),{
                "action":"r22_external_open","model":"deterministic+official-resolver","control_plane":True,
                "completed":completed,"verified":completed,"target":target,"result":result,
            }
        except Exception as exc:
            return True,f"Не смогла открыть {target}: {exc}.",{
                "action":"r22_external_open","model":"deterministic+official-resolver","control_plane":True,
                "completed":False,"verified":False,"target":target,"error":str(exc),
            }

    def _r22_app_compound_turn(self, query: str, app: str, remainder: str) -> tuple[bool, str, dict[str, Any]]:
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
            m=re.search(r"\b(?:напиши|отправь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,60})\s+(.+)$", remainder, re.I)
            if m:
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
                recipient=m.group(1).strip(); message=m.group(2).strip().strip('«»"\'')
                acted,answer,route=self._telegram_send_turn(f"{recipient} в telegram сообщение {message}")
                route={**route,"r22_surface_open":opened}
                return acted,answer,route
        try:
            opened=dict(app_skills.open(app) or {})
        except Exception as exc:
            return True,f"Не смогла открыть нужное приложение: {exc}.",{"action":"r22_app_compound","completed":False,"verified":False,"error":str(exc)}
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
        # Explicitly arbitrary content is a safe UIA choice and should never require Qwen.
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
            try: result=dict(app_skills.play_music() or {})
            except Exception as exc: result={"ok":False,"verified":False,"error":str(exc)}
            verified=bool(result.get("ok") and result.get("verified"))
            return True,("Включила музыку." if verified else f"Яндекс Музыку открыла, но воспроизведение не подтвердилось: {result.get('error') or 'нет Play'}."),{
                "action":"r22_app_compound","model":"deterministic+uia","control_plane":True,"app":app,"opened":opened,"result":result,"completed":bool(result.get('ok')),"verified":verified,
            }
        # The surface ownership is already corrected.  Do not guess the tail with a wrong
        # foreground handler; let the structured mission layer own anything more complex.
        return False,"",{}

    def _priority_control_turn(self, query: str, conversation_id: str) -> tuple[bool, str, dict[str, Any]]:
        """Small deterministic control plane that owns only capabilities it can verify."""
        clean = " ".join(str(query or "").casefold().replace("ё", "е").split())
        tokens = re.findall(r"[a-zа-я0-9]+", clean)

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
            if self.runtime:
                self.runtime.stop_interactive()
            workflow = getattr(self, "universal_workflow", None)
            if workflow is not None:
                try: workflow._clear_pending(conversation_id)
                except Exception: pass
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

        # r22 front-door arbitration: decide ownership before any legacy app/media/page
        # shortcut.  This prevents the current foreground surface from hijacking a command
        # that explicitly names a different app or external entity.
        arbiter=getattr(self,"reliability_router",None) or ReliabilityRouter()
        decision=arbiter.classify(query)
        try: self._trace("R22_ARBITRATE",query=query,decision=decision.to_dict())
        except Exception: pass
        if decision.kind == "capabilities":
            return True,self._r22_capabilities_answer(),{"action":"r22_capabilities","model":"deterministic","control_plane":True,"completed":True,"verified":True}
        if decision.kind == "app_open" and getattr(self,"app_skills",None) is not None:
            try:
                result=dict(self.app_skills.open(decision.app) or {})
                ok=bool(result.get("ok")); verified=bool(result.get("verified",ok))
                shown={"telegram":"Telegram","yandex_music":"Яндекс Музыку","youtube":"YouTube","spotify":"Spotify","discord":"Discord"}.get(decision.app,decision.app)
                return True,(f"Открыла {shown}." if ok else f"Не смогла открыть {shown}: {result.get('error') or 'не найдено'}."),{"action":"r22_app_open","model":"deterministic","control_plane":True,"completed":ok,"verified":verified,"app":decision.app,"result":result}
            except Exception as exc:
                return True,f"Не смогла открыть приложение: {exc}.",{"action":"r22_app_open","model":"deterministic","control_plane":True,"completed":False,"verified":False,"error":str(exc)}
        if decision.kind == "app_compound":
            acted,answer,route=self._r22_app_compound_turn(query,decision.app,decision.remainder)
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
        # universal planning so 'открой Telegram и напиши Кириллу...' cannot wait on Qwen.
        foreground_title = ""
        try:
            fg = self.tools.execute("foreground_window", {}) if self.tools else {}
            foreground_title = str((fg.get("result") or {}).get("title") or "") if fg.get("ok") else ""
        except Exception:
            pass
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
                return True, (f"Открыла {shown}." if ok else f"Не смогла открыть {shown}: {result.get('error') or 'не найдено'}."), {"action":"open_app_priority","model":"deterministic","control_plane":True,"completed":ok,"verified":bool(result.get("verified", ok)),"result":result}
            except Exception as exc:
                return True, f"Не смогла открыть приложение: {exc}.", {"action":"open_app_priority","model":"deterministic","control_plane":True,"completed":False,"verified":False,"error":str(exc)}

        # OS volume is an atomic global primitive. Do not spend a Qwen turn on
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
            label = {"down":"Уменьшила системную громкость.", "up":"Увеличила системную громкость.", "mute":"Переключила системный mute."}[volume_action]
            return True, (label if ok else f"Не смогла изменить системную громкость: {result.get('error') or 'ошибка Windows'}."), {
                "action":"system_volume_priority","model":"deterministic","control_plane":True,
                "verified":ok,"completed":ok,"result":result,
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
        domain_match = re.match(r"^(?:открой\s+)+(https?://\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)[.!]?\s*$", original_query, re.I)
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
                return True, f"Не смогла открыть сайт {target}: {exc}.", {
                    "action":"open_site_priority","model":"deterministic","control_plane":True,
                    "verified":False,"completed":False,"error":str(exc),
                }

        telegram_context = bool(re.search(r"\b(?:telegram|телеграм|телеграмм|тг)\b", clean) or re.search(r"telegram|телеграм", foreground_title, re.I))

        # 'в моём стиле' describes HOW to compose a reply; it is never literal message text.
        if telegram_context and re.search(r"\b(?:напиши|ответь|отправь)\w*", clean) and re.search(r"\bв\s+мо[её]м\s+стиле\b", clean):
            recipient_match=re.search(r"(?:напиши|ответь|отправь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,50})", str(query or ""), re.I)
            rec=recipient_match.group(1).strip() if recipient_match and " ".join(recipient_match.group(1).casefold().replace("ё","е").split()) not in {"сообщение","текст"} else ""
            acted,answer,route=self._telegram_style_reply(recipient=rec)
            return acted,answer,{**route,"control_plane":True}

        if telegram_context and re.search(r"\b(?:напиши|отправь|введи|набери)\w*", clean):
            original = str(query or "").strip()
            m = re.search(r"(?:напиши|отправь)\w*\s+([A-Za-zА-Яа-яЁё0-9_@.-]{2,50})\s+(?:(?:сообщение|текст)\s*)?[:\-]?\s*[«\"']?(.+?)[»\"']?[.!]?\s*$", original, re.I)
            if m:
                recipient, message = m.group(1).strip(), m.group(2).strip().strip('«»"\'')
                if recipient and message:
                    acted, answer, route = self._telegram_send_turn(f"{recipient} в telegram сообщение {message}")
                    return acted, answer, {**route, "control_plane": True}
            # Current open Telegram chat: 'напиши привет' means type+send once here.
            m2 = re.search(r"(?:напиши|набери|введи)\w*\s+(?:просто\s+)?(?:текст\s+)?[«\"']?(.+?)[»\"']?[.!]?\s*$", original, re.I)
            if m2 and workflow is not None and not re.search(r"\b(?:кому|кирилл|тимоф|маме|папе)\w*", clean):
                payload = m2.group(1).strip().strip('«»"\'')
                if payload:
                    focused = workflow._current_window_text_fastpath(f'в текущем окне напиши текст «{payload}» и отправь', text=payload)
                    if focused and focused.get("completed"):
                        verified = bool(focused.get("verified"))
                        return True, ("Сообщение отправила." if verified else "Сообщение отправила один раз; повторять не стала."), {"action":"telegram_current_send","model":"uia","control_plane":True,"completed":True,"verified":verified,"result":focused}

        # Search/navigation inside the current web app is UI work, not conversational Qwen.
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
            # route sent "поставь лайк музыки" to qwen3:1.7b despite a visible Button
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
                    return True, f"Не смогла поставить лайк: {exc}.", {
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

        # User music owns 'включи музыку' before a generic enable workflow. Existing
        # Yandex Music skill already knows how to reuse the current default-browser tab.
        if re.search(r"\b(?:включи|запусти)\w*\s+(?:яндекс\s+)?музык\w*", clean) and self.app_skills is not None:
            self.db.set_setting("neuro_music_suspended", True)
            try: result = self.app_skills.play_music()
            except Exception as exc: result = {"ok":False,"verified":False,"error":str(exc)}
            verified = bool(result.get("ok") and result.get("verified"))
            return True, ("Включила музыку и подтвердила воспроизведение." if verified else f"Яндекс Музыку открыла, но воспроизведение не подтвердилось: {result.get('error') or 'кнопка Play не найдена'}."), {"action":"music_priority","model":"screen-operator","control_plane":True,"verified":verified,"completed":bool(result.get("ok")),"result":result}

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

    def _global_direct_turn(self, query: str) -> tuple[bool, str, dict[str, Any]]:
        clean=" ".join(query.casefold().replace("ё","е").split())
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
        # Natural clarification memory for send workflows. The old code forgot the
        # recipient/text immediately after asking "в каком мессенджере?".
        pending=self.db.get_setting("pending_send", {})
        if isinstance(pending,dict) and pending and (time.time()-float(pending.get("at") or 0)) < 180:
            p_rec=str(pending.get("recipient") or "").strip(); p_platform=str(pending.get("platform") or "").strip(); p_msg=str(pending.get("message") or "").strip()
            if not p_platform and re.search(r"\b(?:телеграм\w*|telegram|тг)\b",clean,re.I):
                p_platform="telegram"
            if not p_msg and not detect_command(query) and len(query.strip())>1 and not re.search(r"\b(?:телеграм\w*|telegram|тг)\b",clean,re.I):
                p_msg=query.strip().strip('«»"')
            if p_rec and p_platform and p_msg:
                self.db.set_setting("pending_send", {})
                return self._telegram_send_turn(f"{p_rec} в telegram сообщение {p_msg}")
            if p_rec or p_platform or p_msg:
                self.db.set_setting("pending_send", {"recipient":p_rec,"platform":p_platform,"message":p_msg,"at":time.time()})

        m=re.match(r"^\s*запомни(?:,| что)?\s+(.+)$",query,re.I)
        if m:
            mid=self.memory.add(m.group(1).strip(),kind="user_fact",importance=5)
            return True,"Запомнила.",{"action":"memory_add","model":"deterministic","memory_id":mid}
        if re.match(r"^\s*(?:что ты обо мне помнишь|что ты помнишь обо мне)\s*[?!.]*$",query,re.I):
            rows=self.memory.search("пользователь предпочтения факты",limit=8)
            items=[str(x.get("content") or "") for x in rows if str(x.get("content") or "").strip()]
            return True,("Помню: "+"; ".join(items[:6]) if items else "Пока долговременных фактов о тебе почти нет."),{"action":"memory_list","model":"deterministic"}
        m=re.match(r"^\s*забудь(?:,|\s+что)?\s+(.+)$",query,re.I)
        if m:
            subject=m.group(1).strip()
            rows=self.memory.search(subject,limit=5)
            if not rows:
                return True,"Не нашла подходящего факта в долговременной памяти.",{"action":"memory_forget","model":"deterministic","deleted":0}
            deleted=0
            for row in rows[:3]:
                content=str(row.get("content") or "").casefold()
                if subject.casefold() in content or any(tok in content for tok in re.findall(r"[а-яёa-z0-9]{4,}",subject.casefold())):
                    if self.memory.delete(int(row.get("id") or 0)): deleted+=1
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
            problem=intent.target.strip() or query.strip()
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
                    return True,f"Локальный поиск не сработал: {exc}",{"action":"find_local_failed","model":"deterministic"}
            try:
                opened=self.tools.browser.search_first_site(intent.target,open_visible=True) if self.tools.browser else {}
                return True,f"Нашла подходящий сайт и сразу открыла его: {opened.get('title') or intent.target}.",{"action":"search_open_result","model":"deterministic","result":opened}
            except Exception as exc:
                return True,f"Не удалось найти и открыть подходящий сайт: {exc}",{"action":"search_open_failed","model":"deterministic"}

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
                    answer = f"Камера работает, но анализ кадра не удался: {exc}"
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
                    return True, f"Курс сейчас получить не удалось: {exc}", {"action":"spatial_rate_failed","model":"deterministic"}
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
                    return True,f"Не удалось вывести сайт пространственным окном: {exc}",{"action":"spatial_browser_failed","model":"deterministic"}
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
                            return True,f"Не удалось вывести {title} поверх камеры: {exc}",{"action":"spatial_browser_failed","model":"deterministic"}
            if re.search(r"\b(?:поиск приложений|поиск программ|меню пуск|пуск)\b", target):
                try:
                    result=self.tools.applications.open_windows_search("") if self.tools.applications else {}
                    return True,"Открыла поиск приложений Windows.",{"action":"windows_app_search","model":"deterministic","result":result}
                except Exception as exc:
                    return True,f"Не удалось открыть поиск Windows: {exc}",{"action":"windows_app_search_failed","model":"deterministic"}
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
                    return True,f"Папку не нашла: {exc}",{"action":"open_local_folder_failed","model":"deterministic"}
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
                return True, f"Приложение «{intent.target}» не найдено, и веб-версию открыть не удалось: {exc}", {"action": "app_missing", "model": "deterministic"}

        if intent.action == "close":
            if re.search(r"\b(?:все|всё|всё запущенное|приложения|процессы)\b", target) and not re.search(r"\b(?:одно|это|текущее)\b", target):
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
            if re.search(r"(?:нейро|фонов|ambient).*музык|музык.*(?:нейро|фонов|ambient)", target):
                self.db.set_setting("neuro_music_suspended", not enabled)
                return True, "Нейромузыку включила." if enabled else "Нейромузыку выключила.", {"action": "ambient_music", "model": "deterministic", "enabled": enabled}
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
                # User music takes precedence over the ambient layer and keeps it paused
                # until the owner explicitly enables neuro-music again.
                self.db.set_setting("neuro_music_suspended", True)
                ambient = getattr(self, "_ambient", None)
                if enabled:
                    if re.search(r"\b(?:на (?:этом|текущем) экране|на этой странице|не открывай|без открытия|здесь)\b", query.casefold()) and getattr(self,"desktop_operator",None) is not None:
                        if self.desktop_operator.click_current(["Воспроизведение","Play","Воспроизвести","play_filled"],goal="yandex_play_current"):
                            return True,"Нажала воспроизведение прямо на текущем экране.",{"action":"music_current_screen","model":"deterministic"}
                    if self.app_skills is not None:
                        result=self.app_skills.play_music()
                        if result.get("ok") and result.get("verified"):
                            return True,"Включила твою волну в Яндекс Музыке. Нейромузыку приглушила.",{"action":"music_verified","model":"screen-operator","result":result}
                        return True,f"Яндекс Музыку открыла, но воспроизведение не подтвердилось: {result.get('error') or 'кнопку не удалось надёжно нажать'}. Оставила страницу перед тобой.",{"action":"music_recovery","model":"screen-operator","result":result}
                return True, "Музыку выключила. Нейромузыку сама не возобновляю — включишь её отдельно, когда захочешь.", {"action": "music_off", "model": "deterministic"}
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
        task_id = self.tasks.enqueue(
            "project_change",
            f"Изменить проект {project_name or 'текущий'}",
            {"name": project_name, "request": text},
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
        fast_model = installed.get(self.settings.fast_model.lower()) or installed.get("qwen3.5:2b") or installed.get("qwen3:1.7b") or self.router.agent_model(query)

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
                r"опубликуй|поставь|закрой|сверни|разверни|измени|переключи|проверь)\w*\b",
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
                        "task_resume", "set_assistant_name", "git_publish",
                    }:
                        return True, self._tool_result_answer(name, args, result), {"action": "tool", "tools": used, "model": fast_model}
            return True, self._self_gendered("Выполнила доступные шаги. Если интерфейс изменился, продолжу от нового состояния.", "Выполнил доступные шаги. Если интерфейс изменился, продолжу от нового состояния."), {"action": "tool", "tools": used, "model": fast_model}

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
        normalized = query.strip().lower().strip(" !?.,")
        # Wake-name ASR is allowed to be fuzzy.  A harmless "Привет, Эрли/Эрби" must
        # not miss the instant path and cold-load a chat model for 7+ seconds.
        greeting = re.fullmatch(r"(привет|здравствуй|здравствуйте)[,;:]?\s+[a-zа-яё0-9_-]{2,24}", normalized, re.I)
        if greeting:
            normalized = greeting.group(1).lower()
        identity = self.identity.get() if self.identity else None
        name = identity.assistant_name if identity else "EIRVEN"
        address = (identity.user_address if identity else "") or "бро"
        commentary=str(getattr(identity,"action_commentary","adaptive") or "adaptive") if identity else "adaptive"
        mood=str(getattr(identity,"emotion_mode","auto") or "auto") if identity else "auto"
        how_are_you=(f"Нормально, {address}. Готова работать." if (identity is None or identity.gender == "female") else f"Нормально, {address}. Готов работать.")
        if commentary=="playful":
            how_are_you=(f"Нормально, {address}. Не дымлюсь — уже успех. Готова работать." if (identity is None or identity.gender == "female") else f"Нормально, {address}. Не дымлюсь — уже успех. Готов работать.")
        elif commentary in {"brief","off"}:
            how_are_you="Нормально. Готова." if (identity is None or identity.gender == "female") else "Нормально. Готов."
        elif mood=="warm":
            how_are_you=(f"Нормально, {address}. Рада тебя слышать. Готова работать." if (identity is None or identity.gender == "female") else f"Нормально, {address}. Рад тебя слышать. Готов работать.")
        replies = {
            "привет": (f"Привет, {address}. Я тут — и пока ничего не сломала." if commentary=="playful" and (identity is None or identity.gender=="female") else f"Привет, {address}. Я тут."),
            "здравствуй": f"Привет, {address}. Я тут.",
            "здравствуйте": f"Привет, {address}. Я на связи.",
            "ты тут": f"Да, {address}. Я здесь.",
            "как дела": how_are_you,
            "как ты": "В рабочем режиме. Всё слушаю.",
            "как настроение": "Нормальное. Особенно когда команды реально выполняются.",
            "как жизнь": "Живу локально, работаю быстро. В целом неплохо.",
            "что делаешь": "Слушаю тебя и слежу, чтобы старые ответы не догоняли новые.",
            "готов": "Готова." if (identity is None or identity.gender == "female") else "Готов.",
            "спасибо": f"Без проблем, {address}.",
            "ок": "Принято.",
            "понял": "Отлично.",
            "ясно": "Ага.",
            "пока": f"Давай, {address}. {name} останется в фоне.",
        }
        if normalized in {"как тебя зовут", "как твое имя", "как твоё имя", "кто ты"}:
            return f"Я {name}."
        if normalized in {"что ты умеешь", "что умеешь", "что ты можешь", "какие у тебя возможности"}:
            return self._r22_capabilities_answer()
        if normalized in {"который час", "сколько времени", "который сейчас час", "сколько сейчас времени"}:
            return time_phrase()
        if normalized in {"какое сегодня число", "какая сегодня дата", "сегодня какое число"}:
            return date_phrase()
        return replies.get(normalized)

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
        query = query.strip()
        request_started = time.monotonic()
        runtime_generation = None
        if self.runtime is not None:
            try: runtime_generation = self.runtime.begin("turn", query, lane="interactive", cancellable=True)
            except Exception: runtime_generation = None
        self._trace("CHAT_IN", query=query, conversation_id=conversation_id or "", mode=mode, images=len(image_paths or []), attachments=len(attachment_paths or []))
        if not query:
            yield {"type": "error", "message": "Введите сообщение"}
            return
        conversation_id = self.memory.ensure_conversation(conversation_id, mode)

        # Voice and text share the same attachment memory. If the owner explicitly says
        # "the attached files/image" after uploading, recover the recent local files even
        # when this turn itself carries no attachment IDs.
        if not attachment_paths and re.search(
            r"\b(?:прикрепл|вложен|загружен|файл(?:ы|а|ов)?|архив(?:ы|а|ов)?|картинк|изображен|фото)\w*",
            query, re.I,
        ):
            attachment_paths = self._recent_attachment_paths(conversation_id)
        # r15.10: user image attachments were removed. Keep generic document/audio/video
        # attachments, but never route an uploaded image into a VLM. Screen-grounding
        # vision remains available to the desktop operator as an internal fallback.
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        attachment_paths = [p for p in (attachment_paths or []) if Path(p).suffix.casefold() not in image_exts]
        image_paths = []

        # Legacy branch remains unreachable because image_paths is intentionally empty. This avoids the
        # general chat router accidentally loading a 4B VLM on a 4 GB GPU.
        non_image_attachments=[p for p in (attachment_paths or []) if Path(p).suffix.casefold() not in {".png",".jpg",".jpeg",".webp",".gif",".bmp"}]
        if image_paths and not non_image_attachments:
            self.stop(conversation_id)
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
                try:self.runtime.finish(answer,ok=not answer.startswith("Не удалось"))
                except Exception:pass
            return

        # Cancellation is scoped to this conversation. A new turn invalidates the old one.
        self.stop(conversation_id)

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
                    try: self.runtime.finish(priority_answer, ok=not bool(priority_route.get("completed") and not priority_route.get("verified", True)))
                    except Exception: pass
                yield {"type": "done", "conversation_id": conversation_id, "answer": priority_answer, "metrics": {"model": "deterministic", "total_seconds": round(time.monotonic()-request_started, 3)}, "stopped": priority_route.get("action") == "cancel_interactive", "route": priority_route}
                return

        # Stateful task continuity (including a live r19 graph correction) must win over
        # creating a brand-new mission. This lets "и ещё ..." revise the active graph.
        if not image_paths:
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

        # r19 long-horizon missions run in TaskManager's dedicated fast lane. They are
        # persistent and may cross applications; the live chat remains free while the
        # mission progresses in the background. Single-surface r18 actions stay inline.
        mission_engine = getattr(self, "mission_engine", None)
        if mission_engine is not None and not image_paths and mission_engine.should_handle(query):
            context_snapshot = mission_engine.capture_context()
            task_id = self.tasks.enqueue(
                "mission",
                f"Миссия: {query[:140]}",
                {"goal": query, "context": context_snapshot},
                conversation_id=conversation_id,
            )
            answer = "Приняла миссию. Выполняю её в фоне по сохранённому графу; можешь давать мне другие задачи параллельно."
            route = {"action":"mission_created","model":"r19-task-graph","engine":"r19","task_id":task_id,"kind":"mission"}
            if persist_user:
                self.memory.add_message(conversation_id, "user", query, metadata={"images":[], "attachments":attachment_paths or []})
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", answer, metadata={"model":"r19-task-graph","route":route})
            self._trace("CHAT_MISSION_CREATED", query=query, task_id=task_id, context=context_snapshot, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type":"start","conversation_id":conversation_id,"route":route}
            yield {"type":"token","content":answer,"full":answer}
            yield {"type":"done","conversation_id":conversation_id,"answer":answer,"metrics":{"model":"r19-task-graph","total_seconds":round(time.monotonic()-request_started,3)},"stopped":False,"route":route}
            if self.runtime is not None:
                try: self.runtime.finish(answer, ok=True)
                except Exception: pass
            return

        # Existing project/task continuity has priority over starting a new generic
        # desktop workflow. This is state resolution, not an application template: an
        # explicit "продолжай" after a failed/running project resumes the same task.
        autonomous_engine = getattr(self, "autonomous_workflow", None)
        workflow_engine = getattr(self, "universal_workflow", None)
        autonomous_pending = bool(autonomous_engine and conversation_id and autonomous_engine.has_pending(conversation_id))
        workflow_pending = bool(workflow_engine and conversation_id and workflow_engine.has_pending(conversation_id))
        if not autonomous_pending and not workflow_pending:
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
        if autonomous_engine is not None and not image_paths and autonomous_engine.should_handle(query, conversation_id):
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
            final = self.enforce_gender(result.summary)
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", final, metadata={"model": "state-policy+uia+tools", "route": {**route, "results": result.steps, "needs_user": result.needs_user}})
            self._trace("CHAT_AUTONOMOUS_OUT", query=query, ok=result.ok, summary=final, results=result.steps, needs_user=result.needs_user, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type": "token", "content": final, "full": final}
            yield {"type": "done", "conversation_id": conversation_id, "answer": final, "metrics": {"model": "state-policy+uia+tools", "total_seconds": round(time.monotonic()-request_started,3)}, "stopped": bool(workflow_stop.is_set()), "route": {**route, "results": result.steps, "needs_user": result.needs_user}}
            if self.runtime is not None:
                try: self.runtime.finish(final, ok=(result.ok or result.needs_user))
                except Exception: pass
            with self._lock:
                if self._stop_events.get(conversation_id) is workflow_stop:
                    self._stop_events.pop(conversation_id, None)
            return

        # r15 desktop-agent core. Any real action (not only a known template or a
        # multi-verb sentence) is handled by the stateful universal workflow. Direct
        # adapters are only accelerators inside it; a template miss falls through to
        # model planning + live UI/system tools instead of returning action_failed_fast.
        if workflow_engine is not None and not image_paths and workflow_engine.should_handle(query, conversation_id):
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
                lambda clause: self._deterministic_intent_turn(clause, conversation_id, [], attachment_paths),
                conversation_id=conversation_id,
                stop_event=workflow_stop,
            )
            final = self.enforce_gender(result.summary)
            if persist_assistant:
                self.memory.add_message(conversation_id, "assistant", final, metadata={"model": "planner+uia+tools", "route": {**route, "results": result.steps, "needs_user": result.needs_user}})
            self._trace("CHAT_WORKFLOW_OUT", query=query, ok=result.ok, summary=final, results=result.steps, needs_user=result.needs_user, total_ms=round((time.monotonic()-request_started)*1000))
            yield {"type": "token", "content": final, "full": final}
            yield {"type": "done", "conversation_id": conversation_id, "answer": final, "metrics": {"model": "planner+uia+tools", "total_seconds": round(time.monotonic()-request_started,3)}, "stopped": bool(workflow_stop.is_set()), "route": {**route, "results": result.steps, "needs_user": result.needs_user}}
            if self.runtime is not None:
                try: self.runtime.finish(final, ok=(result.ok or result.needs_user))
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
        if not image_paths:
            quick_acted, quick_answer, quick_route = self._global_direct_turn(query)
        if not quick_acted and not image_paths:
            instant = self._instant_reply(query)
            if instant is not None:
                quick_acted, quick_answer, quick_route = True, instant, {"action": "instant", "model": "instant"}
        parsed_now = detect_command(query)
        camera_now = bool(self.camera is not None and self.camera.status().get("running"))
        # In Spatial OS, verbs such as "выведи/покажи/прикрепи" mean render, never speak.
        if not quick_acted and self.settings.auto_route and camera_now and parsed_now is not None and parsed_now.action == "show":
            quick_acted, quick_answer, quick_route = self._deterministic_intent_turn(query, conversation_id, image_paths, attachment_paths)
        if not quick_acted:
            quick_acted, quick_answer, quick_route = self._fast_data_turn(query)
        if not quick_acted and self.settings.auto_route:
            quick_acted, quick_answer, quick_route = self._deterministic_intent_turn(query, conversation_id, image_paths, attachment_paths)
        if not quick_acted and self.settings.auto_route:
            quick_acted, quick_answer, quick_route = self._stateful_task_shortcut(query, conversation_id)
        if not quick_acted and self.settings.auto_route:
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
                    self.runtime.finish(quick_answer,ok=True)
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
            instant = self._instant_reply(query) if not image_paths else None
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
            if self.settings.auto_route:
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
                    )
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

            messages = self._messages(conversation_id, mode, query, image_paths, attachment_paths)
            # Keep only the model needed for the foreground conversation. The 1.7B
            # desktop planner used to remain resident beside the chat model and Ollama
            # reached ~5-6 GB on the owner's laptop. Unknown GUI tasks may load it again,
            # but ordinary conversation never pays for both models at once.
            try:
                wf=getattr(self,"universal_workflow",None)
                action_model=wf._planner_model() if wf is not None else ""
                if action_model and action_model != route.model:
                    self.gateway.unload(action_model)
            except Exception:
                pass
            answer = ""
            try:
                for chunk in self.gateway.stream_chat(
                    messages,
                    model=route.model,
                    temperature=route.temperature,
                    think=route.think,
                    num_ctx=route.num_ctx,
                    num_predict=route.num_predict,
                    stop_event=stop_event,
                    timeout_seconds=7.5 if route.model == self.settings.fast_model else 16,
                ):
                    answer += chunk
                    yield {"type": "token", "content": chunk, "full": answer}
            except LLMError as exc:
                yield {"type": "error", "message": f"Ошибка локальной модели: {exc}"}
            finally:
                with self._lock:
                    if self._stop_events.get(conversation_id) is stop_event:
                        self._stop_events.pop(conversation_id, None)

            if answer:
                answer = self.enforce_gender(answer)
                metrics = self.gateway.last_metrics.to_dict() if self.gateway.last_metrics else {}
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
                        self.runtime.finish(answer,ok=True)
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
