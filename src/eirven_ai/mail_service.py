# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import base64
import ctypes
import email
import email.header
import email.utils
import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


class MailError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_protect(text: str) -> str:
    raw = text.encode("utf-8")
    if not raw:
        return ""
    if __import__("platform").system() == "Darwin":
        account = "eirven-" + uuid.uuid4().hex
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account, "-s", "EIRVEN AI", "-w", text],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise MailError("macOS не смогла сохранить секрет в Связке ключей")
        return "keychain:" + account
    if os.name != "nt":
        return "dev:" + base64.b64encode(raw).decode("ascii")
    buf = ctypes.create_string_buffer(raw)
    in_blob = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(ctypes.byref(in_blob), "EIRVEN mail", None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise MailError("Windows не смогла зашифровать пароль почты")
    try:
        data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(data).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(value: str) -> str:
    if not value:
        return ""
    if value.startswith("keychain:"):
        if __import__("platform").system() != "Darwin":
            raise MailError("Этот секрет связан со Связкой ключей macOS")
        account = value.split(":", 1)[1]
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", "EIRVEN AI", "-w"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise MailError("Секрет не найден в Связке ключей macOS")
        return result.stdout.rstrip("\r\n")
    if value.startswith("dev:"):
        if os.name == "nt":
            raise MailError("Небезопасный тестовый секрет нельзя использовать в Windows")
        return base64.b64decode(value[4:]).decode("utf-8")
    if not value.startswith("dpapi:"):
        raise MailError("Неизвестный формат защищённого пароля")
    if os.name != "nt":
        raise MailError("Расшифровка DPAPI доступна только в Windows")
    data = base64.b64decode(value[6:])
    buf = ctypes.create_string_buffer(data)
    in_blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise MailError("Windows не смогла расшифровать пароль почты для текущего пользователя")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _decode_header(value: str) -> str:
    parts = []
    for data, encoding in email.header.decode_header(value or ""):
        if isinstance(data, bytes):
            for codec in (encoding, "utf-8", "cp1251", "latin-1"):
                if not codec:
                    continue
                try:
                    parts.append(data.decode(codec, errors="replace")); break
                except LookupError:
                    continue
        else:
            parts.append(str(data))
    return "".join(parts).strip()


def _plain_body(msg: email.message.Message, limit: int = 5000) -> str:
    chunks: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "").casefold()
        if ctype != "text/plain" or "attachment" in disp:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        chunks.append(text)
        if sum(len(x) for x in chunks) >= limit:
            break
    text = "\n".join(chunks)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def _redact_sensitive_preview(text: str) -> str:
    """Hide authentication secrets before mail content reaches chat, logs or a model."""
    value = str(text or "")
    sensitive_context = re.search(
        r"(?:одноразов\w*\s+код|код\w*\s+(?:вход|подтвержд|провер)|verification\s+code|"
        r"security\s+code|auth(?:entication)?\s+code|\botp\b|\b2fa\b|двух(?:шаг|этап))",
        value,
        re.I,
    )
    if sensitive_context:
        value = re.sub(r"(?<!\d)\d{4,8}(?!\d)", "[скрытый одноразовый код]", value)
        value = re.sub(
            r"((?:verification|security|auth(?:entication)?|одноразовый)\s+code\s*[:\-]?\s*)[A-Z0-9_-]{4,16}",
            r"\1[скрыто]",
            value,
            flags=re.I,
        )
    return re.sub(
        r"((?:api[ _-]?key|пароль|password|secret|token)\s*[:=]\s*)\S+",
        r"\1[скрыто]",
        value,
        flags=re.I,
    )


@dataclass(slots=True)
class MailConfig:
    email: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    smtp_starttls: bool = True
    imap_ssl: bool = True
    auto_move_obvious_spam: bool = False


