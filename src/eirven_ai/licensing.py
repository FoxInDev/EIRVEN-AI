"""Подписка Эрви: ключ, пробный период и проверка на сервере.

Как это работает:
  * Эрви считает устойчивый номер этого компьютера — из идентификаторов
    оборудования, которые Windows уже хранит. Сами серийники наружу не уходят,
    только их хэши: обратно из них ничего не восстановить. Нужны они для одного:
    чтобы пробный период давался один раз на компьютер и чтобы ключ знал, на
    скольких компьютерах он уже работает.
  * Вместе с ними уходит имя компьютера (то, что Windows показывает в «Системе»):
    без него в кабинете нельзя понять, какой из компьютеров отвязывать. Если
    имя компьютера — ваше имя, оно будет видно в кабинете; больше нигде.
  * Сервер отвечает лицензией, подписанной Ed25519. Открытый ключ проверки
    лежит рядом (license_public_key), закрытый есть только у сервера, поэтому
    сделать себе лицензию «на месте» нельзя.
  * Лицензия действует офлайн до отметки check_by. Пока она не прошла, Эрви
    работает без интернета; после — тихо переспрашивает сервер при запуске.

Никакие данные пользователя (переписка, файлы, почта) здесь не участвуют.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ed25519_verify import verify as ed25519_verify
from .rsa_verify import verify as rsa_verify
from .version import APP_BUILD, APP_VERSION

DEFAULT_API = "https://eirven.foxyhosty.ru/api/app/v1"
TOKEN_PREFIX = "EL1"
# Если сервер недоступен, когда пора переспрашивать, Эрви не выключается сразу:
# сначала несколько дней предупреждений. Сбой связи не должен останавливать работу.
NETWORK_GRACE_SECONDS = 3 * 24 * 3600
# Отпечаток компьютера считается из идентификаторов оборудования вместе с этой
# строкой: одинаковые серийники у разных приложений дают разные хэши.
FINGERPRINT_NAMESPACE = "eirven-device-v1"
# Открытый ключ проверки лицензий этого сервера. Подставляется при сборке —
# берётся с /api/app/v1/info своего сервера (поле public_key).
LICENSE_PUBLIC_KEY_B64 = ""
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _b64url_decode(value: str) -> bytes:
    text = str(value or "").strip()
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sha256(value: str) -> str:
    return hashlib.sha256((FINGERPRINT_NAMESPACE + "|" + str(value or "")).encode("utf-8")).hexdigest()


# ── Отпечаток компьютера ─────────────────────────────────────────────────────

_POWERSHELL_PROBE = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$o=[ordered]@{};"
    "$o.uuid=(Get-CimInstance Win32_ComputerSystemProduct).UUID;"
    "$o.board=(Get-CimInstance Win32_BaseBoard).SerialNumber;"
    "$o.volume=(Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='$env:SystemDrive'\").VolumeSerialNumber;"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$o | ConvertTo-Json -Compress"
)


def _machine_guid() -> str:
    """MachineGuid из реестра Windows — быстрее и надёжнее всего остального."""
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value or "").strip()
    except Exception:
        return ""


def _hardware_probe() -> dict[str, str]:
    """Один вызов PowerShell на все серийники: отдельные вызовы слишком медленные."""
    if os.name != "nt":
        # Не Windows (разработка, тесты): берём то, что есть, без subprocess.
        return {"uuid": platform.node(), "board": "", "volume": ""}
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_PROBE],
            capture_output=True, timeout=25, creationflags=creation, text=True, encoding="utf-8", errors="replace",
        )
        payload = json.loads((result.stdout or "").strip() or "{}")
    except Exception:
        return {"uuid": "", "board": "", "volume": ""}
    if not isinstance(payload, dict):
        return {"uuid": "", "board": "", "volume": ""}
    clean: dict[str, str] = {}
    for name in ("uuid", "board", "volume"):
        value = payload.get(name)
        if isinstance(value, list):
            value = next((item for item in value if item), "")
        text = str(value or "").strip()
        # Многие производители пишут заглушки вместо серийника — это не идентификатор.
        if text.lower() in {"", "none", "default string", "to be filled by o.e.m.", "system serial number",
                            "0", "00000000", "ffffffff-ffff-ffff-ffff-ffffffffffff", "not applicable"}:
            text = ""
        clean[name] = text
    return clean


@dataclass(slots=True)
class DeviceIdentity:
    device_id: str
    hardware: dict[str, str]
    source: str

    def payload(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "hardware": dict(self.hardware)}


def device_identity(data_dir: Path) -> DeviceIdentity:
    """Номер этого компьютера. Считается один раз и кладётся в кэш рядом с данными.

    Кэш — только ради скорости: значения выводятся из оборудования, поэтому
    после переустановки Эрви или очистки данных получится тот же номер.
    """
    cache_path = Path(data_dir) / "device.json"
    probe: dict[str, str] | None = None
    cached: dict[str, Any] = {}
    try:
        raw = cache_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("v") == 1:
            cached = parsed
    except Exception:
        cached = {}

    guid = _machine_guid()
    if cached.get("guid") == guid and isinstance(cached.get("hardware"), dict) and cached.get("device_id"):
        return DeviceIdentity(str(cached["device_id"]), dict(cached["hardware"]), "cache")

    probe = _hardware_probe()
    hardware = {
        "machine": _sha256(guid) if guid else "",
        "uuid": _sha256(probe.get("uuid", "")) if probe.get("uuid") else "",
        "board": _sha256(probe.get("board", "")) if probe.get("board") else "",
        "volume": _sha256(probe.get("volume", "")) if probe.get("volume") else "",
    }
    parts = [value for key, value in hardware.items() if value and key != "volume"]
    if parts:
        device_id = hashlib.sha256(("|".join(sorted(parts))).encode("ascii")).hexdigest()
        source = "hardware"
    else:
        # Совсем ничего не прочиталось: заводим случайный номер и храним его.
        existing = str(cached.get("device_id") or "")
        device_id = existing if len(existing) == 64 else hashlib.sha256(os.urandom(32)).hexdigest()
        source = "random"

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "v": 1, "device_id": device_id, "guid": guid, "hardware": hardware,
            "source": source, "at": int(time.time()),
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache_path)
    except Exception:
        pass
    return DeviceIdentity(device_id, hardware, source)


def device_label() -> str:
    """Понятное имя компьютера — чтобы в кабинете было видно, что отвязываешь."""
    try:
        name = platform.node() or ""
    except Exception:
        name = ""
    return str(name).strip()[:120]


# ── Лицензия ─────────────────────────────────────────────────────────────────

def license_public_key_raw(root_dir: Path | None = None) -> str:
    """Открытый ключ сервера строкой: переменная окружения, константа сборки
    или файл assets/license_public_key.txt (в таком порядке)."""
    raw = str(os.getenv("EIRVEN_LICENSE_PUBLIC_KEY", "") or LICENSE_PUBLIC_KEY_B64).strip()
    if not raw:
        for base in filter(None, (root_dir, _PACKAGE_ROOT, Path.cwd())):
            try:
                raw = (Path(base) / "assets" / "license_public_key.txt").read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if raw:
                break
    return raw


def _parse_public_key(raw: str) -> tuple[str, Any] | None:
    raw = str(raw or "").strip()
    try:
        if raw.startswith("RSA."):
            _, n, e = raw.split(".", 2)
            modulus, exponent = _b64url_decode(n), _b64url_decode(e)
            return ("rsa", (modulus, exponent)) if len(modulus) >= 256 else None
        key = _b64url_decode(raw) if raw else b""
        return ("ed25519", key) if len(key) == 32 else None
    except Exception:
        return None


def license_public_key(root_dir: Path | None = None) -> bytes:
    """Совместимость: байты ключа (для RSA — модуль), пусто, если ключа нет."""
    parsed = _parse_public_key(license_public_key_raw(root_dir))
    if parsed is None:
        return b""
    return parsed[1][0] if parsed[0] == "rsa" else parsed[1]


def licensing_configured(root_dir: Path | None = None) -> bool:
    """Настроена ли подписка в этой сборке.

    Если ключ проверки не вписан, Эрви НЕ блокирует себя: неверно собранная
    версия не должна оставить людей без программы. Подписка просто не применяется,
    а в разделе «Подписка» об этом честно написано.
    """
    return _parse_public_key(license_public_key_raw(root_dir)) is not None


def parse_license_token(token: str, root_dir: Path | None = None) -> dict[str, Any] | None:
    """Разобрать и проверить подпись лицензии. None — если подпись не сходится."""
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] not in {"EL1", "EL2"}:
        return None
    parsed = _parse_public_key(license_public_key_raw(root_dir))
    if parsed is None:
        return None
    try:
        signature = _b64url_decode(parts[2])
        message = (parts[0] + "." + parts[1]).encode("ascii")
    except Exception:
        return None
    kind, key = parsed
    if parts[0] == "EL2" and kind == "rsa":
        ok = rsa_verify(message, signature, key[0], key[1])
    elif parts[0] == "EL1" and kind == "ed25519":
        ok = ed25519_verify(message, signature, key)
    else:
        ok = False
    if not ok:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) and int(payload.get("v") or 0) == 1 else None


@dataclass(slots=True)
class LicenseStatus:
    """То, что видит остальная Эрви: можно работать или нет и что сказать человеку."""

    allowed: bool = False
    mode: str = "none"              # subscription | trial | grace | expired | none
    plan: str = ""
    plan_title: str = ""
    trial: bool = False
    expires_at: int = 0
    days_left: int = 0
    key_tail: str = ""
    message: str = ""
    warning: str = ""
    devices_used: int = 0
    max_devices: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "mode": self.mode, "plan": self.plan, "plan_title": self.plan_title,
            "trial": self.trial, "expires_at": self.expires_at, "days_left": self.days_left,
            "key_tail": self.key_tail, "message": self.message, "warning": self.warning,
            "max_devices": self.max_devices,
        }


class LicenseManager:
    """Хранит лицензию, проверяет её и общается с сервером подписок."""

    def __init__(self, root_dir: Path, data_dir: Path, *, api_base: str = ""):
        self.root_dir = Path(root_dir)
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "license.json"
        self.api_base = str(os.getenv("EIRVEN_LICENSE_API", "") or api_base or DEFAULT_API).rstrip("/")
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {}
        self._identity: DeviceIdentity | None = None
        self._plans: list[dict[str, Any]] = []
        self._info_at = 0.0
        self._watcher: threading.Thread | None = None
        self._stop_watch = threading.Event()
        self._load()

    # ── состояние на диске ──────────────────────────────────────────────────
    def _load(self) -> None:
        with self._lock:
            try:
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                self._state = parsed if isinstance(parsed, dict) else {}
            except Exception:
                self._state = {}

    def _save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except Exception:
                pass

    def identity(self) -> DeviceIdentity:
        with self._lock:
            if self._identity is None:
                self._identity = device_identity(self.data_dir)
            return self._identity

    # ── запросы к серверу ───────────────────────────────────────────────────
    def _post(self, path: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self.api_base}/{path.lstrip('/')}", json=payload, timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": f"EIRVEN-AI/{APP_VERSION} ({APP_BUILD})"},
            follow_redirects=False, trust_env=False,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("ok", response.status_code < 400)
        return data

    def _request_payload(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        identity = self.identity()
        payload: dict[str, Any] = identity.payload()
        payload["device_name"] = device_label()
        payload["app_version"] = f"{APP_VERSION} {APP_BUILD}"
        if extra:
            payload.update(extra)
        return payload

    def info(self, *, refresh: bool = True) -> dict[str, Any]:
        """Тарифы и ссылки с сервера — для раздела «Подписка». Кэш на 6 часов."""
        with self._lock:
            fresh = time.time() - self._info_at < 6 * 3600
            if self._plans and (fresh or not refresh):
                return {"plans": list(self._plans), **dict(self._state.get("info") or {})}
        try:
            import httpx

            response = httpx.get(
                f"{self.api_base}/info", timeout=8.0, trust_env=False, follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": f"EIRVEN-AI/{APP_VERSION}"},
            )
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict) or not data.get("ok"):
            with self._lock:
                stored = dict(self._state.get("info") or {})
            return stored
        plans = [plan for plan in (data.get("plans") or []) if isinstance(plan, dict)]
        info = {
            "buy_url": str(data.get("buy_url") or ""),
            "account_url": str(data.get("account_url") or ""),
            "partners_url": str(data.get("partners_url") or ""),
            "telegram": str(data.get("telegram") or ""),
            "plans": plans,
        }
        with self._lock:
            self._plans = plans
            self._info_at = time.time()
            self._state["info"] = info
            self._save()
        return info

    # ── чтение состояния ────────────────────────────────────────────────────
    def _verified(self) -> tuple[dict[str, Any], dict[str, Any]] | tuple[None, None]:
        """Сохранённая лицензия вместе с её проверенным содержимым."""
        with self._lock:
            state = dict(self._state)
        token = str(state.get("token") or "")
        if not token:
            return None, None
        payload = parse_license_token(token, self.root_dir)
        if payload is None:
            return None, None
        return state, payload

    def configured(self) -> bool:
        return licensing_configured(self.root_dir)

    def status(self) -> LicenseStatus:
        """Что сейчас разрешено. Ничего не спрашивает у сервера — только смотрит лицензию."""
        if not self.configured():
            # В сборку не вписан ключ проверки: подписку применять нечем.
            # Блокировать в таком случае нельзя — это сломало бы рабочую программу.
            return LicenseStatus(
                allowed=True, mode="unconfigured",
                message="Подписка в этой сборке не настроена.",
            )
        state, payload = self._verified()
        if state is None or payload is None:
            return LicenseStatus(
                allowed=False, mode="none",
                message="Нужна подписка: введите ключ или запустите бесплатный период.",
            )
        # Лицензия выдана конкретному компьютеру. Скопированный на другую машину
        # файл лицензии здесь и отсекается — иначе один ключ работал бы везде.
        issued_for = str(payload.get("device") or "")
        if issued_for and issued_for != self.identity().device_id:
            return LicenseStatus(
                allowed=False, mode="none",
                message="Эта лицензия выдана другому компьютеру. Введите ключ здесь — он привяжется к этой машине.",
            )
        now = int(time.time())
        expires = int(payload.get("expires_at") or 0)
        check_by = int(payload.get("check_by") or 0)
        trial = bool(payload.get("trial"))
        plan = str(payload.get("plan") or "")
        key = str(payload.get("key") or "")
        titles = {str(item.get("id")): str(item.get("title") or "") for item in (state.get("info", {}).get("plans") or [])}
        title = titles.get(plan) or ("Личный" if plan == "personal" else "Pro" if plan == "pro" else plan)
        base = LicenseStatus(
            plan=plan, plan_title=title, trial=trial, expires_at=expires,
            days_left=max(0, (expires - now + 86399) // 86400) if expires else 0,
            key_tail=key.split("-")[-1] if key else "",
            max_devices=int(payload.get("max_devices") or 0),
        )

        if expires and now >= expires:
            base.allowed = False
            base.mode = "expired"
            base.message = (
                "Бесплатный период закончился. Оформите подписку — и Эрви продолжит с того же места."
                if trial else "Подписка закончилась. Продлите её в личном кабинете, всё сохранено."
            )
            return base

        # Лицензия ещё действует. Проверять у сервера пора после check_by.
        if check_by and now > check_by:
            hard_stop = check_by + NETWORK_GRACE_SECONDS
            if now <= hard_stop:
                base.allowed = True
                base.mode = "grace"
                base.warning = "Не получается связаться с сервером подписки. Эрви работает, но проверьте интернет."
                return base
            base.allowed = False
            base.mode = "expired"
            base.message = "Давно не удавалось проверить подписку. Подключитесь к интернету — Эрви проверит ключ и продолжит работу."
            return base

        base.allowed = True
        base.mode = "trial" if trial else "subscription"
        if base.days_left and base.days_left <= 3:
            base.warning = (
                f"Бесплатный период заканчивается через {base.days_left} дн." if trial
                else f"Подписка заканчивается через {base.days_left} дн."
            )
        return base

    def _signature_problem(self, data: dict[str, Any]) -> str:
        """Лицензия пришла, но подпись не сошлась — объясняем почему."""
        local = license_public_key_raw(self.root_dir)
        remote = ""
        try:
            import httpx

            remote = str(httpx.get(f"{self.api_base}/info", timeout=8.0, trust_env=False).json().get("public_key") or "")
        except Exception:
            pass
        if remote and remote != local:
            return (f"Ключ проверки в этой сборке (…{local[-6:]}) не совпадает с ключом сайта (…{remote[-6:]}). "
                    f"Впишите в assets/license_public_key.txt и в licensing.py ключ сайта: {remote}")
        return "Сервер выдал лицензию, но её подпись не прошла проверку. Проверьте ключ сайта в сборке."

    def _store_token(self, data: dict[str, Any], *, key: str = "") -> bool:
        token = str(data.get("license_token") or "")
        payload = parse_license_token(token, self.root_dir)
        if payload is None:
            return False
        with self._lock:
            self._state.update({
                "token": token,
                "key": key or str(payload.get("key") or self._state.get("key") or ""),
                "plan": str(payload.get("plan") or ""),
                "trial": bool(payload.get("trial")),
                "expires_at": int(payload.get("expires_at") or 0),
                "check_by": int(payload.get("check_by") or 0),
                "checked_at": int(time.time()),
            })
            if isinstance(data.get("license"), dict):
                self._state["license"] = data["license"]
            self._save()
        return True

    # ── действия ────────────────────────────────────────────────────────────
    def activate(self, key: str) -> dict[str, Any]:
        """Ввести ключ. Всё решает сервер: он же считает компьютеры."""
        clean = str(key or "").strip()
        if not clean:
            return {"ok": False, "message": "Вставьте ключ из письма или из личного кабинета."}
        try:
            result = self._post("activate", self._request_payload({"key": clean}))
        except Exception as exc:
            return {"ok": False, "message": f"Не получилось связаться с сервером подписки: {exc}"[:300]}
        if not result.get("ok"):
            return {"ok": False, "message": str(result.get("message") or "Ключ не принят."), "error": str(result.get("error") or "")}
        if not self._store_token(result, key=clean):
            return {"ok": False, "message": self._signature_problem(result)}
        status = self.status()
        return {"ok": True, "message": f"Ключ принят: {status.plan_title} до {self.expires_label()}.", "status": status.payload(), "allowed": status.allowed}

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Переспросить сервер. Обычно вызывается сама при запуске, когда пора."""
        with self._lock:
            key = str(self._state.get("key") or "")
            trial = bool(self._state.get("trial"))
            plan = str(self._state.get("plan") or "")
            check_by = int(self._state.get("check_by") or 0)
        if not force and check_by and int(time.time()) <= check_by:
            return {"ok": True, "skipped": True, "status": self.status().payload()}
        if trial and not key:
            return self.start_trial(plan or "personal")
        if not key:
            return {"ok": False, "message": "Ключ ещё не введён."}
        try:
            result = self._post("refresh", self._request_payload({"key": key}))
        except Exception as exc:
            return {"ok": False, "offline": True, "message": f"Сервер подписки не ответил: {exc}"[:300], "status": self.status().payload()}
        if not result.get("ok"):
            error = str(result.get("error") or "")
            if error in {"expired", "revoked", "not_found", "device_limit", "invalid_key"}:
                # Ключ больше не действует: снимаем лицензию, чтобы Эрви честно
                # показала экран подписки вместо «работает, но нет».
                with self._lock:
                    self._state.pop("token", None)
                    self._state["last_error"] = error
                    self._save()
            return {"ok": False, "message": str(result.get("message") or "Подписка не подтверждена."), "error": error, "status": self.status().payload()}
        self._store_token(result, key=key)
        return {"ok": True, "status": self.status().payload()}

    def start_trial(self, plan: str = "personal") -> dict[str, Any]:
        """Запустить бесплатный период. Сервер сам решает, был ли он уже."""
        try:
            result = self._post("trial", self._request_payload({"plan": str(plan or "personal")}))
        except Exception as exc:
            return {"ok": False, "message": f"Не получилось связаться с сервером подписки: {exc}"[:300]}
        if not result.get("ok"):
            return {"ok": False, "message": str(result.get("message") or "Бесплатный период недоступен."), "error": str(result.get("error") or "")}
        if not self._store_token(result):
            return {"ok": False, "message": self._signature_problem(result)}
        days = int(result.get("days") or 0)
        message = (
            f"Бесплатный период на {days} дн. начался — пользуйтесь без ограничений."
            if result.get("started") else "Бесплатный период продолжается."
        )
        return {"ok": True, "message": message, "status": self.status().payload()}

    def release(self) -> dict[str, Any]:
        """Отвязать этот компьютер от ключа и убрать лицензию с него."""
        with self._lock:
            key = str(self._state.get("key") or "")
        if key:
            try:
                self._post("release", self._request_payload({"key": key}))
            except Exception:
                # Сервер недоступен — снимаем локально, место освободится при следующей проверке.
                pass
        with self._lock:
            self._state.pop("token", None)
            self._state.pop("key", None)
            self._save()
        return {"ok": True, "message": "Ключ убран с этого компьютера."}

    def expires_label(self) -> str:
        with self._lock:
            expires = int(self._state.get("expires_at") or 0)
        if not expires:
            return "бессрочно"
        return time.strftime("%d.%m.%Y", time.localtime(expires))

    def maybe_refresh_async(self) -> None:
        """Проверка при запуске и дальше раз в несколько часов.

        Без повторной проверки долго работающая Эрви сама себя отключала бы:
        срок офлайн-проверки истекает, а переспросить сервер некому.
        """
        if self._watcher is not None:
            return

        def worker() -> None:
            first = True
            while not self._stop_watch.wait(0 if first else 3600.0):
                first = False
                try:
                    if not self.configured():
                        continue
                    self.refresh()
                except Exception:
                    pass

        self._watcher = threading.Thread(target=worker, daemon=True, name="eirven-license")
        self._watcher.start()

    def stop(self) -> None:
        self._stop_watch.set()

    def snapshot(self) -> dict[str, Any]:
        """Полная картинка для раздела «Подписка» в интерфейсе."""
        status = self.status()
        info = self.info(refresh=False)
        with self._lock:
            key = str(self._state.get("key") or "")
        return {
            **status.payload(),
            "key": key,
            "device_name": device_label(),
            "buy_url": str(info.get("buy_url") or ""),
            "account_url": str(info.get("account_url") or ""),
            "partners_url": str(info.get("partners_url") or ""),
            "telegram": str(info.get("telegram") or ""),
            "plans": info.get("plans") or [],
            "expires_label": self.expires_label(),
        }
