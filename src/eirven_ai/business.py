"""Предложение для бизнеса: пакеты, цены и связь с автором — в одном конфиге.

Эрви бесплатна для личного использования и ничего здесь не блокирует. Модуль лишь
показывает, что Эрви можно настроить под задачи компании, и помогает связаться с
автором. Данные человека никуда не отправляются сами: заявка — это готовый текст,
который человек сам отправляет в Telegram или по почте.

Цены и контакты лежат в ``assets/business_offer.json`` (попадает в сборку). Если на
сервере обновлений есть ``<api>/offer`` с тем же форматом, Эрви подхватит его —
так цены и контакты меняются без выпуска новой версии. Сервер только отдаёт JSON:
никаких данных пользователя в этот запрос не входит.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

OFFER_FILE = "business_offer.json"
REMOTE_CACHE_FILE = "business_offer.remote.json"
_MAX_REMOTE_BYTES = 64 * 1024
_REMOTE_TTL = 6 * 3600
_REMOTE_RETRY_AFTER_ERROR = 3600
_REMOTE_RETRY_AFTER_MISSING = 24 * 3600
# ShellExecute и часть браузеров обрезают адреса длиннее ~2 КБ. Полный текст заявки
# всё равно копируется в буфер обмена, а в ссылку попадает столько, сколько влезает.
_MAX_LINK_CHARS = 1900

_TG_HANDLE = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")

_LIMITS: dict[str, int] = {
    "title": 80, "price": 60, "note": 120, "text": 320, "tag": 24, "item": 140,
}

DEFAULT_OFFER: dict[str, Any] = {
    "schema": 1,
    "enabled": True,
    "contact": {
        "telegram": "",
        "email": "",
        "url": "https://foxyhosty.ru",
        "label": "Даниил Павлов — автор Эрви",
    },
    "headline": "Эрви — сотрудник, который работает на вашем компьютере",
    "lead": (
        "Настрою Эрви под задачи вашей компании: отвечать клиентам, публиковать объявления, "
        "разбирать почту и документы. Всё работает на вашем компьютере — без облака, VPN "
        "и оплаты токенов."
    ),
    "highlights": [
        "0 ₽ за токены и облако",
        "Данные клиентов остаются у вас",
        "Запуск за 1–3 дня",
    ],
    "scenarios": [],
    "steps": [],
    "packages": [],
    "subscription": {},
    "express": {},
    "personal_note": "Для себя я бесплатна — без ограничений и рекламы.",
    "license_note": "",
    "chat_pitch": "",
    "chat_process": "",
}


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _clean_items(value: Any, *, limit: int, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [_clean_text(item, limit) for item in value if isinstance(item, (str, int, float))]
    return [item for item in out if item][:max_items]


def _clean_https(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 200 or any(ch.isspace() for ch in raw):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        return ""
    return raw


def _clean_card(value: Any, *, with_items: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    card: dict[str, Any] = {
        "title": _clean_text(value.get("title"), _LIMITS["title"]),
        "text": _clean_text(value.get("text"), _LIMITS["text"]),
    }
    for key in ("price", "note", "tag", "id"):
        if value.get(key) not in (None, ""):
            card[key] = _clean_text(value.get(key), _LIMITS.get(key, 40))
    if "id" in card:
        card["id"] = re.sub(r"[^a-z0-9_-]", "", card["id"].casefold())[:24]
    if with_items:
        card["items"] = _clean_items(value.get("items"), limit=_LIMITS["item"], max_items=8)
    if value.get("featured"):
        card["featured"] = True
    return card if card["title"] else {}


def sanitize_offer(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a safe offer: known keys only, bounded strings and lists, safe links.

    ``raw`` may come from the network, so nothing in it is trusted: unknown keys are
    dropped, every text is length-limited, and the only links kept are an https URL,
    a Telegram username and an e-mail address. The UI renders all of it as text.
    """
    offer = json.loads(json.dumps(base if base is not None else DEFAULT_OFFER))
    if not isinstance(raw, dict):
        return offer
    if "enabled" in raw:
        offer["enabled"] = bool(raw.get("enabled"))

    contact = raw.get("contact")
    if isinstance(contact, dict):
        merged = dict(offer.get("contact") or {})
        if "telegram" in contact:
            match = _TG_HANDLE.fullmatch(str(contact.get("telegram") or "").strip())
            merged["telegram"] = match.group(1) if match else ""
        if "email" in contact:
            email = str(contact.get("email") or "").strip()
            merged["email"] = email if _EMAIL.fullmatch(email) else ""
        if "url" in contact:
            merged["url"] = _clean_https(contact.get("url"))
        if "label" in contact:
            merged["label"] = _clean_text(contact.get("label"), 80)
        offer["contact"] = merged

    for key, limit in (
        ("headline", 120), ("lead", 400), ("personal_note", 200), ("license_note", 400),
        ("chat_pitch", 300), ("chat_process", 200),
    ):
        if key in raw:
            offer[key] = _clean_text(raw.get(key), limit)
    if "highlights" in raw:
        offer["highlights"] = _clean_items(raw.get("highlights"), limit=60, max_items=4)
    if "scenarios" in raw and isinstance(raw.get("scenarios"), list):
        offer["scenarios"] = [c for c in (_clean_card(x) for x in raw["scenarios"]) if c][:8]
    if "steps" in raw and isinstance(raw.get("steps"), list):
        offer["steps"] = [c for c in (_clean_card(x) for x in raw["steps"]) if c][:6]
    if "packages" in raw and isinstance(raw.get("packages"), list):
        offer["packages"] = [c for c in (_clean_card(x, with_items=True) for x in raw["packages"]) if c][:4]
    for key in ("subscription", "express"):
        if key in raw:
            card = _clean_card(raw.get(key), with_items=True)
            # r72-k8: у Express есть своя страница оплаты на сайте — ссылку берём
            # только https, как и контакт.
            source = raw.get(key)
            if key == "express" and card and isinstance(source, dict):
                url = _clean_https(source.get("url"))
                if url:
                    card["url"] = url
            offer[key] = card
    return offer