class MailService:
    """Local IMAP/SMTP helper with Windows-DPAPI credential storage.

    The default mode is deliberately conservative: messages are read with BODY.PEEK,
    obvious server-labelled spam may be moved only when the user enabled it, drafts are
    prepared locally, and sending always requires a second explicit confirmation turn.
    """

    CONFIG_KEY = "mail_config_v1"
    SECRET_KEY = "mail_secret_v1"
    DRAFTS_KEY = "mail_drafts_v1"
    PENDING_KEY = "mail_pending_send_v1"
    PENDING_SCHEMA = "eirven.pending-action.v1"
    MONITOR_KEY = "mail_monitor_enabled_v1"

    def __init__(self, db: Any, gateway: Any, settings: Any, style: Any = None):
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self.style = style
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monitor_status: dict[str, Any] = {"running": False, "message": "Остановлено"}
        self._pending_lock = threading.RLock()

    def configured(self) -> bool:
        cfg = self.db.get_setting(self.CONFIG_KEY, {}) or {}
        secret = str(self.db.get_setting(self.SECRET_KEY, "") or "")
        return bool(isinstance(cfg, dict) and cfg.get("email") and cfg.get("imap_host") and cfg.get("smtp_host") and secret)

    def public_status(self) -> dict[str, Any]:
        cfg = self.db.get_setting(self.CONFIG_KEY, {}) or {}
        email_addr = str(cfg.get("email") or "") if isinstance(cfg, dict) else ""
        return {
            "configured": self.configured(),
            "email": email_addr,
            "imap_host": str(cfg.get("imap_host") or "") if isinstance(cfg, dict) else "",
            "imap_port": int(cfg.get("imap_port") or 993) if isinstance(cfg, dict) else 993,
            "smtp_host": str(cfg.get("smtp_host") or "") if isinstance(cfg, dict) else "",
            "smtp_port": int(cfg.get("smtp_port") or 587) if isinstance(cfg, dict) else 587,
            "imap_ssl": bool(cfg.get("imap_ssl", True)) if isinstance(cfg, dict) else True,
            "smtp_starttls": bool(cfg.get("smtp_starttls", True)) if isinstance(cfg, dict) else True,
            "auto_move_obvious_spam": bool(cfg.get("auto_move_obvious_spam", False)) if isinstance(cfg, dict) else False,
            "secret_storage": "защищённое хранилище Windows" if os.name == "nt" else ("Связка ключей macOS" if __import__("platform").system() == "Darwin" else "локальное хранилище"),
            "monitor": dict(self._monitor_status),
        }

    def set_auto_move_obvious_spam(self, enabled: bool) -> dict[str, Any]:
        """Persist only the reversible, high-confidence spam preference.

        This never deletes mail permanently. The review loop moves messages only when
        the mail server itself labelled them as spam and a Spam/Junk folder exists.
        """
        cfg = self.db.get_setting(self.CONFIG_KEY, {}) or {}
        if not isinstance(cfg, dict) or not cfg:
            raise MailError("Почта не подключена. Открой Настройки → Почта.")
        cfg = dict(cfg)
        cfg["auto_move_obvious_spam"] = bool(enabled)
        self.db.set_setting(self.CONFIG_KEY, cfg)
        return self.public_status()

    def configure(self, *, email_address: str, password: str, imap_host: str, imap_port: int,
                  smtp_host: str, smtp_port: int, imap_ssl: bool = True,
                  smtp_starttls: bool = True, auto_move_obvious_spam: bool = False) -> dict[str, Any]:
        address = str(email_address or "").strip()
        if "@" not in address or len(address) > 320:
            raise MailError("Укажи корректный email")
        password = str(password or "")
        if not password:
            previous = self.db.get_setting(self.CONFIG_KEY, {}) or {}
            same_account = isinstance(previous, dict) and str(previous.get("email") or "").casefold() == address.casefold()
            if same_account:
                try:
                    password = _dpapi_unprotect(str(self.db.get_setting(self.SECRET_KEY, "") or ""))
                except Exception:
                    password = ""
        if not password:
            raise MailError("При первом подключении нужен пароль приложения. После успешной проверки повторно вводить его не придётся")
        cfg = {
            "email": address,
            "imap_host": str(imap_host or "").strip(),
            "imap_port": int(imap_port or 993),
            "smtp_host": str(smtp_host or "").strip(),
            "smtp_port": int(smtp_port or 587),
            "imap_ssl": bool(imap_ssl),
            "smtp_starttls": bool(smtp_starttls),
            "auto_move_obvious_spam": bool(auto_move_obvious_spam),
        }
        if not cfg["imap_host"] or not cfg["smtp_host"]:
            raise MailError("Укажи IMAP и SMTP серверы")
        protected = _dpapi_protect(password)
        # Verify credentials before persisting them.
        self._test_config(cfg, password)
        self.db.set_setting(self.CONFIG_KEY, cfg)
        self.db.set_setting(self.SECRET_KEY, protected)
        return self.public_status()

    def disconnect(self) -> dict[str, Any]:
        self.stop_monitor()
        for key in (self.CONFIG_KEY, self.SECRET_KEY, self.DRAFTS_KEY, self.PENDING_KEY):
            self.db.set_setting(key, {} if key != self.SECRET_KEY else "")
        return self.public_status()

    def start_monitor(self, *, interval_seconds: int = 300) -> dict[str, Any]:
        if not self.configured():
            raise MailError("Почта не подключена. Открой Настройки → Почта.")
        if self._monitor_thread and self._monitor_thread.is_alive():
            return dict(self._monitor_status)
        interval = max(60, min(int(interval_seconds or 300), 3600))
        self._monitor_stop.clear()
        self.db.set_setting(self.MONITOR_KEY, True)
        self._monitor_status = {"running": True, "message": "Фоновая проверка почты запущена", "interval_seconds": interval}
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(interval,), daemon=True, name="eirven-mail-monitor",
        )
        self._monitor_thread.start()
        return dict(self._monitor_status)

    def resume_monitor_if_enabled(self) -> dict[str, Any]:
        if bool(self.db.get_setting(self.MONITOR_KEY, False)) and self.configured():
            return self.start_monitor()
        return dict(self._monitor_status)

    def stop_monitor(self) -> dict[str, Any]:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._monitor_thread = None
        self.db.set_setting(self.MONITOR_KEY, False)
        self._monitor_status = {"running": False, "message": "Фоновая проверка почты остановлена"}
        return dict(self._monitor_status)

    def _monitor_loop(self, interval_seconds: int) -> None:
        while not self._monitor_stop.is_set():
            try:
                result = self.review(limit=30, prepare_replies=True)
                self._monitor_status = {
                    "running": True,
                    "message": "Почта проверена; черновики обновлены",
                    "interval_seconds": interval_seconds,
                    "unread": int(result.get("unread", 0) or 0),
                    "drafts": len(result.get("drafts") or []),
                    "checked_at": time.time(),
                }
            except Exception as exc:
                self._monitor_status = {
                    "running": True,
                    "message": f"Последняя проверка не удалась: {exc}",
                    "interval_seconds": interval_seconds,
                    "checked_at": time.time(),
                }
            if self._monitor_stop.wait(interval_seconds):
                break

    def _cfg(self) -> tuple[dict[str, Any], str]:
        cfg = self.db.get_setting(self.CONFIG_KEY, {}) or {}
        if not isinstance(cfg, dict) or not self.configured():
            raise MailError("Почта не подключена. Открой Настройки → Почта.")
        secret = _dpapi_unprotect(str(self.db.get_setting(self.SECRET_KEY, "") or ""))
        return cfg, secret

    @staticmethod
    def _test_config(cfg: dict[str, Any], password: str) -> None:
        client = None
        try:
            if cfg.get("imap_ssl", True):
                client = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=15)
            else:
                client = imaplib.IMAP4(cfg["imap_host"], int(cfg["imap_port"]), timeout=15)
            status, _ = client.login(cfg["email"], password)
            if status != "OK":
                raise MailError("IMAP не подтвердил вход")
        except Exception as exc:
            raise MailError(f"Не удалось подключить IMAP: {exc}") from exc
        finally:
            if client is not None:
                try: client.logout()
                except Exception: pass

    def _imap(self):
        cfg, password = self._cfg()
        if cfg.get("imap_ssl", True):
            client = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=25)
        else:
            client = imaplib.IMAP4(cfg["imap_host"], int(cfg["imap_port"]), timeout=25)
        client.login(cfg["email"], password)
        return client, cfg

    @staticmethod
    def _folders(client) -> tuple[str, str]:
        spam = ""
        sent = ""
        try:
            status, rows = client.list()
            if status != "OK":
                return spam, sent
            for raw in rows or []:
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                match = re.search(r'"([^"\r\n]+)"\s*$', line)
                name = match.group(1) if match else line.rsplit(" ", 1)[-1].strip('"')
                low = (line + " " + name).casefold().replace("ё", "е")
                if not spam and ("\\junk" in low or re.search(r"\b(spam|junk|спам)\b", low)):
                    spam = name
                if not sent and ("\\sent" in low or re.search(r"\b(sent|отправлен)\w*\b", low)):
                    sent = name
        except Exception:
            pass
        return spam, sent

    @staticmethod
    def _spam_score(msg: email.message.Message) -> tuple[float, str]:
        xflag = str(msg.get("X-Spam-Flag") or "").casefold()
        xstatus = str(msg.get("X-Spam-Status") or "").casefold()
        if xflag.startswith("yes") or xstatus.startswith("yes"):
            return 1.0, "почтовый сервер пометил письмо как спам"
        subject = _decode_header(str(msg.get("Subject") or "")).casefold().replace("ё", "е")
        precedence = str(msg.get("Precedence") or "").casefold()
        unsub = bool(msg.get("List-Unsubscribe"))
        spam_words = ("вы выиграли", "казино", "ставки", "быстрый заработок", "кредит без", "займ без", "crypto giveaway")
        hits = sum(1 for token in spam_words if token in subject)
        score = 0.0
        reasons: list[str] = []
        if precedence in {"bulk", "junk"}: score += .35; reasons.append("bulk/junk рассылка")
        if unsub: score += .20; reasons.append("массовая рассылка")
        if hits: score += min(.35, .2 * hits); reasons.append("спам-маркеры в теме")
        return min(score, .89), ", ".join(reasons) or "нет сильных признаков спама"

    def review(
        self,
        *,
        limit: int = 30,
        prepare_replies: bool = True,
        move_spam: bool | None = None,
        read_only: bool = False,
    ) -> dict[str, Any]:
        """Read the configured INBOX through IMAP.

        ``read_only`` is a hard capability boundary for informational chat turns.  It
        selects the mailbox read-only, disables spam moves even when the background
        preference is enabled, and never prepares reply drafts.  This prevents a model
        that was merely asked to report important mail from turning findings into a
        second action.
        """
        client, cfg = self._imap()
        messages: list[dict[str, Any]] = []
        moved: list[dict[str, Any]] = []
        if read_only:
            prepare_replies = False
            move_spam = False
        try:
            status, _ = client.select("INBOX", readonly=bool(read_only))
            if status != "OK":
                raise MailError("Не удалось открыть INBOX")
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise MailError("Не удалось получить непрочитанные письма")
            uids = (data[0].split() if data and data[0] else [])[-max(1, min(int(limit), 100)):]
            spam_folder, _sent_folder = self._folders(client)
            allow_move = bool(cfg.get("auto_move_obvious_spam", False) if move_spam is None else move_spam)
            for uid in uids:
                status, rows = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not rows:
                    continue
                raw = next((row[1] for row in rows if isinstance(row, tuple) and isinstance(row[1], (bytes, bytearray))), b"")
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                sender_name, sender_addr = email.utils.parseaddr(_decode_header(str(msg.get("From") or "")))
                subject = _decode_header(str(msg.get("Subject") or "")) or "(без темы)"
                body = _redact_sensitive_preview(_plain_body(msg, 3500))
                score, reason = self._spam_score(msg)
                row = {
                    "uid": uid.decode("ascii", errors="ignore") if isinstance(uid, bytes) else str(uid),
                    "from": sender_addr or sender_name,
                    "from_name": sender_name,
                    "subject": subject,
                    "date": str(msg.get("Date") or ""),
                    "message_id": str(msg.get("Message-ID") or "").strip(),
                    "snippet": body[:900],
                    "spam_score": round(score, 2),
                    "spam_reason": reason,
                }
                if allow_move and score >= .90 and spam_folder:
                    moved_ok = self._move_uid(client, row["uid"], spam_folder)
                    row["moved_to_spam"] = moved_ok
                    if moved_ok:
                        moved.append({"from": row["from"], "subject": subject, "reason": reason})
                messages.append(row)
        finally:
            try: client.logout()
            except Exception: pass

        # Only create drafts for messages that were not confidently spam-labelled.
        candidates = [m for m in messages if m.get("spam_score", 0) < .75 and not m.get("moved_to_spam")]
        drafts: list[dict[str, Any]] = []
        if prepare_replies and candidates:
            drafts = self._draft_replies(candidates[:8])
            self.db.set_setting(self.DRAFTS_KEY, drafts)
        result = {
            "ok": True,
            "unread": len(messages),
            "messages": messages,
            "spam_moved": len(moved),
            "spam_details": moved,
            "drafts": drafts,
            "read_only": bool(read_only),
            "verified": True,
            "verification": f"INBOX перечитан через IMAP; найдено непрочитанных: {len(messages)}",
        }
        try:
            self.db.log_action(
                "mail_review",
                {
                    "limit": int(limit),
                    "prepare_replies": bool(prepare_replies),
                    "read_only": bool(read_only),
                },
                {
                    "unread": len(messages),
                    "spam_moved": len(moved),
                    "drafts": len(drafts),
                    "verified": True,
                    "verification": result["verification"],
                },
                "low" if read_only else "medium",
                True,
            )
        except Exception:
            pass
        return result

    def review_read_only(self, *, limit: int = 20) -> dict[str, Any]:
        """Authoritative, side-effect-free mailbox snapshot for an informational turn."""
        return self.review(
            limit=max(1, min(int(limit), 100)),
            prepare_replies=False,
            move_spam=False,
            read_only=True,
        )

    def summarize_read_only(self, query: str, review: dict[str, Any]) -> str:
        """Summarise one verified IMAP snapshot without granting any action tools.

        The model sees only already-redacted mail previews and cannot call desktop,
        browser or SMTP tools.  A deterministic compact summary remains available when
        the local model is unavailable.
        """
        messages = list(review.get("messages") or [])
        unread = int(review.get("unread") or 0)
        if unread <= 0 or not messages:
            return "Проверила подключённую почту через IMAP: непрочитанных писем нет."

        safe_rows = [
            {
                "from": str(item.get("from_name") or item.get("from") or "")[:180],
                "subject": str(item.get("subject") or "(без темы)")[:300],
                "date": str(item.get("date") or "")[:120],
                "snippet": _redact_sensitive_preview(str(item.get("snippet") or ""))[:650],
            }
            for item in messages[:20]
            if isinstance(item, dict)
        ]
        fallback_lines = [
            f"{index}. {item['subject']} — {item['from'] or 'отправитель не указан'}"
            for index, item in enumerate(safe_rows[:6], 1)
        ]
        fallback = (
            f"Проверила подключённую почту через IMAP. Непрочитанных писем: {unread}.\n"
            + "\n".join(fallback_lines)
        ).strip()
        if self.gateway is None or self.settings is None:
            return fallback

        prompt = (
            "Ответь владельцу по проверенному снимку его подключённой почты. Это строго "
            "информационная операция: только краткая сводка, никаких предложений открыть "
            "сайт, нажать кнопку, менять аккаунт, отвечать, удалять, перемещать или отправлять "
            "письма. Выдели действительно важное по содержанию, но не выдумывай приоритет. "
            "Не повторяй одноразовые коды, пароли, токены и другие секреты. Укажи общее число "
            "непрочитанных и перечисли до пяти важных писем с причиной важности.\n"
            f"Запрос владельца: {str(query or '')[:1000]}\n"
            f"Проверенные данные IMAP: {json.dumps(safe_rows, ensure_ascii=False)}"
        )
        try:
            response = self.gateway.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты анализируешь переданные локальные почтовые данные. "
                            "У тебя нет инструментов и права выполнять действия."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.settings.fast_model,
                temperature=0.2,
                think=False,
                num_ctx=min(int(self.settings.task_num_ctx), 8192),
                num_predict=min(int(self.settings.task_num_predict), 700),
                keep_alive=self.settings.keep_alive,
                timeout_seconds=35,
            )
            answer = str(response.get("content") or "").strip()
            return answer or fallback
        except Exception:
            return fallback

    @staticmethod
    def _move_uid(client, uid: str, folder: str) -> bool:
        try:
            typ, _ = client.uid("MOVE", uid, folder)
            return typ == "OK"
        except Exception:
            return False

    def _draft_replies(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        try:
            owner_style = str(self.style.get().prompt() or "") if self.style is not None else ""
        except Exception:
            owner_style = ""
        for item in messages:
            snippet = str(item.get("snippet") or "").strip()
            if not snippet:
                continue
            prompt = (
                "Составь короткий естественный черновик ответа на личное письмо. Не отправляй его и не обещай действий, "
                "которых пользователь не просил. Если письмо явно не требует ответа, верни ровно SKIP.\n"
                "Сохраняй тон, длину, формальность и пунктуацию владельца по описанию стиля; не копируй личные факты в новый контекст.\n"
                f"Стиль владельца:\n{owner_style[:1400]}\n"
                f"От: {item.get('from_name') or item.get('from')} <{item.get('from')}>\n"
                f"Тема: {item.get('subject')}\nТекст:\n{snippet[:2500]}"
            )
            try:
                response = self.gateway.chat(
                    [{"role": "system", "content": "Ты локально готовишь безопасные черновики почтовых ответов."}, {"role": "user", "content": prompt}],
                    model=self.settings.fast_model, temperature=.2, think=False, num_ctx=2048, num_predict=260,
                    keep_alive=self.settings.keep_alive, timeout_seconds=18,
                )
                body = str(response.get("content") or "").strip()
            except Exception:
                body = ""
            if not body or body.upper() == "SKIP":
                continue
            drafts.append({
                "id": uuid.uuid4().hex[:12],
                "to": str(item.get("from") or ""),
                "subject": str(item.get("subject") or ""),
                "body": body[:5000],
                "in_reply_to": str(item.get("message_id") or ""),
            })
        return drafts

    def drafts(self) -> list[dict[str, Any]]:
        value = self.db.get_setting(self.DRAFTS_KEY, []) or []
        return [dict(x) for x in value if isinstance(x, dict)] if isinstance(value, list) else []

    def get_draft(self, index: int) -> dict[str, Any]:
        """Return one locally generated draft without staging it for SMTP."""
        drafts = self.drafts()
        idx = int(index) - 1
        if idx < 0 or idx >= len(drafts):
            raise MailError("Такого черновика нет")
        return dict(drafts[idx])

    @classmethod
    def _confirmation_fingerprint(cls, conversation_id: str, draft: dict[str, Any]) -> str:
        bound = {
            "kind": "mail_send",
            "conversation_id": str(conversation_id or ""),
            "draft": {
                "id": str(draft.get("id") or ""),
                "to": str(draft.get("to") or ""),
                "subject": str(draft.get("subject") or ""),
                "body": str(draft.get("body") or ""),
                "in_reply_to": str(draft.get("in_reply_to") or ""),
            },
        }
        raw = json.dumps(bound, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _pending_envelope(self) -> dict[str, Any] | None:
        value = self.db.get_setting(self.PENDING_KEY, {}) or {}
        if not isinstance(value, dict):
            return None
        if value.get("schema") == self.PENDING_SCHEMA and value.get("kind") == "mail_send":
            payload = value.get("payload")
            if not isinstance(payload, dict) or not payload.get("to"):
                return None
            try:
                expired = float(value.get("expires_at") or 0) <= time.time()
            except (TypeError, ValueError):
                expired = True
            expected = self._confirmation_fingerprint(str(value.get("conversation_id") or ""), payload)
            if expired or str(value.get("fingerprint") or "") != expected:
                self.db.set_setting(self.PENDING_KEY, {})
                return None
            return dict(value)
        # Legacy values are intentionally not confirmable.  A bare "да" must never
        # send a draft staged by an older build without conversation ownership.
        return None

    def stage_draft(self, index: int, *, conversation_id: str = "") -> dict[str, Any]:
        drafts = self.drafts()
        idx = int(index) - 1
        if idx < 0 or idx >= len(drafts):
            raise MailError("Такого черновика нет")
        draft = dict(drafts[idx])
        now = time.time()
        fingerprint = self._confirmation_fingerprint(conversation_id, draft)
        envelope = {
            "schema": self.PENDING_SCHEMA,
            "kind": "mail_send",
            "conversation_id": str(conversation_id or ""),
            "fingerprint": fingerprint,
            "created_at": now,
            "expires_at": now + 300.0,
            "payload": draft,
        }
        with self._pending_lock:
            self.db.set_setting(self.PENDING_KEY, envelope)
        return {**draft, "confirmation_fingerprint": fingerprint, "conversation_id": str(conversation_id or "")}

    def bind_pending(self, conversation_id: str, *, created_after: float = 0.0) -> dict[str, Any] | None:
        """Bind a newly tool-staged draft to the foreground conversation.

        ToolExecutor does not know chat ownership.  ChatService calls this immediately
        after the workflow that staged the draft.  An older/unrelated draft cannot be
        claimed because ``created_after`` is checked before changing the fingerprint.
        """
        cid = str(conversation_id or "")
        if not cid:
            return None
        with self._pending_lock:
            envelope = self._pending_envelope()
            if not envelope:
                return None
            existing = str(envelope.get("conversation_id") or "")
            if existing and existing != cid:
                return None
            try:
                created_at = float(envelope.get("created_at") or 0)
            except (TypeError, ValueError):
                return None
            if created_after and created_at < float(created_after):
                return None
            payload = dict(envelope.get("payload") or {})
            fingerprint = self._confirmation_fingerprint(cid, payload)
            envelope.update({"conversation_id": cid, "fingerprint": fingerprint})
            self.db.set_setting(self.PENDING_KEY, envelope)
            return {**payload, "confirmation_fingerprint": fingerprint, "conversation_id": cid}

    def pending(self, conversation_id: str | None = None, *, fingerprint: str = "") -> dict[str, Any] | None:
        with self._pending_lock:
            envelope = self._pending_envelope()
            if not envelope:
                return None
            cid = str(envelope.get("conversation_id") or "")
            if conversation_id is not None and cid != str(conversation_id or ""):
                return None
            actual = str(envelope.get("fingerprint") or "")
            if fingerprint and actual != str(fingerprint):
                return None
            return {
                **dict(envelope.get("payload") or {}),
                "confirmation_fingerprint": actual,
                "conversation_id": cid,
                "created_at": float(envelope.get("created_at") or 0),
                "expires_at": float(envelope.get("expires_at") or 0),
            }

    def cancel_pending(self, conversation_id: str | None = None, *, fingerprint: str = "") -> bool:
        with self._pending_lock:
            if conversation_id is not None or fingerprint:
                envelope = self._pending_envelope()
                if not envelope:
                    return False
                if conversation_id is not None and str(envelope.get("conversation_id") or "") != str(conversation_id or ""):
                    return False
                if fingerprint and str(envelope.get("fingerprint") or "") != str(fingerprint):
                    return False
            self.db.set_setting(self.PENDING_KEY, {})
            return True

    def send_pending(self, conversation_id: str | None = None, *, fingerprint: str = "") -> dict[str, Any]:
        # Consume before SMTP.  Two simultaneous confirmations therefore cannot send the
        # same message twice; a failed attempt must be explicitly staged again.
        if conversation_id is None or not str(conversation_id) or not str(fingerprint):
            raise MailError("Отправка требует точное подтверждение из того же диалога")
        with self._pending_lock:
            draft = self.pending(conversation_id, fingerprint=fingerprint)
            if not draft:
                raise MailError("Нет письма с таким подтверждением")
            self.db.set_setting(self.PENDING_KEY, {})
        cfg, password = self._cfg()
        msg = EmailMessage()
        msg["From"] = cfg["email"]
        msg["To"] = draft["to"]
        subject = str(draft.get("subject") or "")
        msg["Subject"] = subject if subject.casefold().startswith("re:") else f"Re: {subject}"
        if draft.get("in_reply_to"):
            msg["In-Reply-To"] = draft["in_reply_to"]
            msg["References"] = draft["in_reply_to"]
        message_id = email.utils.make_msgid(domain=cfg["email"].split("@", 1)[-1])
        msg["Message-ID"] = message_id
        msg.set_content(str(draft.get("body") or ""))
        context = ssl.create_default_context()
        accepted = False
        if bool(cfg.get("smtp_starttls", True)):
            smtp = smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=25)
            try:
                smtp.ehlo(); smtp.starttls(context=context); smtp.ehlo(); smtp.login(cfg["email"], password)
                rejected = smtp.send_message(msg)
                accepted = not bool(rejected)
            finally:
                try: smtp.quit()
                except Exception: pass
        else:
            smtp = smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=25, context=context)
            try:
                smtp.login(cfg["email"], password)
                rejected = smtp.send_message(msg)
                accepted = not bool(rejected)
            finally:
                try: smtp.quit()
                except Exception: pass
        if not accepted:
            raise MailError("SMTP-сервер не подтвердил приём письма")
        verified_sent_copy = self._verify_sent_copy(message_id)
        result = {
            "ok": True,
            "accepted_by_smtp": True,
            "sent_copy_verified": verified_sent_copy,
            "verified": bool(verified_sent_copy),
            "message_id": message_id,
            "to": draft["to"],
            "note": "SMTP подтвердил приём письма, но копию в Отправленных пока не удалось подтвердить; доставка зависит от почтовых серверов." if not verified_sent_copy else "SMTP подтвердил приём, а копия найдена в папке Отправленные.",
        }
        try:
            self.db.log_action("mail_send", {"to": draft["to"], "subject": subject}, result, "high", True)
        except Exception:
            pass
        return result

    def _verify_sent_copy(self, message_id: str) -> bool:
        client = None
        try:
            client, _cfg = self._imap()
            _spam, sent = self._folders(client)
            if not sent:
                return False
            if client.select(sent, readonly=True)[0] != "OK":
                return False
            typ, data = client.uid("search", None, "HEADER", "Message-ID", f'"{message_id}"')
            return typ == "OK" and bool(data and data[0].strip())
        except Exception:
            return False
        finally:
            if client is not None:
                try: client.logout()
                except Exception: pass
