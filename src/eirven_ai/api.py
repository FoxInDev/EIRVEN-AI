from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import time
import threading
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse

from . import app_key
from pydantic import BaseModel, Field

from .database import utc_now
from .services import Services
from .style import StyleDNA
from .video import VIDEO_EXTENSIONS
from .ui_choices import enrich_stream_event
from .version import APP_BUILD, APP_VERSION, MOBILE_VERSION
from .release_policy import TEXT_MODEL, VISION_MODEL
from .privacy import wipe_personal_data
from .activity_journal import friendly_action
from .artifacts import discover_result_files, public_file
from .identity import CANONICAL_ASSISTANT_NAME
from .trace import log_event


MOBILE_TOKEN_HEADER = "X-EIRVEN-Mobile-Token"
MOBILE_DEVICE_HEADER = "X-EIRVEN-Mobile-Device"
MOBILE_UPLOAD_LIMIT = 20 * 1024 * 1024 * 1024

_WIFI_INTERFACE_HINTS = (
    "wi-fi", "wifi", "wireless", "wlan", "беспровод", "вай-фай",
)
_ETHERNET_INTERFACE_HINTS = (
    "ethernet", "local area connection", "локальная сеть", "ethernet-сеть",
)
_VIRTUAL_INTERFACE_HINTS = (
    "vethernet", "hyper-v", "hyperv", "wsl", "docker", "virtualbox",
    "vmware", "tailscale", "zerotier", "hamachi", "openvpn", "wireguard",
    "vpn", "loopback", "туннел", "virtual", "виртуаль",
)


def _is_local_client(host: str) -> bool:
    value = str(host or "").strip().casefold()
    if value in {"", "localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_private_client(host: str) -> bool:
    try:
        address = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return bool(address.is_private or address.is_link_local)


def _mobile_path_allowed(path: str, method: str) -> bool:
    value = str(path or "")
    verb = str(method or "GET").upper()
    exact = {
        ("/api/mobile/status", "GET"),
        ("/api/mobile/pair", "POST"),
        ("/api/mobile/disconnect", "POST"),
        ("/api/mobile/video", "POST"),
        ("/api/mobile/sync", "GET"),
        ("/api/mobile/sync/ack", "POST"),
        ("/api/mobile/calendar/snapshot", "POST"),
        ("/api/mobile/organizer/today", "GET"),
        ("/api/mobile/organizer/note", "POST"),
        ("/api/mobile/organizer/event", "POST"),
        ("/api/conversations", "POST"),
        ("/api/chat/stream", "POST"),
        ("/api/voice/speak", "POST"),
        ("/api/voice/transcribe", "POST"),
        ("/api/uploads", "POST"),
        ("/api/tasks", "GET"),
    }
    if (value, verb) in exact:
        return True
    patterns = (
        (r"/api/conversations/[A-Za-z0-9_-]+", "GET"),
        (r"/api/chat/[A-Za-z0-9_-]+/stop", "POST"),
        (r"/api/tasks/[A-Za-z0-9_-]+/cancel", "POST"),
        (r"/api/uploads/[A-Za-z0-9_-]+", "GET"),
    )
    return any(verb == allowed and re.fullmatch(pattern, value) for pattern, allowed in patterns)


def _mobile_bootstrap_path_allowed(path: str, method: str) -> bool:
    """Allow a phone to fetch only the installer before it has a pairing token."""
    value = str(path or "")
    return value in {"/mobile/install", "/mobile/eirven-orb.png", "/api/mobile/app.apk"} and str(method or "GET").upper() in {"GET", "HEAD"}


def _format_mobile_token(token: str) -> str:
    clean = re.sub(r"[^A-Z0-9]", "", str(token or "").upper())
    return "-".join(clean[index:index + 5] for index in range(0, len(clean), 5))


def _default_route_ipv4() -> str:
    """Return the IPv4 selected by the OS routing table without sending traffic."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        return ""


def _interface_kind(name: str) -> str:
    value = str(name or "").casefold()
    if any(hint in value for hint in _VIRTUAL_INTERFACE_HINTS):
        return "virtual"
    if any(hint in value for hint in _WIFI_INTERFACE_HINTS):
        return "wifi"
    if any(hint in value for hint in _ETHERNET_INTERFACE_HINTS):
        return "ethernet"
    return "network"


def _lan_candidates(port: int) -> list[dict[str, Any]]:
    """Rank reachable LAN IPv4 addresses, preferring a real Wi-Fi adapter.

    Windows machines commonly expose WSL, Hyper-V, VPN and Docker addresses next
    to the real Wi-Fi address.  Sorting raw private IPs made the QR code silently
    choose one of those isolated adapters.  Interface-aware ranking keeps them as
    a visible fallback but never prefers them over an active physical adapter.
    """
    route_ip = _default_route_ipv4()
    found: dict[str, dict[str, Any]] = {}

    def add(raw: str, interface: str, *, is_up: bool, source: str) -> None:
        try:
            value = ipaddress.ip_address(str(raw or "").strip())
        except ValueError:
            return
        if (
            value.version != 4
            or not value.is_private
            or value.is_loopback
            or value.is_link_local
            or value.is_unspecified
            or value.is_multicast
        ):
            return
        name = str(interface or "Локальная сеть").strip() or "Локальная сеть"
        kind = _interface_kind(name)
        score = 120 if is_up else 0
        score += {"wifi": 240, "ethernet": 170, "network": 80, "virtual": -360}[kind]
        if str(value) == route_ip:
            score += 115
        if str(value).startswith("192.168."):
            score += 45
        elif str(value).startswith("10."):
            score += 25
        else:
            score += 10
        candidate = {
            "url": f"http://{value}:{int(port)}",
            "ip": str(value),
            "interface": name,
            "kind": kind,
            "source": source,
            "score": score,
        }
        previous = found.get(str(value))
        # Hostname/default-route fallbacks do not know which adapter owns an IP.
        # Never let that generic label erase a real psutil adapter classification
        # (especially the virtual/VPN warning) merely because routing added points.
        if previous is not None and previous.get("source") == "adapter" and source != "adapter":
            return
        if previous is None or int(previous["score"]) < score:
            found[str(value)] = candidate

    try:
        import psutil  # type: ignore

        stats = psutil.net_if_stats()
        for interface, entries in psutil.net_if_addrs().items():
            state = stats.get(interface)
            if state is not None and not state.isup:
                continue
            for entry in entries:
                if entry.family == socket.AF_INET:
                    add(entry.address, interface, is_up=True, source="adapter")
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(str(info[4][0]), "Локальная сеть", is_up=True, source="hostname")
    except OSError:
        pass

    if route_ip:
        add(route_ip, "Системный маршрут", is_up=True, source="route")

    ordered = sorted(
        found.values(),
        key=lambda item: (-int(item["score"]), ipaddress.ip_address(item["ip"])),
    )
    result: list[dict[str, Any]] = []
    for index, item in enumerate(ordered):
        public = {key: value for key, value in item.items() if key != "score"}
        public["recommended"] = index == 0
        public["warning"] = (
            "Это виртуальный/VPN-адаптер; телефон обычно не видит его."
            if item["kind"] == "virtual" else ""
        )
        result.append(public)
    return result


def _lan_addresses(port: int) -> list[str]:
    return [str(item["url"]) for item in _lan_candidates(port)]


def _mobile_network_runtime_status(root_dir: Path, port: int) -> dict[str, Any]:
    """Read the launcher's last firewall check for the current runtime port."""
    if os.name != "nt":
        return {
            "firewall_ready": True,
            "detail": "Проверка Windows Firewall нужна только в Windows.",
        }
    path = root_dir / "logs" / "mobile_network.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if int(value.get("port", 0)) != int(port):
            raise ValueError("status belongs to another port")
        ready = bool(value.get("firewall_ready"))
        detail = str(value.get("detail") or "").strip()
        if not ready and ("UAC_REQUIRED" in detail or "Firewall" in detail or "firewall" in detail):
            detail = (
                "Windows Firewall ещё не разрешил вход с телефона. Перезапусти EIRVEN "
                "через ярлык и подтверди один системный запрос UAC — правило будет только "
                "для локальной сети и порта EIRVEN."
            )
        return {"firewall_ready": ready, "detail": detail}
    except Exception:
        return {
            "firewall_ready": None,
            "detail": (
                "Windows ещё не подтвердила доступ из локальной сети. "
                "Перезапусти EIRVEN через ярлык и подтверди системный запрос UAC."
            ),
        }


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


class SessionConversationRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=128)


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
    model_config = {"extra": "forbid"}

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
    proactive_output_mode: str | None = None
    confirmation_mode: str | None = None
    voice_wake_enabled: bool | None = None
    update_channel: str | None = None
    auto_update_check: bool | None = None
    auto_update_enabled: bool | None = None