# ── Вопросы о цене и работе Эрви в компании ────────────────────────────────────
# Узко и детерминированно: маленькая модель иначе выдумывает цены. Совпадение нужно
# только для вопросов о самой Эрви — «сколько стоит биткоин» или «найди цену на
# айфон» остаются обычными задачами.

_SELF_OBJ = r"(?:тебя|тебе|тобой|твоя|твой|твое|твои|твоей|твоего|твоих|эрви|eirven|вас|ваш\w*)"
_SELF_ANY = re.compile(r"\b(?:ты|тебя|тебе|тобой|твоя|твой|твое|твои|твоей|эрви|eirven)\b")
_BIZ_ALTS = (
    r"бизнес\w*|компани\w*|офис\w*|магазин\w*|салон\w*|фирм\w*|клиник\w*|"
    r"автосервис\w*|организаци\w*|производств\w*|работ\w*|отдел\w*|студи\w*|агентств\w*|"
    r"предприяти\w*|ип|бригад\w*"
)
_BIZ_NOUN = "(?:" + _BIZ_ALTS + ")"
_OWNER = r"(?:(?:мо\w+|наш\w+|сво\w+)\s+)?"

_EXPRESS = (
    re.compile(r"\bэрви\s+(?:express|экспресс)\b"),
    re.compile(
        r"\b(?:что\s+(?:такое|за)|сколько\s+(?:стоит|стоят)|как\s+(?:подключить|получить|купить|оформить|включить)|"
        r"подключи(?:ть)?|хочу|купить|оформить|оплатить)\s+(?:эрви\s+)?(?:express|экспресс)\b"
        r"(?!\s*[-–]?\s*(?:доставк|заказ|курс|поезд|такси|почт|кредит|анализ|тест|кафе|лотере|перевод|выпуск))"
    ),
    re.compile(r"\b(?:express|экспресс)\s*[-–]?\s*(?:канал\w*|верси\w*|подписк\w*|доступ\w*)\b"),
    re.compile(r"\bканал\w*\s+(?:express|экспресс)\b"),
)

