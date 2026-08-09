from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sys
import time
import threading
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .database import utc_now
from .services import Services
from .style import StyleDNA


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    mode: str = "Друг"
    model: str | None = "auto"
    image_ids: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    auto_execute: bool = True
    voice_mode: bool = False
    request_id: str | None = None


class ConversationRequest(BaseModel):
    title: str = "Новый чат"
    mode: str = "Друг"


class RenameConversationRequest(BaseModel):
    title: str


class MemoryRequest(BaseModel):
    content: str
    kind: str = "fact"
    person: str = ""
    importance: int = Field(default=3, ge=1, le=5)


class SocialRequest(BaseModel):
    dialogue: str = ""
    goal: str = ""
    person_name: str = ""
    person_context: str = ""
    image_id: str | None = None
    model: str | None = None


class ProjectTaskRequest(BaseModel):
    name: str = ""
    description: str
    overwrite: bool = False
    conversation_id: str | None = None


class AgentTaskRequest(BaseModel):
    task: str
    conversation_id: str | None = None


class SpeakRequest(BaseModel):
    text: str
    mode: str | None = None
    emotion: str | None = None
    voice_key: str | None = None


class IdentityRequest(BaseModel):
    assistant_name: str | None = None
    user_address: str | None = None
    gender: str | None = None
    voice_key: str | None = None
    avatar: str | None = None
    custom_avatar_path: str | None = None
    accent_color: str | None = None
    voice_mode: str | None = None
    emotion_mode: str | None = None
    background_enabled: bool | None = None
    desktop_avatar_enabled: bool | None = None
    desktop_avatar_size: int | None = None
    desktop_avatar_opacity: float | None = None
    game_control_enabled: bool | None = None
    creative_backend: str | None = None
    action_commentary: str | None = None
    ambient_music_enabled: bool | None = None
    ambient_music_volume: float | None = None
    speech_speed: float | None = None
    strict_wake_name: bool | None = None
    onboarding_completed: bool | None = None


class PreferencesRequest(BaseModel):
    voice_output_volume: float | None = Field(default=None, ge=0.0, le=1.0)
    language: str | None = None
    autostart: bool | None = None
    notifications_enabled: bool | None = None
    mini_mode: bool | None = None
    sphere_motion: bool | None = None
    sphere_intensity: str | None = None
    desktop_comments_enabled: bool | None = None
    desktop_eyes_enabled: bool | None = None
    update_channel: str | None = None
    auto_update_check: bool | None = None


