from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .action_model import action_num_gpu
from .resilience import AdaptiveRecovery
from .tasks import TaskNeedsUser
from .trace import log_event


@dataclass(slots=True)
class AutonomousResult:
    ok: bool
    summary: str
    steps: list[dict[str, Any]]
    needs_user: bool = False
    prompt: str = ""


@dataclass(slots=True)
class Observation:
    title: str
    handle: int | None
    pid: int | None
    elements: list[dict[str, Any]]
    compact: str
    fingerprint: str
    window_class: str = ""
    window_rect: tuple[int, int, int, int] | None = None
    browser: bool = False
    context_lost: bool = False
    ts: float = field(default_factory=time.time)


class AutonomousWorkflowEngine:
    """r16 state-driven workflow engine.

    Unlike the r15 high-level workflow, this engine does not create an application recipe
    before touching the UI.  It receives one final owner goal and repeats the same bounded
    control loop:

        observe foreground -> enumerate affordances -> choose ONE local action -> execute
        -> wait for a new settled state -> verify -> choose the next local action.

    UIA is the primary grounding channel.  The local action model is only asked to choose
    among affordances visible *now* (or a small set of generic bootstrap primitives such as
    launching an app / opening a search).  Committed side effects are single-shot: an
    uncertain result is never repaired by blindly repeating the same click/send/submit.
    """

    _ACTION = re.compile(
        r"\b(открой|зайди|запусти|включи|выключи|закрой|нажми|кликни|перейди|найди|поиск|"
        r"добавь|положи|ответь|напиши|отправь|введи|вбей|выбери|поставь|включи|"
        r"пролистай|листай|прокрути|скачай|установи|сохрани|удали|создай|заполни)\w*",
        re.I,
    )
    _DEPENDENCY = re.compile(
        r"\b(его|ее|её|их|этот|эту|это|там|затем|потом|после|последн\w*|всем|"
        r"непрочитан\w*|корзин\w*|результат\w*|карточк\w*)\b",
        re.I,
    )
    _RESUME = re.compile(
        r"^\s*(готово|сделал|сделала|вошел|вошёл|вошла|авторизовался|авторизовалась|продолжай|дальше)\s*[.!]?\s*$",
        re.I,
    )
    _PAYMENT = re.compile(
        r"\b(оплат|плат[её]ж|checkout|place order|оформить заказ|подтвердить заказ|куп\w*|buy now)\w*",
        re.I,
    )
    _AUTH = re.compile(r"\b(login|sign in|captcha|2fa|uac|парол|авторизац|войти|подтвердить вход)\w*", re.I)
    _GENERIC_WORDS = {
        "найди", "найти", "открой", "открыть", "добавь", "добавить", "положи", "корзину", "корзина",
        "браслет", "товар", "карточку", "карточка", "приложение", "сайт", "альбом", "музыку", "последний",
        "ответь", "всем", "непрочитанным", "личным", "чатам", "каналы", "пропусти", "моем", "моём", "стиле",
        "и", "в", "на", "по", "его", "ее", "её", "их", "мне", "мой", "моя", "это", "этот", "эту",
        "еще", "ещё", "после", "этого", "дальше", "далее", "заодно", "потом", "затем",
    }
    _SEARCH_FIELD_MARKERS = ("поиск", "search", "найти", "find", "query")
    _SEARCH_BUTTON_MARKERS = ("искать", "найти", "search", "поиск")
    _MENU_MARKERS = ("меню", "menu", "каталог", "catalog", "categories", "категории")
    _NAV_WORDS = ("раздел", "категория", "категории", "вкладка", "пункт", "меню", "каталог", "корзина", "cart", "bag")
    _CART_MARKERS = ("добавить в корзину", "в корзину", "add to cart", "add to bag")
    _SEND_MARKERS = ("отправить", "send", "reply", "ответить")
    _DELETE_MARKERS = ("удалить", "delete", "remove")

    def __init__(self, services: Any):
        self.services = services
        self.tools = services.tools
        self.operator = services.desktop_operator
        self.gateway = services.gateway
        # The autonomous policy, deterministic app skills and MissionEngine all operate
        # on one real foreground desktop.  A shared RLock keeps nested calls safe while
        # preventing two workflows from acting on different assumptions at once.
        self._lock = getattr(services, "desktop_lock", None) or threading.RLock()
        self._active_anchor_handle: int | None = None
        self._active_anchor_title = ""
        self._active_anchor_pid: int | None = None
        self._active_anchor_browser = False
        self._browser_handoff_until = 0.0

    @staticmethod
    def _norm(text: Any) -> str:
        text = str(text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _trace(self, event: str, **data: Any) -> None:
        try:
            log_event(self.services.settings.root_dir, event, **data)
        except Exception:
            pass

    def _runtime_step(self, text: str, **meta: Any) -> None:
        try:
            runtime = getattr(self.services, "runtime", None)
            if runtime is not None:
                runtime.step(text, **meta)
        except Exception:
            pass

    def _reset_anchor(self) -> None:
        self._active_anchor_handle = None
        self._active_anchor_title = ""
        self._active_anchor_pid = None
        self._active_anchor_browser = False
        self._browser_handoff_until = 0.0

    def _record_experience(
        self, goal: str, observation: Observation, recovery: AdaptiveRecovery,
        *, ok: bool, verified: bool, error: str = "", history: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            cognition = getattr(self.services, "cognition", None)
            if cognition is None:
                return
            result = cognition.record_outcome(
                goal, observation.title, strategy=recovery.strategy_generation,
                ok=ok, verified=verified, error=error, steps=history,
            )
            if result.get("skill_suggestion"):
                self._trace("SKILL_SUGGESTION_READY", goal_key=result.get("key"), successes=result.get("successes"))
        except Exception:
            pass

    def _pending_key(self, conversation_id: str) -> str:
        return f"autonomous_workflow_pending:{conversation_id or 'voice'}"

    def has_pending(self, conversation_id: str) -> bool:
        try:
            value = self.services.db.get_setting(self._pending_key(conversation_id), None)
            return isinstance(value, dict) and bool(value.get("goal"))
        except Exception:
            return False

    def _save_pending(self, conversation_id: str, payload: dict[str, Any]) -> None:
        try:
            self.services.db.set_setting(self._pending_key(conversation_id), payload)
        except Exception:
            pass

    def _clear_pending(self, conversation_id: str) -> None:
        try:
            self.services.db.set_setting(self._pending_key(conversation_id), {})
        except Exception:
            pass

    @classmethod
    def _clarification_prompt(cls, goal: str) -> str:
        """Ask only for information that materially changes a consequential action."""
        clean = cls._norm(goal)
        if not clean:
            return "Что именно нужно сделать?"
        if "корзин" in clean and re.search(r"\b(?:добав|полож)\w*\b", clean):
            arbitrary = bool(re.search(r"\b(?:любой|любое|любую|случайн\w*)\b", clean))
            generic = bool(re.search(
                r"\b(?:добав|полож)\w*\s+(?:(?:мне|пожалуйста)\s+)?(?:какой то\s+)?(?:товар|позици\w*|что нибудь)\s+(?:в\s+)?корзин",
                clean,
            ))
            if generic and not arbitrary:
                return "Какой именно товар добавить в корзину? Назови товар или скажи «любой»."
        if re.search(r"\bудал\w*\b", clean) and re.search(r"\b(?:чат|сообщен)\w*\b", clean):
            plural_last = bool(re.search(r"\bпоследн(?:ие|их)\s+сообщен\w*\b", clean))
            has_count = bool(re.search(r"\b(?:одно|два|три|четыре|пять|\d{1,2})\s+(?:последн\w*\s+)?сообщен\w*\b", clean))
            if plural_last and not has_count:
                return "Сколько последних сообщений удалить — и удалить их только у тебя или у всех?"
            has_scope = bool(re.search(r"\b(?:у\s+всех|для\s+всех|у\s+(?:меня|тебя)|только\s+у\s+(?:меня|тебя))\b", clean))
            if not has_scope:
                return "Удалить сообщение только у тебя или у всех участников?"
            if re.search(r"\b(?:в|из)\s+чат\w*\b", clean) and not re.search(r"\b(?:с|у)\s+[a-zа-я0-9_@.-]{2,}\b", clean):
                return "В каком именно чате удалить сообщения?"
        return ""

    def should_handle(self, query: str, conversation_id: str = "") -> bool:
        if conversation_id and self.has_pending(conversation_id):
            return bool(self._RESUME.match(query)) or bool(self._ACTION.search(query))
        if not self._ACTION.search(query):
            return False
        actions = len(self._ACTION.findall(query))
        dependent = bool(self._DEPENDENCY.search(query))
        sequence = bool(re.search(r"[,;]|\b(?:и|затем|потом|после этого|после чего)\b", query, re.I))
        # r18 also owns single-step navigation/search commands while a browser page is
        # foreground.  This prevents phrases such as "зайди в раздел каталог" from
        # leaking into Windows file/folder intent simply because the verb is "открой".
        browser_context = False
        try:
            win = self._active_window() or {}
            browser_context = self._browser_window(str(win.get("title") or ""), str(win.get("class_name") or ""))
        except Exception:
            browser_context = False
        page_navigation = bool(re.search(
            r"\b(?:зайди|перейди|открой|нажми|выбери|найди|отыщи)\w*.{0,80}\b(?:раздел|каталог|категори|корзин|на этой странице|на странице|на сайте)\w*",
            str(query or ""), re.I,
        ))
        return (actions >= 2 and sequence) or dependent or (browser_context and page_navigation) or bool(re.search(r"\b(?:до результата|сам(?:а)? разберись|самостоятельно)\b", query, re.I))

    def _active_window(self) -> dict[str, Any] | None:
        try:
            result = self.tools.execute("foreground_window", {})
            if result.get("ok"):
                row = dict(result.get("result") or {})
                if str(row.get("title") or "").strip():
                    return row
        except Exception:
            pass
        return None

    @staticmethod
    def _element_blob(element: dict[str, Any]) -> str:
        return " ".join(
            str(element.get(key) or "")
            for key in ("control_type", "name", "automation_id", "class_name", "value")
        )

    @staticmethod
    def _browser_window(title: str, class_name: str = "") -> bool:
        blob = f"{title} {class_name}".casefold()
        return bool(re.search(r"(?:chrome|chromium|edge|firefox|opera|brave|samsung browser|yandex browser|браузер)", blob))

    def _browser_chrome(self, element: dict[str, Any]) -> bool:
        try:
            checker = getattr(self.operator, "_is_browser_chrome", None)
            if callable(checker):
                return bool(checker(element))
        except Exception:
            pass
        blob = self._norm(self._element_blob(element))
        rect = element.get("rectangle") or []
        try:
            if len(rect) == 4 and int(rect[3]) <= 150:
                return True
        except Exception:
            pass
        return any(marker in blob for marker in (
            "omnibox", "address bar", "адресная строка", "tabstrip", "tabs toolbar",
            "browserappmenubutton", "locationbar", "windowcaptionbutton", "view 1012",
        ))

    @staticmethod
    def _page_scoped_goal(goal: str) -> bool:
        return bool(re.search(
            r"\b(?:(?:на|в)\s+(?:(?:этой|текущей|этом|текущем)\s+)?(?:странице|сайте|каталоге|экране)|здесь|тут)\b",
            str(goal or ""), re.I,
        ))

    def _visual_point(self, observation: Observation, x: Any, y: Any) -> tuple[int, int] | None:
        try:
            import pyautogui
            width, height = pyautogui.size()
            fx = max(0.0, min(1.0, float(x)))
            fy = max(0.0, min(1.0, float(y)))
            px = int(fx * max(1, width - 1)); py = int(fy * max(1, height - 1))
        except Exception:
            return None
        rect = observation.window_rect
        if rect:
            left, top, right, bottom = rect
            if px < left or px > right or py < top or py > bottom:
                return None
            # Never let the visual fallback touch tabs/address/search chrome.  Win32 uses
            # logical coordinates on the owner's DPI-scaled desktop; ~90 px covers the
            # complete Chromium/Samsung toolbar seen in the trace.
            if observation.browser and py <= top + 90:
                return None
        return px, py

    def _visual_browser_decision(self, goal: str, observation: Observation, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not observation.browser or self.operator is None:
            return None
        vision = getattr(self.operator, "_tiny_visual_json", None)
        if not callable(vision):
            return None
        subject = self._search_subject(goal) or " ".join(self._goal_terms(goal)[:3])
        page_scope = self._page_scoped_goal(goal)
        schema = {
            "type":"object",
            "properties":{
                "action":{"type":"string","enum":["click","type_subject","press_enter","scroll_down","scroll_up","wait","done","fail"]},
                "x":{"type":"number"},"y":{"type":"number"},
                "label":{"type":"string"},"reason":{"type":"string"},"evidence":{"type":"string"},
                "commit_kind":{"type":"string","enum":["","cart","send","delete"]},
                "subject_visible":{"type":"boolean"},
            },
            "required":["action","x","y","label","reason","evidence","commit_kind","subject_visible"],
        }
        recent = [{k: step.get(k) for k in ("action","target","text","ok","verified","changed","error") if step.get(k) not in (None, "")} for step in history[-5:]]
        prompt = (
            "Ты visual step-policy EIRVEN. Выбери РОВНО ОДНО следующее действие только по реально видимой веб-странице текущего браузера. "
            "ПОЛНОСТЬЮ игнорируй верхнюю панель браузера: вкладки, адресную строку, браузерный поиск, меню и системные кнопки; координаты там запрещены. "
            "Если нужный товар/результат уже виден на странице — открывай его, не начинай новый поиск. "
            "type_subject разрешён только для поля поиска/фильтра ВНУТРИ САМОЙ СТРАНИЦЫ; движок введёт только SUBJECT и НЕ нажмёт Enter в этом же шаге. "
            "press_enter — отдельный следующий шаг только после ввода в такое поле. "
            + ("Пользователь явно сказал искать на текущей странице: НЕ используй адресную строку и не уходи во внешний/web search. " if page_scope else "") +
            "Если виден Add to cart/Добавить в корзину именно у нужного товара и цель это явно просит, action=click, commit_kind=cart и subject_visible=true. "
            "Никогда не оформляй заказ/платёж, не вводи пароль/CAPTCHA/2FA. Если контент ещё грузится — wait. "
            f"ЦЕЛЬ: {goal}\nSUBJECT ДЛЯ ВВОДА: {subject}\nПОСЛЕДНИЕ ДЕЙСТВИЯ: {json.dumps(recent, ensure_ascii=False)}"
        )
        try:
            data = vision(prompt, schema, timeout=4.0)
        except Exception as exc:
            self._trace("R16_VISUAL_DECISION_ERROR", goal=goal, error=str(exc)[:600])
            return None
        if not isinstance(data, dict):
            return None
        action = str(data.get("action") or "fail")
        common = {
            "reason": str(data.get("reason") or "visual page grounding"),
            "expected": str(data.get("evidence") or "page state changed"),
            "evidence": str(data.get("evidence") or ""),
            "target": str(data.get("label") or ""),
            "x": data.get("x", .5), "y": data.get("y", .5),
            "commit_kind": str(data.get("commit_kind") or ""),
            "subject_visible": bool(data.get("subject_visible")),
        }
        if action == "type_subject":
            if not subject:
                return {"action":"fail","target_index":-1,"text":"","reason":"visual policy requested input without a subject","expected":""}
            return {"action":"visual_type","target_index":-1,"text":subject, **common}
        if action == "click":
            return {"action":"visual_click","target_index":-1,"text":"","commit":bool(common["commit_kind"]), **common}
        if action == "press_enter":
            return {"action":"press","target_index":-1,"text":"","key":"enter","commit":False, **common}
        if action == "scroll_down":
            return {"action":"scroll","target_index":-1,"text":"","amount":-7, **common}
        if action == "scroll_up":
            return {"action":"scroll","target_index":-1,"text":"","amount":7, **common}
        if action in {"wait","done","fail"}:
            return {"action":action,"target_index":-1,"text":"","amount":1 if action == "wait" else 0, **common}
        return None

    def _observe(self) -> Observation:
        win = self._active_window() or {}
        title = str(win.get("title") or "")
        handle = int(win.get("handle") or 0) or None
        pid = int(win.get("pid") or 0) or None
        window_class = str(win.get("class_name") or "")
        raw_rect = win.get("rectangle") or []
        window_rect = None
        try:
            if len(raw_rect) == 4:
                window_rect = tuple(int(v) for v in raw_rect)
        except Exception:
            window_rect = None
        browser = self._browser_window(title, window_class)
        if self._active_anchor_handle and handle and int(handle) != int(self._active_anchor_handle):
            legitimate_browser_handoff = bool(
                browser and self._active_anchor_browser
                and pid and self._active_anchor_pid and int(pid) == int(self._active_anchor_pid)
                and time.monotonic() <= self._browser_handoff_until
            )
            if legitimate_browser_handoff:
                self._active_anchor_handle = int(handle)
                self._active_anchor_title = title
            else:
                raw = f"TITLE={title}\nCONTEXT_LOST expected={self._active_anchor_title!r}"
                return Observation(
                    title=title, handle=handle, pid=pid, elements=[], compact=raw,
                    fingerprint=hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest(),
                    window_class=window_class, window_rect=window_rect, browser=browser, context_lost=True,
                )
        rows: list[dict[str, Any]] = []
        if title and self.operator is not None:
            try:
                rows = list(self.operator._elements(title, limit=300, handle=handle))
            except Exception:
                rows = []
        useful: list[dict[str, Any]] = []
        lines: list[str] = []
        for row in rows:
            if not row.get("visible", True):
                continue
            rect = row.get("rectangle") or []
            if len(rect) == 4:
                try:
                    if int(rect[2]) <= int(rect[0]) or int(rect[3]) <= int(rect[1]):
                        continue
                except Exception:
                    pass
            # Critical r16.1 grounding guard: browser chrome is not a webpage affordance.
            # Samsung exposes its Omnibox as a Group named "Адресная строка и строка
            # поиска" even when the site exposes no accessibility tree at all.
            if browser and self._browser_chrome(row):
                continue
            item = dict(row)
            item["index"] = len(useful)
            useful.append(item)
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            typ = str(item.get("control_type") or "")
            aid = str(item.get("automation_id") or "")
            focus = " focus" if item.get("focused") else ""
            if name or value or typ in {"Edit", "ComboBox", "Button", "Hyperlink", "ListItem", "MenuItem", "TabItem"}:
                lines.append(f"[{item['index']}] {typ}{focus} name={name!r} value={value!r} id={aid!r}")
            if len(lines) >= 180:
                break
        raw = f"TITLE={title}\nBROWSER={browser}\n" + "\n".join(lines)
        fingerprint = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
        observation = Observation(
            title=title, handle=handle, pid=pid, elements=useful, compact=raw[:11000], fingerprint=fingerprint,
            window_class=window_class, window_rect=window_rect, browser=browser, context_lost=False,
        )
        try:
            cognition = getattr(self.services, "cognition", None)
            if cognition is not None:
                cognition.record_observation(title=title, handle=handle, fingerprint=fingerprint, browser=browser)
        except Exception:
            pass
        return observation

    def _planner_model(self) -> str:
        settings = self.services.settings
        try:
            installed = {str(x).casefold() for x in self.gateway.installed_models()}
        except Exception:
            installed = set()
        for candidate in (
            getattr(settings, "action_model", ""),
            getattr(settings, "fast_model", ""),
            getattr(settings, "model", ""),
        ):
            candidate = str(candidate or "").strip()
            if candidate and (not installed or candidate.casefold() in installed):
                return candidate
        return str(getattr(settings, "fast_model", "") or getattr(settings, "model", ""))

    def _agent_num_gpu(self) -> int | None:
        try:
            return action_num_gpu(self.services.settings)
        except Exception:
            return None

    def _goal_terms(self, goal: str) -> list[str]:
        words = [w for w in self._norm(goal).split() if len(w) >= 3 and w not in self._GENERIC_WORDS]
        # Preserve order, but keep the state prompt compact.
        out: list[str] = []
        for word in words:
            if word not in out:
                out.append(word)
        return out[:12]

    def _search_subject(self, goal: str) -> str:
        text = str(goal or "").strip()
        text = re.sub(r"^\s*(?:(?:и\s+)?(?:ещ[её]|заодно|дальше|далее)|(?:и\s+)?после\s+этого|(?:и\s+)?потом|(?:и\s+)?затем)\s*[,;:.-]*\s*", "", text, flags=re.I)
        m = re.search(r"\b(?:найди|найти|поищи|поиск)\w*\s+(.+?)(?=\s+(?:и|затем|потом)\s+|$)", text, re.I)
        candidate = (m.group(1) if m else "").strip(" ,.;:\"'«»")
        # Location words describe *where* to search and must never become the query.
        # Example from the owner's trace: "найди на странице браслет кармы" -> "кармы",
        # not "на странице браслет кармы".
        candidate = re.sub(r"^(?:(?:на|в)\s+(?:(?:этой|текущей|этом|текущем)\s+)?(?:странице|сайте|каталоге|экране)|здесь|тут)\s+", "", candidate, flags=re.I)
        object_match = re.match(r"^(?:товар\w*|браслет\w*|альбом\w*|трек\w*|исполнител\w*|контакт\w*|чат\w*)\s+(.+)$", candidate, flags=re.I)
        if object_match:
            candidate = object_match.group(1).strip()
            # Conservative Russian genitive recovery for a very common -ма -> -мы
            # name/noun pattern ("браслет кармы" -> search "карма"). This is language
            # normalization, not a site/product recipe, and avoids sending an inflected
            # owner phrase to literal site search.
            if re.fullmatch(r"[А-Яа-яЁё]{4,}", candidate) and candidate.casefold().endswith("мы"):
                candidate = candidate[:-1] + ("А" if candidate[-2:].isupper() else "а")
        return candidate[:220]

    def _best_matching_element(self, observation: Observation, terms: list[str], roles: tuple[str, ...] = ()) -> tuple[int, float] | None:
        wanted_roles = {self._norm(x) for x in roles}
        best: tuple[int, float] | None = None
        for element in observation.elements:
            if not element.get("enabled", True):
                continue
            typ = self._norm(element.get("control_type"))
            if wanted_roles and typ not in wanted_roles:
                continue
            blob = self._norm(self._element_blob(element))
            name = self._norm(element.get("name"))
            if not blob:
                continue
            score = 0.0
            for term in terms:
                t = self._norm(term)
                if not t:
                    continue
                if name == t:
                    score += 5.0
                elif t in name:
                    score += 3.4
                elif t in blob:
                    score += 2.2
                else:
                    score += SequenceMatcher(None, t, name or blob).ratio() * .7
            if typ in {"button", "hyperlink", "listitem", "menuitem", "tabitem", "treeitem"}:
                score += .5
            idx = int(element.get("index") or 0)
            if best is None or score > best[1]:
                best = (idx, score)
        return best

    def _navigation_target(self, goal: str) -> str:
        """Extract a named destination on the current site, never a Windows object."""
        raw = str(goal or "").strip().strip(" .!?")
        n = self._norm(raw)
        # "перейди в корзину" / "открой каталог" are common without the word section.
        m = re.search(r"\b(?:зайди|перейди|открой|нажми|выбери)\w*\s+(?:на\s+(?:этой|текущей)\s+странице\s+)?(?:в|на)?\s*(?:раздел\w*|категори\w*|вкладк\w*|пункт\w*|меню)?\s*[«\"']?(.+?)[»\"']?$", n, re.I)
        target = (m.group(1).strip() if m else "")
        target = re.sub(r"^(?:в|на)\s+", "", target).strip()
        target = re.sub(r"\b(?:на|в)\s+(?:этой|текущей)\s+(?:странице|сайте)\b", "", target).strip()
        target = {"корзину":"корзина", "каталога":"каталог", "каталоге":"каталог"}.get(target, target)
        # Never treat a whole search/product command as a navigation label.
        if not target or len(target) > 70 or re.search(r"\b(?:найди|добав|купи|поиск)\w*", target):
            return ""
        return target

    def _site_navigation_match(self, observation: Observation, target: str) -> tuple[int, float] | None:
        target_n = self._norm(target)
        if not target_n:
            return None
        best = None
        for e in observation.elements:
            typ = self._norm(e.get("control_type"))
            if typ not in {"button","hyperlink","listitem","menuitem","tabitem","treeitem"}:
                continue
            name = self._norm(e.get("name"))
            blob = self._norm(self._element_blob(e))
            if not name:
                continue
            score = 0.0
            if name == target_n:
                score = 6.0
            elif target_n in name:
                score = 4.2
            elif name in target_n and len(name) >= 4:
                score = 3.4
            else:
                score = SequenceMatcher(None, target_n, name).ratio() * 2.2
            if any(x in blob for x in ("nav", "menu", "header", "catalog", "category", "cart", "корзин")):
                score += .45
            if e.get("visible", True):
                score += .25
            idx = int(e.get("index") or 0)
            if best is None or score > best[1]:
                best = (idx, score)
        return best

    def _heuristic_decision(self, goal: str, observation: Observation, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Cheap generic affordance decisions before the local model.

        Browser chrome is already removed from ``observation.elements``.  This is
        intentional: an Omnibox is never a site search field.
        """
        goal_n = self._norm(goal)
        subject = self._search_subject(goal)
        subject_n = self._norm(subject)
        last = history[-1] if history else {}
        typed_subject = bool(subject_n and any(
            step.get("action") in {"type", "visual_type"}
            and step.get("ok")
            and self._norm(step.get("text")) == subject_n
            for step in history
        ))
        submitted_search = any(
            step.get("action") in {"click", "press", "visual_click"}
            and step.get("ok")
            and (
                "search submit" in self._norm(step.get("reason"))
                or self._norm(step.get("target")) in {"искать", "найти", "search"}
                or (step.get("action") == "press" and self._norm(step.get("key")) == "enter" and typed_subject)
            )
            for step in history
        )
        search_scrolls = sum(
            1 for step in history
            if step.get("action") == "scroll" and self._norm(step.get("reason")).startswith("search recovery")
        )
        search_triggered = any(
            step.get("action") == "click" and step.get("ok")
            and self._norm(step.get("reason")).startswith("search trigger")
            for step in history
        )

        # Generic inbox/chat discovery for unfamiliar sites (Kwork included).  This is
        # based only on semantic UI roles and unread state, never site coordinates.
        chat_inspection = bool(re.search(
            r"\b(?:посмотри|проверь|прочитай|открой)\w*.{0,45}\b(?:чат|диалог|сообщен|переписк)\w*",
            goal_n, re.I,
        ))
        if chat_inspection:
            grounded, evidence = self._chat_inspection_evidence(goal, observation, history)
            if grounded:
                return {"action":"done", "target_index":-1, "reason":"chat inspection grounded",
                        "evidence":evidence, "expected":"active conversation visible"}
            inbox_opened = any(
                step.get("action") == "click" and step.get("ok")
                and self._norm(step.get("reason")).startswith("chat inbox navigation")
                for step in history
            )
            if not inbox_opened:
                inbox = self._best_matching_element(
                    observation,
                    ["сообщения", "чаты", "диалоги", "переписка", "messages", "inbox", "chats"],
                    ("Button", "Hyperlink", "ListItem", "MenuItem", "TabItem"),
                )
                if inbox and inbox[1] >= 3.0:
                    return {"action":"click", "target_index":inbox[0],
                            "reason":"chat inbox navigation: semantic messages section",
                            "expected":"chat list"}

            specific = re.search(r"\b(?:чат|диалог|переписк)\w*\s+(?:с|у)\s+([a-zа-я0-9_@.-]{2,50})", goal_n, re.I)
            specific_name = specific.group(1).strip() if specific else ""
            if specific_name not in {"клиент", "клиентом", "заказчик", "заказчиком", "человек", "человеком"}:
                peer = self._best_matching_element(
                    observation, [specific_name],
                    ("Button", "Hyperlink", "ListItem", "TreeItem"),
                ) if specific_name else None
                if peer and peer[1] >= 3.0:
                    return {"action":"click", "target_index":peer[0],
                            "reason":"chat peer explicitly named", "expected":specific_name}

            unread: list[dict[str, Any]] = []
            for element in observation.elements:
                typ = self._norm(element.get("control_type"))
                if typ not in {"button", "hyperlink", "listitem", "treeitem"} or not element.get("visible", True) or not element.get("enabled", True):
                    continue
                blob = self._norm(self._element_blob(element))
                if any(mark in blob for mark in ("unread", "непрочитан", "new message", "новое сообщение", "badge unread", "has unread")):
                    unread.append(element)
            if len(unread) == 1:
                return {"action":"click", "target_index":int(unread[0].get("index") or 0),
                        "reason":"single unread client conversation", "expected":"active conversation"}
            if inbox_opened and len(unread) > 1:
                return {"action":"ask_user", "target_index":-1,
                        "reason":"Вижу несколько непрочитанных диалогов. Уточни имя клиента.", "expected":"recipient clarification"}
            if inbox_opened and not unread:
                return {"action":"ask_user", "target_index":-1,
                        "reason":"Раздел сообщений открыт, но не вижу однозначного непрочитанного чата. Уточни имя клиента.", "expected":"recipient clarification"}

        # r18 site navigation is surface-aware. A named destination on a foreground
        # browser page (catalog/category/cart/menu) wins over any OS interpretation.
        nav_target = self._navigation_target(goal) if observation.browser else ""
        if nav_target:
            nav = self._site_navigation_match(observation, nav_target)
            if nav and nav[1] >= 3.0:
                return {"action":"click", "target_index":nav[0], "reason":"site navigation target", "expected":nav_target}
            # If a destination is not exposed yet, open a generic site menu once and
            # observe again. This is structure discovery, not a site-specific recipe.
            menu_opened = any(self._norm(x.get("reason")).startswith("site menu reveal") and x.get("ok") for x in history)
            if not menu_opened:
                menu = self._best_matching_element(observation, list(self._MENU_MARKERS), ("Button","Hyperlink","MenuItem"))
                if menu and menu[1] >= 2.2:
                    return {"action":"click", "target_index":menu[0], "reason":"site menu reveal", "expected":nav_target}

        # 1) If the correct product is already open and an explicit cart affordance is
        # visible, commit that instead of clicking the product title again.
        if "корзин" in goal_n:
            cart = self._best_matching_element(observation, list(self._CART_MARKERS), ("Button", "Hyperlink"))
            if cart and cart[1] >= 2.0 and self._subject_visible(goal, observation):
                return {"action":"click", "target_index":cart[0], "reason":"explicit add-to-cart affordance", "expected":"cart changed", "commit":True}

        # r22 generic commerce fallback for an explicitly arbitrary product.  There is no
        # product name to search for, so asking the language model to invent one is wrong.
        # Choose one visible content/product card, then allow the explicit cart commit on
        # the next observation.  This remains site-agnostic and coordinate-free.
        any_product = bool(re.search(r"\b(?:любой|любое|любую)\s+(?:товар|позици|карточк)\w*", goal_n))
        any_product_opened = any(
            step.get("action") == "click" and step.get("ok")
            and self._norm(step.get("reason")).startswith("explicit any product")
            for step in history
        )
        if any_product and "корзин" in goal_n:
            if any_product_opened:
                cart = self._best_matching_element(observation, list(self._CART_MARKERS), ("Button", "Hyperlink"))
                if cart and cart[1] >= 2.0:
                    return {"action":"click", "target_index":cart[0], "reason":"explicit add-to-cart after arbitrary product", "expected":"cart changed", "commit":True, "subject_visible":True}
            else:
                ranked=[]
                banned=(
                    "поиск","search","меню","menu","главная","home","каталог","корзин","cart","фильтр","filter","сортир",
                    "реклам","promo","promotion","banner","баннер","акция","скидк","sale","campaign","hero",
                )
                for e in observation.elements:
                    typ=self._norm(e.get("control_type")); rect=e.get("rectangle") or []
                    if typ not in {"hyperlink","listitem","button","group"} or len(rect)!=4 or not e.get("visible",True):
                        continue
                    name=self._norm(e.get("name")); blob=self._norm(self._element_blob(e))
                    if not name or any(x in blob for x in banned): continue
                    product_evidence = any(x in blob for x in ("product","товар","card","карточ","price","цена","руб","₽","₽"))
                    if not product_evidence:
                        continue
                    score=0.0
                    if typ in {"hyperlink","listitem"}: score+=1.2
                    if product_evidence: score+=3.2
                    if 5 <= len(name) <= 180: score+=1.0
                    if int(rect[1]) > 250: score+=.5
                    ranked.append((score,int(e.get("index") or 0)))
                if ranked and max(ranked)[0] >= 2.8:
                    return {"action":"click", "target_index":max(ranked)[1], "reason":"explicit any product: visible content card", "expected":"product details"}

        # 2) Prefer an already-visible semantic result/product over opening any search.
        terms = self._goal_terms(subject or goal)
        if terms:
            result = self._best_matching_element(observation, terms, ("Hyperlink", "ListItem", "Button", "Text", "Group"))
            if result and result[1] >= max(2.8, len(terms) * 1.35):
                return {"action":"click", "target_index":result[0], "reason":"semantic result match", "expected":"matching item/details"}

        # 3) Search is a state machine, not a retry loop. Once a subject was verified
        # in the page field, never type it again just because UIA does not expose the
        # field value. Prefer submit; after one submit, reveal results by scrolling.
        if subject and typed_subject and not submitted_search:
            match = self._best_matching_element(observation, list(self._SEARCH_BUTTON_MARKERS), ("Button", "Hyperlink", "MenuItem"))
            if match and match[1] >= 2.0:
                return {"action":"click", "target_index":match[0], "reason":"visible search submit", "expected":"search results"}
            if observation.browser and search_scrolls < 2:
                return {"action":"scroll", "target_index":-1, "amount":-5, "reason":"search recovery: reveal submit", "expected":"search controls"}

        if subject and submitted_search:
            # The site may render matches below the fold without changing the document
            # title or accessibility value. Scroll through the result region instead of
            # re-entering the same query four times.
            if observation.browser and search_scrolls < 7 and not self._subject_visible(goal, observation):
                return {"action":"scroll", "target_index":-1, "amount":-7, "reason":"search recovery: reveal results", "expected":subject}
            if observation.browser and search_scrolls < 7 and not any(
                step.get("action") == "click" and "semantic result" in self._norm(step.get("reason"))
                for step in history
            ):
                return {"action":"scroll", "target_index":-1, "amount":-7, "reason":"search recovery: scan results", "expected":subject}

        # 4) A site may expose only a Search icon/button until its modal/drawer is
        # opened (e.g. large commerce sites). Reveal that field once, then re-observe.
        if subject and not typed_subject and not search_triggered:
            has_field = any(
                self._norm(e.get("control_type")) in {"edit","combobox"}
                and any(m in self._norm(self._element_blob(e)) for m in self._SEARCH_FIELD_MARKERS)
                for e in observation.elements
            )
            if not has_field:
                trigger = self._best_matching_element(observation, list(self._SEARCH_BUTTON_MARKERS), ("Button","Hyperlink","MenuItem"))
                if trigger and trigger[1] >= 2.0:
                    return {"action":"click", "target_index":trigger[0], "reason":"search trigger: reveal field", "expected":"search field"}

        # 5) If a real PAGE search input exists and this query has not been entered yet,
        # type only the extracted subject. Prefer a concrete Edit over wrapper Groups.
        if subject and not typed_subject:
            candidates = []
            role_rank = {"edit": 5, "combobox": 4, "group": 2, "document": 1}
            for e in observation.elements:
                typ = self._norm(e.get("control_type"))
                blob = self._norm(self._element_blob(e))
                if typ in role_rank and any(m in blob for m in self._SEARCH_FIELD_MARKERS):
                    candidates.append(e)
            if candidates:
                field = max(
                    candidates,
                    key=lambda e: (
                        role_rank.get(self._norm(e.get("control_type")), 0),
                        1 if e.get("focused") else 0,
                    ),
                )
                current = self._norm(f"{field.get('value','')} {field.get('name','')}")
                if subject_n and subject_n not in current:
                    return {"action":"type", "target_index":int(field.get("index") or 0), "text":subject, "reason":"visible search field", "expected":"search query visible"}

        # r22: after we deliberately opened a Search affordance, some SPAs expose the
        # modal field as an unnamed/focused Edit.  The successful trigger is the grounding
        # evidence; use only a real page Edit/ComboBox and never the browser Omnibox.
        if subject and not typed_subject and search_triggered:
            generic_fields=[]
            for e in observation.elements:
                typ=self._norm(e.get("control_type")); rect=e.get("rectangle") or []
                if typ not in {"edit","combobox"} or len(rect)!=4 or not e.get("visible",True) or not e.get("enabled",True):
                    continue
                score=(5 if e.get("focused") else 0) + (2 if int(rect[1]) > 145 else 0) + min(max(0,int(rect[2])-int(rect[0])),900)/900.0
                generic_fields.append((score,e))
            if generic_fields:
                field=max(generic_fields,key=lambda x:x[0])[1]
                current=self._norm(f"{field.get('value','')} {field.get('name','')}")
                if subject_n and subject_n not in current:
                    return {"action":"type", "target_index":int(field.get("index") or 0), "text":subject, "reason":"revealed generic search field", "expected":"search query visible"}

        return None

    def _decision(self, goal: str, observation: Observation, history: list[dict[str, Any]], recovery: AdaptiveRecovery | None = None) -> dict[str, Any]:
        generation = recovery.strategy_generation if recovery is not None else 0
        heuristic = self._heuristic_decision(goal, observation, history) if generation == 0 else None
        if heuristic:
            return heuristic
        # The first four semantic/UIA attempts already failed: switch modality before
        # asking the same small text policy to choose another look-alike control.
        if generation >= 1 and observation.browser:
            visual = self._visual_browser_decision(goal, observation, history)
            if visual:
                return visual
        # Samsung/Chromium can expose only browser chrome through UIA.  In that case do
        # not ask a text model to hallucinate an affordance from an empty tree; use one
        # bounded screenshot-grounded page action instead.
        if observation.browser and len(observation.elements) <= 3:
            visual = self._visual_browser_decision(goal, observation, history)
            if visual:
                return visual
        schema = {
            "type":"object",
            "properties":{
                "action":{"type":"string","enum":["click","type","press","scroll","visual_click","visual_type","launch_application","search_web","open_url","wait","done","ask_user","fail"]},
                "target_index":{"type":"integer"},
                "text":{"type":"string"},
                "key":{"type":"string"},
                "amount":{"type":"integer"},
                "target":{"type":"string"},
                "reason":{"type":"string"},
                "expected":{"type":"string"},
                "evidence":{"type":"string"},
                "commit":{"type":"boolean"},
            },
            "required":["action","target_index","text","key","amount","target","reason","expected","evidence","commit"],
        }
        recent = [
            {k: step.get(k) for k in ("action","target","text","ok","verified","changed","error") if step.get(k) not in (None, "")}
            for step in history[-7:]
        ]
        recovery_context = recovery.prompt_context() if recovery is not None else "Стратегия 0: свежая попытка."
        experience_context = ""
        try:
            cognition = getattr(self.services, "cognition", None)
            if cognition is not None:
                experience_context = cognition.guidance(goal, observation.title)
        except Exception:
            experience_context = ""
        prompt = (
            "Ты локальный step-policy r18 для Windows desktop-agent EIRVEN. У тебя есть ОДНА конечная цель владельца и ТЕКУЩЕЕ "
            "состояние интерфейса. Не строй сценарий наперёд. Выбери ровно ОДНО следующее локальное действие по affordances, которые видны сейчас. "
            "После него движок заново увидит экран и снова спросит тебя. target_index обязан ссылаться на существующий индекс для click/type. "
            "Предпочитай UIA-элементы; браузерная адресная строка/Omnibox никогда не является поиском сайта и уже исключена из списка. "
            "launch_application/search_web/open_url только когда нужного приложения/сайта реально нет перед тобой. "
            "type только вводит текст, не отправляет. press enter/кнопка отправки — отдельный шаг. commit=true ставь для отправки сообщения, удаления, "
            "добавления в корзину и других действий, которые нельзя бездумно повторять. Не оформляй покупку/платёж и не обходи login/CAPTCHA/UAC/2FA. "
            "done разрешён только если текущий экран уже содержит наблюдаемое подтверждение всей конечной цели; evidence назови буквально по экрану. "
            "Если нужен ручной login/CAPTCHA/UAC/2FA — ask_user. Если интерфейс грузится — wait. "
            "Никогда не повторяй тот же успешный type/click, если он уже есть в ПОСЛЕДНИХ ДЕЙСТВИЯХ и состояние не изменилось: "
            "измени стратегию (submit, scroll, wait или другой видимый affordance).\n\n"
            f"КОНЕЧНАЯ ЦЕЛЬ: {goal}\n"
            f"ВОССТАНОВЛЕНИЕ:\n{recovery_context}\n"
            f"ОПЫТ ПРОШЛЫХ ЗАПУСКОВ:\n{experience_context or 'Нет.'}\n"
            f"СТИЛЬ ВЛАДЕЛЬЦА (для ответов/сообщений): {self._style_prompt()[:1800]}\n"
            f"ПОСЛЕДНИЕ ДЕЙСТВИЯ: {json.dumps(recent, ensure_ascii=False)}\n\n"
            f"ТЕКУЩИЙ UI:\n{observation.compact}"
        )
        try:
            data = self.gateway.json(
                [{"role":"user","content":prompt}],
                model=self._planner_model(),
                temperature=0.0,
                schema=schema,
                num_ctx=1900,
                num_predict=180,
                keep_alive="45s",
                timeout_seconds=5.5,
                num_gpu=self._agent_num_gpu(),
            )
            if isinstance(data, dict):
                return data
        except Exception as exc:
            self._trace("R16_DECISION_ERROR", goal=goal, error=str(exc)[:700])
        return {"action":"fail","target_index":-1,"text":"","key":"","amount":0,"target":"","reason":"local policy unavailable","expected":"","evidence":"","commit":False}

    def _style_prompt(self) -> str:
        try:
            return self.services.style.get().prompt()
        except Exception:
            return ""

    def _subject_visible(self, goal: str, observation: Observation) -> bool:
        terms = self._goal_terms(self._search_subject(goal) or goal)
        if not terms:
            return True
        screen = self._norm(observation.compact)
        # One distinctive term is enough for named products/artists.  For generic words,
        # require two. _goal_terms already removes most generic action nouns.
        hits = sum(1 for term in terms if term in screen)
        return hits >= (1 if len(terms) <= 2 else 2)

    def _is_commit(self, decision: dict[str, Any], element: dict[str, Any] | None) -> bool:
        if bool(decision.get("commit")):
            return True
        blob = self._norm(self._element_blob(element or {}))
        return any(marker in blob for marker in (*self._CART_MARKERS, *self._SEND_MARKERS, *self._DELETE_MARKERS))

    def _commit_kind(self, element: dict[str, Any] | None) -> str:
        blob = self._norm(self._element_blob(element or {}))
        if any(x in blob for x in self._CART_MARKERS):
            return "cart"
        if any(x in blob for x in self._SEND_MARKERS):
            return "send"
        if any(x in blob for x in self._DELETE_MARKERS):
            return "delete"
        return "generic"

    def _precommit_check(self, goal: str, observation: Observation, decision: dict[str, Any], element: dict[str, Any] | None) -> tuple[bool, str]:
        goal_n = self._norm(goal)
        blob = self._norm(self._element_blob(element or {}))
        if self._PAYMENT.search(goal) or self._PAYMENT.search(blob):
            return False, "Оформление заказа/платёж не выполняются автономно"
        kind = self._commit_kind(element)
        if kind == "cart":
            if "корзин" not in goal_n:
                return False, "Добавление в корзину не было явно запрошено"
            arbitrary = bool(re.search(r"\b(?:любой|любое|любую)\s+(?:товар|позици|карточк)\w*", goal_n))
            if not (self._subject_visible(goal, observation) or (arbitrary and bool(decision.get("subject_visible")))):
                return False, "Перед добавлением в корзину не подтверждён нужный товар"
        elif kind == "send":
            if not re.search(r"\b(отправ|ответ|напиш)\w*", goal_n):
                return False, "Отправка сообщения не была явно запрошена"
        elif kind == "delete":
            if not re.search(r"\bудал\w*", goal_n):
                return False, "Удаление не было явно запрошено"
            if re.search(r"\b(?:telegram|телеграм|чат|сообщен)\w*\b", goal_n):
                wants_everyone = bool(re.search(r"\b(?:у|для)\s+всех\b", goal_n))
                wants_self = bool(re.search(r"\b(?:только\s+)?у\s+(?:меня|тебя)\b", goal_n))
                if not (wants_everyone or wants_self):
                    return False, "Не указан безопасный охват удаления: у себя или у всех"
                if wants_everyone and re.search(r"\b(?:delete\s+for\s+me|удалить\s+у\s+меня|только\s+у\s+меня)\b", blob):
                    return False, "Выбран охват «только у меня», но владелец запросил удаление у всех"
                if wants_self and re.search(r"\b(?:delete\s+for\s+everyone|удалить\s+у\s+всех|для\s+всех)\b", blob):
                    return False, "Выбран охват «у всех», но владелец запросил удаление только у себя"
        return True, ""

    def _signature(self, observation: Observation, decision: dict[str, Any], element: dict[str, Any] | None, goal: str = "") -> str:
        kind = str(decision.get("commit_kind") or "") or (self._commit_kind(element) if element else "generic")
        # Cart add is a final side effect for the r16 commerce acceptance.  Its identity
        # must survive a cosmetic UI state change after the click, otherwise a changed
        # fingerprint could make the same Add-to-cart button look retryable.  Messaging
        # keeps state in the signature because one goal may intentionally send to several
        # distinct chats.
        state = observation.fingerprint
        if kind == "cart":
            state = "cart:" + self._norm(self._search_subject(goal) or goal)[:220]
        payload = {
            "state": state,
            "action": decision.get("action"),
            "target": self._norm(self._element_blob(element or {}))[:280],
            "text": str(decision.get("text") or "")[:500],
            "key": str(decision.get("key") or ""),
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _wait_transition(self, observation: Observation, expected: str = "", timeout: float = 5.5, stop_event: threading.Event | None = None) -> dict[str, Any]:
        if observation.browser:
            # r18 observations intentionally exclude browser chrome. Do not compare that
            # filtered set with DesktopOperator.wait_for_state(), which enumerates chrome
            # again and would report a false transition on every click.
            end = time.monotonic() + max(.35, min(float(timeout), 6.0))
            expected_terms = [x for x in self._norm(expected).split() if len(x) >= 4][:4]
            last_fp = observation.fingerprint; stable_since = 0.0; changed = False; latest = observation
            while time.monotonic() < end:
                if stop_event and stop_event.is_set():
                    return {"changed":False,"expected_seen":False,"settled":False,"cancelled":True}
                time.sleep(.16)
                latest = self._observe()
                if latest.context_lost:
                    return {"changed":False,"expected_seen":False,"settled":False,"context_lost":True}
                if latest.fingerprint != observation.fingerprint:
                    changed = True
                expected_seen = any(term in self._norm(latest.compact) for term in expected_terms) if expected_terms else False
                now = time.monotonic()
                if latest.fingerprint == last_fp:
                    if not stable_since: stable_since = now
                else:
                    last_fp = latest.fingerprint; stable_since = 0.0
                if (changed or expected_seen) and stable_since and now - stable_since >= .28:
                    return {"changed":changed,"expected_seen":expected_seen,"settled":True,"title":latest.title,"rows":latest.elements,"fingerprint":latest.fingerprint}
            return {"changed":changed,"expected_seen":False,"settled":False,"title":latest.title,"rows":latest.elements,"fingerprint":latest.fingerprint}
        if not self.operator or not observation.title:
            time.sleep(.35)
            return {"changed": True, "settled": True}
        expected_terms = [x for x in self._norm(expected).split() if len(x) >= 4][:4]
        try:
            if stop_event and stop_event.is_set():
                return {"changed":False,"settled":False,"cancelled":True}
            # Bound each wait slice so a newly accepted foreground command can revoke
            # ownership before another click/type is attempted.
            remaining=max(.25,min(float(timeout),6.0)); before=list(observation.elements)
            end=time.monotonic()+remaining
            latest={"changed":False,"settled":False}
            while time.monotonic()<end:
                if stop_event and stop_event.is_set():
                    return {"changed":False,"settled":False,"cancelled":True}
                latest=self.operator.wait_for_state(
                    handle=observation.handle,title=observation.title,before_rows=before,
                    timeout=min(.65,max(.2,end-time.monotonic())),expected=expected_terms,
                )
                if latest.get("changed") or latest.get("expected_seen"):
                    return latest
            return latest
        except Exception:
            if stop_event and stop_event.is_set():
                return {"changed":False,"settled":False,"cancelled":True}
            time.sleep(.18)
            return {"changed": False, "settled": False}

    def _execute_local(self, goal: str, observation: Observation, decision: dict[str, Any], committed: set[str], stop_event: threading.Event | None = None) -> dict[str, Any]:
        action = str(decision.get("action") or "fail")
        result: dict[str, Any] = {"action": action, "reason": str(decision.get("reason") or ""), "expected": str(decision.get("expected") or "")}
        if stop_event and stop_event.is_set():
            return {**result,"ok":False,"verified":False,"cancelled":True,"error":"Остановлено новой командой владельца"}
        if action == "fail":
            return {**result, "ok":False, "verified":False, "error":result["reason"] or "Нет безопасного следующего шага"}
        if action == "ask_user":
            raise TaskNeedsUser(str(decision.get("reason") or "Нужен ручной шаг владельца"))
        if action == "wait":
            delay = max(.2, min(float(decision.get("amount") or 1), 5.0))
            self.tools.execute("wait", {"seconds": delay})
            return {**result, "ok":True, "verified":True, "changed":False, "waited":delay}
        if action == "scroll":
            amount = int(decision.get("amount") or -6)
            tool = self.tools.execute("scroll", {"amount": max(-18, min(18, amount))})
            transition = self._wait_transition(observation, result["expected"], timeout=2.8, stop_event=stop_event)
            return {**result, "ok":bool(tool.get("ok")), "verified":bool(tool.get("ok")), "changed":bool(transition.get("changed"))}
        if action == "press":
            key = str(decision.get("key") or "enter").strip().lower() or "enter"
            # Enter can be a submit/send. Treat it as committed when the policy says so.
            signature = self._signature(observation, decision, None, goal)
            if bool(decision.get("commit")):
                if self._PAYMENT.search(goal):
                    return {**result, "ok":False, "verified":False, "precommit_blocked":True, "error":"Платёж/покупка не выполняются автономно"}
                if not re.search(r"\b(отправ|ответ|напиш)\w*", self._norm(goal)):
                    return {**result, "ok":False, "verified":False, "precommit_blocked":True, "error":"Committed Enter не подтверждён конечной целью"}
            if bool(decision.get("commit")) and signature in committed:
                return {**result, "ok":False, "completed":True, "verified":False, "error":"Committed key action already executed in this state; duplicate blocked"}
            if stop_event and stop_event.is_set(): return {**result,"ok":False,"verified":False,"cancelled":True,"error":"Остановлено новой командой владельца"}
            tool = self.tools.execute("press_key", {"key": key})
            if bool(decision.get("commit")) and tool.get("ok"):
                committed.add(signature)
            transition = self._wait_transition(observation, result["expected"], stop_event=stop_event)
            return {**result, "ok":bool(tool.get("ok")), "completed":bool(tool.get("ok") and decision.get("commit")), "verified":bool(tool.get("ok") and transition.get("changed")), "changed":bool(transition.get("changed")), "key":key}
        if action in {"visual_click", "visual_type"}:
            point = self._visual_point(observation, decision.get("x", .5), decision.get("y", .5))
            if point is None:
                return {**result, "ok":False, "verified":False, "error":"Visual target rejected: outside webpage content/browser chrome"}
            px, py = point
            before_digest = ""
            try:
                _path, before_digest = self.operator._screenshot_digest() if self.operator else ("", "")
            except Exception:
                pass
            # Re-check that the autonomous task still owns the same browser window before
            # a coordinate action. This closes the trace bug where the task continued in Notepad.
            now = self._active_window() or {}
            if observation.handle and int(now.get("handle") or 0) != int(observation.handle):
                return {**result, "ok":False, "verified":False, "error":"Активное окно сменилось перед visual action"}

            commit_kind = str(decision.get("commit_kind") or "")
            committed_action = bool(decision.get("commit") or commit_kind)
            if commit_kind == "cart":
                if "корзин" not in self._norm(goal):
                    return {**result, "ok":False, "verified":False, "precommit_blocked":True, "error":"Добавление в корзину не было явно запрошено"}
                if not bool(decision.get("subject_visible")):
                    return {**result, "ok":False, "verified":False, "precommit_blocked":True, "error":"Visual pre-commit не подтвердил нужный товар"}
                if self._PAYMENT.search(goal):
                    return {**result, "ok":False, "verified":False, "precommit_blocked":True, "error":"Платёж/покупка не выполняются автономно"}
            signature = self._signature(observation, decision, None, goal)
            if committed_action and signature in committed:
                return {**result, "ok":False, "completed":True, "verified":False, "error":"Committed visual action already executed; duplicate blocked"}

            if stop_event and stop_event.is_set(): return {**result,"ok":False,"verified":False,"cancelled":True,"error":"Остановлено новой командой владельца"}
            if observation.browser:
                self._browser_handoff_until = time.monotonic() + 5.5
            self.tools.execute("mouse_move", {"x":px,"y":py,"duration":.12})
            clicked = bool(self.tools.execute("click", {"x":px,"y":py}).get("ok"))
            if not clicked:
                return {**result, "ok":False, "verified":False, "error":"Visual click не выполнился"}
            verified = clicked
            evidence = "visual_page_point"
            if action == "visual_type":
                text = str(decision.get("text") or "").strip()
                if not text:
                    return {**result, "ok":False, "verified":False, "error":"Пустой текст visual input"}
                try:
                    import pyperclip
                    old_clip = str(pyperclip.paste() or "")
                    pyperclip.copy(text)
                    self.tools.execute("hotkey", {"keys":["ctrl","a"]})
                    pasted = bool(self.tools.execute("hotkey", {"keys":["ctrl","v"]}).get("ok"))
                    sentinel = f"__EIRVEN_R16_{time.monotonic_ns()}__"
                    pyperclip.copy(sentinel)
                    self.tools.execute("hotkey", {"keys":["ctrl","a"]})
                    copied_ok = bool(self.tools.execute("hotkey", {"keys":["ctrl","c"]}).get("ok"))
                    time.sleep(.05)
                    copied = str(pyperclip.paste() or "")
                    verified = bool(pasted and copied_ok and copied != sentinel and self._norm(copied) == self._norm(text))
                    self.tools.execute("press_key", {"key":"end"})
                    try: pyperclip.copy(old_clip)
                    except Exception: pass
                    evidence = "visual_clipboard_roundtrip" if verified else "visual_input_unverified"
                except Exception:
                    verified = False
                if not verified:
                    return {**result, "text":text, "ok":False, "verified":False, "changed":False, "error":"Visual input не подтвердил введённый текст"}
                result["text"] = text

            if committed_action and clicked:
                committed.add(signature)
            self.tools.execute("wait", {"seconds":.45})
            changed = False
            try:
                _path, after_digest = self.operator._screenshot_digest() if self.operator else ("", "")
                changed = bool(before_digest and after_digest and before_digest != after_digest)
            except Exception:
                pass
            return {
                **result, "target":str(decision.get("target") or "visual page target"),
                "ok":bool(clicked and verified), "verified":bool(verified), "changed":changed,
                "completed":bool(committed_action and clicked), "commit":committed_action,
                "commit_kind":commit_kind or ("generic" if committed_action else ""), "evidence":evidence,
            }

        if action == "launch_application":
            target = str(decision.get("target") or decision.get("text") or "").strip()
            tool = self.tools.execute("launch_application", {"application": target})
            self.tools.execute("wait", {"seconds": .6})
            after = self._observe()
            return {**result, "target":target, "ok":bool(tool.get("ok")), "verified":bool(tool.get("ok") and after.fingerprint != observation.fingerprint), "changed":after.fingerprint != observation.fingerprint}
        if action == "search_web":
            query = str(decision.get("target") or decision.get("text") or "").strip()
            tool = self.tools.execute("default_search", {"query": query})
            self.tools.execute("wait", {"seconds": .7})
            after = self._observe()
            return {**result, "target":query, "ok":bool(tool.get("ok")), "verified":bool(tool.get("ok") and after.fingerprint != observation.fingerprint), "changed":after.fingerprint != observation.fingerprint}
        if action == "open_url":
            url = str(decision.get("target") or decision.get("text") or "").strip()
            tool = self.tools.execute("open_default_url", {"url": url})
            self.tools.execute("wait", {"seconds": .7})
            after = self._observe()
            return {**result, "target":url, "ok":bool(tool.get("ok")), "verified":bool(tool.get("ok") and after.fingerprint != observation.fingerprint), "changed":after.fingerprint != observation.fingerprint}

        raw_idx = decision.get("target_index")
        idx = int(raw_idx if raw_idx is not None else -1)
        if idx < 0 or idx >= len(observation.elements):
            return {**result, "ok":False, "verified":False, "error":f"Некорректный target_index={idx}"}
        element = observation.elements[idx]
        result["target"] = str(element.get("name") or element.get("automation_id") or element.get("control_type") or idx)
        signature = self._signature(observation, decision, element, goal)
        committed_action = self._is_commit(decision, element)
        if committed_action:
            allowed, error = self._precommit_check(goal, observation, decision, element)
            if not allowed:
                return {**result, "ok":False, "verified":False, "error":error, "precommit_blocked":True}
            if signature in committed:
                return {**result, "ok":False, "completed":True, "verified":False, "error":"Committed action already executed in this state; duplicate blocked"}

        if action == "click":
            if stop_event and stop_event.is_set(): return {**result,"ok":False,"verified":False,"cancelled":True,"error":"Остановлено новой командой владельца"}
            if observation.browser:
                self._browser_handoff_until = time.monotonic() + 5.5
            ok = bool(self.operator and self.operator.click_element(observation.title, element, goal=goal))
            if committed_action and ok:
                committed.add(signature)
            transition = self._wait_transition(observation, result["expected"], stop_event=stop_event)
            changed = bool(transition.get("changed") or transition.get("expected_seen"))
            return {
                **result,
                "ok": bool(ok and (changed or committed_action)),
                "completed": bool(ok and committed_action),
                "verified": bool(ok and changed),
                "changed": changed,
                "commit": committed_action,
                "commit_kind": self._commit_kind(element) if committed_action else "",
            }

        if action == "type":
            text = str(decision.get("text") or "").strip()
            if not text:
                return {**result, "ok":False, "verified":False, "error":"Пустой текст для ввода"}
            element_blob = self._norm(self._element_blob(element))
            purpose = "search" if any(m in element_blob for m in self._SEARCH_FIELD_MARKERS) else "input"
            search_goal = bool(self._search_subject(goal))
            impostor = any(mark in element_blob for mark in (
                "сортир", "sort", "фильтр", "filter", "диапазон цен", "price range", "количество", "quantity",
            ))
            if search_goal and (impostor or purpose != "search"):
                return {
                    **result, "text": text, "ok": False, "verified": False,
                    "error": "Выбранное поле не является поиском страницы; сортировка/фильтр отклонены",
                }
            acquired = {
                "ok": True,
                "title": observation.title,
                "handle": observation.handle,
                "field": element,
                "focused": bool(element.get("focused")),
                "rows": observation.elements,
                "purpose": purpose,
            }
            if stop_event and stop_event.is_set(): return {**result,"text":text,"ok":False,"verified":False,"cancelled":True,"error":"Остановлено новой командой владельца"}
            typed = self.operator.type_verified(acquired, text, submit=False, require_verified=True) if self.operator else {"ok":False,"verified":False}
            after = self._observe()
            changed = after.fingerprint != observation.fingerprint
            return {**result, "text":text, "ok":bool(typed.get("ok")), "verified":bool(typed.get("verified")), "changed":changed, "evidence":typed.get("evidence","")}

        return {**result, "ok":False, "verified":False, "error":f"Неизвестное действие: {action}"}

    def _grounded_evidence(self, evidence: str, observation: Observation) -> bool:
        words = [w for w in self._norm(evidence).split() if len(w) >= 4]
        if not words:
            return False
        screen = self._norm(observation.compact)
        hits = sum(1 for word in words if word in screen)
        return hits >= min(2, len(words))

    def _chat_inspection_evidence(
        self, goal: str, observation: Observation, history: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        goal_n = self._norm(goal)
        if not re.search(r"\b(?:посмотри|проверь|прочитай|открой)\w*.{0,45}\b(?:чат|диалог|сообщен|переписк)\w*", goal_n, re.I):
            return False, ""
        navigated = any(
            step.get("action") == "click" and step.get("ok")
            and any(mark in self._norm(step.get("reason")) for mark in (
                "chat inbox navigation", "single unread client conversation", "chat peer explicitly named",
            ))
            for step in history
        )
        composer = False
        message_rows = 0
        active = False
        for element in observation.elements:
            if not element.get("visible", True):
                continue
            typ = self._norm(element.get("control_type"))
            blob = self._norm(self._element_blob(element))
            if typ in {"edit", "group", "document"} and any(mark in blob for mark in (
                "message", "сообщение", "reply", "ответ", "composer", "chat input", "написать",
            )):
                composer = True
            if "active" in self._norm(element.get("class_name")) and typ in {"listitem", "hyperlink", "button"}:
                active = True
            name = str(element.get("name") or "").strip()
            if typ in {"text", "listitem", "group"} and 2 <= len(name) <= 800:
                if not re.fullmatch(r"(?:message|сообщение|reply|ответ|search|поиск)", self._norm(name)):
                    message_rows += 1
        if composer and message_rows >= 2 and (navigated or active):
            return True, "active chat composer and messages visible"
        return False, ""

    def _verify_completion(self, goal: str, observation: Observation, history: list[dict[str, Any]], evidence_hint: str = "") -> tuple[bool, str]:
        goal_n = self._norm(goal)
        screen = self._norm(observation.compact)
        last = history[-1] if history else {}
        chat_done, chat_evidence = self._chat_inspection_evidence(goal, observation, history)
        if chat_done:
            return True, chat_evidence
        # Strong deterministic post-commit evidence for the main acceptance class.
        if "корзин" in goal_n and last.get("commit_kind") == "cart" and last.get("completed"):
            cart_state = any(x in screen for x in ("в корзине", "добавлено в корзину", "cart 1", "корзина 1"))
            if cart_state:
                return True, "cart state visible"
            if observation.browser and len(observation.elements) <= 3 and self.operator is not None:
                try:
                    if self.operator.verify_visible_goal(goal, timeout=3.4):
                        return True, "cart state visually confirmed"
                except Exception:
                    pass
        if evidence_hint and self._grounded_evidence(evidence_hint, observation):
            return True, evidence_hint

        schema = {
            "type":"object",
            "properties":{
                "done":{"type":"boolean"},
                "evidence":{"type":"string"},
                "confidence":{"type":"number"},
            },
            "required":["done","evidence","confidence"],
        }
        prompt = (
            "Проверь только по ТЕКУЩЕМУ UI, достигнута ли конечная цель целиком. Не делай вывод из того, что клик был выполнен. "
            "done=true только если на экране есть конкретное наблюдаемое подтверждение результата. В evidence процитируй/назови видимый признак.\n\n"
            f"ЦЕЛЬ: {goal}\n"
            f"UI:\n{observation.compact}"
        )
        try:
            data = self.gateway.json(
                [{"role":"user","content":prompt}], model=self._planner_model(), temperature=0.0,
                schema=schema, num_ctx=1450, num_predict=90, keep_alive="45s", timeout_seconds=4.2,
                num_gpu=self._agent_num_gpu(),
            )
            if isinstance(data, dict) and bool(data.get("done")) and float(data.get("confidence") or 0) >= .72:
                evidence = str(data.get("evidence") or "")
                if self._grounded_evidence(evidence, observation):
                    return True, evidence
        except Exception as exc:
            self._trace("R16_VERIFY_ERROR", goal=goal, error=str(exc)[:600])
        return False, ""

    def execute_goal(
        self,
        goal: str,
        *,
        conversation_id: str = "",
        stop_event: threading.Event | None = None,
        max_steps: int = 24,
    ) -> AutonomousResult:
        with self._lock:
            pending: dict[str, Any] | None = None
            if conversation_id and self._RESUME.match(goal):
                try:
                    raw = self.services.db.get_setting(self._pending_key(conversation_id), None)
                    pending = dict(raw) if isinstance(raw, dict) and raw.get("goal") else None
                except Exception:
                    pending = None
            if pending:
                final_goal = str(pending.get("goal") or goal)
                history = list(pending.get("history") or [])
                committed = set(str(x) for x in (pending.get("committed") or []))
                start_step = int(pending.get("step") or len(history))
                recovery = AdaptiveRecovery.from_dict(pending.get("recovery"))
                try:
                    cognition = getattr(self.services, "cognition", None)
                    if cognition is not None:
                        cognition.clear_auth_checkpoint()
                except Exception:
                    pass
                self._trace("R16_RESUME", goal=final_goal, step=start_step)
            else:
                final_goal = str(goal or "").strip()
                history: list[dict[str, Any]] = []
                committed: set[str] = set()
                start_step = 0
                recovery = AdaptiveRecovery(attempts_per_strategy=4, max_strategy_changes=3)

            if not final_goal:
                return AutonomousResult(False, "Пустая цель.", history)
            clarification = "" if pending else self._clarification_prompt(final_goal)
            if clarification:
                return AutonomousResult(False, clarification, history, needs_user=True, prompt=clarification)

            unchanged_streak = 0
            self._reset_anchor()
            for step_no in range(start_step, max(1, min(int(max_steps), 32))):
                if stop_event and stop_event.is_set():
                    if conversation_id:
                        self._save_pending(conversation_id, {"goal":final_goal,"history":history,"committed":sorted(committed),"step":step_no,"state":"interrupted","saved_at":time.time()})
                    return AutonomousResult(False, "Остановила текущую автономную задачу.", history)

                observation = self._observe()
                if observation.context_lost:
                    if conversation_id:
                        self._clear_pending(conversation_id)
                    return AutonomousResult(False, "Активное окно сменилось во время автономной задачи; остановилась, чтобы не вводить/кликать в чужом окне.", history)
                if self._active_anchor_handle is None and observation.handle:
                    self._active_anchor_handle = int(observation.handle)
                    self._active_anchor_title = observation.title
                    self._active_anchor_pid = observation.pid
                    self._active_anchor_browser = bool(observation.browser)
                self._runtime_step(
                    f"Автономный шаг {step_no + 1}: анализирую текущее состояние",
                    stage="autonomous_workflow", step=step_no + 1, title=observation.title,
                )
                self._trace("R16_OBSERVE", goal=final_goal, step=step_no + 1, title=observation.title, fingerprint=observation.fingerprint, elements=len(observation.elements))

                # Verify the whole final goal only when the last action can plausibly be
                # terminal. Typing/search/scroll are intermediate transitions; running a
                # local LLM verifier after each one caused 4-5 s stalls and encouraged
                # stale-action retries when the verifier timed out.
                if history and history[-1].get("ok"):
                    last_action = str(history[-1].get("action") or "")
                    terminal_candidate = bool(
                        history[-1].get("commit")
                        or history[-1].get("completed")
                        or (
                            last_action in {"click", "visual_click"}
                            and "semantic result" not in self._norm(history[-1].get("reason"))
                            and "search submit" not in self._norm(history[-1].get("reason"))
                        )
                    )
                    if terminal_candidate:
                        done, evidence = self._verify_completion(final_goal, observation, history)
                        if done:
                            self._record_experience(final_goal, observation, recovery, ok=True, verified=True, history=history)
                            if conversation_id:
                                self._clear_pending(conversation_id)
                            self._trace("R16_DONE", goal=final_goal, step=step_no, evidence=evidence)
                            return AutonomousResult(True, "Готово. Конечная цель достигнута и подтверждена по текущему состоянию.", history)

                decision = self._decision(final_goal, observation, history, recovery)
                # The local model call above is bounded but not instantaneous.  Re-check
                # cancellation before trusting its now potentially stale UI decision.
                if stop_event and stop_event.is_set():
                    if conversation_id:
                        self._save_pending(conversation_id, {"goal":final_goal,"history":history,"committed":sorted(committed),"step":step_no,"state":"interrupted","saved_at":time.time()})
                    return AutonomousResult(False, "Остановила текущую автономную задачу.", history)
                if str(decision.get("action") or "") == "scroll" and self._norm(decision.get("reason")).startswith("search recovery"):
                    self._trace("R17_RECOVER", goal=final_goal, step=step_no + 1, strategy="scroll", reason=str(decision.get("reason") or ""))
                self._trace("R16_DECIDE", goal=final_goal, step=step_no + 1, decision=decision)
                if str(decision.get("action") or "") == "done":
                    done, evidence = self._verify_completion(final_goal, observation, history, str(decision.get("evidence") or ""))
                    if done:
                        self._record_experience(final_goal, observation, recovery, ok=True, verified=True, history=history)
                        if conversation_id:
                            self._clear_pending(conversation_id)
                        return AutonomousResult(True, "Готово. Конечная цель достигнута и подтверждена по текущему состоянию.", history)
                    # A premature done is not fatal: force a fresh local action next round.
                    history.append({"action":"done","ok":False,"verified":False,"error":"completion evidence not grounded"})
                    continue

                switching = str(decision.get("action") or "") in {"launch_application", "search_web", "open_url"}
                old_anchor = (
                    self._active_anchor_handle, self._active_anchor_title,
                    self._active_anchor_pid, self._active_anchor_browser,
                    self._browser_handoff_until,
                )
                if switching:
                    self._reset_anchor()
                try:
                    local = self._execute_local(final_goal, observation, decision, committed, stop_event=stop_event)
                    if switching and not local.get("ok"):
                        (
                            self._active_anchor_handle, self._active_anchor_title,
                            self._active_anchor_pid, self._active_anchor_browser,
                            self._browser_handoff_until,
                        ) = old_anchor
                except TaskNeedsUser as exc:
                    if conversation_id:
                        self._save_pending(conversation_id, {"goal":final_goal,"history":history,"committed":sorted(committed),"step":step_no,"state":"waiting_user","saved_at":time.time()})
                    prompt = exc.prompt or "Нужен ручной шаг владельца"
                    self._trace("R16_WAIT_USER", goal=final_goal, step=step_no + 1, prompt=prompt)
                    return AutonomousResult(False, prompt + " После этого скажи «готово».", history, needs_user=True, prompt=prompt)

                local["step"] = step_no + 1
                local["state"] = observation.fingerprint
                history.append(local)
                self._trace("R16_ACT", goal=final_goal, step=step_no + 1, result=local)

                if local.get("completed") and not local.get("verified"):
                    # The side effect happened once.  Never turn uncertainty into a duplicate.
                    if conversation_id:
                        self._clear_pending(conversation_id)
                    return AutonomousResult(False, "Действие выполнила один раз, но подтверждение результата недостаточно; повтор не делаю.", history)

                if not local.get("ok"):
                    error = self._norm(local.get("error"))
                    if self._AUTH.search(error):
                        prompt = "Я дошла до авторизации/подтверждения. Заверши этот шаг вручную"
                        try:
                            cognition = getattr(self.services, "cognition", None)
                            if cognition is not None:
                                cognition.save_auth_checkpoint(goal=final_goal, surface=observation.title, conversation_id=conversation_id)
                        except Exception:
                            pass
                        if conversation_id:
                            self._save_pending(conversation_id, {"goal":final_goal,"history":history,"committed":sorted(committed),"step":step_no + 1,"state":"waiting_user","saved_at":time.time()})
                        return AutonomousResult(False, prompt + " и скажи «готово».", history, needs_user=True, prompt=prompt)
                    if local.get("precommit_blocked"):
                        if conversation_id:
                            self._clear_pending(conversation_id)
                        return AutonomousResult(False, str(local.get("error") or "Предкоммит-проверка не пройдена."), history)

                made_progress = bool(local.get("ok") and (local.get("changed") or local.get("verified") or local.get("completed")))
                if made_progress:
                    recovery.record_success()
                else:
                    self._record_experience(
                        final_goal, observation, recovery, ok=False, verified=False,
                        error=str(local.get("error") or local.get("reason") or "интерфейс не изменился"), history=history,
                    )
                    directive = recovery.record_failure(
                        signature=AdaptiveRecovery.signature(observation.fingerprint, local.get("action"), local.get("target")),
                        reason=str(local.get("error") or local.get("reason") or "интерфейс не изменился"),
                        completed=bool(local.get("completed")), verified=bool(local.get("verified")),
                    )
                    if directive.action == "switch_strategy":
                        unchanged_streak = 0
                        self._reset_anchor()
                        self._trace("ADAPTIVE_STRATEGY_SWITCH", goal=final_goal, generation=directive.strategy_generation)
                        self._runtime_step("Четыре подхода не сработали — меняю стратегию", stage="adaptive_strategy_switch")
                    elif directive.action == "stop":
                        if conversation_id:
                            self._clear_pending(conversation_id)
                        return AutonomousResult(False, "Перепробовала несколько разных стратегий; нужен новый ориентир или ручной шаг.", history)

                if local.get("changed"):
                    unchanged_streak = 0
                else:
                    unchanged_streak += 1

                if conversation_id:
                    self._save_pending(conversation_id, {"goal":final_goal,"history":history,"committed":sorted(committed),"step":step_no + 1,"state":"running","recovery":recovery.to_dict(),"saved_at":time.time()})

            if conversation_id:
                self._clear_pending(conversation_id)
            return AutonomousResult(False, f"Достигнут лимит автономных шагов ({max_steps}); неподтверждённые действия не повторяла.", history)