# (шаблон, нужно ли явно упомянуть Эрви в той же фразе)
_LICENSE: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"\bкоммерческ\w*\s+(?:лицензи\w*|использовани\w*|верси\w*)"), True),
    (re.compile(r"\bлицензи\w*\s+(?:на\s+|для\s+)?" + _SELF_OBJ + r"\b"), False),
    (re.compile(
        r"\b(?:можно|разрешено|законно|легально|нельзя)\s+(?:ли\s+)?(?:мне\s+|нам\s+)?"
        r"(?:использовать|ставить|поставить|применять|продавать|перепродавать|брать)\s+"
        r"(?:тебя|эрви)\s+(?:в|на|для)\s+" + _OWNER + r"(?:" + _BIZ_ALTS + r"|коммерческ\w*)"
    ), False),
    (re.compile(r"\bнужн\w*\s+(?:ли\s+)?лицензи\w*"), True),
)

# Короткий вопрос без объекта («Нужна лицензия?») в чате Эрви — про саму Эрви.
_LICENSE_BARE = re.compile(
    r"(?:а\s+)?(?:нужн\w*\s+(?:ли\s+)?лицензи\w*|коммерческ\w*\s+(?:лицензи\w*|использовани\w*|верси\w*))"
)

_OFFER = (
    re.compile(r"\bсколько\s+(?:ты\s+)?стоишь\b"),
    re.compile(r"\bсколько\s+(?:стоит|стоят|будет\s+стоить)\s+(?:эрви|eirven)\b"),
    re.compile(
        r"\bсколько\s+(?:стоит|стоят|будет\s+стоить)\s+(?:твоя\s+|твой\s+|твои\s+|ваш\w*\s+)?"
        r"(?:настройк\w*|внедрени\w*|сборк\w*|установк\w*|подключени\w*)\s+"
        r"(?:эрви\b|тебя\b|(?:под|для|в)\s+" + _OWNER + _BIZ_NOUN + r"\b)"
    ),
    re.compile(
        r"\b(?:как\w*|сколько)\s+(?:у\s+)?" + _SELF_OBJ + r"\s+(?:цен\w*|стоимост\w*|тариф\w*|"
        r"расценк\w*|прайс\w*|пакет\w*)"
    ),
    re.compile(r"\b(?:цен\w*|стоимост\w*|тариф\w*|прайс\w*|расценк\w*)\s+(?:на\s+|у\s+)?(?:эрви|eirven|тебя|вас)\b"),
    re.compile(
        r"\b(?:ты|эрви)\s+(?:правда\s+|реально\s+|вообще\s+|совсем\s+|полностью\s+|точно\s+)?"
        r"(?:бес)?платн(?:ая|ый|ое|а|ые)\b"
    ),
    re.compile(r"\b(?:бес)?платн(?:ая|ый|ое|а)\s+(?:ли\s+)?(?:ты|эрви)\b"),
    re.compile(
        r"\b(?:как|где|можно(?:\s+ли)?)\s+(?:мне\s+|нам\s+)?(?:тебя|эрви)\s+(?:купить|приобрести|заказать|оплатить)\b"
    ),
    re.compile(r"\b(?:купить|приобрести|заказать|оплатить)\s+(?:тебя|эрви)\b"),
    re.compile(
        r"\b(?:настро\w*|внедр\w*|подключ\w*|установ\w*|использова\w*|поставить|заказать|купить|взять|хочу|хотим)\s+"
        r"(?:тебя|эрви)\s+(?:для|под|в|на)\s+" + _OWNER + _BIZ_NOUN + r"\b"
    ),
    re.compile(
        r"\b(?:тебя|эрви)\s+(?:можно\s+|нужно\s+|хочу\s+|хотим\s+)?(?:настроить|внедрить|подключить|установить|"
        r"поставить|использовать|купить|заказать|взять)\s+(?:для|под|в|на)\s+" + _OWNER + _BIZ_NOUN + r"\b"
    ),
    re.compile(
        r"\b(?:эрви|тебя)\s+(?:для|под)\s+" + _OWNER
        + r"(?:бизнес\w*|компани\w*|офис\w*|магазин\w*|салон\w*|фирм\w*|организаци\w*)\b"
    ),
    re.compile(r"\b(?:эрви|ты)\s+(?:для\s+бизнеса|в\s+бизнесе)\b"),
    re.compile(r"\b(?:бизнес|корпоративн\w*)\s*[-–]?\s*(?:верси\w*|тариф\w*|пакет\w*)\s+(?:эрви|тебя)\b"),
    re.compile(r"\b(?:верси\w*|тариф\w*|пакет\w*)\s+(?:эрви\s+)?для\s+бизнеса\b"),
)

