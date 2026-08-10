from __future__ import annotations

import asyncio
import secrets
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .config import Settings
from .database import Database
from .llm import ModelGateway
from .style import StyleStore
from .dialogue import is_chat_pairing_request


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
        self._remote_handler: Any = None
        self._pairing_lock = threading.RLock()
        self._pairing: dict[str, Any] = {}
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
            "authorized": bool(self.db.get_setting("telegram_authorized", False)),
            "autostart": bool(self.db.get_setting("telegram_autostart", False)),
        }

    def save_config(self, api_id: int, api_hash: str, phone: str) -> dict[str, Any]:
        previous = (self.settings.telegram_api_id, self.settings.telegram_api_hash, self.settings.telegram_phone)
        api_id = int(api_id)
        api_hash = api_hash.strip()
        phone = phone.strip()
        if api_id <= 0 or len(api_hash) < 20 or not phone:
            raise TelegramError("Укажите API ID, API Hash и номер телефона")
        self.settings.telegram_api_id = api_id
        self.settings.telegram_api_hash = api_hash
        self.settings.telegram_phone = phone
        self.db.set_setting("telegram_config", {"api_id": api_id, "api_hash": api_hash, "phone": phone})
        if previous != (api_id, api_hash, phone):
            self.db.set_setting("telegram_authorized", False)
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
            me = await client.get_me()
            await client.disconnect()
            self._auth_client = None
            self.db.set_setting("telegram_authorized", True)
            return {
                "authorized": True, "message": "Telegram уже авторизован",
                "user_id": str(getattr(me, "id", "") or ""),
                "username": str(getattr(me, "username", "") or ""),
            }
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
            self.db.set_setting("telegram_authorized", True)
            return {
                "authorized": True, "message": f"Авторизация завершена: {getattr(me, 'first_name', '')}",
                "user_id": str(getattr(me, "id", "") or ""),
                "username": str(getattr(me, "username", "") or ""),
            }
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

    def bind_remote_handler(self, handler: Any) -> None:
        """Bind the local agent without coupling Telegram transport to ChatService."""
        self._remote_handler = handler

    def _pairing_snapshot(self, *, include_code: bool = False) -> dict[str, Any]:
        with self._pairing_lock:
            value = dict(self._pairing)
            if value and float(value.get("expires_at") or 0) <= time.time():
                self._pairing = {}
                value = {}
        result = {
            "active": bool(value),
            "expires_at": float(value.get("expires_at") or 0),
            "replace": bool(value.get("replace", True)),
        }
        if include_code and value:
            result["code"] = str(value.get("code") or "")
        return result

    def pairing_status(self) -> dict[str, Any]:
        return self._pairing_snapshot(include_code=True)

    def begin_pairing(self, *, replace: bool = True, ttl_seconds: int = 600) -> dict[str, Any]:
        """Arm a brief owner-controlled window that can bind the current Telegram chat."""
        if not self.config().get("configured"):
            raise TelegramError("Сначала заполните API ID, API Hash и номер телефона")
        if not self.config().get("authorized") and not (self._thread and self._thread.is_alive() and self._client is not None):
            raise TelegramError("Сначала запросите код Telegram и подтвердите вход")
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._pairing_lock:
            self._pairing = {
                "code": code,
                "replace": bool(replace),
                "created_at": time.time(),
                "expires_at": time.time() + max(120, min(int(ttl_seconds or 600), 900)),
            }
        try:
            self.start()
        except Exception:
            with self._pairing_lock:
                self._pairing = {}
            raise
        return {
            **self._pairing_snapshot(include_code=True),
            "message": (
                "Открой нужный Telegram-чат и отправь от своего аккаунта: "
                "«Эрви, сюда буду отправлять команды». Для входящего сообщения добавь код " + code
            ),
        }

    def cancel_pairing(self) -> dict[str, Any]:
        with self._pairing_lock:
            self._pairing = {}
        return {"active": False, "message": "Привязка отменена"}

    def remote_config(self) -> dict[str, Any]:
        value = self.db.get_setting("telegram_remote_control", {})
        value = value if isinstance(value, dict) else {}
        chats = value.get("chats") or []
        if isinstance(chats, str):
            chats = [part.strip() for part in chats.split(",") if part.strip()]
        return {
            "enabled": bool(value.get("enabled", False)),
            "chats": [str(item).strip() for item in chats if str(item).strip()][:20],
            "prefix": str(value.get("prefix") or "Эрви,").strip()[:40],
        }

    def save_remote_config(self, enabled: bool, chats: list[str], prefix: str = "Эрви,") -> dict[str, Any]:
        normalized = []
        for item in chats[:20]:
            value = str(item).strip().lstrip("@")
            if value and value != "*" and value.casefold() not in {x.casefold() for x in normalized}:
                normalized.append(value)
        prefix = str(prefix or "Эрви,").strip()[:40]
        if enabled and not normalized:
            raise TelegramError("Для удалённого управления укажите точный ID, username или название разрешённого чата")
        if len(prefix) < 2:
            raise TelegramError("Префикс команды должен быть не короче двух символов")
        config = {"enabled": bool(enabled), "chats": normalized, "prefix": prefix}
        self.db.set_setting("telegram_remote_control", config)
        return config

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
        return {**dict(self._status), "pairing": self._pairing_snapshot(include_code=False)}

    def start(self) -> dict[str, Any]:
        if self._thread and self._thread.is_alive():
            return self.status()
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            raise TelegramError("Нужны Telegram API ID и API Hash")
        enabled = [rule for rule in self.rules() if rule.get("enabled")]
        remote = self.remote_config()
        pairing = self._pairing_snapshot(include_code=False)
        if not enabled and not remote.get("enabled") and not pairing.get("active"):
            raise TelegramError("Нет включённых правил или удалённого управления")
        if any(not rule.get("chats") for rule in enabled):
            raise TelegramError("У каждого правила должен быть список разрешённых чатов")
        if remote.get("enabled") and (not remote.get("chats") or self._remote_handler is None):
            raise TelegramError("Удалённое управление не готово: проверьте разрешённые чаты")
        if pairing.get("active") and self._remote_handler is None:
            raise TelegramError("Контур удалённого управления ещё не подключён")
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
            self.db.set_setting("telegram_authorized", False)
            await client.disconnect()
            raise TelegramError(
                "Telegram ещё не подключён. Откройте «Настройки» → «Telegram», введите данные и подтвердите вход"
            )

        # Remote control may originate from the owner's Saved Messages and is therefore
        # an outgoing update.  Automatic reply rules below still ignore outgoing events.
        @client.on(events.NewMessage())
        async def on_message(event: Any) -> None:
            await self._handle_event(event)

        me = await client.get_me()
        self.db.set_setting("telegram_authorized", True)
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

        pairing = self._pairing_snapshot(include_code=True)
        if pairing.get("active"):
            code = str(pairing.get("code") or "")
            owner_outgoing = bool(getattr(event, "out", False))
            explicit_code = bool(code and re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text))
            natural_bind = is_chat_pairing_request(text)
            if natural_bind and (owner_outgoing or explicit_code):
                previous = self.remote_config()
                chats = [chat_id] if pairing.get("replace", True) else list(previous.get("chats") or []) + [chat_id]
                saved = self.save_remote_config(True, chats, str(previous.get("prefix") or "Эрви,"))
                with self._pairing_lock:
                    self._pairing = {}
                await event.reply(
                    f"Готово. Этот чат привязан к Эрви (ID {chat_id}). "
                    f"Теперь команды начинай с «{saved.get('prefix') or 'Эрви,'}»."
                )
                self._status = {**self._status, "message": f"Telegram привязан: {title}"}
                return

        remote = self.remote_config()
        allowed_remote = {str(item).casefold().lstrip("@") for item in remote.get("chats") or []}
        prefix = str(remote.get("prefix") or "Эрви,").strip()
        command = self._remote_command(text, prefix)
        remote_match = bool(
            remote.get("enabled")
            and allowed_remote
            # The connected owner's own message is outgoing on every device. Requiring
            # that flag prevents another member of an allow-listed private/group chat
            # from issuing PC commands merely by copying the wake prefix.
            and bool(getattr(event, "out", False))
            and any(identifier.casefold().lstrip("@") in allowed_remote for identifier in identifiers)
            and command is not None
        )
        if remote_match:
            command = str(command or "").strip(" \t\r\n,:;.!?—-")
            if not command:
                await event.reply("Я на связи. Напиши задачу после префикса.")
                return
            if self._remote_handler is None:
                await event.reply("Контур удалённого управления ещё не готов.")
                return
            try:
                result = await asyncio.to_thread(self._remote_handler, command, chat_id)
                if isinstance(result, dict):
                    answer = str(result.get("answer") or result.get("message") or "Готово.")
                else:
                    answer = str(result or "Готово.")
            except Exception as exc:
                answer = f"План прервался: {exc}. Я сохранила контекст — уточни команду, и продолжу с другого шага."
            await event.reply(answer.strip()[:4000])
            return

        if bool(getattr(event, "out", False)):
            return

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

    @staticmethod
    def _remote_command(text: str, configured_prefix: str) -> str | None:
        """Accept the configured prefix plus common wake-name punctuation variants."""
        raw = str(text or "").strip()
        prefix = str(configured_prefix or "Эрви,").strip()
        if prefix and raw.casefold().startswith(prefix.casefold()):
            return raw[len(prefix):]
        match = re.match(r"^\s*(?:эрви|эйрви|эйрвен|эйрвэн|eirven)\b\s*[,;:!?.—-]*\s*(.*)$", raw, re.I | re.S)
        if match:
            return match.group(1)
        return None

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