class OnboardingCompleteRequest(BaseModel):
    """One atomic first-run profile committed by the local UI."""

    model_config = {"extra": "forbid"}

    user_address: str = Field(min_length=1, max_length=48)
    style: dict[str, Any]
    voice_wake_enabled: bool = False
    desktop_control_enabled: bool = True


class PrivacyWipeRequest(BaseModel):
    confirmation: str
    preserve_entitlement: bool = True


class AdultPhotoRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=1600)
    mode: str = "realistic"
    aspect: str = "portrait"


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


class TelegramMonitorRequest(BaseModel):
    exclude_groups: bool = False
    exclude_channels: bool = True
    max_per_hour: int = Field(default=20, ge=1, le=120)


class TelegramChatPrefsRequest(BaseModel):
    excluded: list[str] | None = None
    styles: dict[str, str] | None = None


class TelegramBroadcastRequest(BaseModel):
    chats: list[str] = []
    message: str = ""
    when: str | None = None


class TeachingStartRequest(BaseModel):
    goal: str = ""


class TransferRequest(BaseModel):
    # Module level on purpose: api.py uses postponed annotations, so a model declared
    # inside the builder is invisible to FastAPI and silently becomes a query param.
    path: str = ""


class TelegramPrefsRequest(BaseModel):
    # Must live at module level: api.py uses postponed annotations, so FastAPI
    # resolves the type name in the module namespace. A class defined inside the
    # builder function is invisible there and the parameter silently degrades to a
    # query argument -- which surfaced as "query -> request: Field required".
    reply_style: str | None = None
    autostart_monitor: bool | None = None


class TelegramTaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)


class MailConfigRequest(BaseModel):
    email: str
    password: str
    imap_host: str
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str
    smtp_port: int = Field(default=587, ge=1, le=65535)
    imap_ssl: bool = True
    smtp_starttls: bool = True
    auto_move_obvious_spam: bool = False
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


class MobileSyncAckRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class MobilePairRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=160)
    display_name: str = Field(default="", max_length=120)
    platform: str = Field(default="android", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MobileCalendarSnapshotRequest(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class MobileNoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class MobileEventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=400)
    starts_at: str
    ends_at: str | None = None
    reminders: list[int] = Field(default_factory=lambda: [120, 60, 30, 10])


def _sse(data: dict[str, Any], event: str | None = None) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    encoded = json.dumps(data, ensure_ascii=False, default=str)
    lines.append(f"data: {encoded}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def build_api(services: Services) -> FastAPI:
    # Ключ сеанса создаётся до того, как сервер начнёт принимать соединения:
    # лончер и окно приложения читают его из файла сразу после ответа на ping.
    app_session_key = app_key.issue(services.settings.root_dir)
    log_event(services.settings.root_dir, "APP_KEY_ISSUED", file=str(app_key.key_path(services.settings.root_dir)))

    app = FastAPI(
        title="EIRVEN AI Local API",
        version=APP_VERSION,
        description="Локальный voice-first персональный ИИ с очередью задач и управлением компьютером",
    )
    upload_dir = services.settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    def mobile_token(*, regenerate: bool = False) -> str:
        stored = "" if regenerate else str(
            services.db.get_setting("mobile_access_token", "") or ""
        )
        clean = re.sub(r"[^A-Z0-9]", "", stored.upper())
        if len(clean) < 20:
            clean = secrets.token_hex(10).upper()
            services.db.set_setting("mobile_access_token", clean)
            if regenerate:
                try:
                    services.phone_sync.revoke_all_devices()
                except Exception:
                    pass
        return clean

    def mobile_device_id(request: Request) -> str:
        return str(request.headers.get(MOBILE_DEVICE_HEADER, "") or "").strip()[:160]

    def add_mobile_cors(response: Response, request: Request) -> Response:
        origin = request.headers.get("origin", "").strip().casefold()
        trusted = {"null", "https://appassets.eirven", "http://appassets.eirven"}
        if origin in trusted:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = (
                f"Content-Type, {MOBILE_TOKEN_HEADER}, {MOBILE_DEVICE_HEADER}"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
        return response

    @app.middleware("http")
    async def local_network_guard(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        client_host = request.client.host if request.client is not None else ""
        local = _is_local_client(client_host)
        mobile_method = request.headers.get("access-control-request-method", method)
        mobile_allowed = _mobile_path_allowed(path, mobile_method)
        mobile_bootstrap = _mobile_bootstrap_path_allowed(path, mobile_method)

        # Desktop settings and the full API never leave this machine. A private-LAN
        # phone receives only the small allowlisted surface and must present its token.
        if not local:
            if not _is_private_client(client_host):
                log_event(services.settings.root_dir, "PAIRING_REJECT", client=client_host, path=path, reason="not_private_lan")
                return PlainTextResponse(
                    "EIRVEN доступна только в частной локальной сети",
                    status_code=403,
                )
            # A phone owner often types or reopens only the address shown under the QR.
            # Send those harmless entry paths to the mobile checkpoint instead of a
            # desktop-only error page.  The desktop UI itself remains inaccessible.
            if method in {"GET", "HEAD"} and path in {"/", "/ui", "/ui/"}:
                return RedirectResponse("/mobile/install", status_code=307)
            if method == "OPTIONS" and (mobile_allowed or mobile_bootstrap):
                return add_mobile_cors(Response(status_code=204), request)
            if not mobile_allowed and not mobile_bootstrap:
                log_event(services.settings.root_dir, "PAIRING_REJECT", client=client_host, path=path, reason="path_not_allowed")
                return PlainTextResponse(
                    "Этот раздел доступен только на компьютере",
                    status_code=403,
                )
            if not mobile_bootstrap:
                supplied = re.sub(
                    r"[^A-Z0-9]", "", request.headers.get(MOBILE_TOKEN_HEADER, "").upper()
                )
                if not supplied or not secrets.compare_digest(supplied, mobile_token()):
                    log_event(services.settings.root_dir, "PAIRING_REJECT", client=client_host, path=path, reason="token_rejected")
                    return add_mobile_cors(
                        PlainTextResponse("Неверный код подключения", status_code=401), request
                    )
                device_id = mobile_device_id(request)
                if (
                    device_id
                    and path != "/api/mobile/pair"
                    and not services.phone_sync.touch_device(device_id)
                ):
                    log_event(
                        services.settings.root_dir, "PAIRING_REJECT",
                        client=client_host, path=path, reason="device_revoked",
                    )
                    return add_mobile_cors(
                        PlainTextResponse("Телефон отключён. Подключи его заново.", status_code=401),
                        request,
                    )

        # С самого компьютера — только окно приложения. Раньше любой браузер и любая
        # программа могли открыть 127.0.0.1:7860/ui/ и получить полный доступ.
        # Теперь нужен ключ сеанса: окно приложения получает его при входе в виде
        # защищённой метки и дальше шлёт её с каждым запросом само.
        if local and path not in app_key.OPEN_PATHS:
            supplied = request.cookies.get(app_key.COOKIE_NAME) or request.headers.get(app_key.HEADER_NAME)
            if not app_key.matches(app_session_key, supplied):
                log_event(services.settings.root_dir, "APP_KEY_REJECT", path=path[:120], method=method)
                if path.startswith("/api/"):
                    return PlainTextResponse("Эрви доступна только в приложении", status_code=403)
                return HTMLResponse(app_key.BLOCKED_PAGE, status_code=403)

        response = await call_next(request)
        if not local and path == "/api/mobile/status":
            log_event(services.settings.root_dir, "PAIRING_ACCEPT", client=client_host, path=path, status=response.status_code)
        if path.startswith("/ui"):
            # Every EIRVEN release is served from the same localhost URL. Browsers may
            # otherwise keep an old app.js and make a new backend look broken.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if mobile_allowed or mobile_bootstrap:
            add_mobile_cors(response, request)
        return response

    def attachment_path(attachment_id: str) -> Path:
        with services.db.connect() as conn:
            row = conn.execute(
                "SELECT path FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Файл не найден")
        path = Path(row["path"]).resolve()
        allowed_roots = tuple(
            item.resolve()
            for item in (
                upload_dir,
                services.settings.workspace_dir,
                services.settings.root_dir / "video_results",
                services.settings.root_dir / "video",
                services.settings.data_dir / "generated",
            )
        )
        if not any(path == root or root in path.parents for root in allowed_roots):
            raise HTTPException(status_code=403, detail="Некорректный путь файла")
        return path

    def publish_result_files(
        event: dict[str, Any], query: str, conversation_id: str | None, since: float
    ) -> dict[str, Any]:
        if event.get("type") != "done":
            return event
        route = dict(event.get("route") or {})
        paths = discover_result_files(
            route,
            query,
            root_dir=services.settings.root_dir,
            data_dir=services.settings.data_dir,
            workspace_dir=services.settings.workspace_dir,
            since=since,
        )
        if not paths:
            return event
        files: list[dict[str, Any]] = []
        with services.db.connect() as conn:
            for path in paths:
                row = conn.execute(
                    "SELECT id FROM attachments WHERE path=? ORDER BY created_at DESC LIMIT 1",
                    (str(path),),
                ).fetchone()
                attachment_id = str(row["id"]) if row else secrets.token_hex(16)
                if not row:
                    conn.execute(
                        """INSERT INTO attachments(
                               id,conversation_id,path,media_type,original_name,created_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            attachment_id,
                            conversation_id or None,
                            str(path),
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            path.name,
                            utc_now(),
                        ),
                    )
                files.append(public_file(path, attachment_id))
        route["files"] = files
        event["route"] = route
        event["files"] = files

        # Keep download cards after a reload on both desktop and phone.
        if conversation_id:
            with services.db.connect() as conn:
                row = conn.execute(
                    "SELECT id,metadata FROM messages WHERE conversation_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if row:
                    try:
                        metadata = json.loads(row["metadata"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        metadata = {}
                    metadata["files"] = files
                    conn.execute(
                        "UPDATE messages SET metadata=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), int(row["id"])),
                    )
        return event

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    @app.get("/ui/open")
    def ui_open(request: Request, k: str = "", next: str = "/ui/"):
        # Ключ в адресе — только на этот один переход. Сервер меняет его на метку
        # HttpOnly, недоступную скриптам страницы, и сразу уводит на чистый адрес,
        # чтобы ключ не оставался в адресной строке и истории окна.
        if not app_key.matches(app_session_key, k):
            log_event(services.settings.root_dir, "APP_KEY_OPEN_REJECT")
            return HTMLResponse(app_key.BLOCKED_PAGE, status_code=403)
        target = next if str(next).startswith("/ui") else "/ui/"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(app_key.COOKIE_NAME, app_session_key, httponly=True,
                            samesite="lax", path="/")
        log_event(services.settings.root_dir, "APP_KEY_OPEN_OK", next=target[:80])
        return response

    @app.get("/api/ping")
    def ping() -> dict[str, str]:
        # Lightweight startup probe: never calls Ollama, Telegram, voice or hardware.
        # Сборка — чтобы отличить эту Эрви от другой той же версии. При сборке
        # на порту могла отвечать открытая старая Эрви, и проверка принимала её
        # ответ за ответ нового exe.
        return {"app": "eirven", "status": "ok", "version": APP_VERSION, "build": APP_BUILD}

    def mobile_apk_path() -> Path | None:
        configured = str(os.getenv("EIRVEN_MOBILE_APK", "") or "").strip()
        configured_path = Path(configured).expanduser() if configured else None
        if configured_path is not None and not configured_path.is_absolute():
            configured_path = services.settings.root_dir / configured_path
        candidates = [
            configured_path,
            services.settings.root_dir / "mobile_client" / "EIRVEN-Mobile.apk",
            services.settings.root_dir / "EIRVEN-Mobile.apk",
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and resolved.stat().st_size >= 100_000:
                    return resolved
            except OSError:
                continue
        return None

    from .network_doctor import NetworkDoctor
    network_doctor = NetworkDoctor(services.settings.root_dir, services.settings.port, services.settings.host)

    def mobile_config_payload() -> dict[str, Any]:
        candidates = _lan_candidates(services.settings.port)
        addresses = [str(item["url"]) for item in candidates]
        apk = mobile_apk_path()
        preferred = addresses[0] if addresses else ""
        # Сетевой доктор проверяет доступ с телефона по-настоящему и чинит, что может.
        # Экран ждёт его не больше 6 секунд; иначе — прежний статус, а доктор
        # доделает работу в фоне к следующему открытию экрана.
        network_status = None
        try:
            network_status = network_doctor.check_for_screen(candidates, wait=6.0)
        except Exception as exc:
            log_event(services.settings.root_dir, "NETDOC_SCREEN_FAILED", error=str(exc)[:200])
        if network_status is None:
            network_status = _mobile_network_runtime_status(
                services.settings.root_dir, services.settings.port
            )
        return {
            "addresses": addresses,
            "address_options": candidates,
            "preferred_address": preferred,
            "token": _format_mobile_token(mobile_token()),
            "lan_enabled": not _is_local_client(services.settings.host),
            **network_status,
            "port": int(services.settings.port),
            "apk_available": True,
            "apk_filename": apk.name if apk is not None else f"EIRVEN-Mobile-{MOBILE_VERSION}.apk",
            "apk_size_bytes": apk.stat().st_size if apk is not None else 0,
            "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest() if apk is not None else "",
            "download_url": f"{preferred}/api/mobile/app.apk" if preferred else "",
            "install_url": f"{preferred}/mobile/install" if preferred else "",
            "paired_devices": services.phone_sync.devices(),
        }

    @app.get("/api/mobile/config")
    def mobile_config() -> dict[str, Any]:
        return mobile_config_payload()

    @app.post("/api/mobile/token/regenerate")
    def regenerate_mobile_token() -> dict[str, Any]:
        mobile_token(regenerate=True)
        return mobile_config_payload()

    @app.api_route("/api/mobile/app.apk", methods=["GET", "HEAD"])
    def mobile_apk_download() -> FileResponse:
        apk = mobile_apk_path()
        if apk is None:
            return RedirectResponse("https://eirven.foxyhosty.ru/api/download/android", status_code=307)
        return FileResponse(
            apk,
            media_type="application/vnd.android.package-archive",
            filename=f"EIRVEN-Mobile-{MOBILE_VERSION}.apk",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.api_route("/mobile/install", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def mobile_install_page() -> HTMLResponse:
        """Human-readable LAN checkpoint before Android downloads the installer."""
        apk = mobile_apk_path()
        if apk is None:
            return HTMLResponse(
                "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Установка EIRVEN Mobile</title><style>"
                "body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070b14;color:#eef8ff;font:16px/1.5 system-ui,sans-serif}"
                ".card{max-width:620px;margin:20px;padding:28px;border:1px solid #263552;border-radius:24px;background:#0d1423;text-align:center}"
                "a{display:block;margin-top:22px;padding:16px 20px;border-radius:14px;background:#67e5ff;color:#07111d;text-decoration:none;font-weight:800}"
                "p{color:#aebada}</style></head><body><main class='card'>"
                "<h1>Связь с компьютером есть</h1>"
                "<p>Нажми кнопку ниже, чтобы скачать официальный APK EIRVEN Mobile.</p>"
                "<a href='https://eirven.foxyhosty.ru/api/download/android'>Скачать EIRVEN Mobile {MOBILE_VERSION}</a>"
                "</main></body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        digest = hashlib.sha256(apk.read_bytes()).hexdigest()
        body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Установка EIRVEN Mobile</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at top,#13244a 0,#090d19 45%,#05070f 100%);color:#eef8ff;font:16px/1.55 system-ui,sans-serif}}main{{max-width:720px;margin:auto;padding:34px 18px 40px}}.card{{padding:26px;border:1px solid rgba(100,142,223,.22);border-radius:28px;background:linear-gradient(180deg,rgba(10,17,35,.92),rgba(7,12,24,.96));box-shadow:0 30px 90px rgba(0,0,0,.34)}}.hero{{display:grid;justify-items:center;text-align:center;gap:12px;margin-bottom:18px}}.ok{{margin:0;color:#dffcff;font-size:31px;letter-spacing:-.04em}}.lead{{margin:0;max-width:540px;color:#aebada}}.orb{{width:108px;height:108px;border-radius:50%;filter:drop-shadow(0 0 28px rgba(96,184,255,.36));animation:float 6s ease-in-out infinite}}.orb img{{display:block;width:100%;height:100%;aspect-ratio:1;object-fit:contain;image-rendering:auto}}a.download{{display:block;margin:22px 0 12px;padding:16px 18px;border-radius:16px;background:linear-gradient(135deg,#59e4ff,#817bff 58%,#ef7bd1);color:#08111e;text-align:center;text-decoration:none;font-weight:800;box-shadow:0 16px 44px rgba(91,117,255,.3)}}.steps{{margin:18px 0 0;padding:0;list-style:none;display:grid;gap:12px}}.steps li{{display:grid;grid-template-columns:28px 1fr;gap:12px;align-items:flex-start;padding:12px 14px;border:1px solid rgba(100,142,223,.14);border-radius:18px;background:rgba(255,255,255,.03)}}.steps span{{display:grid;place-items:center;width:28px;height:28px;border-radius:50%;background:rgba(97,224,255,.14);color:#bff8ff;font-weight:700}}.why{{margin-top:18px;padding:14px 16px;border:1px solid rgba(100,142,223,.14);border-radius:18px;background:rgba(255,255,255,.03);color:#9aa9ca}}small{{display:block;margin-top:16px;color:#8ea1bf;overflow-wrap:anywhere}}code{{color:#bdefff}}@keyframes float{{50%{{transform:translateY(-5px)}}}}
</style></head><body><main><div class="card"><div class="hero"><div class="orb" aria-hidden="true"><img src="/mobile/eirven-orb.png" alt="" style="display:block;width:100%;height:100%;object-fit:contain;image-rendering:auto"></div><h1 class="ok">Связь с компьютером есть</h1><p class="lead">Телефон уже видит EIRVEN в домашней сети. Осталось скачать приложение, установить его и ввести код подключения с компьютера.</p></div>
<a class="download" href="/api/mobile/app.apk?build={APP_BUILD}&sha={digest[:12]}">Скачать EIRVEN Mobile {MOBILE_VERSION}</a>
<ul class="steps"><li><span>1</span><div><b>Скачай APK</b><div>Нажми кнопку выше и дождись завершения загрузки.</div></div></li><li><span>2</span><div><b>Разреши установку, если Android спросит</b><div>Обычно нужно подтвердить установку приложений из этого браузера один раз.</div></div></li><li><span>3</span><div><b>Открой EIRVEN Mobile</b><div>В приложении введи адрес компьютера и код подключения из раздела «Телефон» на ПК.</div></div></li></ul>
<div class="why"><b>Зачем этот экран:</b> он показывает, что связь с компьютером уже есть, и даёт правильный APK именно из этой локальной установки EIRVEN.</div>
<small>EIRVEN {APP_VERSION} · {APP_BUILD}<br>SHA-256: <code>{digest}</code></small></div></main></body></html>"""
        return HTMLResponse(body, headers={"Cache-Control": "no-store, max-age=0"})

    @app.api_route("/mobile/eirven-orb.png", methods=["GET", "HEAD"])
    def mobile_orb() -> FileResponse:
        path = services.settings.root_dir / "src" / "eirven_ai" / "web" / "eirven-orb.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Канонический визуал Эрви не найден")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/mobile/status")
    def mobile_status(request: Request) -> dict[str, Any]:
        settings = services.settings
        device_id = mobile_device_id(request)
        asr_model = (
            settings.gigaam_model
            if settings.asr_engine == "gigaam"
            else settings.whisper_model
        )
        return {
            "ok": True,
            "version": APP_VERSION,
            "device_id": device_id,
            "paired": bool(device_id and services.phone_sync.touch_device(device_id)),
            "models": {
                "chat": settings.model,
                "fast": settings.fast_model,
                "code": settings.code_model,
                "vision": settings.vision_model,
                "asr": asr_model,
                "tts": (
                    "Бая · Silero"
                    if settings.tts_engine in {"auto", "silero"}
                    else settings.tts_engine
                ),
            },
        }

    @app.post("/api/mobile/pair")
    def mobile_pair(request: MobilePairRequest) -> dict[str, Any]:
        try:
            device = services.phone_sync.pair_device(
                request.device_id,
                display_name=request.display_name,
                platform=request.platform,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "device": device,
            "sync": services.phone_sync.sync_payload(
                device_id=str(device["device_id"]), limit=250
            ),
        }

    @app.post("/api/mobile/disconnect")
    def mobile_disconnect(request: Request) -> dict[str, Any]:
        device_id = mobile_device_id(request)
        if not device_id:
            raise HTTPException(status_code=400, detail="Телефон не идентифицирован")
        try:
            disconnected = services.phone_sync.revoke_device(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "disconnected": disconnected}

    @app.get("/api/mobile/devices")
    def mobile_devices() -> dict[str, Any]:
        rows = services.phone_sync.devices()
        return {"ok": True, "devices": rows, "count": len(rows)}

    @app.delete("/api/mobile/devices/{device_id}")
    def disconnect_mobile_device(device_id: str) -> dict[str, Any]:
        try:
            disconnected = services.phone_sync.revoke_device(device_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "disconnected": disconnected}

    @app.get("/api/mobile/sync")
    def mobile_sync(request: Request) -> dict[str, Any]:
        try:
            return services.phone_sync.sync_payload(
                device_id=mobile_device_id(request)
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.post("/api/mobile/sync/ack")
    def mobile_sync_ack(payload: MobileSyncAckRequest, request: Request) -> dict[str, Any]:
        try:
            acked = services.phone_sync.ack(
                payload.ids, device_id=mobile_device_id(request)
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"ok": True, "acked": acked}

    @app.post("/api/mobile/calendar/snapshot")
    def mobile_calendar_snapshot(
        payload: MobileCalendarSnapshotRequest, request: Request
    ) -> dict[str, Any]:
        device_id = mobile_device_id(request) or "legacy"
        try:
            accepted = services.phone_sync.replace_calendar_snapshot(
                payload.events, device_id=device_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"ok": True, "accepted": accepted}

    @app.get("/api/mobile/organizer/today")
    def mobile_organizer_today() -> dict[str, Any]:
        return {"ok": True, **services.phone_sync.today()}

    @app.post("/api/mobile/organizer/note")
    def mobile_organizer_note(payload: MobileNoteRequest, request: Request) -> dict[str, Any]:
        source = "mobile:" + (mobile_device_id(request) or "legacy")
        return {"ok": True, "note": services.phone_sync.add_note(payload.text, source=source)}

    @app.post("/api/mobile/organizer/event")
    def mobile_organizer_event(payload: MobileEventRequest, request: Request) -> dict[str, Any]:
        from datetime import datetime, timedelta
        try:
            start = datetime.fromisoformat(payload.starts_at)
            end = datetime.fromisoformat(payload.ends_at) if payload.ends_at else start + timedelta(hours=1)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректная дата события") from exc
        source = "mobile:" + (mobile_device_id(request) or "legacy")
        event = services.phone_sync.add_event(
            payload.title, start, end, reminders=payload.reminders, source=source
        )
        return {"ok": True, "event": event}

    @app.post("/api/mobile/video")
    async def mobile_video(file: UploadFile = File(...)) -> dict[str, Any]:
        original = Path(file.filename or "video.mp4").name
        suffix = Path(original).suffix.casefold()
        if suffix not in VIDEO_EXTENSIONS:
            raise HTTPException(
                status_code=415, detail="Выбери видео в поддерживаемом формате"
            )
        safe_stem = re.sub(
            r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", Path(original).stem
        ).strip(" ._")[:96] or "video"
        inbox = Path(services.video.inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        temporary = inbox / f".eirven-upload-{uuid.uuid4().hex}{suffix}"
        target = inbox / f"{safe_stem}{suffix}"
        if target.exists():
            target = inbox / f"{safe_stem}-{uuid.uuid4().hex[:6]}{suffix}"
        size = 0
        try:
            with temporary.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MOBILE_UPLOAD_LIMIT:
                        raise HTTPException(status_code=413, detail="Видео больше 20 ГБ")
                    output.write(chunk)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return {"ok": True, "name": target.name, "size": size, "queued": True}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "app": "ok",
            "version": APP_VERSION,
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
            "video": services.video.status(),
            "phone_sync": {"ok": True, "today": services.phone_sync.today()},
            "modes": services.modes.status() if services.modes is not None else {},
        }


    @app.get("/api/hardware")
    def hardware() -> dict[str, Any]:
        return services.hardware.to_dict()

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        installed = {
            str(name).casefold(): str(name)
            for name in services.gateway.installed_models()
        }
        allowed = (services.settings.model, services.settings.vision_model)
        allowed_keys = {str(name).casefold() for name in allowed}
        return {
            "models": [installed[key] for key in allowed_keys if key in installed],
            "details": [
                row for row in services.gateway.model_details()
                if str(row.get("name") or row.get("model") or "").casefold() in allowed_keys
            ],
            "recommended": {
                "fast": services.settings.fast_model,
                "main": services.settings.model,
                "code": services.settings.code_model,
                "vision": services.settings.vision_model,
            },
        }

    @app.post("/api/models/{model_name:path}/warm")
    def warm_model(model_name: str) -> dict[str, Any]:
        if model_name.casefold() not in {services.settings.model.casefold(), services.settings.vision_model.casefold()}:
            raise HTTPException(status_code=400, detail="Этот режим не выбран для данного устройства")
        services.gateway.warm(model_name)
        return {"ok": True, "model": model_name}

    @app.get("/api/conversations")
    def conversations(limit: int = 100) -> list[dict[str, Any]]:
        return services.memory.list_conversations(limit)

    @app.post("/api/conversations")
    def create_conversation(request: ConversationRequest) -> dict[str, Any]:
        conversation_id = services.memory.create_conversation(request.mode, request.title)
        return services.memory.conversation(conversation_id) or {"id": conversation_id}

    @app.get("/api/session")
    def current_session() -> dict[str, Any]:
        """Restore durable profile and chat selection without browser storage.

        The payload deliberately comes from the same SQLite transaction-backed
        services used by chat/onboarding.  localStorage may accelerate paint, but it
        is never the only copy of the owner's name, style or active conversation.
        """
        conversation = services.memory.active_conversation()
        if conversation is not None:
            conversation["messages"] = services.memory.history(
                str(conversation["id"]), limit=None, include_metadata=True
            )
        return {
            "identity": services.identity.get().to_dict(),
            "style": services.style.get().to_dict(),
            "conversation": conversation,
        }

    @app.put("/api/session/conversation")
    def select_session_conversation(
        request: SessionConversationRequest,
    ) -> dict[str, Any]:
        if not services.memory.set_active_conversation(request.conversation_id):
            raise HTTPException(status_code=404, detail="Чат не найден")
        conversation = services.memory.conversation(request.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Чат не найден")
        conversation["messages"] = services.memory.history(
            request.conversation_id, limit=None, include_metadata=True
        )
        return conversation

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
        # image_ids remains for backward compatibility. attachment_ids is the generic path.
        # Keep the split explicit so uploads from the Android/WebView client reach the
        # vision lane instead of being silently discarded as they were in r37.
        ids = list(dict.fromkeys([*request.attachment_ids, *request.image_ids]))[:16]
        all_paths = resolve_attachments(ids)
        image_paths: list[str] = []
        non_images: list[str] = []
        for path in all_paths:
            media = mimetypes.guess_type(path)[0] or ""
            if media.startswith("image/") or Path(path).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                image_paths.append(path)
            else:
                non_images.append(path)
        return image_paths, non_images

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
        request_started = time.time()
        image_paths, attachment_paths = request_files(request)
        result = services.chat.complete(
            request.message,
            request.conversation_id,
            request.mode,
            request.model,
            image_paths=image_paths,
            attachment_paths=attachment_paths,
        )
        enriched = enrich_stream_event({"type": "done", "answer": result.get("answer", ""), "route": result.get("route") or {}}, request.message)
        enriched = publish_result_files(
            enriched,
            request.message,
            str(result.get("conversation_id") or request.conversation_id or ""),
            request_started,
        )
        route = dict(enriched.get("route") or {})
        result["route"] = route
        if route.get("action"):
            result["action"] = route.get("action")
        if route.get("task_id"):
            result["task_id"] = route.get("task_id")
        if enriched.get("files"):
            result["files"] = enriched["files"]
        return result

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        request_started = time.time()
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
                enriched = enrich_stream_event(event, request.message)
                enriched = publish_result_files(
                    enriched,
                    request.message,
                    str(enriched.get("conversation_id") or request.conversation_id or ""),
                    request_started,
                )
                yield _sse(enriched)

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
        # Images are ordinary local attachments now. They stay on the paired PC and are
        # routed to the local vision model; Photo Studio restrictions must not disable
        # normal "прикрепи фото и спроси" analysis from the phone.
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
        with services.db.connect() as conn:
            row = conn.execute(
                "SELECT original_name,media_type FROM attachments WHERE id=?",
                (attachment_id,),
            ).fetchone()
        return FileResponse(
            path,
            media_type=(str(row["media_type"]) if row else None),
            filename=(str(row["original_name"]) if row else path.name),
        )

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
            raise HTTPException(status_code=409, detail="Сначала включи камеру")
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
        payload = dict(runtime.status()) if runtime is not None else {"action":"idle"}
        # Эмоция едет в том же ответе, который интерфейс опрашивает дважды в секунду, —
        # отдельного опроса не нужно.
        try:
            emotions = getattr(services, "emotions", None)
            payload["emotion"] = emotions.current() if emotions is not None else "joy"
        except Exception:
            payload["emotion"] = "joy"
        return payload

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
        if "user_address" in values:
            try:
                style = services.style.get()
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
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], cwd=str(services.settings.root_dir), capture_output=True, text=True, timeout=20, creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
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
            "proactive_output_mode": str(services.db.get_setting("proactive_output_mode", "voice") or "voice"),
            "confirmation_mode": str(services.db.get_setting("confirmation_mode", "full") or "full"),
            "desktop_control_enabled": bool(services.settings.enable_desktop_control),
            "voice_wake_enabled": bool(services.db.get_setting("voice_wake_enabled", True)),
            "action_journal_enabled": True,
            "update_channel": str(services.db.get_setting("update_channel", "stable") or "stable"),
            "auto_update_check": bool(services.db.get_setting("auto_update_check", True)),
            "auto_update_enabled": bool(services.db.get_setting("auto_update_enabled", False)),
            "distribution": services.updater.distribution.public() if services.updater is not None else {},
            "version": APP_VERSION,
            "build": APP_BUILD,
            "model": services.settings.model,
            "fast_model": services.settings.fast_model,
            "code_model": services.settings.code_model,
            "vision_model": services.settings.vision_model,
            "strict_release_model": bool(services.settings.strict_release_model),
            "model_only_mode": bool(getattr(services.settings, "model_only_mode", False)),
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
        if "proactive_output_mode" in values:
            output_mode = str(values["proactive_output_mode"] or "visible").casefold()
            if output_mode not in {"visible", "voice"}:
                raise HTTPException(status_code=400, detail="Неизвестный способ подсказок")
            services.db.set_setting("proactive_output_mode", output_mode)
        if "confirmation_mode" in values:
            confirmation_mode = str(values["confirmation_mode"] or "full").casefold()
            if confirmation_mode not in {"every", "critical", "full"}:
                raise HTTPException(status_code=400, detail="Неизвестный режим подтверждений")
            # Full access never disables the immutable confirmation boundary for
            # sends, payments, orders, deletes or uploads; it only suppresses extra
            # prompts for reversible interactions.
            services.db.set_setting("confirmation_mode", confirmation_mode)
            services.settings.confirmation_mode = confirmation_mode
        if "voice_wake_enabled" in values:
            enabled = bool(values["voice_wake_enabled"])
            services.db.set_setting("voice_wake_enabled", enabled)
            daemon = services.voice_daemon
            if daemon is not None:
                try:
                    if enabled:
                        daemon.start()
                    else:
                        daemon.stop()
                except Exception:
                    pass
        if "update_channel" in values:
            channel = str(values["update_channel"] or "stable")
            if channel not in {"stable", "preview"}:
                raise HTTPException(status_code=400, detail="Неизвестный канал обновлений")
            services.db.set_setting("update_channel", channel)
        if "auto_update_check" in values:
            services.db.set_setting("auto_update_check", bool(values["auto_update_check"]))
        if "auto_update_enabled" in values:
            services.db.set_setting("auto_update_enabled", bool(values["auto_update_enabled"]))
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

    @app.post("/api/onboarding/complete")
    def complete_onboarding(request: OnboardingCompleteRequest) -> dict[str, Any]:
        """Commit identity, style and permissions as one durable first-run state."""
        user_address = " ".join(str(request.user_address or "").split())[:48]
        if not user_address:
            raise HTTPException(status_code=400, detail="Укажи имя, чтобы завершить знакомство")

        identity = services.identity.get()
        identity.user_address = user_address
        identity.onboarding_completed = True
        identity = services.identity._normalize(identity)

        style = StyleDNA.from_dict(dict(request.style or {}))
        style.owner_name = user_address
        style.preferred_address = user_address
        style = style.normalized()

        desktop_enabled = bool(request.desktop_control_enabled)
        voice_enabled = bool(request.voice_wake_enabled)
        services.db.set_settings({
            services.identity.KEY: asdict(identity),
            services.style.KEY: style.to_dict(),
            "voice_wake_enabled": voice_enabled,
            "desktop_control_enabled": desktop_enabled,
        })
        services.settings.enable_desktop_control = desktop_enabled
        services.settings.enable_game_control = desktop_enabled

        daemon = services.voice_daemon
        if daemon is not None:
            try:
                if voice_enabled:
                    daemon.start()
                else:
                    daemon.stop()
            except Exception:
                # The profile is durable even when an optional microphone backend is
                # temporarily unavailable; its runtime status explains the failure.
                pass
        try:
            services.companion.start()
            services.companion.show()
        except Exception:
            pass
        return {
            "identity": services.identity.get().to_dict(),
            "style": services.style.get().to_dict(),
            "preferences": _preferences_payload(),
        }

    @app.get("/api/activity-journal")
    def activity_journal(limit: int = 80, details: bool = False) -> list[dict[str, Any]]:
        rows = services.db.recent_action_logs(limit=max(1, min(int(limit), 300)))
        # Repeated UIA probes used to fill the page with indistinguishable
        # ``Window elements`` failures.  Keep every distinct event, but collapse
        # identical consecutive diagnostics into one honest row with a repeat count.
        output: list[dict[str, Any]] = []
        for row in rows:
            item = friendly_action(row, details=bool(details))
            key = (item.get("tool"), item.get("status"), item.get("summary"), item.get("reason"))
            previous_key = None
            if output:
                previous = output[-1]
                previous_key = (previous.get("tool"), previous.get("status"), previous.get("summary"), previous.get("reason"))
            if key == previous_key:
                output[-1]["repeat_count"] = int(output[-1].get("repeat_count", 1)) + 1
                continue
            item["repeat_count"] = 1
            output.append(item)
        return output[:max(1, min(int(limit), 300))]

    @app.delete("/api/activity-journal")
    def clear_activity_journal() -> dict[str, Any]:
        return {"ok": True, "deleted": services.db.clear_action_logs()}

    @app.get("/api/mail/status")
    def mail_status() -> dict[str, Any]:
        return services.mail.public_status() if services.mail is not None else {"configured": False}

    @app.put("/api/mail/config")
    def mail_config(request: MailConfigRequest) -> dict[str, Any]:
        if services.mail is None:
            raise HTTPException(status_code=503, detail="Почтовый модуль недоступен")
        try:
            return services.mail.configure(
                email_address=request.email, password=request.password, imap_host=request.imap_host,
                imap_port=request.imap_port, smtp_host=request.smtp_host, smtp_port=request.smtp_port,
                imap_ssl=request.imap_ssl, smtp_starttls=request.smtp_starttls,
                auto_move_obvious_spam=request.auto_move_obvious_spam,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/mail/config")
    def mail_disconnect() -> dict[str, Any]:
        if services.mail is None:
            return {"configured": False}
        return services.mail.disconnect()

    @app.post("/api/privacy/wipe")
    def privacy_wipe(request: PrivacyWipeRequest) -> dict[str, Any]:
        expected = "УДАЛИТЬ ВСЕ ДАННЫЕ"
        if request.confirmation.strip().upper() != expected:
            raise HTTPException(
                status_code=400,
                detail=f"Для подтверждения введи: {expected}",
            )
        result = wipe_personal_data(
            services,
            preserve_entitlement=bool(request.preserve_entitlement),
        )

        # Stop after the response has reached the local UI. The next launch starts the
        # short onboarding with clean memory; installed models and owner-created files
        # remain in place.
        stop_file = services.settings.root_dir / "logs" / "stop.request"
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch(exist_ok=True)

        def request_stop() -> None:
            time.sleep(1.25)
            try:
                services.companion.hide()
            except Exception:
                pass
            try:
                os._exit(0)
            except Exception:
                pass

        threading.Thread(target=request_stop, daemon=True, name="eirven-privacy-wipe").start()
        return {**result, "restart_required": True}

    @app.get("/api/updates/check")
    def check_updates() -> dict[str, Any]:
        if services.updater is None:
            return {"ok": False, "current": APP_VERSION, "build": APP_BUILD, "error": "Updater недоступен"}
        try:
            return services.updater.check()
        except Exception as exc:
            return {
                "ok": False, "current": APP_VERSION, "build": APP_BUILD,
                "error": str(exc)[:300],
                "distribution": services.updater.distribution.public(),
            }

    @app.get("/api/updates/status")
    def update_status() -> dict[str, Any]:
        if services.updater is None:
            return {"state": "unavailable", "error": "Updater недоступен"}
        return services.updater.status()

    @app.post("/api/updates/install")
    def install_update() -> dict[str, Any]:
        if services.updater is None:
            raise HTTPException(status_code=503, detail="Updater недоступен")
        result = services.updater.install()
        if not result.get("ok") and not result.get("deferred"):
            raise HTTPException(status_code=502, detail=str(result.get("error") or "Не удалось запустить обновление"))
        return result

    @app.get("/api/updates/distribution")
    def update_distribution() -> dict[str, Any]:
        if services.updater is None:
            return {"source": "unknown", "claimed_channel": "standard", "has_entitlement": False}
        return services.updater.distribution.public()

    @app.get("/api/companion")
    def companion_status() -> dict[str, Any]:
        return services.companion.status()

    @app.post("/api/companion/show")
    def companion_show(respect_setting: bool = False) -> dict[str, Any]:
        # respect_setting — для показа при запуске Эрви: тогда сфера появляется,
        # только если она включена в настройках. Раньше лончер вызывал её
        # безусловно, и сфера всплывала после каждого запуска, даже выключенная.
        # Без флага — явная просьба человека (например, из трея): показать всегда.
        if respect_setting:
            try:
                enabled = bool(services.identity.get().desktop_avatar_enabled)
            except Exception:
                enabled = True
            if not enabled:
                log_event(services.settings.root_dir, "COMPANION_SHOW_SKIPPED", reason="выключена в настройках")
                return services.companion.status()
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
            "styles": [
                {"key": key, **{k: v for k, v in style.items() if k != "prompt"}}
                for key, style in services.telegram.REPLY_STYLES.items()
            ],
            "reply_style": str(services.db.get_setting("telegram_reply_style", "manager") or "manager"),
            "autostart_monitor": bool(services.db.get_setting("telegram_autostart_monitor", False)),
            "status": services.telegram.status(),
            "rules": services.telegram.rules(),
            "config": services.telegram.config(),
            "monitor": services.telegram.monitor_config(),
        }

    @app.put("/api/telegram/config")
    def telegram_config(request: TelegramConfigRequest) -> dict[str, Any]:
        try:
            return services.telegram.save_config(request.api_id, request.api_hash, request.phone)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/login/code")
    def telegram_login_code(force_sms: bool = False) -> dict[str, Any]:
        try:
            return services.telegram.request_login_code(force_sms=force_sms)
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

    @app.post("/api/telegram/reply-unread")
    def telegram_reply_unread(request: TelegramMonitorRequest) -> dict[str, Any]:
        try:
            return services.telegram.reply_unread(
                exclude_groups=request.exclude_groups,
                exclude_channels=request.exclude_channels,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/telegram/chats")
    def telegram_chats(limit: int = 60) -> dict[str, Any]:
        try:
            return services.telegram.list_chats(limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/chat-prefs")
    def telegram_chat_prefs(request: TelegramChatPrefsRequest) -> dict[str, Any]:
        try:
            return {"ok": True, "prefs": services.telegram.set_chat_prefs(
                excluded=request.excluded, styles=request.styles,
            )}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/broadcast")
    def telegram_broadcast(request: TelegramBroadcastRequest) -> dict[str, Any]:
        try:
            if request.when:
                return services.telegram.schedule_broadcast(request.chats, request.message, request.when)
            return services.telegram.broadcast(request.chats, request.message)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/teaching")
    def teaching_status() -> dict[str, Any]:
        session = getattr(services, "teaching", None)
        return session.status() if session else {"active": False, "steps": 0}

    @app.post("/api/teaching/start")
    def teaching_start(request: TeachingStartRequest) -> dict[str, Any]:
        session = getattr(services, "teaching", None)
        if session is None:
            raise HTTPException(status_code=400, detail="Обучение недоступно")
        goal = request.goal.strip() or str(services.db.get_setting("last_failed_goal", "") or "")
        return session.start(goal)

    @app.post("/api/teaching/finish")
    def teaching_finish() -> dict[str, Any]:
        session = getattr(services, "teaching", None)
        if session is None:
            raise HTTPException(status_code=400, detail="Обучение недоступно")
        return session.finish()

    @app.post("/api/transfer/export")
    def transfer_export(request: TransferRequest) -> dict[str, Any]:
        """Выгрузить личные данные в один файл для переноса на другой компьютер."""
        target = str(request.path or "").strip()
        if not target:
            target = str(Path.home() / "Desktop" / "eirven-backup.zip")
        script = services.settings.root_dir / "scripts" / "transfer_data.ps1"
        if not script.is_file():
            raise HTTPException(status_code=400, detail="Скрипт переноса не найден")
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Export", target, "-Quiet"],
                cwd=str(services.settings.root_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-500:]
                raise HTTPException(status_code=400, detail=detail or "Выгрузка не удалась")
            size = Path(target).stat().st_size if Path(target).is_file() else 0
            return {
                "ok": True, "path": target, "size": size,
                "message": f"Готово: {target} ({round(size / 1048576, 1)} МБ). "
                           "Перенеси файл на новый компьютер и нажми там «Восстановить».",
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/transfer/import")
    def transfer_import(request: TransferRequest) -> dict[str, Any]:
        """Восстановить данные из файла, снятого на прежнем компьютере."""
        source = str(request.path or "").strip()
        if not source or not Path(source).is_file():
            raise HTTPException(status_code=400, detail="Укажи путь к файлу выгрузки")
        script = services.settings.root_dir / "scripts" / "transfer_data.ps1"
        if not script.is_file():
            raise HTTPException(status_code=400, detail="Скрипт переноса не найден")
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Import", source, "-Quiet"],
                cwd=str(services.settings.root_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-500:]
                raise HTTPException(status_code=400, detail=detail or "Восстановление не удалось")
            return {
                "ok": True,
                "message": "Данные восстановлены. Перезапусти Эрви. "
                           "Пароль почты и ключи Telegram нужно ввести заново.",
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/prefs")
    def telegram_prefs(request: TelegramPrefsRequest) -> dict[str, Any]:
        if request.reply_style is not None:
            key = str(request.reply_style).strip()
            if key not in services.telegram.REPLY_STYLES:
                raise HTTPException(status_code=400, detail=f"Неизвестный стиль: {key}")
            services.db.set_setting("telegram_reply_style", key)
        if request.autostart_monitor is not None:
            services.db.set_setting("telegram_autostart_monitor", bool(request.autostart_monitor))
        return {
            "reply_style": str(services.db.get_setting("telegram_reply_style", "manager") or "manager"),
            "autostart_monitor": bool(services.db.get_setting("telegram_autostart_monitor", False)),
        }

    @app.put("/api/telegram/general-prompt")
    def telegram_general_prompt(request: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "general_prompt": services.telegram.save_general_prompt(str(request.get("general_prompt") or ""))}

    @app.post("/api/telegram/monitor")
    def telegram_monitor(request: TelegramMonitorRequest) -> dict[str, Any]:
        try:
            config = services.telegram.configure_monitor(
                exclude_groups=request.exclude_groups,
                exclude_channels=request.exclude_channels,
                max_per_hour=request.max_per_hour,
            )
            status = services.telegram.start()
            return {"monitor": config, "status": status}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/task")
    def telegram_task(request: TelegramTaskRequest) -> dict[str, Any]:
        try:
            return services.telegram.execute_api_task(request.task)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/pairing/start")
    def telegram_pairing_start() -> dict[str, Any]:
        try:
            return services.telegram.begin_pairing(replace=True, ttl_seconds=600)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/telegram/pairing")
    def telegram_pairing_status() -> dict[str, Any]:
        return services.telegram.pairing_status()

    @app.post("/api/telegram/pairing/cancel")
    def telegram_pairing_cancel() -> dict[str, Any]:
        return services.telegram.cancel_pairing()

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
        add("Компоненты интеллекта", bool(installed), f"Найдено моделей: {len(installed)}" if installed else "Не найдены")
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
        voice_detail = "Распознавание проверено" if voice.get("stt_ready") else "Компонент не готов"
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

    @app.get("/api/video")
    def video_status() -> dict[str, Any]:
        return services.video.status()

    @app.post("/api/video/open")
    def open_video_folder() -> dict[str, Any]:
        result = services.video.open_inbox()
        if not result.get("opened") and result.get("error"):
            raise HTTPException(status_code=400, detail=str(result.get("error")))
        return result

    return app