# Просьба открыть сам раздел — это навигация по интерфейсу, а не задача для ПК.
_OPEN_SECTION = re.compile(
    r"^(?:эрви\s+)?(?:открой|покажи|перейди\s+в|зайди\s+в)\s+(?:раздел\s+|вкладку\s+|страницу\s+)?"
    r"для\s+бизнеса$"
)

# Задача про внешний объект, а не вопрос о самой Эрви: «найди цену на айфон»,
# «какая у тебя подписка на музыку», «оплати интернет».
_TASK_NOT_ABOUT_ERVI = re.compile(
    r"\b(?:найди|найти|поищи|поискать|посмотри|узнай|узнать|проверь|сравни|закажи|купи|оплати|"
    r"напомни|запиши|добавь|открой|включи|напиши|отправь|посчитай|переведи|ответь)\b"
)
_PRICE_OF_OTHER = re.compile(
    r"\b(?:цен\w*|стоимост\w*|подписк\w*|лицензи\w*|тариф\w*)\s+(?:на|для)\s+(?!эрви\b|тебя\b|вас\b|нее\b|неё\b|бизнес)\w+"
)
_WORDS = re.compile(r"[a-zа-я0-9]+")


def business_intent(text: str) -> str:
    """Classify a question about buying or using Эрви in a company.

    Returns ``"express"``, ``"license"``, ``"offer"`` or ``""``.
    """
    clean = " ".join(str(text or "").casefold().replace("ё", "е").split())
    clean = " ".join(re.sub(r"[^\w\s–-]", " ", clean).split())
    if not clean or len(_WORDS.findall(clean)) > 24:
        return ""
    if _OPEN_SECTION.fullmatch(clean):
        return "offer"
    if any(p.search(clean) for p in _EXPRESS):
        return "express"
    if _TASK_NOT_ABOUT_ERVI.search(clean) or _PRICE_OF_OTHER.search(clean):
        return ""
    mentions_ervi = bool(_SELF_ANY.search(clean))
    bare = bool(_LICENSE_BARE.fullmatch(clean))
    for pattern, needs_self in _LICENSE:
        if pattern.search(clean) and (not needs_self or mentions_ervi or bare):
            return "license"
    if any(p.search(clean) for p in _OFFER):
        return "offer"
    return ""


