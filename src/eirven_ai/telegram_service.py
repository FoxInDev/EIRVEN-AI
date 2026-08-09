from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .config import Settings
from .database import Database
from .llm import ModelGateway
from .style import StyleStore


class TelegramError(RuntimeError):
    pass


class TelegramMonitor:
    """Optional Telegram monitor for explicitly allow-listed chats.

    It uses a dedicated Telethon session in data/telegram.session. Login is a one-time,
    interactive operation. The monitor never enables itself merely because credentials exist.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        gateway: ModelGateway,
        style: StyleStore,
    ):
        self.settings = settings
        self.db = db
        self.gateway = gateway
        self.style = style
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"running": False, "message": "Выключено"}
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._last_reply: dict[str, float] = {}
        self._auth_loop: asyncio.AbstractEventLoop | None = None
        self._auth_thread: threading.Thread | None = None
        self._auth_client: Any = None
        self._auth_phone_code_hash: str = ""
        stored = self.db.get_setting("telegram_config", {})
        if isinstance(stored, dict):
            self.settings.telegram_api_id = int(stored.get("api_id") or self.settings.telegram_api_id or 0)
            self.settings.telegram_api_hash = str(stored.get("api_hash") or self.settings.telegram_api_hash or "")
            self.settings.telegram_phone = str(stored.get("phone") or self.settings.telegram_phone or "")


    def config(self) -> dict[str, Any]:
        value = self.settings.telegram_api_hash
        masked = (value[:4] + "…" + value[-3:]) if len(value) > 9 else ("настроен" if value else "")
        return {
            "api_id": self.settings.telegram_api_id or "",
            "api_hash_masked": masked,
            "phone": self.settings.telegram_phone,
            "configured": bool(self.settings.telegram_api_id and self.settings.telegram_api_hash and self.settings.telegram_phone),
            "autostart": bool(self.db.get_setting("telegram_autostart", False)),
        }

    def save_config(self, api_id: int, api_hash: str, phone: str) -> dict[str, Any]:
        api_id = int(api_id)
        api_hash = api_hash.strip()
        phone = phone.strip()
        if api_id <= 0 or len(api_hash) < 20 or not phone:
            raise TelegramError("Укажите API ID, API Hash и номер телефона")
        self.settings.telegram_api_id = api_id
        self.settings.telegram_api_hash = api_hash
        self.settings.telegram_phone = phone
        self.db.set_setting("telegram_config", {"api_id": api_id, "api_hash": api_hash, "phone": phone})
        return self.config()

    def _ensure_auth_loop(self) -> asyncio.AbstractEventLoop:
        if self._auth_loop and self._auth_thread and self._auth_thread.is_alive():
            return self._auth_loop
        ready = threading.Event()
        loop = asyncio.new_event_loop()
        def runner() -> None:
            asyncio.set_event_loop(loop)
            self._auth_loop = loop
            ready.set()
            loop.run_forever()
        self._auth_thread = threading.Thread(target=runner, daemon=True, name="telegram-auth")
        self._auth_thread.start()
        ready.wait(timeout=5)
        return loop

    def _auth_call(self, coroutine: Any, timeout: int = 90) -> Any:
        loop = self._ensure_auth_loop()
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return future.result(timeout=timeout)

    def request_login_code(self) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            raise TelegramError("Сначала остановите мониторинг Telegram")
        if not self.config()["configured"]:
            raise TelegramError("Сначала сохраните данные Telegram API")
        return self._auth_call(self._request_login_code_async())

    async def _request_login_code_async(self) -> dict[str, Any]:
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise TelegramError("Telethon не установлен. Запустите восстановление компонентов") from exc
        if self._auth_client:
            try:
                await self._auth_client.disconnect()
            except Exception:
                pass
        session = str(self.settings.data_dir / "telegram")
        client = TelegramClient(session, self.settings.telegram_api_id, self.settings.telegram_api_hash)
        await client.connect()
        self._auth_client = client
        if await client.is_user_authorized():
            await client.disconnect()
            self._auth_client = None
            return {"authorized": True, "message": "Telegram уже авторизован"}
        sent = await client.send_code_request(self.settings.telegram_phone)
        self._auth_phone_code_hash = str(sent.phone_code_hash)
        return {"authorized": False, "code_sent": True, "message": "Код отправлен в Telegram"}

    def confirm_login(self, code: str, password: str = "") -> dict[str, Any]:
        return self._auth_call(self._confirm_login_async(code.strip(), password), timeout=120)

    async def _confirm_login_async(self, code: str, password: str) -> dict[str, Any]:
        if not self._auth_client:
            raise TelegramError("Сначала запросите код")
        try:
            from telethon.errors import SessionPasswordNeededError
            try:
                await self._auth_client.sign_in(
                    phone=self.settings.telegram_phone,
                    code=code,
                    phone_code_hash=self._auth_phone_code_hash,
                )
            except SessionPasswordNeededError:
                if not password:
                    return {"authorized": False, "requires_password": True, "message": "Нужен пароль двухэтапной защиты"}
                await self._auth_client.sign_in(password=password)
            me = await self._auth_client.get_me()
            await self._auth_client.disconnect()
            self._auth_client = None
            self._auth_phone_code_hash = ""
            return {"authorized": True, "message": f"Авторизация завершена: {getattr(me, 'first_name', '')}"}
        except Exception:
            # Keep the client alive so a corrected code/password can be submitted.
            raise

    def close_auth(self) -> None:
        if self._auth_loop:
            try:
                if self._auth_client:
                    asyncio.run_coroutine_threadsafe(self._auth_client.disconnect(), self._auth_loop).result(timeout=5)
            except Exception:
                pass
            self._auth_loop.call_soon_threadsafe(self._auth_loop.stop)
        if self._auth_thread:
            self._auth_thread.join(timeout=3)
        self._auth_loop = None
        self._auth_thread = None
        self._auth_client = None

    def rules(self) -> list[dict[str, Any]]:
        value = self.db.get_setting("telegram_rules", [])
        return value if isinstance(value, list) else []

    def save_rules(self, rules: list[dict[str, Any]]) -> None:
        normalized: list[dict[str, Any]] = []
        for rule in rules[:50]:
            chats = rule.get("chats") or []
            if isinstance(chats, str):
                chats = [part.strip() for part in chats.split(",") if part.strip()]
            normalized.append(
                {
                    "name": str(rule.get("name") or "Правило")[:100],
                    "enabled": bool(rule.get("enabled", True)),
                    "chats": [str(item).strip() for item in chats if str(item).strip()][:100],
                    "pattern": str(rule.get("pattern") or ".*")[:500],
                    "reply": str(rule.get("reply") or "Привет")[:4000],
                    "mode": "ai" if rule.get("mode") == "ai" else "template",
                    "max_per_hour": max(1, min(int(rule.get("max_per_hour") or 20), 120)),
                }
            )
        self.db.set_setting("telegram_rules", normalized)

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def start(self) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return self.status()
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            raise TelegramError("Нужны Telegram API ID и API Hash")
        enabled = [rule for rule in self.rules() if rule.get("enabled")]
        if not enabled:
            raise TelegramError("Нет включённых правил")
        if any(not rule.get("chats") for rule in enabled):
            raise TelegramError("У каждого правила должен быть список разрешённых чатов")
        self._stop.clear()
        self._status = {"running": True, "message": "Подключаю Telegram…"}
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="telegram-monitor")
        self._thread.start()
        self.db.set_setting("telegram_autostart", True)
        return {"running": True, "message": "Запуск Telegram-монитора"}

    def stop(self, persist: bool = True) -> dict[str, Any]:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread:
            self._thread.join(timeout=8)
        self._status = {"running": False, "message": "Остановлено"}
        if persist:
            self.db.set_setting("telegram_autostart", False)
        return self.status()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._status = {"running": False, "message": f"Ошибка: {exc}"}

    async def _run(self) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError as exc:
            raise TelegramError("Telethon не установлен. Запустите repair_windows.ps1") from exc

        self._loop = asyncio.get_running_loop()
        session = str(self.settings.data_dir / "telegram")
        client = TelegramClient(
            session,
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash,
        )
        self._client = client
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramError(
                "Telegram ещё не подключён. Откройте «Настройки» → «Telegram», введите данные и подтвердите вход"
            )

        @client.on(events.NewMessage(incoming=True))
        async def on_message(event: Any) -> None:
            await self._handle_event(event)

        me = await client.get_me()
        self._status = {
            "running": True,
            "message": f"Мониторинг запущен: {getattr(me, 'first_name', '')}",
        }
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
        finally:
            await client.disconnect()
            self._client = None
            self._loop = None
            self._status = {"running": False, "message": "Остановлено"}

    async def _handle_event(self, event: Any) -> None:
        text = (getattr(event, "raw_text", "") or "").strip()
        if not text:
            return
        chat = await event.get_chat()
        sender = await event.get_sender()
        chat_id = str(getattr(event, "chat_id", "") or "")
        username = str(getattr(chat, "username", "") or "")
        title = str(
            getattr(chat, "title", "")
            or getattr(chat, "first_name", "")
            or username
            or chat_id
        )
        identifiers = {chat_id.lower(), username.lower(), title.lower()}

        for rule in self.rules():
            if not rule.get("enabled"):
                continue
            allowed = {str(item).lower().lstrip("@") for item in rule.get("chats") or []}
            if not allowed or ("*" not in allowed and not any(identifier.lstrip("@") in allowed for identifier in identifiers)):
                continue
            try:
                if not re.search(str(rule.get("pattern") or ".*"), text, re.IGNORECASE):
                    continue
            except re.error:
                continue
            if not self._rate_allowed(chat_id, int(rule.get("max_per_hour") or 20)):
                continue

            sender_name = str(
                getattr(sender, "first_name", "")
                or getattr(sender, "username", "")
                or "собеседник"
            )
            if rule.get("mode") == "ai":
                reply = await asyncio.to_thread(
                    self._ai_reply,
                    text,
                    title,
                    sender_name,
                    str(rule.get("reply") or ""),
                )
            else:
                reply = str(rule.get("reply") or "Привет").format(
                    name=sender_name,
                    chat=title,
                    text=text,
                )
            reply = reply.strip()[:4000]
            if reply:
                await event.reply(reply)
                self._record_reply(chat_id)
            break

    def _ai_reply(self, text: str, chat: str, sender: str, instruction: str) -> str:
        message = self.gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        f"{self.style.get().prompt()}\n"
                        "Сформулируй один короткий ответ для Telegram. Не добавляй пояснений."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Чат: {chat}\nОтправитель: {sender}\nСообщение: {text}\n"
                        f"Правило владельца: {instruction}"
                    ),
                },
            ],
            model=self.settings.fast_model,
            temperature=0.55,
            think=False,
            num_ctx=self.settings.chat_num_ctx,
            num_predict=250,
        )
        return str(message.get("content") or "")

    def _rate_allowed(self, chat_id: str, max_per_hour: int) -> bool:
        now = time.time()
        if now - self._last_reply.get(chat_id, 0) < self.settings.telegram_min_reply_interval:
            return False
        history = self._recent[chat_id]
        while history and now - history[0] > 3600:
            history.popleft()
        return len(history) < max_per_hour

    def _record_reply(self, chat_id: str) -> None:
        now = time.time()
        self._last_reply[chat_id] = now
        self._recent[chat_id].append(now)