class CameraPointerRequest(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ApplicationLaunchRequest(BaseModel):
    application: str


class GameTaskRequest(BaseModel):
    goal: str
    max_minutes: int = Field(default=15, ge=1, le=120)
    conversation_id: str | None = None


class TelegramRulesRequest(BaseModel):
    rules: list[dict[str, Any]]


class TelegramConfigRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str


class TelegramRemoteRequest(BaseModel):
    enabled: bool = False
    chats: list[str] = Field(default_factory=list)
    prefix: str = "Эрви,"


class TelegramLoginRequest(BaseModel):
    code: str
    password: str = ""


class SettingRequest(BaseModel):
    value: Any


class CameraDescribeRequest(BaseModel):
    prompt: str = "Что ты видишь перед камерой? Опиши кратко и конкретно."


class ProactiveSettingsRequest(BaseModel):
    enabled: bool | None = None
    media_minutes: int | None = Field(default=None, ge=1, le=240)


def _sse(data: dict[str, Any], event: str | None = None) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    encoded = json.dumps(data, ensure_ascii=False, default=str)
    lines.append(f"data: {encoded}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def build_api(services: Services) -> FastAPI:
    app = FastAPI(
        title="EIRVEN AI Local API",
        version="1.4.0",
        description="Локальный voice-first персональный ИИ с очередью задач и управлением компьютером",
    )
    upload_dir = services.settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    @app.middleware("http")
    async def disable_stale_local_ui_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            # Every EIRVEN release is served from the same localhost URL. Browsers may
            # otherwise keep an old app.js and make a new backend look broken.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def attachment_path(attachment_id: str) -> Path:
        with services.db.connect() as conn:
            row = conn.execute(
                "SELECT path FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")
        path = Path(row["path"]).resolve()
        if upload_dir.resolve() not in path.parents:
            raise HTTPException(status_code=403, detail="Некорректный путь файла")
        return path

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    @app.get("/api/ping")
    def ping() -> dict[str, str]:
        # Lightweight startup probe: never calls Ollama, Telegram, voice or hardware.
        return {"app": "eirven", "status": "ok", "version": "1.4.0"}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "app": "ok",
            "version": "1.4.0",
            "llm": services.gateway.health(),
            "hardware": services.hardware.to_dict(),
            "workspace": str(services.settings.workspace_dir),
            "browser_ready": services.browser.available(),
            "desktop_control": services.settings.enable_desktop_control,
            "access": services.tools.tool_access_status(),
            "semantic_memory": services.settings.semantic_memory,
            "voice": services.voice.status(),
            "native_voice": services.voice_daemon.status() if services.voice_daemon is not None else {"running": False, "state": "disabled"},
            "telegram": services.telegram.status(),
            "identity": services.identity.get().to_dict(),
            "companion": services.companion.status(),
            "game_control": services.settings.enable_game_control,
            "creative": services.creative.health(),
            "modes": services.modes.status() if services.modes is not None else {},
        }

    @app.get("/api/hardware")
    def hardware() -> dict[str, Any]:
        return services.hardware.to_dict()

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        return {
            "models": services.gateway.models(),
            "details": services.gateway.model_details(),
            "recommended": {
                "fast": services.hardware.recommended_fast_model,
                "main": services.hardware.recommended_main_model,
                "code": services.hardware.recommended_code_model,
                "vision": services.hardware.recommended_vision_model,
            },
        }

    @app.post("/api/models/{model_name:path}/warm")
    def warm_model(model_name: str) -> dict[str, Any]:
        services.gateway.warm(model_name)
        return {"ok": True, "model": model_name}

    @app.get("/api/conversations")
    def conversations(limit: int = 100) -> list[dict[str, Any]]:
        return services.memory.list_conversations(limit)

    @app.post("/api/conversations")
    def create_conversation(request: ConversationRequest) -> dict[str, Any]:
        conversation_id = services.memory.create_conversation(request.mode, request.title)
        return services.memory.conversation(conversation_id) or {"id": conversation_id}

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str) -> dict[str, Any]:
        conversation = services.memory.conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Чат не найден")
        conversation["messages"] = services.memory.history(
            conversation_id, limit=None, include_metadata=True
        )
        return conversation

    @app.put("/api/conversations/{conversation_id}")
    def rename_conversation(
        conversation_id: str, request: RenameConversationRequest
    ) -> dict[str, bool]:
        return {"updated": services.memory.rename_conversation(conversation_id, request.title)}

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str) -> dict[str, bool]:
        return {"deleted": services.memory.delete_conversation(conversation_id)}

    def resolve_attachments(attachment_ids: list[str]) -> list[str]:
        paths: list[str] = []
        for attachment_id in attachment_ids[:16]:
            path = attachment_path(attachment_id)
            if path.is_file():
                paths.append(str(path))
        return paths

    def request_files(request: ChatRequest) -> tuple[list[str], list[str]]:
        # image_ids remains for backward compatibility. attachment_ids is the new generic path.
        ids = list(dict.fromkeys([*request.attachment_ids, *request.image_ids]))[:16]
        all_paths = resolve_attachments(ids)
        non_images = [
            path for path in all_paths
            if not ((mimetypes.guess_type(path)[0] or "").startswith("image/")
                    or Path(path).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
        ]
        return [], non_images

    @app.post("/api/chat/jobs")
    def create_chat_job(request: ChatRequest) -> dict[str, Any]:
        if request.request_id:
            existing = services.chat_jobs.get_by_client_request_id(request.request_id)
            if existing:
                return existing
        image_paths, attachment_paths = request_files(request)
        return services.chat_jobs.enqueue(
            message=request.message,
            conversation_id=request.conversation_id,
            mode=request.mode,
            model=request.model,
            image_paths=image_paths,
            attachment_paths=attachment_paths,
            voice_mode=request.voice_mode,
            supersede_same_chat=True,
            client_request_id=request.request_id,
        )

    @app.get("/api/chat/jobs")
    def active_chat_jobs(conversation_id: str | None = None) -> list[dict[str, Any]]:
        return services.chat_jobs.list_active(conversation_id)

    @app.get("/api/chat/jobs/{job_id}")
    def get_chat_job(job_id: str) -> dict[str, Any]:
        job = services.chat_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Ответ не найден")
        return job

    @app.post("/api/chat/jobs/{job_id}/cancel")
    def cancel_chat_job(job_id: str) -> dict[str, bool]:
        return {"cancelled": services.chat_jobs.cancel(job_id)}

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> dict[str, Any]:
        image_paths, attachment_paths = request_files(request)
        result = services.chat.complete(
            request.message,
            request.conversation_id,
            request.mode,
            request.model,
            image_paths=image_paths,
            attachment_paths=attachment_paths,
        )
        route = dict(result.get("route") or {})
        if route.get("action"):
            result["action"] = route.get("action")
        if route.get("task_id"):
            result["task_id"] = route.get("task_id")
        return result

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        image_paths, attachment_paths = request_files(request)
        def event_stream():
            for event in services.chat.stream_events(
                request.message,
                request.conversation_id,
                request.mode,
                request.model,
                image_paths=image_paths,
                attachment_paths=attachment_paths,
            ):
                yield _sse(event)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/{conversation_id}/stop")
    def stop_chat(conversation_id: str) -> dict[str, bool]:
        return {"stopped": services.chat.stop(conversation_id)}

    @app.post("/api/uploads")
    async def upload(
        file: UploadFile = File(...), conversation_id: str | None = Form(default=None)
    ) -> dict[str, Any]:
        original = Path(file.filename or "file.bin").name
        suffix = Path(original).suffix.lower()[:20]
        data = await file.read()
        if len(data) > 100_000_000:
            raise HTTPException(status_code=413, detail="Максимальный размер одного вложения — 100 МБ")
        media_type = file.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream"
        if media_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            raise HTTPException(status_code=415, detail="Прикрепление изображений отключено в этой сборке")
        attachment_id = secrets.token_hex(16)
        safe_stem = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", Path(original).stem).strip(" ._")[:72] or "file"
        path = upload_dir / f"{attachment_id}-{safe_stem}{suffix}"
        path.write_bytes(data)
        with services.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO attachments(
                    id, conversation_id, path, media_type, original_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    conversation_id,
                    str(path),
                    media_type,
                    original,
                    utc_now(),
                ),
            )
        return {
            "id": attachment_id,
            "name": original,
            "media_type": media_type,
            "size": len(data),
            "url": f"/api/uploads/{attachment_id}",
        }

    @app.get("/api/uploads/{attachment_id}")
    def get_upload(attachment_id: str) -> FileResponse:
        path = attachment_path(attachment_id)
        return FileResponse(path)

    @app.post("/api/voice/transcribe")
    async def transcribe_voice(audio: UploadFile = File(...)) -> dict[str, str]:
        data = await audio.read()
        if len(data) > 30_000_000:
            raise HTTPException(status_code=413, detail="Аудио слишком большое")
        suffix = Path(audio.filename or "speech.webm").suffix or ".webm"
        try:
            return {"text": await run_in_threadpool(services.voice.transcribe_bytes, data, suffix)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/voice/runtime")
    def voice_runtime() -> dict[str, Any]:
        return services.voice_daemon.status() if services.voice_daemon is not None else {"running": False, "state": "disabled"}

    @app.post("/api/voice/runtime/restart")
    def restart_voice_runtime() -> dict[str, Any]:
        if services.voice_daemon is None:
            raise HTTPException(status_code=503, detail="Фоновый голосовой контур недоступен")
        services.voice_daemon.stop()
        services.voice_daemon.start()
        return services.voice_daemon.status()

    @app.get("/api/camera")
    def camera_status() -> dict[str, Any]:
        if services.camera is None:
            return {"running": False, "available": False, "error": "Камерный режим удалён"}
        return services.camera.status()

    @app.post("/api/camera/start")
    def camera_start() -> dict[str, Any]:
        if services.camera is None:
            raise HTTPException(status_code=410, detail="Камерный режим удалён")
        return services.camera.start()

    @app.post("/api/camera/stop")
    def camera_stop() -> dict[str, Any]:
        if services.camera is None:
            return {"running": False}
        status=services.camera.stop()
        if getattr(services,"runtime",None) is not None:
            threading.Thread(target=services.runtime.reset_after_camera,daemon=True,name="eirven-camera-api-reset").start()
        return status

    @app.get("/api/camera/stream")
    def camera_stream() -> StreamingResponse:
        if services.camera is None:
            raise HTTPException(status_code=410, detail="Камерный режим удалён")
        if not services.camera.status().get("running"):
            services.camera.start()
        return StreamingResponse(
            services.camera.mjpeg(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/spatial/browser")
    def spatial_browser_status() -> dict[str, Any]:
        state=dict(services.browser.spatial_state())
        layout=services.db.get_setting("spatial_browser_layout", {})
        if isinstance(layout,dict): state.update(layout)
        return state

    @app.get("/api/spatial/browser/frame")
    def spatial_browser_frame() -> Response:
        try:
            data = services.browser.frame()
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @app.get("/api/spatial/widgets")
    def spatial_widgets() -> list[dict[str, Any]]:
        items = services.db.get_setting("spatial_widgets", [])
        rows = [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        changed = False
        now = time.time()
        for item in rows:
            if item.get("kind") == "rate" and str(item.get("currency") or "").upper() == "USD":
                if now - float(item.get("updated_at") or 0) >= 60:
                    try:
                        rate = services.browser.currency_rate("USD")
                        value = float(rate.get("rub") or 0)
                        item["value"] = value
                        item["body"] = f"1 USD = {value:.2f} RUB · {rate.get('date') or ''}".strip(" ·")
                        item["source"] = rate.get("source") or ""
                        item["updated_at"] = now
                        changed = True
                    except Exception:
                        item["updated_at"] = now
                        changed = True
        if changed:
            services.db.set_setting("spatial_widgets", rows)
        return rows

    @app.delete("/api/spatial/widgets/{widget_id}")
    def spatial_widget_delete(widget_id: str) -> dict[str, bool]:
        items = services.db.get_setting("spatial_widgets", [])
        if not isinstance(items, list):
            items = []
        kept = [item for item in items if not isinstance(item, dict) or str(item.get("id") or "") != widget_id]
        services.db.set_setting("spatial_widgets", kept)
        return {"deleted": len(kept) != len(items)}

    @app.post("/api/camera/pointer")
    def camera_pointer(request: CameraPointerRequest) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Камерный режим удалён")


    @app.post("/api/camera/describe")
    async def camera_describe(request: CameraDescribeRequest) -> dict[str, str]:
        if services.camera is None:
            raise HTTPException(status_code=410, detail="Камерный режим удалён")
        try:
            text = await run_in_threadpool(services.camera.describe, request.prompt)
            return {"answer": text}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/camera/click")
    def camera_click(request: CameraPointerRequest) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Камерный режим удалён")


    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        registry=getattr(services,"capabilities",None)
        return registry.refresh() if registry is not None else {}

    @app.get("/api/runtime")
    def runtime_status() -> dict[str, Any]:
        runtime=getattr(services,"runtime",None)
        return runtime.status() if runtime is not None else {"action":"idle"}

    @app.get("/api/performance")
    def performance() -> list[dict[str, Any]]:
        runtime=getattr(services,"runtime",None)
        return runtime.performance(60) if runtime is not None else []

    @app.post("/api/command/stop-all")
    def command_stop_all() -> dict[str, Any]:
        runtime=getattr(services,"runtime",None)
        return runtime.stop_all() if runtime is not None else {"cancelled":0}

    @app.get("/api/selftest")
    def selftest() -> dict[str, Any]:
        return services.db.get_setting("startup_selftest", {})

    @app.post("/api/selftest")
    async def run_selftest() -> dict[str, Any]:
        test=getattr(services,"selftest",None)
        return await run_in_threadpool(test.run) if test is not None else {}

    @app.get("/api/modes")
    def mode_status() -> dict[str, Any]:
        return services.modes.status() if services.modes is not None else {}

    @app.put("/api/settings/proactive")
    def proactive_settings(request: ProactiveSettingsRequest) -> dict[str, Any]:
        if request.enabled is not None:
            services.db.set_setting("proactive_enabled", bool(request.enabled))
        if request.media_minutes is not None:
            services.db.set_setting("proactive_media_minutes", int(request.media_minutes))
        return services.modes.status() if services.modes is not None else {}

    @app.post("/api/voice/speak")
    async def speak(request: SpeakRequest) -> FileResponse:
        try:
            path = await run_in_threadpool(
                services.voice.synthesize,
                request.text,
                mode=request.mode,
                emotion=request.emotion,
                voice_key=request.voice_key,
            )
            return FileResponse(path, media_type="audio/wav", filename=Path(path).name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks")
    def tasks(limit: int = 100) -> list[dict[str, Any]]:
        return services.tasks.list(limit)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        task = services.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        task["events"] = services.tasks.events(task_id)
        return task

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return services.tasks.events(task_id, after_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, bool]:
        return {"cancelled": services.tasks.cancel(task_id)}

    @app.post("/api/projects")
    def project_task(request: ProjectTaskRequest) -> dict[str, str]:
        project_name = services.projects.clean_name(request.name)
        task_id = services.tasks.enqueue(
            "project",
            f"Создать проект {project_name}",
            {
                "name": project_name,
                "description": request.description,
                "overwrite": request.overwrite,
            },
            conversation_id=request.conversation_id,
        )
        return {"task_id": task_id}

    @app.post("/api/agent")
    def agent_task(request: AgentTaskRequest) -> dict[str, str]:
        task_id = services.tasks.enqueue(
            "agent",
            "Задача на компьютере",
            {"task": request.task},
            conversation_id=request.conversation_id,
        )
        return {"task_id": task_id}

    @app.post("/api/social")
    def social(request: SocialRequest) -> dict[str, str]:
        try:
            image_path = str(attachment_path(request.image_id)) if request.image_id else None
            answer = services.social.analyze(
                request.dialogue,
                request.goal,
                request.person_name,
                request.person_context,
                image_path,
                request.model,
            )
            return {"answer": answer}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/memory")
    def list_memory(limit: int = 100) -> list[dict[str, Any]]:
        return services.memory.list(limit=max(1, min(limit, 500)))

    @app.post("/api/memory")
    def add_memory(request: MemoryRequest) -> dict[str, int]:
        return {
            "id": services.memory.add(
                request.content, request.kind, request.person, request.importance
            )
        }

    @app.delete("/api/memory/{memory_id}")
    def delete_memory(memory_id: int) -> dict[str, bool]:
        return {"deleted": services.memory.delete(memory_id)}

    @app.get("/api/style")
    def get_style() -> dict[str, Any]:
        return services.style.get().to_dict()

    @app.put("/api/style")
    def update_style(style: StyleDNA) -> dict[str, Any]:
        return services.style.save(style).to_dict()

    @app.get("/api/identity")
    def get_identity() -> dict[str, Any]:
        return services.identity.get().to_dict()

    @app.put("/api/identity")
    def update_identity(request: IdentityRequest) -> dict[str, Any]:
        values = request.model_dump(exclude_none=True)
        identity = services.identity.update(values)
        # Keep the conversational style identity in sync with the simplified profile UI.
        if "assistant_name" in values or "user_address" in values:
            try:
                style = services.style.get()
                style.assistant_name = identity.assistant_name
                style.owner_name = identity.user_address
                if identity.user_address:
                    style.preferred_address = identity.user_address
                services.style.save(style)
            except Exception:
                pass
        # The living sphere is the persistent entry point and remains visible during
        # onboarding, while voice acceptance itself is blocked until onboarding completes.
        services.companion.start()
        services.companion.show()
        return identity.to_dict()

    @app.put("/api/identity/avatar/{attachment_id}")
    def set_custom_avatar(attachment_id: str) -> dict[str, Any]:
        path = attachment_path(attachment_id)
        media_type = mimetypes.guess_type(path.name)[0] or ""
        if not media_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Для персонажа нужно изображение")
        identity = services.identity.update({"custom_avatar_path": str(path)})
        services.companion.stop()
        services.companion.start()
        return identity.to_dict()

    def _autostart_shortcut() -> Path:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "EIRVEN AI.lnk"
        return services.settings.root_dir / "data" / ".autostart-unavailable"

    def _autostart_enabled() -> bool:
        try:
            return _autostart_shortcut().is_file()
        except Exception:
            return bool(services.db.get_setting("autostart_enabled", True))

    def _set_autostart(enabled: bool) -> bool:
        services.db.set_setting("autostart_enabled", bool(enabled))
        if os.name != "nt":
            return bool(enabled)
        shortcut = _autostart_shortcut()
        if enabled:
            script = services.settings.root_dir / "scripts" / "install_autostart.ps1"
            if not script.is_file():
                raise RuntimeError("Скрипт автозапуска не найден")
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=str(services.settings.root_dir), capture_output=True, text=True, timeout=20)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "Не удалось включить автозапуск").strip()[-500:])
        else:
            shortcut.unlink(missing_ok=True)
        return _autostart_enabled()

    def _preferences_payload() -> dict[str, Any]:
        identity = services.identity.get()
        return {
            "voice_output_volume": float(services.db.get_setting("voice_output_volume", 0.82)),
            "language": str(services.db.get_setting("ui_language", "ru") or "ru"),
            "autostart": _autostart_enabled(),
            "notifications_enabled": bool(services.db.get_setting("notifications_enabled", True)),
            "mini_mode": bool(identity.desktop_avatar_enabled),
            "sphere_motion": bool(services.db.get_setting("sphere_motion", True)),
            "sphere_intensity": str(services.db.get_setting("sphere_intensity", "vivid") or "vivid"),
            "desktop_comments_enabled": bool(services.db.get_setting("desktop_comments_enabled", True)),
            "desktop_eyes_enabled": bool(services.db.get_setting("desktop_eyes_enabled", True)),
            "update_channel": str(services.db.get_setting("update_channel", "stable") or "stable"),
            "auto_update_check": bool(services.db.get_setting("auto_update_check", True)),
            "version": "1.4.0",
            "build": "r26-cognitive-live-agent",
        }

    @app.get("/api/preferences")
    def get_preferences() -> dict[str, Any]:
        return _preferences_payload()

    @app.put("/api/preferences")
    def update_preferences(request: PreferencesRequest) -> dict[str, Any]:
        values = request.model_dump(exclude_none=True)
        if "voice_output_volume" in values:
            services.db.set_setting("voice_output_volume", max(0.0, min(float(values["voice_output_volume"]), 1.0)))
        if "language" in values:
            language = str(values["language"] or "ru").casefold()
            if language not in {"ru"}:
                raise HTTPException(status_code=400, detail="Сейчас интерфейс поддерживает русский язык")
            services.db.set_setting("ui_language", language)
        if "notifications_enabled" in values:
            services.db.set_setting("notifications_enabled", bool(values["notifications_enabled"]))
        if "sphere_motion" in values:
            services.db.set_setting("sphere_motion", bool(values["sphere_motion"]))
        if "sphere_intensity" in values:
            intensity = str(values["sphere_intensity"] or "vivid")
            if intensity not in {"soft", "balanced", "vivid"}:
                raise HTTPException(status_code=400, detail="Неизвестная яркость сферы")
            services.db.set_setting("sphere_intensity", intensity)
        if "desktop_comments_enabled" in values:
            services.db.set_setting("desktop_comments_enabled", bool(values["desktop_comments_enabled"]))
        if "desktop_eyes_enabled" in values:
            services.db.set_setting("desktop_eyes_enabled", bool(values["desktop_eyes_enabled"]))
        if "update_channel" in values:
            channel = str(values["update_channel"] or "stable")
            if channel not in {"stable", "preview"}:
                raise HTTPException(status_code=400, detail="Неизвестный канал обновлений")
            services.db.set_setting("update_channel", channel)
        if "auto_update_check" in values:
            services.db.set_setting("auto_update_check", bool(values["auto_update_check"]))
        if "autostart" in values:
            try:
                _set_autostart(bool(values["autostart"]))
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "mini_mode" in values:
            identity = services.identity.update({"desktop_avatar_enabled": bool(values["mini_mode"])})
            if identity.desktop_avatar_enabled:
                services.companion.start(); services.companion.show()
            else:
                services.companion.hide()
        return _preferences_payload()

    def _release_version(value: str) -> tuple[int, int, int, int]:
        parts = [int(x) for x in re.findall(r"\d+", str(value or ""))[:4]]
        return tuple((parts + [0, 0, 0, 0])[:4])

    @app.get("/api/updates/check")
    def check_updates() -> dict[str, Any]:
        """Read public update metadata directly from GitHub Releases.

        No token is shipped with EIRVEN. A user-provided GITHUB_TOKEN is used only when
        present in the environment. Stable checks use /releases/latest; preview checks
        inspect recent published releases and may select a prerelease.
        """
        current = "1.4.0"
        build = "r26-cognitive-live-agent"
        repo = str(os.getenv("EIRVEN_UPDATE_REPO", "FoxInDev/EIRVEN-AI") or "FoxInDev/EIRVEN-AI").strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            return {"ok": False, "current": current, "build": build, "error": "Некорректный EIRVEN_UPDATE_REPO"}
        channel = str(services.db.get_setting("update_channel", "stable") or "stable")
        endpoint = f"https://api.github.com/repos/{repo}/releases/latest" if channel == "stable" else f"https://api.github.com/repos/{repo}/releases?per_page=12"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "EIRVEN-AI/1.4.0",
        }
        token = str(os.getenv("GITHUB_TOKEN", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            import httpx
            response = httpx.get(endpoint, timeout=4.0, trust_env=False, headers=headers)
            response.raise_for_status()
            payload: Any = response.json() if response.content else {}
            if channel == "preview":
                releases = payload if isinstance(payload, list) else []
                payload = next((item for item in releases if isinstance(item, dict) and not item.get("draft")), {})
            if not isinstance(payload, dict):
                payload = {}
            latest = str(payload.get("tag_name") or payload.get("name") or "").strip()
            url = str(payload.get("html_url") or "").strip()
            assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
            candidates = []
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name") or "")
                download = str(asset.get("browser_download_url") or "")
                if not download:
                    continue
                score = 0
                low = name.casefold()
                if "windows" in low or "win" in low: score += 3
                if "eirven" in low: score += 2
                if low.endswith(".zip"): score += 2
                if "source" in low: score -= 3
                candidates.append((score, name, download, str(asset.get("digest") or "")))
            candidates.sort(reverse=True)
            chosen = candidates[0] if candidates else (0, "", "", "")
            available = bool(latest) and _release_version(latest) > _release_version(current)
            return {
                "ok": True, "repo": repo, "channel": channel, "current": current, "build": build,
                "latest": latest, "url": url, "update_available": available,
                "asset_name": chosen[1], "asset_url": chosen[2], "asset_digest": chosen[3],
            }
        except Exception as exc:
            return {"ok": False, "repo": repo, "current": current, "build": build, "error": str(exc)[:240]}

    @app.get("/api/companion")
    def companion_status() -> dict[str, Any]:
        return services.companion.status()

    @app.post("/api/companion/show")
    def companion_show() -> dict[str, Any]:
        services.companion.start()
        services.companion.show()
        return services.companion.status()

    @app.post("/api/companion/hide")
    def companion_hide() -> dict[str, Any]:
        services.companion.hide()
        return services.companion.status()

    @app.post("/api/system/shutdown")
    def system_shutdown() -> dict[str, Any]:
        """Stop the current EIRVEN lifecycle; never leave the UI stuck on «Выключаю…»."""
        stop_file = services.settings.root_dir / "logs" / "stop.request"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch(exist_ok=True)

        def request_stop() -> None:
            # The supervisor sees the marker within ~120 ms.  This process-level fallback
            # also covers development/direct starts where no supervisor exists.
            time.sleep(.85)
            try:
                services.companion.hide()
            except Exception:
                pass
            try:
                os._exit(0)
            except Exception:
                pass

        threading.Thread(target=request_stop, daemon=True, name="eirven-ui-shutdown").start()
        return {"ok": True, "scheduled": True, "pid": os.getpid()}

    @app.get("/api/applications")
    def applications(refresh: bool = False) -> list[dict[str, str]]:
        return services.applications.list_installed(refresh=refresh)

    @app.post("/api/applications/launch")
    def launch_application(request: ApplicationLaunchRequest) -> dict[str, Any]:
        try:
            return services.applications.launch(request.application)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/game")
    def game_task(request: GameTaskRequest) -> dict[str, str]:
        task_id = services.tasks.enqueue(
            "game",
            "Игровая задача в Minecraft",
            {"goal": request.goal, "window_title": "Minecraft", "max_minutes": request.max_minutes},
            conversation_id=request.conversation_id,
        )
        return {"task_id": task_id}

    @app.get("/api/telegram")
    def telegram_status() -> dict[str, Any]:
        return {
            "status": services.telegram.status(),
            "rules": services.telegram.rules(),
            "config": services.telegram.config(),
            "remote": services.telegram.remote_config(),
        }

    @app.put("/api/telegram/config")
    def telegram_config(request: TelegramConfigRequest) -> dict[str, Any]:
        try:
            return services.telegram.save_config(request.api_id, request.api_hash, request.phone)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/login/code")
    def telegram_login_code() -> dict[str, Any]:
        try:
            return services.telegram.request_login_code()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/login/confirm")
    def telegram_login_confirm(request: TelegramLoginRequest) -> dict[str, Any]:
        try:
            return services.telegram.confirm_login(request.code, request.password)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/telegram/rules")
    def telegram_rules(request: TelegramRulesRequest) -> dict[str, Any]:
        services.telegram.save_rules(request.rules)
        return {"rules": services.telegram.rules()}

    @app.put("/api/telegram/remote")
    def telegram_remote(request: TelegramRemoteRequest) -> dict[str, Any]:
        try:
            return {"remote": services.telegram.save_remote_config(request.enabled, request.chats, request.prefix)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/start")
    def telegram_start() -> dict[str, Any]:
        try:
            return services.telegram.start()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/stop")
    def telegram_stop() -> dict[str, Any]:
        return services.telegram.stop()



    @app.post("/api/diagnostics")
    def diagnostics() -> dict[str, Any]:
        started = time.perf_counter()
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        llm_health = services.gateway.health()
        add("Локальный интеллект", bool(llm_health.get("ok")), "Готов" if llm_health.get("ok") else str(llm_health.get("error") or "Локальный сервис недоступен"))
        installed = services.gateway.installed_models()
        add("Компоненты интеллекта", bool(installed), f"Готово: {len(installed)}" if installed else "Не найдены")
        if llm_health.get("ok") and installed:
            try:
                route = services.router.chat_route("Ответь одним словом: готов")
                message = services.gateway.chat(
                    [{"role": "user", "content": "Ответь одним словом: готов"}],
                    model=route.model,
                    temperature=0.0,
                    think=False,
                    num_ctx=1024,
                    num_predict=16,
                )
                text = str(message.get("content") or "").strip()
                add("Пробный ответ", bool(text), text[:120] if text else "Ответ не получен")
            except Exception as exc:
                add("Пробный ответ", False, str(exc))

        try:
            target = services.settings.workspace_dir / ".eirven-diagnostic"
            target.write_text("ok", encoding="utf-8")
            readback = target.read_text(encoding="utf-8")
            target.unlink(missing_ok=True)
            add("Файлы и рабочая папка", readback == "ok", str(services.settings.workspace_dir))
        except Exception as exc:
            add("Файлы и рабочая папка", False, str(exc))

        try:
            with services.db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            add("Локальная база", True, str(services.db.path))
            add(
                "Режим памяти",
                True,
                "смысловой поиск" if services.settings.semantic_memory else "быстрый локальный поиск без переключения модели",
            )
        except Exception as exc:
            add("Локальная база", False, str(exc))

        voice = services.voice.status()
        voice_detail = "Готово" if voice.get("stt_ready") else "Компонент не готов"
        add("Распознавание речи", bool(voice.get("stt_ready")), voice_detail)
        add("Голос EIRVEN", bool(voice.get("tts_ready")), "Готов" if voice.get("tts_ready") else "Локальные голоса не готовы")
        native = services.voice_daemon.status() if services.voice_daemon is not None else {"running": False}
        mic_ok = bool(native.get("running")) and not native.get("error")
        mic_detail = str(native.get("input_device") or native.get("error") or native.get("state") or "Фоновый микрофон не запущен")
        add("Фоновый микрофон", mic_ok, mic_detail)
        access = services.tools.tool_access_status()
        add("Управление компьютером", bool(access.get("desktop_session", access.get("effective_full_access", True))), "Рабочий стол доступен" if bool(access.get("desktop_session", access.get("effective_full_access", True))) else "Нет интерактивной сессии Windows")
        add("Работа с браузером", services.browser.available(), "Playwright установлен" if services.browser.available() else "компонент недоступен")
        if os.name == "nt":
            try:
                import pywinauto  # noqa: F401
                add("Управление окнами Windows", True, "UI Automation установлен")
            except Exception as exc:
                add("Управление окнами Windows", False, str(exc))

        return {
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
            "duration_seconds": round(time.perf_counter() - started, 2),
        }

    @app.put("/api/settings/desktop-control")
    def desktop_control(request: SettingRequest) -> dict[str, Any]:
        enabled = bool(request.value)
        services.settings.enable_desktop_control = enabled
        services.settings.enable_game_control = enabled
        services.db.set_setting("desktop_control_enabled", enabled)
        return {"enabled": enabled}

    @app.put("/api/settings/game-control")
    def game_control(request: SettingRequest) -> dict[str, Any]:
        # Backward-compatible alias. There is now one desktop permission.
        enabled = bool(request.value)
        services.settings.enable_desktop_control = enabled
        services.settings.enable_game_control = enabled
        services.db.set_setting("desktop_control_enabled", enabled)
        return {"enabled": enabled}

    @app.post("/api/system/open-workspace")
    def open_workspace() -> dict[str, Any]:
        path = services.settings.workspace_dir
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":  # pragma: no cover
                os.spawnlp(os.P_NOWAIT, "open", "open", str(path))
            else:  # pragma: no cover
                os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", str(path))
            return {"opened": True, "path": str(path)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app
