from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .action_model import action_num_gpu
from .intent_engine import CommandIntent, detect_commands
from .tasks import TaskNeedsUser
from .trace import log_event


@dataclass(slots=True)
class WorkflowResult:
    ok: bool
    summary: str
    steps: list[dict[str, Any]]
    needs_user: bool = False
    prompt: str = ""


class UniversalWorkflowEngine:
    """Model-guided, stateful desktop agent for arbitrary Windows workflows.

    Templates/direct adapters are optional accelerators only. The core path is:
      owner goal -> compact plan -> observe real desktop/system -> one action -> verify
      -> recover/re-plan -> continue.

    The agent uses Windows UI Automation before pixels. Vision is a last, bounded fallback,
    so one difficult screen cannot freeze ASR/chat for tens of seconds. If login/CAPTCHA/UAC
    requires the owner, a checkpoint is stored and the same workflow resumes after "готово".
    """

    _STOP = {
        "и","а","но","потом","затем","после","этого","там","тут","здесь","мне","мой","моя","мое","моё",
        "в","на","с","к","по","из","для","до","от","у","же","бы","пожалуйста","просто","сейчас","все","всё",
        "приложение","сайт","окно","экран","кнопка","кнопку","режим","задача","задачу",
    }
    _ACTION = re.compile(
        r"\b(открой|запусти|включи|выключи|закрой|нажми|кликни|перейди|найди|посмотри|"
        r"проверь|проанализируй|исправь|почини|сделай|выполни|напиши|отправь|ответь|"
        r"скачай|установи|закоммить|закоммитить|запушь|commit|push|прикрепи|загрузи|"
        r"запомни|сохрани|удали|переименуй|перемести|скопируй|создай|пройди|заполни|"
        r"поставь|полистай|пролистай|листай|прокрути|возобнови|продолжи)\w*",
        re.I,
    )
    _CODE_CONTEXT = re.compile(r"\b(баг|ошибк|traceback|код|тест|коммит|git|репозитор|проект)\w*", re.I)
    _RESUME = re.compile(r"^\s*(готово|сделал|сделала|вошел|вошёл|вошла|авторизовался|авторизовалась|продолжай|дальше)\s*[.!]?\s*$", re.I)

    def __init__(self, services: Any):
        self.services = services
        self.tools = services.tools
        self.gateway = services.gateway
        self.operator = services.desktop_operator
        self._lock = threading.RLock()

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

    def _style_prompt(self) -> str:
        try:
            style = getattr(self.services, "style", None)
            return style.get().prompt() if style is not None else ""
        except Exception:
            return ""

    @staticmethod
    def _norm(text: str) -> str:
        text = str(text or "").casefold().replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def intents(self, query: str) -> list[CommandIntent]:
        # Kept for compatibility/fast-path diagnostics; not the main planner anymore.
        return [x for x in detect_commands(query) if x.confidence >= .70]

    def is_compound(self, query: str) -> bool:
        if len(self.intents(query)) >= 2:
            return True
        actions = len(self._ACTION.findall(query))
        separators = bool(re.search(r"[,;]|\b(?:и|затем|потом|после этого|после чего)\b", query, re.I))
        return actions >= 2 and separators

    def _is_shell_window(self, row: dict[str, Any]) -> bool:
        title = self._norm(row.get("title"))
        cls = self._norm(row.get("class_name"))
        rect = row.get("rectangle") or []
        ignored = {"eirven", "панель задач", "program manager"}
        try:
            configured = self._norm(self.services.identity.get().assistant_name)
            if configured:
                ignored.add(configured)
        except Exception:
            pass
        if title in ignored:
            return True
        # The companion sphere is a tiny Tk top-level whose title follows the configured
        # assistant name. It must never become the desktop agent's source window.
        if cls == "tktoplevel" and len(rect) == 4:
            try:
                if abs(int(rect[2]) - int(rect[0])) <= 260 and abs(int(rect[3]) - int(rect[1])) <= 260:
                    return True
            except Exception:
                pass
        return False

    def _active_window(self) -> dict[str, Any] | None:
        """Return the foreground user window without paying for a full UIA enumeration.

        r15.3 diagnostics caught a single ``window_list`` call blocking for ~41 seconds.
        The native ``foreground_window`` tool is a constant-time Win32 lookup and already
        returns title/handle/class/rectangle, which is sufficient for the normal current
        window path. Only fall back to UIA enumeration when the EIRVEN shell itself owns
        focus or the native lookup is unavailable.
        """
        if not self.operator:
            return None
        try:
            result = self.tools.execute("foreground_window", {})
            row = dict(result.get("result") or {}) if result.get("ok") else {}
            if str(row.get("title") or "").strip() and not self._is_shell_window(row):
                return row
        except Exception:
            pass

        windows = self.operator._windows()
        if not windows:
            return None
        foreground = ""
        try:
            foreground = str(self.tools._foreground_window_title() or "")
        except Exception:
            pass
        fg_norm = self._norm(foreground)
        if fg_norm:
            for row in windows:
                title = self._norm(row.get("title"))
                if title == fg_norm and not self._is_shell_window(row):
                    return row
        # If the sphere currently owns focus, use the first real user window instead.
        return next((row for row in windows if not self._is_shell_window(row)), None)

    def should_handle(self, query: str, conversation_id: str = "") -> bool:
        if conversation_id and self.has_pending(conversation_id):
            return bool(self._RESUME.match(query)) or bool(self._ACTION.search(query))
        if self._ACTION.search(query):
            return True
        # Do not enumerate Windows on every casual chat turn. Only developer-context
        # questions need the foreground window to decide whether they are actionable.
        if not self._CODE_CONTEXT.search(query):
            return False
        active = self._active_window() or {}
        title = self._norm(active.get("title"))
        return any(x in title for x in ("visual studio code", "vscode", "pycharm", "studio", "code"))

    def _interactive_elements(self, title: str, limit: int = 180, *, handle: int | None = None) -> list[dict[str, Any]]:
        if not self.operator or not title:
            return []
        # Do not ask UIA for 500 nodes when the caller needs a compact tree. On the
        # owner's Samsung Browser this enumeration alone cost multiple seconds. When the
        # foreground Win32 handle is known, pass it through so pywinauto does not have to
        # enumerate every top-level window just to resolve the title again.
        requested = max(40, min(320, int(limit or 180)))
        if handle:
            rows = self.operator._elements(title, limit=requested, handle=handle)
        else:
            rows = self.operator._elements(title, limit=requested)
        useful = []
        for el in rows:
            if not el.get("visible", True) or not el.get("enabled", True):
                continue
            typ = self._norm(el.get("control_type"))
            name = str(el.get("name") or "").strip()
            aid = str(el.get("automation_id") or "").strip()
            if typ not in {"button","hyperlink","listitem","edit","group","document","text","checkbox","combobox","menuitem","tabitem","treeitem"}:
                continue
            if not name and not aid:
                continue
            rect = el.get("rectangle") or []
            # Browser chrome is useful for navigation/auth, but deprioritize it rather than hiding all of it.
            if len(rect) == 4 and int(rect[3]) <= 145 and typ not in {"edit", "button", "tabitem"}:
                continue
            useful.append(el)
            if len(useful) >= limit:
                break
        return useful

    def _terms(self, goal: str) -> list[str]:
        tokens = [t for t in self._norm(goal).split() if len(t) >= 3 and t not in self._STOP]
        bad = ("отк", "включ", "выключ", "закр", "наж", "клик", "посмотр", "пров", "найд", "напиш", "отправ", "ответ", "запом", "прикреп", "попрос")
        return [t for t in tokens if not any(t.startswith(x) for x in bad)][-12:]

    def _heuristic_element(self, elements: list[dict[str, Any]], goal: str) -> tuple[int, float] | None:
        terms = self._terms(goal)
        if not terms:
            return None
        best = None
        for i, el in enumerate(elements):
            blob = self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
            if not blob:
                continue
            score = 0.0
            for term in terms:
                if term == blob:
                    score = max(score, 2.0)
                elif term in blob:
                    score = max(score, 1.25)
                else:
                    score = max(score, SequenceMatcher(None, term, blob).ratio())
            typ = self._norm(el.get("control_type"))
            if typ in {"button","hyperlink","listitem","checkbox","menuitem","tabitem"}:
                score += .14
            if best is None or score > best[1]:
                best = (i, score)
        return best if best and best[1] >= .82 else None

    def _compact_tree(self, elements: list[dict[str, Any]], limit: int = 180) -> str:
        rows = []
        for i, el in enumerate(elements[:limit]):
            name = str(el.get("name") or "").replace("\n", " ")[:110]
            aid = str(el.get("automation_id") or "")[:64]
            typ = str(el.get("control_type") or "")
            rows.append(f"{i}: {typ} | {name} | id={aid}")
        return "\n".join(rows)

    def _planner_model(self) -> str:
        try:
            installed = {str(x).casefold(): str(x) for x in self.gateway.installed_models()}
        except Exception:
            installed = {}
        # On 4-GB mobile GPUs native tool prompts on qwen3.5:2b repeatedly missed the
        # 6-second budget in the real r15 log. A smaller 1.7B action model is kept resident
        # on CPU for the desktop loop; larger chat/code models remain separate.
        low_vram = float(getattr(getattr(self.services, "hardware", None), "vram_gb", 0.0) or 0.0) <= 6.0
        candidates = (
            ("qwen3:1.7b", "qwen3.5:2b", str(self.services.settings.fast_model), str(self.services.settings.model))
            if low_vram else
            (str(self.services.settings.fast_model), "qwen3.5:2b", "qwen3:1.7b", str(self.services.settings.model))
        )
        for candidate in candidates:
            if candidate.casefold() in installed:
                return installed[candidate.casefold()]
        return str(self.services.settings.fast_model)

    def _agent_num_gpu(self) -> int | None:
        """Use a measured per-machine lane when available; manual env override still wins."""
        import os
        raw = str(os.environ.get("EIRVEN_ACTION_NUM_GPU", "")).strip()
        if raw:
            try:
                return max(0, int(raw))
            except Exception:
                pass
        try:
            return action_num_gpu(self.services.settings, model=self._planner_model())
        except Exception:
            return None

    def _ui_model(self) -> str:
        try:
            installed = {str(x).casefold(): str(x) for x in self.gateway.installed_models()}
        except Exception:
            installed = {}
        for candidate in (self._planner_model(), "qwen3:1.7b", "qwen3.5:2b", str(self.services.settings.fast_model)):
            if candidate.casefold() in installed:
                return installed[candidate.casefold()]
        return str(self.services.settings.fast_model)

    def _model_decision(self, goal: str, elements: list[dict[str, Any]], *, text_to_type: str = "") -> dict[str, Any]:
        if not elements:
            return {}
        model = self._ui_model()
        schema = {"type":"object","properties":{
            "action":{"type":"string","enum":["click","type","enter","escape","scroll_down","scroll_up","wait","done","fail"]},
            "index":{"type":"integer"},"text":{"type":"string"},"reason":{"type":"string"}},
            "required":["action","index","text","reason"]}
        prompt = (
            "Ты быстрый GUI-планировщик EIRVEN. Ниже только РЕАЛЬНЫЕ элементы активного Windows-окна. "
            "Выбери один следующий шаг к цели. Нельзя придумывать элементы. Сначала оцени, не достигнута ли цель уже. "
            "Если страница/приложение грузится или элементы временно заблокированы — wait. Для click/type укажи index. "
            "Если ТЕКСТ_ДЛЯ_ВВОДА задан — вставляй только его. Не вводи пароли, коды 2FA, платёжные данные. "
            "Если нужен логин/CAPTCHA/UAC и без владельца дальше нельзя — fail с reason, начинающимся USER:.\n"
            f"ЦЕЛЬ: {goal}\nТЕКСТ_ДЛЯ_ВВОДА: {text_to_type[:500] if text_to_type else '<нет>'}\nЭЛЕМЕНТЫ:\n" + self._compact_tree(elements)
        )
        try:
            result = self.gateway.json(
                [{"role":"user","content":prompt}], model=model, temperature=0.0, schema=schema,
                num_ctx=1024, num_predict=64, keep_alive="45s", timeout_seconds=4.8,
                num_gpu=self._agent_num_gpu(),
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception as exc:
            self._trace("UNIVERSAL_UI_MODEL_TIMEOUT", goal=goal, error=str(exc)[:500], model=model)
            return {}

    def _click(self, title: str, el: dict[str, Any], goal: str) -> bool:
        return bool(self.operator and self.operator.click_element(title, el, goal=goal))

    def _type(self, title: str, el: dict[str, Any], text: str, goal: str) -> bool:
        if not text:
            return False
        rect = el.get("rectangle") or []
        if len(rect) == 4:
            x = int((rect[0] + rect[2]) / 2); y = int((rect[1] + rect[3]) / 2)
            self.tools.execute("click", {"x": x, "y": y})
        try:
            import pyperclip
            pyperclip.copy(text)
            self.tools.execute("hotkey", {"keys": ["ctrl", "a"]})
            self.tools.execute("hotkey", {"keys": ["ctrl", "v"]})
            return True
        except Exception:
            return bool(self.tools.execute("type_text", {"text": text, "interval": .005}).get("ok"))

    def _explicit_current_text(self, goal: str) -> str:
        """Extract only text the owner explicitly provided for immediate focused entry."""
        original = str(goal or "").strip()
        quoted = re.search(r"[«\"]([^»\"]{1,1200})[»\"]", original)
        if quoted:
            return quoted.group(1).strip()
        norm = self._norm(original)
        # Scope-first: "в текущем окне напиши R154 тест 1 раз и отправь".
        m = re.search(
            r"\b(?:в|на)\s+(?:текущем|этом)\s+(?:окне|чате|поле|экране)\s+"
            r"(?:напиши|введи|вставь|отправь|набери)\s+(.+?)(?=\s+и\s+(?:отправ|нажм)\w*|$)",
            norm, re.I,
        )
        if not m:
            # Explicit keyboard wording also means the currently focused interaction
            # surface; this covers "напиши на клавиатуре привет и нажми Enter".
            m = re.search(
                r"\b(?:напиши|введи|вставь|набери)\s+на\s+клавиатуре(?:\s+(?:просто\s+)?(?:слово|текст))?\s+(.+?)(?=\s+и\s+(?:отправ|нажм)\w*|$)",
                norm, re.I,
            )
        if not m:
            # Action-first legacy wording with the scope later in the sentence.
            m = re.search(
                r"\b(?:напиши|введи|вставь|отправь|набери)\s+(.+?)\s+"
                r"(?:человеку|сюда|в\s+(?:текущем|этом)\s+(?:окне|чате|поле)|на\s+(?:текущем|этом)\s+экране)\b",
                norm, re.I,
            )
        if m:
            value = m.group(1).strip(" .,!?:;-")
            if 1 <= len(value) <= 1200:
                return value
        return ""


    def _is_atomic_current_text(self, goal: str) -> bool:
        """Treat 'type X in this window and send' as one atomic side effect.

        The conjunction 'and send' used to classify this as a compound workflow, forcing
        a high-level Qwen plan before the deterministic composer primitive.
        """
        goal_n = self._norm(goal)
        scoped = any(x in goal_n for x in (
            "текущем окне", "этом окне", "текущем чате", "этом чате",
            "текущем поле", "этом поле", "сюда", "на клавиатуре",
        ))
        action = bool(re.search(r"\b(напиш|введ|встав|отправ|набер)\w*", goal_n))
        return bool(scoped and action and self._explicit_current_text(goal))

    def _media_action_for_goal(self, goal: str, *, implicit: bool = False) -> str:
        """Map transport wording to an OS/player media primitive.

        Player *settings* such as autoplay are intentionally excluded. They have their
        own semantic toggle and must never become ``media_control(stop)``.
        """
        goal_n = self._norm(goal)
        if "автовоспро" in goal_n or "autoplay" in goal_n:
            return ""
        has_media = any(x in goal_n for x in (
            "видео", "ролик", "трек", "песня", "песню", "музыка", "музыку",
            "воспроизведение", "плеер", "playback", "media",
        ))
        if not has_media and not implicit:
            return ""
        if re.search(r"\b(?:следующ|next)\w*", goal_n):
            return "next"
        if re.search(r"\b(?:предыдущ|prev)\w*", goal_n):
            return "previous"
        if re.search(r"\b(?:останов|stop)\w*", goal_n):
            return "stop"
        if (
            re.search(r"\b(?:пауз|приостанов)\w*", goal_n)
            or re.search(r"\b(?:включ|продолж|возобнов|воспроизвед|запуст|игра(?:й|ть)?|play|resume)\w*", goal_n)
        ):
            return "play_pause"
        return ""

    def _desired_media_state(self, goal: str) -> str:
        goal_n = self._norm(goal)
        if "автовоспро" in goal_n or "autoplay" in goal_n:
            return ""
        if re.search(r"\b(?:пауз|приостанов)\w*", goal_n):
            return "paused"
        if re.search(r"\b(?:включ|продолж|возобнов|воспроизвед|запуст|игра(?:й|ть)?|play|resume)\w*", goal_n):
            return "playing"
        if re.search(r"\b(?:останов|stop)\w*", goal_n):
            return "stopped"
        return ""

    @staticmethod
    def _rect_center(el: dict[str, Any]) -> tuple[int, int] | None:
        rect = el.get("rectangle") or []
        if len(rect) != 4:
            return None
        try:
            return int((int(rect[0])+int(rect[2]))/2), int((int(rect[1])+int(rect[3]))/2)
        except Exception:
            return None

    def _media_snapshot(self) -> dict[str, Any]:
        """Observe player transport state from explicit action controls.

        ``Автовоспроизведение`` is a player setting, not the Play button. r15.5 matched
        that substring as ``воспроизведение`` and clicked it when the owner said resume.
        """
        win = self._active_window()
        if not win:
            return {"is_media": False, "state": "", "window": {}, "elements": []}
        title = str(win.get("title") or "")
        handle = int(win.get("handle") or 0) or None
        elements = self._interactive_elements(title, limit=260, handle=handle)
        play_el = None
        pause_el = None
        autoplay_el = None
        autoplay_state = ""
        for el in elements:
            if self._norm(el.get("control_type")) != "button":
                continue
            rect = el.get("rectangle") or []
            if len(rect) == 4 and int(rect[3]) <= 180:
                continue
            name = self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
            if not name:
                continue
            if "автовоспро" in name or "autoplay" in name:
                autoplay_el = autoplay_el or el
                if re.search(r"(?:выключен|off|disabled)", name):
                    autoplay_state = "off"
                elif re.search(r"(?:включен|on|enabled)", name):
                    autoplay_state = "on"
                continue
            # A visible action labelled Pause means the player is currently playing.
            # Yandex Music also exposes an explicit VibePlayerControls ... playing class;
            # use that structural state instead of guessing from generic text.
            if re.search(r"(?:кнопк.{0,24}пауз|\bpause\b|^пауза(?:\s|$)|\(pause\)|vibeplayercontrols\s+playbutton\s+playing)", name):
                pause_el = pause_el or el
                continue
            # Require an actual Play/Resume action, not any word containing
            # "воспроизведение". A Yandex player button without the "playing" class is
            # a safe structural Play affordance and therefore means the session is paused.
            if re.search(r"(?:кнопк.{0,28}(?:воспроизвести|продолжить)|^воспроизвести(?:\s|$)|^продолжить(?:\s|$)|\bplay\b|\bresume\b|vibeplayercontrols\s+playbutton(?:\s|$))", name):
                play_el = play_el or el
        title_n = self._norm(title)
        title_media = bool(re.search(r"youtube|ютуб|twitch|vk video|вк видео|kinopoisk|кинопоиск|netflix|ivi|okko|vlc|media player|видео|video|яндекс музыка|yandex music", title_n))
        state = "playing" if pause_el else ("paused" if play_el else "")
        return {
            "is_media": bool(state or title_media), "state": state, "window": win,
            "elements": elements, "play_element": play_el, "pause_element": pause_el,
            "autoplay_element": autoplay_el, "autoplay_state": autoplay_state,
        }

    def _poll_media_state(self, desired: str, *, timeout: float = 1.8) -> dict[str, Any]:
        deadline = time.monotonic() + max(.2, timeout)
        snap = self._media_snapshot()
        while time.monotonic() < deadline:
            if desired == "stopped":
                if snap.get("state") in {"", "paused"}:
                    return snap
            elif snap.get("state") == desired:
                return snap
            time.sleep(.18)
            snap = self._media_snapshot()
        return snap

    def _verify_media_goal(self, goal: str) -> bool:
        desired = self._desired_media_state(goal)
        if not desired:
            return False
        snap = self._media_snapshot()
        if desired == "stopped":
            return snap.get("state") in {"", "paused"}
        return str(snap.get("state") or "") == desired

    def ensure_autoplay_goal(self, goal: str, *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        goal_n = self._norm(goal)
        if "автовоспро" not in goal_n and "autoplay" not in goal_n:
            return None
        desired = "off" if re.search(r"\b(?:выключ|отключ|убер|запрет|off|disable)\w*", goal_n) else (
            "on" if re.search(r"\b(?:включ|разреш|on|enable)\w*", goal_n) else ""
        )
        if not desired:
            return None
        before = self._media_snapshot()
        element = before.get("autoplay_element")
        state = str(before.get("autoplay_state") or "")
        if state == desired:
            return {"ok": True, "completed": False, "verified": True, "desired": desired, "method": "already", "before_state": state, "after_state": state}
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "verified": False, "desired": desired}
        win = before.get("window") or {}
        title = str(win.get("title") or "")
        if not element or not title or not self.operator or not self._click(title, element, goal):
            return {"ok": False, "completed": False, "verified": False, "desired": desired, "error": "Переключатель автовоспроизведения не найден"}
        deadline = time.monotonic() + 1.6
        after = self._media_snapshot()
        while time.monotonic() < deadline and after.get("autoplay_state") != desired:
            time.sleep(.18); after = self._media_snapshot()
        verified = after.get("autoplay_state") == desired
        self._trace("AUTOPLAY_CONTROL_ENSURE", goal=goal, desired=desired, before=state, after=after.get("autoplay_state"), verified=verified)
        return {"ok": bool(verified), "completed": True, "verified": bool(verified), "desired": desired, "method": "semantic_autoplay_button", "before_state": state or "unknown", "after_state": after.get("autoplay_state") or "unknown", "error": "" if verified else "Переключатель нажат один раз, но состояние не подтвердилось"}

    def ensure_media_goal(self, goal: str, *, allow_implicit: bool = False, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        """Set transport state without ever issuing two toggles for one request.

        r15.5 did ``VK_MEDIA_PLAY_PAUSE`` and, if UIA was still stale 320 ms later,
        clicked another toggle. The first action paused YouTube and the second resumed it.
        Prefer a precise semantic action button; otherwise send one OS media key and poll.
        """
        action = self._media_action_for_goal(goal, implicit=allow_implicit)
        if not action:
            return None
        before = self._media_snapshot()
        if not before.get("is_media"):
            return None
        desired = self._desired_media_state(goal)
        if desired in {"playing", "paused"} and before.get("state") == desired:
            self._trace("MEDIA_CONTROL_ENSURE", goal=goal, desired=desired, before=desired, after=desired, method="already")
            return {"ok": True, "completed": False, "verified": True, "action": action, "desired": desired, "method": "already", "state": desired}
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "verified": False, "action": action}

        sent: dict[str, Any] = {}
        method = ""
        # For pause/resume, semantic accessibility already tells us which state-changing
        # action exists. Click that exact action once. Never follow a successful click
        # with another toggle merely because verification is delayed.
        if desired in {"playing", "paused"}:
            candidate = before.get("play_element") if desired == "playing" else before.get("pause_element")
            title = str((before.get("window") or {}).get("title") or "")
            if candidate and title and self.operator:
                clicked = self._click(title, candidate, goal)
                if clicked:
                    method = "semantic_media_button"
                    after = self._poll_media_state(desired, timeout=1.8)
                    verified = after.get("state") == desired
                    self._trace("MEDIA_CONTROL_ENSURE", goal=goal, desired=desired, before=before.get("state"), after=after.get("state"), method=method, verified=verified)
                    return {"ok": bool(verified), "completed": True, "verified": bool(verified), "action": action, "desired": desired, "method": method, "before_state": before.get("state") or "unknown", "after_state": after.get("state") or "unknown", "tool_result": {"ok": True, "result": {"semantic_click": True}}, "error": "" if verified else "Кнопка плеера нажата один раз, но состояние не подтвердилось"}

        sent = self.tools.execute("media_control", {"action": action})
        if not sent.get("ok"):
            return {"ok": False, "completed": False, "verified": False, "action": action, "error": str(sent.get("error") or "media_control failed")}
        method = "system_media_key"
        after = self._poll_media_state(desired, timeout=1.8) if desired else self._media_snapshot()
        verified = bool(desired and ((desired == "stopped" and after.get("state") in {"", "paused"}) or after.get("state") == desired))
        self._trace("MEDIA_CONTROL_ENSURE", goal=goal, desired=desired, before=before.get("state"), after=after.get("state"), method=method, verified=verified)
        return {"ok": bool(verified), "completed": True, "verified": bool(verified), "action": action, "desired": desired, "method": method, "before_state": before.get("state") or "unknown", "after_state": after.get("state") or "unknown", "tool_result": sent, "error": "" if verified else "Медиа-команда отправлена один раз, но требуемое состояние не подтверждено"}

    def _extract_site_url(self, text: str) -> str:
        """Extract an explicitly spoken/typed web address without a search-model hop."""
        raw = str(text or "").strip()
        m = re.search(r"https?://[^\s<>]+", raw, re.I)
        if m:
            return m.group(0).rstrip('.,;:!?)]}')
        m = re.search(r"\b(?:www\.)?([a-z0-9][a-z0-9-]{0,62})\s*[.\s]\s*(ru|com|net|org|io|ai|dev|app|me|co|de|uk|рф)\b", raw, re.I)
        if not m:
            return ""
        host = m.group(1).casefold()
        tld = m.group(2).casefold()
        if tld == "рф":
            try:
                tld = "xn--p1ai"
            except Exception:
                return ""
        return f"https://{host}.{tld}"

    def _site_goals(self, query: str) -> list[dict[str, Any]]:
        """Split 'open site X and find/go to Y' into deterministic browser + UI goals."""
        url = self._extract_site_url(query)
        if not url or not re.search(r"\b(?:открой|зайди|перейди)\w*\s+(?:сайт|на сайт|по адресу)?", self._norm(query)):
            return []
        goals: list[dict[str, Any]] = [{
            "goal": f"открой сайт {url}", "mode": "web", "success": "Нужный адрес открыт в браузере по умолчанию", "text": "", "url": url,
        }]
        # Preserve the owner's requested continuation, but remove the address clause.
        parts = re.split(r"\s+и\s+", str(query or ""), maxsplit=1, flags=re.I)
        if len(parts) == 2:
            rest = parts[1].strip(" .,;:-")
            if re.search(r"\b(?:перейди|найди|открой|нажми|выбери)\w*", self._norm(rest)):
                goals.append({"goal": rest, "mode": "web", "success": "Запрошенный раздел или элемент найден и открыт", "text": ""})
        return goals

    def _open_site_goal(self, spec: dict[str, Any], *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        url = str(spec.get("url") or "").strip()
        if not url:
            return None
        started = time.monotonic()
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "verified": False, "error": "Остановлено пользователем"}
        opened = self.tools.execute("open_default_url", {"url": url})
        if not opened.get("ok"):
            return {"ok": False, "completed": False, "verified": False, "error": str(opened.get("error") or "Не удалось открыть адрес")}
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].casefold()
        host_label = host.split(".", 1)[0]
        verified = False
        evidence = ""
        # The browser may need a short moment to navigate. Verify from foreground title,
        # address-bar accessibility, or document labels; never assume tool success == page success.
        for _ in range(6):
            if stop_event and stop_event.is_set():
                return {"ok": False, "cancelled": True, "completed": True, "verified": False, "error": "Остановлено пользователем"}
            time.sleep(.28)
            win = self._active_window() or {}
            title = str(win.get("title") or "")
            if host_label and host_label in self._norm(title):
                verified = True; evidence = "foreground_title"; break
            handle = int(win.get("handle") or 0) or None
            rows = self.operator._elements(title, limit=90, handle=handle) if (title and self.operator) else []
            labels = " ".join(self._norm(f"{x.get('name','')} {x.get('automation_id','')} {x.get('class_name','')}") for x in rows)
            if host in labels or (host_label and host_label in labels):
                verified = True; evidence = "address_or_document"; break
        self._trace("UNIVERSAL_SITE_OPEN", url=url, verified=verified, evidence=evidence)
        return {
            "ok": bool(verified), "completed": True, "verified": bool(verified),
            "answer": "Адрес открыт и подтверждён." if verified else "Адрес отправлен браузеру, но страницу не удалось подтвердить.",
            "route": {"action": "open_default_url", "model": "deterministic", "result": opened},
            "url": url, "evidence": evidence, "elapsed_ms": round((time.monotonic()-started)*1000),
        }

    def _scroll_fastpath(self, goal: str, *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        """Perform an explicitly requested current-window scroll without model planning."""
        goal_n = self._norm(goal)
        if not re.search(r"\b(?:полистай|листай|прокрути|пролистай|скролл)\w*", goal_n):
            return None
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "verified": False, "error": "Остановлено пользователем"}
        win = self._active_window()
        if not win:
            return {"ok": False, "completed": False, "verified": False, "error": "Нет активного пользовательского окна"}
        title = str(win.get("title") or "")
        handle = int(win.get("handle") or 0) or None
        before = self._interactive_elements(title, limit=140, handle=handle)
        before_sig = [self._norm(f"{x.get('control_type')}:{x.get('name')}") for x in before if x.get("visible", True)]
        rect = win.get("rectangle") or [0, 0, 1200, 800]
        docs = [x for x in before if self._norm(x.get("control_type")) == "document" and len(x.get("rectangle") or []) == 4]
        if docs:
            rect = max((x.get("rectangle") for x in docs), key=lambda r: max(0, int(r[2])-int(r[0])) * max(0, int(r[3])-int(r[1])))
        l,t,r,b = [int(v) for v in rect]
        # "list/chats/sidebar" means the left content pane; otherwise scroll the center.
        left_zone = any(x in goal_n for x in ("список", "чат", "диалог", "боков", "сайдбар"))
        x = int(l + (r-l) * (0.18 if left_zone else 0.52))
        y = int(t + (b-t) * 0.52)
        self.tools.execute("mouse_move", {"x": x, "y": y, "duration": .08})
        down = not bool(re.search(r"\b(?:вверх|выше|назад)\b", goal_n))
        amount = -7 if down else 7
        scrolled = self.tools.execute("scroll", {"amount": amount})
        if not scrolled.get("ok"):
            return {"ok": False, "completed": False, "verified": False, "error": str(scrolled.get("error") or "scroll failed")}
        time.sleep(.32)
        after = self._interactive_elements(title, limit=140, handle=handle)
        after_sig = [self._norm(f"{x.get('control_type')}:{x.get('name')}") for x in after if x.get("visible", True)]
        verified = before_sig != after_sig
        self._trace("UNIVERSAL_SCROLL_PRIMITIVE", goal=goal, title=title, amount=amount, verified=verified, x=x, y=y)
        return {
            "ok": True, "completed": True, "verified": bool(verified), "amount": amount,
            "answer": "Прокрутку выполнила." if verified else "Прокрутку выполнила один раз, но UIA не показал изменение списка.",
            "route": {"action": "scroll", "model": "deterministic", "result": scrolled},
        }

    def click_named_current(self, goal: str, *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        """Ground an explicitly named current-page section and verify the transition."""
        goal_n=self._norm(goal)
        if not re.search(r"\b(?:зайди|перейди|открой|нажми|выбери)\w*",goal_n): return None
        m=re.search(r"\b(?:раздел|категори|вкладк|пункт|ссылк)\w*\s+[«\"']?(.+?)[»\"']?(?:[.!]|$)",goal_n)
        if not m:
            # Natural browser phrasing: "открой на этом сайте каталог".  The location
            # phrase is context, not part of the target label.
            m=re.search(r"\b(?:зайди|перейди|открой|нажми|выбери)\w*\s+(?:на\s+(?:этом|текущем)\s+сайте|на\s+(?:этой|текущей)\s+странице)\s+(?:раздел\w*\s+)?[«\"']?(.+?)[»\"']?(?:[.!]|$)",goal_n)
        if not m:
            m=re.search(r"\b(?:зайди|перейди|открой)\w*.{0,50}\bстраниц\w*\s+[«\"']?(.+?)[»\"']?(?:[.!]|$)",goal_n)
        if not m:
            m=re.search(r"\b(?:зайди|перейди|открой)\w*\s+(?:в|на)\s+[«\"']?(каталог|корзин\w*|меню|новинк\w*|категори\w*)[»\"']?(?:[.!]|$)",goal_n)
        if not m: return None
        target=re.sub(r"\b(?:на открывшейся странице|на текущей странице|на странице|сейчас)\b.*$","",m.group(1)).strip()
        if not target or len(target)>80: return None
        if stop_event and stop_event.is_set(): return {"ok":False,"cancelled":True,"completed":False,"verified":False,"target":target}
        win=self._active_window()
        if not win: return {"ok":False,"completed":False,"verified":False,"target":target,"error":"Нет активного окна"}
        title=str(win.get("title") or ""); handle=int(win.get("handle") or 0) or None
        rows=self._interactive_elements(title,limit=320,handle=handle)
        op=getattr(self,"operator",None)
        target_el=op.resolve_element(title,[target],handle=handle,roles=("Hyperlink","Button","ListItem","MenuItem","TabItem","TreeItem"),purpose="activate",content_only=True,rows=rows) if op else None
        if target_el is None and not op:
            ranked=[]
            for el in rows:
                if self._norm(el.get("control_type")) not in {"hyperlink","button","listitem","menuitem","tabitem","treeitem"}: continue
                name=self._norm(el.get("name")); score=3.0 if name==target else (2.0 if target in name else SequenceMatcher(None,target,name).ratio())
                ranked.append((score,el))
            target_el=max(ranked,key=lambda x:x[0])[1] if ranked and max(ranked,key=lambda x:x[0])[0]>=.78 else None
        if target_el is None:
            return {"ok":False,"completed":False,"verified":False,"target":target,"error":f"На текущей странице не нашла интерактивный раздел «{target}»"}
        before=list(rows)
        clicked=self._click(title,target_el,goal)
        if not clicked:
            return {"ok":False,"completed":False,"verified":False,"target":target,"matched":str(target_el.get("name") or ""),"error":"Элемент найден, но клик не выполнился"}
        state=op.wait_for_state(handle=handle,title=title,before_rows=before,timeout=6.0,stable_for=.35,expected=[target]) if op else {"changed":True,"settled":True}
        verified=bool(state.get("changed") and state.get("settled"))
        self._trace("UNIVERSAL_NAMED_CURRENT",goal=goal,target=target,matched=str(target_el.get("name") or ""),clicked=True,verified=verified,state_changed=bool(state.get("changed")))
        return {"ok":verified,"completed":True,"verified":verified,"target":target,"matched":str(target_el.get("name") or ""),"route":{"action":"named_current_click","model":"uia"},"answer":f"Перешла в раздел «{target}»." if verified else f"Нажала «{target}» один раз, но переход пока не подтвердился."}

    def activate_any_current_content(self, goal: str, *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        """Activate one safe visible content card when the owner explicitly says *any*.

        This is intentionally generic: it scores page content affordances, not site names
        or coordinates.  It is useful for requests such as "open YouTube and play any
        video" or "choose any product from this list" where asking a language model to
        invent a target is both slower and less reliable than honoring the visible cards.
        """
        q=self._norm(goal)
        wants_video=bool(re.search(r"\b(?:любое|любой|любую)\s+(?:видео|ролик)\w*",q))
        wants_product=bool(re.search(r"\b(?:любой|любое|любую)\s+(?:товар|позици|карточк)\w*",q))
        if not (wants_video or wants_product): return None
        if stop_event and stop_event.is_set():
            return {"ok":False,"cancelled":True,"completed":False,"verified":False,"error":"Остановлено пользователем"}
        win=self._active_window()
        if not win: return {"ok":False,"completed":False,"verified":False,"error":"Нет активного окна"}
        title=str(win.get("title") or ""); handle=int(win.get("handle") or 0) or None
        rows=self._interactive_elements(title,limit=420,handle=handle)
        banned=("поиск","search","меню","menu","главная","home","назад","back","войти","login","профиль","profile",
                "настрой","settings","подпис","subscribe","корзин","cart","добав","add to cart","сортир","filter","фильтр")
        scored=[]
        for el in rows:
            if not el.get("visible",True) or not el.get("enabled",True): continue
            typ=self._norm(el.get("control_type")); rect=el.get("rectangle") or []
            if typ not in {"hyperlink","button","listitem","group"} or len(rect)!=4: continue
            l,t,r,b=[int(v) for v in rect]; w=max(0,r-l); h=max(0,b-t)
            if w<70 or h<28 or b<=155: continue
            blob=self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
            name=self._norm(el.get("name"))
            if not name or any(x in blob for x in banned): continue
            score=0.0
            if typ in {"hyperlink","listitem"}: score+=2.2
            elif typ=="button": score+=1.2
            if 6<=len(name)<=180: score+=1.3
            if wants_video and any(x in blob for x in ("video","watch","thumbnail","ролик","видео","preview","media")): score+=4.2
            if wants_product and any(x in blob for x in ("product","товар","card","карточ","price","цена","₽","руб")): score+=4.2
            # Prefer central page content over thin sidebars/navigation rails.
            if l>=180: score+=.8
            if t>=260: score+=.7
            scored.append((score,el))
        if not scored: return {"ok":False,"completed":False,"verified":False,"error":"На странице не нашла безопасную видимую карточку контента"}
        score,target=max(scored,key=lambda x:x[0])
        threshold=4.2 if (wants_video or wants_product) else 4.5
        if score<threshold:
            return {"ok":False,"completed":False,"verified":False,"error":"Видимые элементы недостаточно похожи на контент"}
        before=list(rows)
        if stop_event and stop_event.is_set():
            return {"ok":False,"cancelled":True,"completed":False,"verified":False,"error":"Остановлено пользователем"}
        clicked=self._click(title,target,goal)
        if not clicked: return {"ok":False,"completed":False,"verified":False,"error":"Карточка найдена, но клик не выполнился"}
        state=self.operator.wait_for_state(handle=handle,title=title,before_rows=before,timeout=3.2,stable_for=.25) if self.operator else {"changed":True,"settled":True}
        verified=bool(state.get("changed"))
        self._trace("R22_ANY_CONTENT",goal=goal,matched=str(target.get("name") or ""),score=round(score,2),verified=verified)
        return {"ok":bool(clicked),"completed":True,"verified":verified,"matched":str(target.get("name") or ""),"kind":"video" if wants_video else "product","route":{"action":"any_content_click","model":"uia"}}

    def scroll_current_goal(self, goal: str, *, stop_event: threading.Event | None = None) -> dict[str, Any] | None:
        return self._scroll_fastpath(goal, stop_event=stop_event)

    def _current_window_text_fastpath(self, goal: str, *, text: str = "") -> dict[str, Any] | None:
        """Acquire a real composer/input, verify text presence, then submit once."""
        goal_n=self._norm(goal)
        if not any(x in goal_n for x in ("текущем окне","этом окне","текущем чате","этом чате","текущем поле","на клавиатуре")): return None
        if not re.search(r"\b(напиш|введ|встав|отправ|набер)\w*",goal_n): return None
        payload=(text or self._explicit_current_text(goal)).strip()
        if not payload or not self.operator: return None
        if not hasattr(self.operator,"acquire_input"):
            # Compatibility for lightweight/mock operators and older embedded adapters.
            win=self._active_window(); title=str((win or {}).get("title") or ""); handle=int((win or {}).get("handle") or 0) or None
            rows=self._interactive_elements(title,limit=220,handle=handle) if title else []
            candidates=[]
            for el in rows:
                typ=self._norm(el.get("control_type")); rect=el.get("rectangle") or []; blob=self._norm(f"{el.get('name','')} {el.get('automation_id','')} {el.get('class_name','')}")
                if len(rect)!=4 or typ not in {"edit","text","group","document"}: continue
                marker=any(x in blob for x in ("message","сообщение","composer","contenteditable","textbox","input message"))
                if typ=="edit" or marker: candidates.append(((5 if typ=="edit" else 0)+(5 if marker else 0)+int(rect[1])/500.0,el))
            if not candidates: return None
            target=max(candidates,key=lambda x:x[0])[1]
            before={self._norm(e.get("name")) for e in rows if str(e.get("name") or "").strip()}
            if not self._type(title,target,payload,goal): return {"ok":False,"completed":False,"verified":False,"error":"Не удалось ввести текст"}
            self.tools.execute("press_key",{"key":"enter"}); time.sleep(.05)
            after=self._interactive_elements(title,limit=120,handle=handle); payload_n=self._norm(payload)
            appeared=any(payload_n==self._norm(e.get("name")) or payload_n in self._norm(e.get("name")) for e in after if self._norm(e.get("name")) not in before)
            return {"ok":True,"completed":True,"submitted":True,"verified":appeared,"method":"compat-contenteditable","title":title,"text_chars":len(payload)}
        acquired=self.operator.acquire_input(
            purpose="composer",aliases=["input-message-input","write a message","сообщение","message","composer","contenteditable","textbox","edit"],
            trigger_aliases=None,max_scrolls=0,visual_fallback=False,
        )
        if not acquired.get("ok"):
            # For a plain native Edit, purpose=input is less restrictive than composer.
            acquired=self.operator.acquire_input(purpose="input",aliases=["edit","input","text"],trigger_aliases=None,max_scrolls=0,visual_fallback=False)
        if not acquired.get("ok"):
            return {"ok":False,"completed":False,"verified":False,"method":"grounded-input","error":"Не нашла и не сфокусировала поле ввода"}
        typed=self.operator.type_verified(acquired,payload,submit=False,require_verified=True)
        if not typed.get("ok"):
            return {"ok":False,"completed":False,"verified":False,"method":"grounded-input","error":str(typed.get("error") or "Ввод не подтверждён")}
        if hasattr(self.operator,"commit_composer"):
            commit=self.operator.commit_composer(acquired)
            submitted=bool(commit.get("ok"))
            commit_method=str(commit.get("method") or "")
            commit_error=str(commit.get("error") or "")
        else:
            submitted=bool(self.tools.execute("press_key",{"key":"enter"}).get("ok",False))
            commit_method="enter"
            commit_error=""
        if not submitted:
            return {"ok":False,"completed":False,"verified":False,"method":"grounded-input","error":commit_error or "Текст подтверждён в поле, но отправка не выполнилась"}
        self._trace("UNIVERSAL_FOCUSED_TEXT",goal=goal,title=acquired.get("title"),chars=len(payload),verified=True,completed=True,focus_verified=acquired.get("focused"),commit_method=commit_method)
        return {"ok":True,"completed":True,"submitted":True,"verified":True,"method":"grounded-input","commit_method":commit_method,"title":acquired.get("title"),"text_chars":len(payload),"evidence":"field_value_confirmed_before_submit"}

    def accessible_goal(self, goal: str, *, text_to_type: str = "", max_steps: int = 9, stop_event: threading.Event | None = None) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        unchanged = 0
        previous_signature = ""
        last_action_key: tuple[str, int, str] | None = None
        for n in range(max(1, min(int(max_steps), 12))):
            if stop_event and stop_event.is_set():
                return {"ok": False, "cancelled": True, "steps": history}
            win = self._active_window()
            if not win:
                return {"ok": False, "error": "Нет активного пользовательского окна", "steps": history}
            title = str(win.get("title") or "")
            handle = int(win.get("handle") or 0) or None
            elements = self._interactive_elements(title, handle=handle)
            signature = "|".join(self._norm(f"{e.get('control_type')}:{e.get('name')}:{e.get('automation_id')}") for e in elements[:140])
            if previous_signature and signature == previous_signature:
                unchanged += 1
            else:
                unchanged = 0

            decision: dict[str, Any] = {}
            heuristic = self._heuristic_element(elements, goal)
            if heuristic:
                idx, score = heuristic
                if score >= 1.30:
                    decision = {"action":"click","index":idx,"reason":"semantic-ui-match","score":round(score,3)}
            if not decision:
                decision = self._model_decision(goal, elements, text_to_type=text_to_type)
            action = str(decision.get("action") or "fail")
            reason = str(decision.get("reason") or "")[:300]
            try:
                idx = int(decision.get("index", -1))
            except Exception:
                idx = -1
            action_key = (action, idx, title)
            if last_action_key == action_key and previous_signature and signature == previous_signature and action in {"click", "type", "enter"}:
                self._trace("UNIVERSAL_REPEAT_BLOCKED", goal=goal, title=title, action=action, index=idx)
                return {"ok": False, "verified": False, "steps": history, "method": "uia-agent",
                        "error": "Повторное действие не изменило интерфейс; останавливаю цикл вместо повторных кликов"}
            history.append({"step": n + 1, "title": title, "action": action, "index": idx, "reason": reason})
            self._trace("UNIVERSAL_UI_STEP", goal=goal, step=n+1, title=title, action=action, index=idx, reason=reason)

            if action == "done":
                return {"ok": True, "verified": True, "steps": history, "method": "uia-agent"}
            if action == "fail":
                if reason.upper().startswith("USER:"):
                    raise TaskNeedsUser(reason.split(":", 1)[1].strip() or "Нужно действие пользователя в открытом окне. После этого скажи «готово».")
                # One bounded pixel fallback only. Never a 7s GPU + 22s CPU chain.
                if self.operator and n == 0:
                    try:
                        if self.operator.visual_click(goal, self._terms(goal) or [goal], timeout=2.6):
                            time.sleep(.25)
                            previous_signature = signature
                            continue
                    except Exception:
                        pass
                return {"ok": False, "verified": False, "steps": history, "method":"uia-agent", "error":"Не нашла следующий шаг на текущем экране"}
            if action in {"click", "type"} and 0 <= idx < len(elements):
                if stop_event and stop_event.is_set():
                    return {"ok": False, "cancelled": True, "verified": False, "steps": history}
                model_text = str(decision.get("text") or "").strip()[:1200]
                allowed_text = text_to_type or model_text
                before_rows = elements
                ok = self._type(title, elements[idx], allowed_text, goal) if action == "type" else self._click(title, elements[idx], goal)
                if not ok:
                    return {"ok": False, "verified": False, "steps": history, "error":"Не удалось применить выбранное действие"}
                if action == "click" and re.search(r"\b(?:нажми|кликни|щёлкни|щелкни)\b", self._norm(goal)):
                    target_name = str(elements[idx].get("name") or "")
                    state = self.operator.wait_for_state(handle=handle, title=title, before_rows=before_rows, timeout=3.0, stable_for=.25, expected=[target_name]) if self.operator else {"changed": True, "settled": True}
                    changed = bool(state.get("changed"))
                    self._trace("UNIVERSAL_EXPLICIT_CLICK_DONE", goal=goal, title=title, element=target_name, state_changed=changed)
                    return {"ok": True, "completed": True, "verified": changed, "steps": history, "method": "uia-explicit-click", "element": target_name, "state_changed": changed}
            elif action == "enter":
                self.tools.execute("press_key", {"key":"enter"})
            elif action == "escape":
                self.tools.execute("press_key", {"key":"esc"})
            elif action == "scroll_down":
                self.tools.execute("scroll", {"amount": -6})
            elif action == "scroll_up":
                self.tools.execute("scroll", {"amount": 6})
            elif action == "wait":
                time.sleep(.65 if unchanged < 3 else 1.0)
            else:
                return {"ok": False, "verified": False, "steps": history, "error":"Некорректный шаг GUI-agent"}
            time.sleep(.18)
            last_action_key = action_key
            previous_signature = signature
        return {"ok": False, "verified": False, "steps": history, "method":"uia-agent", "error":"Лимит GUI-шагов достигнут"}

    def extract_visible_text(self, question: str, *, max_chars: int = 12000) -> str:
        win = self._active_window()
        if not win:
            return ""
        title = str(win.get("title") or "")
        els = self._interactive_elements(title, limit=280)
        chunks, seen = [], set()
        for el in els:
            name = str(el.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name); chunks.append(name)
            if sum(map(len, chunks)) >= max_chars:
                break
        raw = "\n".join(chunks)
        if not raw:
            return ""
        model = self._ui_model()
        prompt = (
            "Ответь по-русски только по реально видимым элементам текущего окна. Не придумывай. "
            f"Вопрос владельца: {question}\nОкно: {title}\nИнтерфейс:\n{raw[:max_chars]}"
        )
        try:
            response = self.gateway.chat([{"role":"user","content":prompt}], model=model, temperature=.05, think=False,
                                         num_ctx=2048, num_predict=180, keep_alive="45s", timeout_seconds=4.0)
            return str(response.get("content") or "").strip()
        except Exception:
            return raw[:2400]

    def _fast_direct_allowed(self, query: str, *, mode: str = "auto") -> bool:
        """Use legacy/direct code only as a narrow latency accelerator.

        Communication, coding, repair, analysis and arbitrary UI manipulation always go
        through the general agent. The fast lane is limited to compact open/close actions
        and a few OS/camera toggles whose result is directly observable.
        """
        intents = self.intents(query)
        if len(intents) != 1 or intents[0].mixed or intents[0].confidence < .88:
            return False
        intent = intents[0]
        if intent.action in {"open", "close"} and len(self._norm(intent.target).split()) <= 5:
            return True
        if intent.action in {"enable", "disable"}:
            target = self._norm(intent.target)
            return any(token in target for token in (
                "темн", "светл", "wifi", "wi fi", "вай фай", "bluetooth", "блют",
                "режим полета", "режим полета", "камер", "нейромузык", "музык",
            ))
        return False

    @staticmethod
    def _direct_success(acted: bool, answer: str, route: dict[str, Any]) -> bool:
        if not acted:
            return False
        action = str((route or {}).get("action") or "").casefold()
        if any(t in action for t in ("failed", "need_", "unavailable", "not_found", "show_help", "clarify", "fallback")):
            return False
        low = str(answer or "").casefold()
        return not any(x in low for x in ("не удалось", "не смогла", "не могу", "не нашла", "не найден", "нужно уточнить", "уточни "))

    def _state_context(self, max_chars: int = 3600) -> str:
        """Tiny live hint for the fast planner/executor.

        Process enumeration and huge UI dumps are available as tools on demand. They do
        not belong in every prompt: r14 spent more time serialising Windows than acting.
        """
        win = self._active_window() or {}
        title = str(win.get("title") or "")
        elements = self._interactive_elements(title, limit=52) if title else []
        labels = self._compact_tree(elements, limit=52)
        try:
            windows = [
                str(row.get("title") or "")[:120]
                for row in (self.operator._windows() if self.operator else [])
                if not self._is_shell_window(row)
            ][:10]
        except Exception:
            windows = []
        try:
            raw_caps = self.services.capabilities.snapshot() if self.services.capabilities else {}
            browser = (raw_caps.get("browser") or {}).get("default") if isinstance(raw_caps, dict) else ""
        except Exception:
            browser = ""
        head = f"FOREGROUND: {title or '<none>'}\nVISIBLE_WINDOWS: {json.dumps(windows, ensure_ascii=False)}\nDEFAULT_BROWSER: {browser or 'system'}\nVISIBLE_UI:\n"
        return (head + labels)[:max_chars]

    def _simple_compound_goals(self, query: str) -> list[dict[str, Any]]:
        """Zero-LLM split for obviously independent basic OS actions.

        This is a generic command grammar, not an application recipe. It prevents a slow
        high-level model from blocking requests like "open A, then open B, then switch theme".
        Ambiguous/semantic workflows still go to the model planner unchanged.
        """
        intents = self.intents(query)
        if len(intents) < 2:
            return []
        allowed = {"open", "close", "enable", "disable"}
        if any(i.action not in allowed or i.mixed or i.confidence < .88 for i in intents):
            return []
        if any(len(self._norm(i.target).split()) > 7 or not self._norm(i.target) for i in intents):
            return []
        verbs = {"open":"Открой", "close":"Закрой", "enable":"Включи", "disable":"Выключи"}
        return [
            {
                "goal": f"{verbs[i.action]} {i.target}".strip(),
                "mode": "auto",
                "success": "Действие реально выполнено и проверено",
                "text": "",
            }
            for i in intents
        ]

    def _fallback_goals(self, query: str) -> list[dict[str, Any]]:
        # If the high-level planner is unavailable, preserve the owner's complete natural
        # goal. A regex splitter cannot safely understand dependencies, pronouns or
        # application boundaries (the exact r14 failure was turning a whole sentence into
        # an application name). The universal agent can still execute the full goal step
        # by step with its own observation/action loop.
        return [{
            "goal": query.strip(),
            "mode": "auto",
            "success": "Вся цель пользователя реально достигнута и проверена",
            "text": "",
        }]

    def plan(self, query: str) -> list[dict[str, Any]]:
        schema = {
            "type":"object",
            "properties":{"goals":{"type":"array","items":{"type":"object","properties":{
                "goal":{"type":"string"},
                "mode":{"type":"string","enum":["auto","desktop","system","files","code","web","communication"]},
                "success":{"type":"string"},
                "text":{"type":"string"}},
                "required":["goal","mode","success","text"]}}},
            "required":["goals"]
        }
        prompt = (
            "Ты high-level планировщик Windows desktop-agent EIRVEN. Разбей запрос владельца на минимальное число "
            "ПОСЛЕДОВАТЕЛЬНЫХ проверяемых целей (1..10). Не превращай всю фразу в имя приложения. Не программируй интерфейс и "
            "не придумывай селекторы: исполнитель сам исследует текущий экран. Сохраняй зависимости и местоимения по смыслу. "
            "mode=desktop/communication/web для видимого GUI; code/files/system когда нужны файлы/терминал/Git/установка; auto если смешано. "
            "Поле success — конкретный наблюдаемый признак успеха. text заполняй только если владелец явно дал текст для ввода/сообщения. "
            "Если для цели понадобится авторизация/CAPTCHA/UAC, исполнитель сам дойдёт до неё и попросит владельца. "
            "Тебе НЕ нужно анализировать текущий экран: это делает исполнитель после планирования.\n\n"
            f"ЗАПРОС: {query}"
        )
        model = self._planner_model()
        try:
            data = self.gateway.json([{"role":"user","content":prompt}], model=model, temperature=0.0, schema=schema,
                                     num_ctx=1152, num_predict=180, keep_alive="45s", timeout_seconds=5.0,
                                     num_gpu=self._agent_num_gpu())
            goals = list(data.get("goals") or []) if isinstance(data, dict) else []
            cleaned = []
            for item in goals[:10]:
                if not isinstance(item, dict):
                    continue
                goal = str(item.get("goal") or "").strip()
                if not goal:
                    continue
                cleaned.append({
                    "goal": goal[:1800],
                    "mode": str(item.get("mode") or "auto"),
                    "success": str(item.get("success") or "Цель достигнута")[:700],
                    "text": str(item.get("text") or "")[:5000],
                })
            if cleaned:
                self._trace("UNIVERSAL_PLAN", query=query, model=model, goals=cleaned)
                return cleaned
        except Exception as exc:
            self._trace("UNIVERSAL_PLAN_ERROR", query=query, model=model, error=str(exc)[:900])
        return self._fallback_goals(query)

    def _pending_key(self, conversation_id: str) -> str:
        return f"universal_agent_pending:{conversation_id or 'voice'}"

    def has_pending(self, conversation_id: str) -> bool:
        try:
            value = self.services.db.get_setting(self._pending_key(conversation_id), None)
            return isinstance(value, dict) and bool(value.get("goals"))
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

    def _execute_goal(self, spec: dict[str, Any], execute_direct: Any, *, stop_event: threading.Event | None = None) -> dict[str, Any]:
        goal = str(spec.get("goal") or "").strip()
        mode = str(spec.get("mode") or "auto")
        text = str(spec.get("text") or "")
        started = time.monotonic()

        site_open = self._open_site_goal(spec, stop_event=stop_event)
        if site_open is not None:
            return {"goal": goal, "mode": mode, **site_open}

        scroll_result = self._scroll_fastpath(goal, stop_event=stop_event)
        if scroll_result is not None:
            return {"goal": goal, "mode": mode, **scroll_result, "elapsed_ms": round((time.monotonic()-started)*1000)}

        autoplay_result = self.ensure_autoplay_goal(goal, stop_event=stop_event)
        if autoplay_result is not None:
            return {"goal": goal, "mode": mode, **autoplay_result, "answer": ("Автовоспроизведение переключила и подтвердила." if autoplay_result.get("verified") else str(autoplay_result.get("error") or "Не смогла переключить автовоспроизведение.")), "route": {"action":"autoplay_control","model":"uia"}, "elapsed_ms": round((time.monotonic()-started)*1000)}

        named_result = self.click_named_current(goal, stop_event=stop_event)
        if named_result is not None:
            return {"goal": goal, "mode": mode, **named_result, "elapsed_ms": round((time.monotonic()-started)*1000)}

        # Fast adapters are accelerators, never the source of truth. They are safest for
        # compact atomic goals produced by the planner.
        if self._fast_direct_allowed(goal, mode=mode):
            acted, answer, route = execute_direct(goal)
            if self._direct_success(acted, answer, route):
                return {"ok": True, "goal": goal, "mode": mode, "answer": answer, "route": route,
                        "verified": True, "elapsed_ms": round((time.monotonic()-started)*1000)}

        # Media control belongs to the deterministic control plane.  It is state-aware:
        # observe -> toggle only if needed -> verify -> one semantic-button fallback.
        goal_n = self._norm(goal)
        media_result = self.ensure_media_goal(goal, allow_implicit=False, stop_event=stop_event)
        if media_result is not None:
            self._trace("UNIVERSAL_MEDIA_PRIMITIVE", goal=goal, action=media_result.get("action"), verified=media_result.get("verified"))
            verified = bool(media_result.get("verified"))
            desired = str(media_result.get("desired") or "")
            state_word = {"paused":"пауза", "playing":"воспроизведение", "stopped":"остановка"}.get(desired, desired)
            return {
                "ok": verified, "goal": goal, "mode": mode,
                "answer": (
                    f"Медиа-состояние подтверждено: {state_word}." if verified else
                    "Команду плееру отправила один раз, но нужное состояние не удалось подтвердить."
                ),
                "route": {"action": "media_control", "model": "deterministic", "result": media_result},
                "verified": verified,
                "completed": bool(media_result.get("completed")),
                "cancelled": bool(media_result.get("cancelled")),
                "error": str(media_result.get("error") or ""),
                "elapsed_ms": round((time.monotonic()-started)*1000),
            }

        # If the owner explicitly points at a currently focused composer and provides
        # literal text, type/submit it directly through the generic UIA path. This is an
        # interface primitive, not a messenger recipe.
        focused = self._current_window_text_fastpath(goal, text=text)
        if focused and focused.get("ok") and focused.get("completed"):
            verified = bool(focused.get("verified"))
            return {
                "ok": verified,
                "completed": True,
                "goal": goal,
                "mode": mode,
                "answer": (
                    "Текст ввела, отправила и подтвердила в текущем окне."
                    if verified else
                    "Текст ввела и отправила один раз, но интерфейс не дал надёжно подтвердить доставку."
                ),
                "route": {"action": "universal_focused_text", "model": "uia", "result": focused},
                "verified": verified,
                "error": "Отправка выполнена без UIA-подтверждения; повтор не делаю, чтобы не дублировать сообщение" if not verified else "",
                "elapsed_ms": round((time.monotonic()-started)*1000),
            }

        # GUI-first for visible interaction. Accessibility lets the agent behave like a
        # user without paying a vision model on every click.
        if mode in {"desktop", "web", "communication"} or any(x in self._norm(goal) for x in ("на экране", "текущем окне", "наж", "чат", "браузер")):
            generic = self.accessible_goal(goal, text_to_type=text, max_steps=10, stop_event=stop_event)
            if generic.get("ok"):
                return {"ok": True, "goal": goal, "mode": mode, "answer": "Шаг выполнен на текущем экране.",
                        "route": {"action":"universal_uia","model":"uia+fast-text","result":generic},
                        "verified": bool(generic.get("verified")), "elapsed_ms": round((time.monotonic()-started)*1000)}

        # General tool agent handles files, terminal, Git, missing prerequisites, web
        # research and can come back to GUI. This is model-guided, not an app template.
        context = self._state_context(max_chars=3200)
        prompt = (
            f"ЦЕЛЬ ВЛАДЕЛЬЦА: {goal}\nКРИТЕРИЙ УСПЕХА: {spec.get('success') or 'цель реально достигнута'}\n"
            f"ЯВНО РАЗРЕШЁННЫЙ ТЕКСТ ДЛЯ ВВОДА: {text or '<нет>'}\n\n"
            "Работай как настоящий desktop-agent. Сначала наблюдай состояние, затем используй минимальный реальный инструмент. "
            "Не считай действие успешным только потому, что команда/клик отработали: проверь состояние после него. "
            "Если отсутствует необходимая утилита (например Git) и её безопасно поставить через официальный Windows package manager, "
            "установи её как зависимость задачи и продолжай. Если нужен UAC/login/CAPTCHA/2FA/password, открой нужное место и попроси владельца "
            "сделать только этот ручной шаг; после слова «готово» сценарий продолжится. Для ошибки команды/сайта можешь искать точный текст ошибки через web_search. "
            "Не обходи защиту сайтов, CAPTCHA, UAC или аутентификацию. Не покупай и не подтверждай платежи. "
            "Для переписки сначала прочитай достаточный локальный контекст чата, чтобы понять адресата и стиль владельца; "
            "не выдумывай факты от имени владельца и не отправляй сообщение, если адресат неоднозначен. "
            "Если интерфейс загружается или кнопка временно недоступна — используй wait/window_wait и повторно наблюдай, а не считай это ошибкой сразу.\n\n"
            f"СТИЛЬ ВЛАДЕЛЬЦА/ЭЙРВЕН:\n{self._style_prompt()[:3500]}\n\n"
            f"ТЕКУЩЕЕ СОСТОЯНИЕ:\n{context}"
        )
        # Keep the native-tool prompt small. Tool-schema bloat was a major reason the
        # 2B action model missed its latency budget in r15. This remains a universal
        # primitive set; we merely expose only the primitive families relevant to the goal.
        universal_tools = {
            "foreground_window", "window_list", "window_elements", "window_focus",
            "window_click", "window_type", "scroll", "press_key", "hotkey",
            "launch_application", "wait", "window_wait", "media_control", "system_volume",
        }
        goal_n = self._norm(goal)
        if mode in {"web", "communication"} or any(x in goal_n for x in ("брауз", "сайт", "веб", "telegram", "телеграм", "youtube", "ютуб", "чат", "сообщ")):
            universal_tools.update({"open_default_url", "default_search", "web_search"})
        if mode in {"system", "files", "code"} or self._CODE_CONTEXT.search(goal) or any(
            x in goal_n for x in ("файл", "папк", "powershell", "команд", "установ", "скача", "репозитор", "git")
        ):
            universal_tools.update({
                "system_find", "system_open_path", "system_list_files", "system_read_file",
                "system_write_file", "powershell", "command_available", "web_search",
            })
        # Auto goals can still need web/system recovery, but avoid exposing all families
        # unless the wording gives a reason.
        if mode == "auto" and any(x in goal_n for x in ("ошиб", "баг", "почин", "исправ")):
            universal_tools.update({
                "system_find", "system_list_files", "system_read_file", "system_write_file",
                "powershell", "command_available", "web_search",
            })
        require_side_effect = bool(re.search(
            r"\b(исправ|почин|отправ|напиш|включ|выключ|установ|скача|коммит|push|прикреп|загруз|удал|созда|перемест|скопир|заполни|наж)\w*",
            self._norm(goal), re.I,
        ))
        agent_steps = 16 if mode in {"communication", "code"} else 12
        report = self.services.agent.run(
            prompt,
            model=self._planner_model(),
            max_steps=agent_steps,
            external_stop_event=stop_event,
            allowed_tools=universal_tools,
            auto_vision=False,
            require_tool_action=True,
            require_side_effect=require_side_effect,
            require_verification=require_side_effect,
            num_gpu=self._agent_num_gpu(),
        )
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "goal": goal, "mode": mode,
                    "answer": "Остановлено пользователем.", "verified": False,
                    "route": {"action":"universal_agent","model":self._planner_model()},
                    "elapsed_ms": round((time.monotonic()-started)*1000)}
        low = self._norm(report)
        if any(x in low for x in ("нужно войти", "нужна авторизация", "авторизуй", "captcha", "2fa", "uac", "подтверди вход", "введите пароль", "введи пароль")):
            raise TaskNeedsUser("Я дошла до шага авторизации/подтверждения в открытом окне. Заверши его вручную")
        bad = any(x in low for x in ("не удалось", "не смог", "остановилась", "остановлено пользователем", "остановлен пользователем", "лимит шагов", "модель остановилась"))
        return {"ok": not bad, "goal": goal, "mode": mode, "answer": report,
                "route": {"action":"universal_agent","model":self._planner_model()},
                "verified": not bad, "elapsed_ms": round((time.monotonic()-started)*1000)}

    def execute_task(self, query: str, execute_direct: Any, *, conversation_id: str = "", stop_event: threading.Event | None = None) -> WorkflowResult:
        # Camera is a sensor in r14, not a second desktop. A normal desktop task suspends
        # capture first so OpenCV never competes with the agent/ASR for resources.
        try:
            camera = self.services.camera
            if camera is not None and camera.status().get("running") and "камер" not in self._norm(query):
                camera.stop()
                self._trace("CAMERA_AUTO_SUSPEND_FOR_DESKTOP", query=query)
        except Exception:
            pass
        # Resume from an auth/UAC/CAPTCHA checkpoint.
        pending = None
        if conversation_id and self._RESUME.match(query):
            try:
                pending = self.services.db.get_setting(self._pending_key(conversation_id), None)
            except Exception:
                pending = None
        if isinstance(pending, dict) and pending.get("goals"):
            task = str(pending.get("task") or query)
            goals = list(pending.get("goals") or [])
            start_index = int(pending.get("index") or 0)
            results = list(pending.get("results") or [])
            replans = int(pending.get("replans") or 0)
            self._trace("UNIVERSAL_RESUME", task=task, index=start_index, replans=replans)
        else:
            task = query
            # Truly atomic direct command gets a zero-LLM attempt first. If it fails, the
            # generic planner takes over instead of emitting "fast executor failed".
            detected = self.intents(query)
            has_mixed_intent = any(bool(getattr(item, "mixed", False)) for item in detected)
            if not self.is_compound(query) and not has_mixed_intent and self._fast_direct_allowed(query):
                acted, answer, route = execute_direct(query)
                if self._direct_success(acted, answer, route):
                    return WorkflowResult(True, answer, [{"goal":query,"ok":True,"answer":answer,"route":route}])
            # Explicit spoken URLs/domains are deterministic navigation primitives.
            # Split navigation + requested section before consulting a planner.
            site_goals = self._site_goals(query)
            if site_goals:
                goals = site_goals
                self._trace("UNIVERSAL_SITE_PLAN", query=query, goals=goals)
            # A literal current-composer action is atomic even if Russian wording says
            # "введи ... и отправь".  Never pay a planner cold-load before this primitive.
            elif self._is_atomic_current_text(query):
                goals = self._fallback_goals(query)
                self._trace("UNIVERSAL_ATOMIC_CURRENT_TEXT", query=query)
            elif self.is_compound(query):
                goals = self._simple_compound_goals(query) or self.plan(query)
            else:
                goals = self._fallback_goals(query)
            start_index = 0
            replans = 0
            results: list[dict[str, Any]] = []
            if conversation_id and goals:
                self._save_pending(conversation_id, {
                    "task": task, "goals": goals, "index": 0, "results": [], "replans": 0,
                    "state": "running", "saved_at": time.time(),
                })

        for index in range(start_index, len(goals)):
            if stop_event and stop_event.is_set():
                if conversation_id:
                    self._save_pending(conversation_id, {
                        "task": task, "goals": goals, "index": index, "results": results, "replans": replans,
                        "state": "interrupted", "saved_at": time.time(),
                    })
                return WorkflowResult(False, "Приостановила текущий сценарий по новой команде. Могу продолжить с этого этапа.", results)
            spec = goals[index]
            self._runtime_step(
                f"Этап {index + 1}/{len(goals)}: {str(spec.get('goal') or '')[:180]}",
                stage="desktop_agent", index=index + 1, total=len(goals), goal=spec.get("goal"),
            )
            self._trace("UNIVERSAL_GOAL_BEGIN", task=task, index=index+1, goal=spec)
            try:
                result = self._execute_goal(spec, execute_direct, stop_event=stop_event)
            except TaskNeedsUser as exc:
                checkpoint = {"task": task, "goals": goals, "index": index, "results": results, "prompt": exc.prompt, "replans": replans, "saved_at": time.time()}
                if conversation_id:
                    self._save_pending(conversation_id, checkpoint)
                self._runtime_step("Жду владельца: " + exc.prompt[:220], stage="waiting_user", index=index + 1, total=len(goals))
                self._trace("UNIVERSAL_WAIT_USER", task=task, index=index+1, prompt=exc.prompt)
                return WorkflowResult(False, exc.prompt + " После этого скажи «готово».", results, needs_user=True, prompt=exc.prompt)
            if (stop_event and stop_event.is_set()) or result.get("cancelled"):
                result = {**result, "ok": False, "verified": False, "cancelled": True, "answer": "Остановлено пользователем."}
            results.append(result)
            self._trace("UNIVERSAL_GOAL_END", task=task, index=index+1, ok=result.get("ok"), result=result)
            if result.get("cancelled"):
                if conversation_id:
                    self._clear_pending(conversation_id)
                self._runtime_step("Остановлено пользователем", stage="desktop_agent_cancelled", index=index + 1, total=len(goals))
                return WorkflowResult(False, "Остановила текущую задачу.", results)
            if result.get("ok"):
                self._runtime_step(f"Этап {index + 1}/{len(goals)} подтверждён", stage="desktop_agent_verified", index=index + 1, total=len(goals))
                if conversation_id and index + 1 < len(goals):
                    self._save_pending(conversation_id, {
                        "task": task, "goals": goals, "index": index + 1, "results": results, "replans": replans,
                        "state": "running", "saved_at": time.time(),
                    })
            if not result.get("ok"):
                # A side effect that already happened must never be "repaired" by a second
                # planner/agent. This protects message submission and media play/pause from
                # duplicate sends/toggles when the application does not expose enough UIA
                # state for verification.
                if result.get("completed"):
                    self._runtime_step(
                        f"Этап {index + 1}/{len(goals)} выполнен один раз, но не подтверждён",
                        stage="desktop_agent_completed_unverified",
                        index=index + 1,
                        total=len(goals),
                    )
                    self._trace(
                        "UNIVERSAL_REPLAN_SKIPPED_COMPLETED",
                        task=task,
                        index=index + 1,
                        goal=spec.get("goal"),
                    )
                    break
                # A model timeout is infrastructure failure, not evidence that the plan is
                # wrong. Do not immediately pay for another planner timeout on the same turn.
                model_timed_out = "локальная модель не ответила" in self._norm(result.get("answer") or result.get("error") or "")
                # One re-plan with fresh desktop state for genuine UI/tool surprises.
                if replans < 1 and not model_timed_out:
                    repair_query = (
                        f"Исходная задача: {task}\nНе удалось выполнить промежуточную цель: {spec.get('goal')}\n"
                        f"Ошибка/отчёт: {result.get('answer') or result.get('error')}\n"
                        "Продолжи исходную задачу с текущего состояния и предложи только оставшиеся цели."
                    )
                    replacement = self.plan(repair_query)
                    if replacement and replacement != self._fallback_goals(repair_query):
                        goals = goals[:index+1] + replacement
                        replans += 1
                        if conversation_id:
                            self._save_pending(conversation_id, {"task":task,"goals":goals,"index":index+1,"results":results,"replans":replans,"saved_at":time.time()})
                            return self.execute_task("готово", execute_direct, conversation_id=conversation_id, stop_event=stop_event)
                break

        if conversation_id:
            self._clear_pending(conversation_id)
        done = sum(1 for r in results if r.get("ok"))
        completed = sum(1 for r in results if r.get("completed"))
        total = len(goals)
        ok = bool(total and done == total)
        if ok:
            summary = f"Готово. Выполнила и проверила {done} из {total} этапов."
        elif done:
            summary = f"Выполнила {done} этапов из {total}, но следующий результат не удалось надёжно подтвердить."
        elif completed:
            summary = "Действие выполнила один раз, но результат не удалось надёжно подтвердить; повторять его не стала."
        else:
            summary = "Не смогла надёжно выполнить первый этап; ничего не буду выдавать за успех без подтверждения."
        return WorkflowResult(ok, summary, results)

    # Backward-compatible wrapper used by old tests/call sites.
    def execute_compound(self, query: str, execute_direct: Any, *, stop_event: threading.Event | None = None) -> WorkflowResult:
        return self.execute_task(query, execute_direct, stop_event=stop_event)