class BusinessOffer:
    """Bundled offer plus an optional server copy; never blocks the UI thread."""

    def __init__(self, root_dir: Path, data_dir: Path, *, api_base: str = "", build: str = "", headers: dict[str, str] | None = None):
        self.root_dir = Path(root_dir)
        self.data_dir = Path(data_dir)
        self.api_base = str(api_base or "").rstrip("/")
        self.build = str(build or "")
        self._headers = dict(headers or {})
        self._lock = threading.RLock()
        self._bundled: dict[str, Any] | None = None
        self._remote: dict[str, Any] | None = None
        self._remote_loaded = False
        self._next_fetch = 0.0
        self._fetching = False

    # ── loading ────────────────────────────────────────────────────────────────
    def _bundled_path(self) -> Path | None:
        here = Path(__file__).resolve()
        for candidate in (
            self.root_dir / "assets" / OFFER_FILE,
            here.parents[2] / "assets" / OFFER_FILE if len(here.parents) > 2 else None,
        ):
            if candidate is not None and candidate.is_file():
                return candidate
        return None

    def bundled(self) -> dict[str, Any]:
        with self._lock:
            if self._bundled is None:
                raw: Any = {}
                path = self._bundled_path()
                if path is not None:
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8-sig"))
                    except Exception:
                        raw = {}
                self._bundled = sanitize_offer(raw)
            return self._bundled

    def _cache_path(self) -> Path:
        return self.data_dir / "cache" / REMOTE_CACHE_FILE

    def _load_remote_cache(self) -> None:
        if self._remote_loaded:
            return
        self._remote_loaded = True
        try:
            payload = json.loads(self._cache_path().read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("offer"), dict):
                self._remote = payload["offer"]
                fetched = float(payload.get("fetched_at") or 0)
                self._next_fetch = fetched + _REMOTE_TTL
        except Exception:
            self._remote = None

    def get(self, *, refresh: bool = True) -> dict[str, Any]:
        with self._lock:
            self._load_remote_cache()
            offer = sanitize_offer(self._remote, base=self.bundled()) if self._remote else dict(self.bundled())
            should_fetch = refresh and bool(self.api_base) and not self._fetching and time.time() >= self._next_fetch
            if should_fetch:
                self._fetching = True
                threading.Thread(target=self._fetch_remote, daemon=True, name="eirven-offer").start()
            offer["refreshing"] = self._fetching
            return offer

    def _fetch_remote(self) -> None:
        retry_after = _REMOTE_RETRY_AFTER_ERROR
        try:
            import httpx

            with httpx.Client(timeout=4.0, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", f"{self.api_base}/offer", headers={
                    **self._headers, "Accept": "application/json",
                }) as response:
                    if response.status_code in (404, 410):
                        retry_after = _REMOTE_RETRY_AFTER_MISSING
                        return
                    if response.status_code != 200:
                        return
                    body = b""
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > _MAX_REMOTE_BYTES:
                            return
            data = json.loads(body.decode("utf-8-sig"))
            if not isinstance(data, dict) or int(data.get("schema") or 0) != 1:
                return
            retry_after = _REMOTE_TTL
            with self._lock:
                self._remote = data
            cache = self._cache_path()
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": time.time(), "offer": data}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache)
        except Exception:
            return
        finally:
            with self._lock:
                self._fetching = False
                self._next_fetch = time.time() + retry_after

    # ── contact ────────────────────────────────────────────────────────────────
    def compose_request(self, kind: str, fields: dict[str, Any] | None = None) -> str:
        fields = fields or {}
        kind = kind if kind in {"business", "express", "license"} else "business"
        want = {
            "business": "Хочу настроить Эрви под задачи бизнеса.",
            "express": "Хочу подключить Эрви Express.",
            "license": "Нужна коммерческая лицензия на Эрви.",
        }[kind]
        lines = ["Здравствуйте! Пишу из Эрви" + (f" ({self.build})" if self.build else "") + ".", want]
        labels = (("business", "Сфера"), ("task", "Задача"), ("contact", "Связь со мной"))
        for key, label in labels:
            value = _clean_text(fields.get(key), 700 if key == "task" else 120)
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines)

    def contact_link(self, text: str, *, kind: str = "business") -> tuple[str, str]:
        """Return ``(link, channel)``; the link pre-fills the request when it can."""
        offer = self.get(refresh=False)
        contact = offer.get("contact") or {}
        handle = str(contact.get("telegram") or "")
        email = str(contact.get("email") or "")
        url = str(contact.get("url") or "")
        body = str(text or "")
        if kind == "express":
            # Express оплачивается на сайте: страница сразу выдаёт персональную
            # ссылку на установщик, писать автору и ждать ответа не нужно.
            checkout = str((offer.get("express") or {}).get("url") or "")
            if checkout:
                return _with_utm(checkout, "express"), "checkout"
        if handle:
            base = f"https://t.me/{handle}"
            return base + "?text=" + quote(_fit(body, len(base) + 6), safe=""), "telegram"
        if email:
            base = f"mailto:{email}?subject=" + quote("Эрви для бизнеса", safe="") + "&body="
            return base + quote(_fit(body, len(base)), safe=""), "email"
        if url:
            content = kind if kind in {"business", "express", "license"} else "business"
            return _with_utm(url, content), "url"
        return "", ""

    def chat_answer(self, intent: str) -> tuple[str, dict[str, Any]]:
        offer = self.get(refresh=False)
        packages = offer.get("packages") or []
        subscription = offer.get("subscription") or {}
        express = offer.get("express") or {}
        route: dict[str, Any] = {
            "action": "business_offer", "model": "deterministic", "control_plane": True,
            "completed": True, "verified": True, "business_intent": intent,
        }

        def support_sentence() -> str:
            price = str(subscription.get("price") or "").strip()
            if not price:
                return ""
            note = str(subscription.get("note") or "").strip()
            title = str(subscription.get("title") or "Сопровождение").strip()
            return f"{title} — {price}" + (f" {note}" if note else "") + "."

        if intent == "express":
            price = str(express.get("price") or "").strip()
            text = str(express.get("text") or "ранние версии и приоритетная поддержка").strip().rstrip(".")
            answer = f"Эрви Express — {text[:1].lower() + text[1:]}" + (f". Стоит {price}." if price else ".")
            answer += " Подключить можно в настройках, раздел «Обновления» — там же видно, какой канал у тебя сейчас."
            route["open_settings_tab"] = "updates"
            return answer, route
        personal = str(offer.get("personal_note") or "").strip()
        if intent == "license":
            note = str(offer.get("license_note") or "").strip() or (
                "Использовать меня в компании ради прибыли можно по коммерческой лицензии — "
                "она входит в любой пакет настройки."
            )
            parts = [personal, note, support_sentence()]
        else:
            pitch = str(offer.get("chat_pitch") or "").strip() or (
                "Для бизнеса меня настраивают под ваши задачи: ответы клиентам, объявления, почта и документы."
            )
            parts = [personal, pitch]
            priced = [f"«{p['title']}» — {p['price']}" for p in packages if p.get("title") and p.get("price")]
            if priced:
                parts.append("Пакеты: " + ", ".join(priced) + ".")
            parts.append(support_sentence())
            parts.append(str(offer.get("chat_process") or "").strip())
        parts.append("Подробности и заявка — в разделе «Для бизнеса» на главном экране Эрви.")
        route["open_view"] = "business"
        answer = " ".join(" ".join(p.split()) for p in parts if p)
        return answer, route


def _with_utm(url: str, content: str) -> str:
    """Метка источника без личных данных: по ней в статистике сайта видно, что
    человек пришёл из приложения, и из какой кнопки."""
    if "utm_" in url:
        return url
    tag = f"utm_source=eirven_app&utm_medium=in_app&utm_campaign=business&utm_content={content}"
    base, _, fragment = url.partition("#")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{tag}" + (f"#{fragment}" if fragment else "")


def _fit(text: str, prefix_len: int) -> str:
    """Trim text so that its percent-encoded form fits into a safe link length."""
    budget = max(200, _MAX_LINK_CHARS - prefix_len)
    value = str(text or "")
    while value and len(quote(value, safe="")) > budget:
        value = value[: max(0, int(len(value) * 0.85))]
    if value != text:
        value = value.rstrip() + "…"
    return value
