from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from . import pace
from .action_model import action_num_gpu
from .action_protocol import SCHEMA_VERSION, ProtocolError, TaskState, deterministic_plan, plan_from_model
from .intent_engine import CommandIntent, detect_commands, is_capability_question
from .identity import CANONICAL_ASSISTANT_NAME
from .tasks import TaskNeedsUser
from .trace import log_event
from .dialogue import is_affirmative_confirmation, is_cancel_confirmation, is_resume_confirmation


@dataclass(slots=True)
class WorkflowResult:
    ok: bool
    summary: str
    steps: list[dict[str, Any]]
    needs_user: bool = False
    prompt: str = ""


class UniversalWorkflowEngine:
    MAX_REPLANS = 6
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
        r"\b(открой|запусти|включи|выключи|закрой|нажми|кликни|перейди|переключи|переключись|найди|поищи|посмотри|"
        r"проверь|проанализируй|исправь|почини|сделай|выполни|напиши|запиши|отправь|ответь|измени|замени|поменяй|допиши|дополни|"
        r"скачай|распакуй|упакуй|установи|закоммить|закоммитить|запушь|commit|push|прикрепи|загрузи|"
        r"запомни|сохрани|удали|очисти|почисти|убери|разложи|сортируй|переименуй|перемести|скопируй|создай|пройди|заполни|"
        r"поставь|полистай|пролистай|листай|прокрути|возобнови|продолжи|прочитай|"
        r"проведи|подготовь|разбери|сверь|сравни|выбери|добавь|убавь|увеличь|уменьши|"
        r"оплати|купи|приобрети|закажи|вызови|построй|забронируй|"
        r"покажи|собери|посчитай|выдели|обнови|вернись|верни|останови|дождись)\w*",
        re.I,
    )
    _OBSERVE_ACTION = re.compile(r"\b(?:расскажи|суммируй|обобщи|перечисли)\w*", re.I)
    _CODE_CONTEXT = re.compile(r"\b(баг|ошибк|traceback|код|тест|коммит|git|репозитор|проект)\w*", re.I)
    _ACTION_REQUEST_HINT = re.compile(
        r"\b(?:можешь|сможешь|надо|нужно|хочу\s+чтобы|сделай\s+так|помоги\s+мне)\b", re.I
    )
    _PASSIVE_ACTION_OBJECT = re.compile(
        r"\b(?:почт|письм|файл|папк|браузер|вкладк|приложен|программ|музык|плеер|"
        r"телеграм|telegram|чат|календар|напоминан|звук|громкост|wifi|wi\s*fi|блют|"
        r"bluetooth|экран|окн|документ|фото|видео|архив|загрузк|писал|написал|сообщен|рабочий\s+стол)\w*", re.I
    )
    _TRAVEL_QUERY = re.compile(
        r"\b(?:ехать|доехать|добраться|добир\w*|маршрут\w*|дорог\w*|"
        r"в\s+пути|пробк\w*|такси|сколько\s+(?:времени\s+)?(?:ехать\s+)?до)\b", re.I,
    )
    _TRAVEL_ORIGIN = re.compile(
        r"\b(?:из|от|с|со)\s+(?:города\s+|района\s+|улицы\s+)?[a-zа-яё0-9][a-zа-яё0-9 ._-]{1,80}",
        re.I,
    )
    _TRAVEL_DESTINATION = re.compile(
        r"\b(?:до|в|на)\s+[a-zа-яё0-9][a-zа-яё0-9 ._-]{1,80}", re.I,
    )
    _TRAVEL_MODE = re.compile(
        r"\b(?:пешком|пеш(?:ая|ую)?\s*ходьб\w*|машин\w*|авто(?:мобил\w*)?|"
        r"такси|метро|автобус\w*|троллейбус\w*|трамва\w*|электричк\w*|"
        r"общественн\w*\s+транспорт\w*|транспорт\w*|велосипед\w*|самокат\w*|"
        r"car|taxi|walk|walking|transit|bus|train|bike)\b", re.I,
    )
    _TRAVEL_VAGUE_ORIGIN = re.compile(
        r"\b(?:от|из|с|со)\s+(?:меня|сюда|здесь|тут|дома|моего\s+местоположения)\b",
        re.I,
    )

    def __init__(self, services: Any):
        self.services = services
        self.tools = services.tools
        self.gateway = services.gateway
        self.operator = services.desktop_operator
        self._lock = getattr(services, "desktop_lock", None) or threading.RLock()
        self._desktop_lock = self._lock
        self._checkpoint_lock = threading.RLock()
        # Медленнейшее из недавних управляющих решений, секунд — по нему подстраивается срок.
        self._admission_slowest = 0.0

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

    @classmethod
    def _travel_missing_fields(
        cls,
        text: str,
        owner_slots: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return only route parameters that are absent from the owner's wording.

        This is a generic slot guard, not a city/service recipe. It runs before desktop
        observation so an under-specified route question cannot spend the whole agent
        budget alternating unrelated foreground/window observers.
        """
        raw = str(text or "")
        slots = {
            str(key): str(value).strip()
            for key, value in dict(owner_slots or {}).items()
            if str(key) in {"origin", "destination", "transport"} and str(value).strip()
        }
        # A resumed checkpoint contains both the previous question and the owner's
        # answer. Ignore the question's examples ("пешком, машина...") when checking
        # whether the answer itself supplied a transport mode.
        question_marker = re.search(r"(?:уточняющий вопрос эрви|owner question)\s*:", raw, re.I)
        if question_marker:
            prefix = raw[:question_marker.start()]
            answer_match = re.search(r"(?:ответ владельца|owner answer)\s*:\s*([\s\S]*)$", raw, re.I)
            raw = prefix + (" " + answer_match.group(1) if answer_match else "")
        clean = cls._norm(raw)
        if not clean or not cls._TRAVEL_QUERY.search(clean):
            return []
        missing: list[str] = []
        if not slots.get("destination") and not cls._TRAVEL_DESTINATION.search(clean):
            missing.append("destination")
        if not slots.get("origin") and (
            not cls._TRAVEL_ORIGIN.search(clean) or cls._TRAVEL_VAGUE_ORIGIN.search(clean)
        ):
            missing.append("origin")
        if not slots.get("transport") and not cls._TRAVEL_MODE.search(clean):
            missing.append("transport")
        return missing

    @classmethod
    def _travel_answer_slots(cls, answer: str, missing_fields: list[str]) -> dict[str, str]:
        """Type only route fields explicitly supplied by the owner in this turn."""
        raw = str(answer or "").strip()
        clean = cls._norm(raw)
        expected = {str(item).strip() for item in missing_fields if str(item).strip()}
        if not clean or not expected:
            return {}
        unavailable = bool(re.fullmatch(
            r"(?:не\s+знаю|неважно|любой|любая|любое|как\s+угодно|пропусти|без\s+разницы)",
            clean,
            re.I,
        ))
        slots: dict[str, str] = {}
        if "transport" in expected:
            mode_match = cls._TRAVEL_MODE.search(raw)
            if mode_match:
                # Keep only the explicit mode span.  A combined answer such as
                # "От меня, на автобусе" must not store the vague origin inside the
                # transport slot and later leak it into the route query.
                slots["transport"] = mode_match.group(0).strip()
        if "origin" in expected:
            vague_origin = bool(cls._TRAVEL_VAGUE_ORIGIN.search(clean))
            explicit_origin = bool(cls._TRAVEL_ORIGIN.search(clean)) and not vague_origin
            # A single-field answer such as "Москва" is grounded by the typed question;
            # vague relative phrases still require a concrete location.
            # A typed clarification may ask for origin and transport together.  A
            # natural answer such as "Москва, Красная площадь" supplies only the
            # origin even though the pending field set contains both slots.  Keep
            # relative answers ("от меня") rejected and let a transport-looking
            # answer fill only transport below.
            origin_only_answer = (
                "origin" in expected
                and not cls._TRAVEL_MODE.search(clean)
                and not cls._TRAVEL_DESTINATION.search(clean)
            )
            if not unavailable and not vague_origin and (explicit_origin or origin_only_answer):
                slots["origin"] = raw
        if "destination" in expected:
            explicit_destination = bool(cls._TRAVEL_DESTINATION.search(clean))
            destination_only_answer = (
                "destination" in expected
                and not cls._TRAVEL_MODE.search(clean)
                and not cls._TRAVEL_ORIGIN.search(clean)
            )
            if not unavailable and (explicit_destination or destination_only_answer):
                slots["destination"] = raw
        return slots

    @staticmethod
    def _travel_clarification_prompt(missing: list[str]) -> str:
        fields = set(missing)
        if fields >= {"origin", "transport"}:
            return "Назови точку отправления и способ передвижения."
        if "origin" in fields:
            return "Назови город или точный адрес отправления; одного «от меня» недостаточно."
        if "transport" in fields:
            return "Каким способом ехать: пешком, на машине, такси или общественным транспортом?"
        if "destination" in fields:
            return "Куда построить маршрут?"
        return "Уточни параметры маршрута."

    @classmethod
    def _travel_search_query(
        cls,
        text: str,
        owner_slots: dict[str, Any] | None = None,
    ) -> str:
        """Build a bounded neutral search query from the owner's filled route slots."""
        raw = str(text or "")
        # Keep follow-up prompts and their examples out of the external query.  A
        # resumed checkpoint contains the original question plus every clarification;
        # sending that transcript to search produced noisy queries such as
        # "пешком на машине..." and made route providers return irrelevant pages.
        original = raw
        marker = re.search(r"(?:уточняющий вопрос эрви|owner question)\s*:", raw, re.I)
        if marker:
            original = raw[:marker.start()]
        answer_match = re.search(r"(?:ответ владельца|owner answer)\s*:\s*([\s\S]*)$", raw, re.I)
        question_marker = re.search(r"(?:уточняющий вопрос эрви|owner question)\s*:", raw, re.I)
        if question_marker:
            prefix = raw[:question_marker.start()]
            raw = prefix + (" " + answer_match.group(1) if answer_match else "")
        typed = {
            str(key): str(value).strip()
            for key, value in dict(owner_slots or {}).items()
            if str(key) in {"origin", "destination", "transport"} and str(value).strip()
        }
        typed_text = " ".join(
            f"{label} {typed[key]}"
            for key, label in (
                ("origin", "отправление"),
                ("destination", "назначение"),
                ("transport", "способ"),
            )
            if key in typed
        )
        # Destination is normally present in the original request ("до Мытищ")
        # rather than in a clarification slot.  Extract only the place phrase and
        # drop the travel verb, punctuation and the rest of the transcript.
        destination = ""
        destination_match = re.search(
            r"(?:\bдо|\bв|\bна)\s+(.+?)(?=\s+(?:ехать|доехать|добраться|добир\w*)\b|[?!.,;:]|$)",
            original,
            re.I | re.S,
        )
        if destination_match:
            destination = re.sub(r"\s+", " ", destination_match.group(1)).strip()
        pieces = ["маршрут"]
        if typed.get("origin"):
            pieces.append(f"отправление {typed['origin']}")
        if destination:
            pieces.append(f"назначение {destination}")
        elif typed.get("destination"):
            pieces.append(f"назначение {typed['destination']}")
        if typed.get("transport"):
            pieces.append(f"способ {typed['transport']}")
        if len(pieces) > 1:
            return " ".join(pieces)[:240].strip()
        clean = cls._norm((raw + " " + typed_text).strip())
        return ("маршрут " + clean)[:240].strip()

    @classmethod
    def _route_duration_row(
        cls,
        rows: list[dict[str, Any]],
        destination: str = "",
    ) -> dict[str, Any] | None:
        """Pick a fresh web result that actually contains a route duration.

        This is deliberately a postcondition parser, not an intent router: the caller
        has already received the typed ``route_search`` action from the semantic model.
        Keeping the check here prevents a generic search result or a map-app launch from
        being presented as a verified travel answer.
        """
        destination_tokens = [
            token for token in cls._norm(destination).split()
            if len(token) >= 4
        ]
        candidates: list[tuple[dict[str, Any], float, int]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            snippet = str(row.get("snippet") or "")
            title_snippet = f"{row.get('title') or ''} {snippet}"
            duration_match = re.search(
                r"\b\d+(?:[.,]\d+)?\s*(?:мин\w*|ч\w*|час\w*|м)\b",
                snippet,
                re.I,
            )
            if not duration_match:
                continue
            if not re.search(r"(?:маршрут|сколько|расстояни|дорог|ехать|врем)", title_snippet, re.I):
                continue
            if destination_tokens and not any(
                token in cls._norm(title_snippet) for token in destination_tokens
            ):
                continue
            raw_value = duration_match.group(0).replace(",", ".").casefold()
            number_match = re.search(r"\d+(?:\.\d+)?", raw_value)
            if not number_match:
                continue
            try:
                value = float(number_match.group(0))
            except ValueError:
                continue
            minutes = value * 60.0 if any(unit in raw_value for unit in ("ч", "час")) else value
            candidates.append((row, minutes, len(candidates)))
        if not candidates:
            return None
        # Search engines can mix the requested nearby route with a same-name
        # long-distance route (for example a town with the same name in another
        # region). Prefer the duration cluster repeated by independent results,
        # rather than the first result's ranking.
        def support(candidate: tuple[dict[str, Any], float, int]) -> tuple[int, float, int]:
            _, minutes, index = candidate
            matches = sum(
                1
                for _, other, _ in candidates
                if abs(other - minutes) <= max(12.0, min(45.0, minutes * 0.35))
            )
            return (matches, -minutes, -index)
        return max(candidates, key=support)[0]

    @staticmethod
    def _route_map_url(rows: list[dict[str, Any]]) -> str:
        """Return a clearly identifiable map URL, never an arbitrary article."""
        for row in rows:
            url = str(row.get("url") or "").strip()
            title = str(row.get("title") or "")
            if re.search(r"(?:yandex\.ru/maps|google\.[^/]+/maps|2gis\.)", url, re.I):
                return url
            if re.search(r"(?:яндекс\s+карты|google\s+maps|2гис|карты)", title, re.I):
                if url.startswith(("http://", "https://")):
                    return url
        return ""

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
        ignored.add(self._norm(CANONICAL_ASSISTANT_NAME))
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

    @classmethod
    def _pure_content_generation(cls, query: str) -> bool:
        """Keep requests for *text/code as an answer* out of desktop automation.

        The old action regex treated every Russian ``напиши`` as an OS side effect.
        As a result, a harmless request such as "напиши код змейки на Python" was sent
        to the desktop planner and failed before the language model could answer.  A
        request becomes an action only when the owner names a destination or an actual
        UI/file operation.
        """
        raw = str(query or "")
        clean = cls._norm(raw)
        content = bool(re.search(
            r"\b(?:напиши|сгенерируй|придумай|покажи|дай|создай)\w*.{0,60}"
            r"\b(?:код|скрипт|функц|класс|регулярк|sql|python|питон|javascript|typescript|html|css)\w*",
            clean, re.I | re.S,
        ))
        if not content:
            return False
        destination = bool(re.search(
            r"\b(?:в|во)\s+(?:файл|vscode|vs\s*code|pycharm|редактор|проект)\b|"
            r"\b(?:сохрани|запиши|положи|оставь|создай\s+файл|измени\s+файл|запусти|выполни)\w*|"
            r"(?:[A-Za-zА-Яа-яЁё0-9_.-]+\.(?:py|js|ts|html|css|json|md|txt))\b|"
            r"\b(?:рабоч(?:ий|ем|его)\s+стол|desktop)\b",
            clean, re.I,
        ))
        # Messaging is always a side effect even if the payload happens to be code.
        messaging = bool(re.search(r"\b(?:telegram|телеграм|тг|whatsapp|ватсап|discord|дискорд)\b", clean, re.I))
        return not destination and not messaging

    @classmethod
    def _pure_answer_request(cls, query: str) -> bool:
        """Keep calculations and explanations in chat instead of driving the desktop.

        Imperatives such as ``реши`` and ``покажи формулы`` describe the desired answer,
        not a side effect. The broad action matcher used to treat ``покажи`` as an OS
        command, invoke the desktop planner and leave an ordinary problem running for
        minutes. A named app, file, or device still wins and remains actionable.
        """
        raw = str(query or "")
        clean = cls._norm(raw)
        if not clean or cls._PASSIVE_ACTION_OBJECT.search(clean):
            return False
        if re.search(
            r"\b(?:в|во|на)\s+(?:браузер|сайт|страниц|приложен|программ|окн|экран|"
            r"файл|папк|редактор|vscode|pycharm|telegram|телеграм|чат)\w*",
            clean,
            re.I,
        ):
            return False
        return bool(re.search(
            r"\b(?:реши|вычисли|посчитай|объясни|ответь|расскажи|докажи|выведи|"
            r"переведи|сформулируй|перескажи|сравни|покажи)\w*\b|"
            r"\b(?:формул|latex|уравнен|пример|задач|сколько|чему\s+равен)\w*",
            clean,
            re.I,
        ))

    def should_handle(self, query: str, conversation_id: str = "") -> bool:
        """Admit natural side-effect requests without an application/template whitelist.

        The action engine is intentionally broad: concrete imperatives, polite requests and
        task-oriented phrases are accepted. Pure text/code generation stays in chat. A later
        planner decides *how* to act from live capabilities and state.
        """
        if conversation_id and self.has_pending(conversation_id):
            try:
                pending = self.services.db.get_setting(self._pending_key(conversation_id), None)
            except Exception:
                pending = None
            if isinstance(pending, dict) and pending.get("clarification"):
                # The owner's next utterance is the answer to a question such as
                # destination, transport or arbitrary service name. It need not contain
                # an imperative verb and must not fall through to ordinary chat.
                return True
            if isinstance(pending, dict) and (
                pending.get("risk_confirmation_fingerprint") or pending.get("confirmation_step_id")
            ):
                return bool(
                    is_affirmative_confirmation(query)
                    or is_resume_confirmation(query)
                    or is_cancel_confirmation(query)
                    or self._ACTION.search(query)
                )
            return is_resume_confirmation(query) or bool(self._ACTION.search(query))
        clean = self._norm(query)
        if not clean:
            return False
        # Ask the local model first.  The lexical branches below are compatibility
        # fallbacks for an unavailable backend, never the primary understanding path.
        semantic = self._semantic_admission(query)
        if semantic is not None:
            return semantic
        if is_capability_question(query):
            return False
        # Known unambiguous imperative forms are a latency shortcut only. They are not
        # the capability boundary: new services and unseen phrasings go through the
        # semantic admission model below.
        if self._ACTION.search(query) and not self._pure_answer_request(query) and not self._pure_content_generation(query):
            return True
        # Travel questions need live route/map state even when phrased as an ordinary
        # question ("сколько до Мытищ ехать").  This is an admission accelerator only;
        # route planning itself remains fully reactive and service-agnostic.
        if re.search(
            r"\b(?:ехать|доехать|добраться|добир|маршрут|дорог|в\s+пути|пробк|такси)\w*\b",
            clean,
            re.I,
        ):
            return True
        schema = {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": ["computer_action", "conversation"]},
                "reason": {"type": "string"},
            },
            "required": ["route", "reason"],
        }
        prompt = (
            "Классифицируй реплику владельца для универсального ассистента. "
            "computer_action — если для ответа нужно наблюдать или менять реальный компьютер, "
            "подключённый аккаунт/почту, текущий сайт/приложение, актуальный маршрут/сервис, "
            "либо выполнить внешний шаг. conversation — если достаточно обычного разговора, "
            "объяснения, вычисления или генерации текста без доступа к реальному состоянию. "
            "Не используй whitelist приложений и не требуй знакомой формулировки. "
            f"Реплика: {query}"
        )
        try:
            # Admission is a tiny binary gate; keep it on the resident fast
            # checkpoint.  The main checkpoint is reserved for the actual plan/tool
            # loop, where its extra context is useful and its output need not be a
            # one-line JSON classification.
            admission_model = str(getattr(self.services.settings, "fast_model", "") or self._planner_model())
            decision = self.gateway.json(
                [{"role": "user", "content": prompt}],
                model=admission_model, temperature=0.0, schema=schema,
                num_ctx=2048, num_predict=48,
                keep_alive=self.services.settings.keep_alive,
                timeout_seconds=pace.scale(min(6.0, max(3.0, float(self.services.settings.llm_first_token_timeout))), cap=90.0),
            )
            return str(decision.get("route") or "") == "computer_action"
        except Exception:
            # Fail open only when local deterministic evidence says the request targets
            # external state. A classifier outage must not turn every chat into clicks.
            return bool(
                (self._OBSERVE_ACTION.search(query) or self._ACTION_REQUEST_HINT.search(clean))
                and self._PASSIVE_ACTION_OBJECT.search(clean)
            )

    def planning_strategy(self, query: str) -> str:
        """Expose the routing strategy for release tests and diagnostics.

        `model_plan` is the default for new/unknown tasks; direct paths are only verified
        latency accelerators and are never required for capability coverage.
        """
        if self._site_goals(query):
            return "site_primitive"
        if self._is_atomic_current_text(query):
            return "focused_text_primitive"
        if not self.is_compound(query) and self._fast_direct_allowed(query):
            return "verified_direct_then_model"
        return "model_plan"

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
        hardware = getattr(self.services, "hardware", None)
        # The semantic envelope is the single source of truth for an action turn.
        # Prefer the configured release/main model here: a tiny hardware heuristic
        # used to silently replace it with qwen3.5:4b, which was fast but routinely
        # confused Yandex Music with an ordinary conversation.  The configured fast
        # model remains the immediate fallback when the main checkpoint is absent.
        candidates = (
            str(self.services.settings.model),
            str(self.services.settings.fast_model),
            str(self.services.settings.code_model),
            str(getattr(hardware, "recommended_fast_model", "")),
        )
        for candidate in candidates:
            if candidate.casefold() in installed:
                return installed[candidate.casefold()]
        return str(self.services.settings.fast_model)

    def _snapshot_ollama_state(self, reason: str) -> None:
        """Записать, что происходит внутри Ollama, когда модель не ответила.

        Журнал Эрви не видит, что делает сама Ollama: занята ли модель, выгружена ли,
        сколько её на видеокарте. Без этого причину медленных ответов приходится
        угадывать. Здесь — локальный запрос /api/ps: какие модели загружены, какая их
        доля в видеопамяти (меньше 100% — часть модели считается на процессоре, в разы
        медленнее), размер контекста и когда модель будет выгружена. В фоне, ход не ждёт.
        """
        def work() -> None:
            try:
                import json as _json
                import urllib.request
                base = str(getattr(self.services.settings, "ollama_url", "") or "http://127.0.0.1:11434").rstrip("/")
                with urllib.request.urlopen(base + "/api/ps", timeout=3) as response:
                    data = _json.loads(response.read().decode("utf-8", "replace"))
                models = []
                for item in data.get("models") or []:
                    size = float(item.get("size") or 0)
                    vram = float(item.get("size_vram") or 0)
                    models.append({
                        "name": item.get("name"),
                        "size_gb": round(size / 1e9, 2),
                        "vram_gb": round(vram / 1e9, 2),
                        "on_gpu_percent": round(100 * vram / size) if size else None,
                        "context": item.get("context_length"),
                        "expires_at": str(item.get("expires_at") or "")[:19],
                    })
                self._trace("OLLAMA_STATE", reason=reason, loaded=len(models), models=models)
            except Exception as exc:
                self._trace("OLLAMA_STATE_FAILED", reason=reason, error=str(exc)[:200])
        import threading as _threading
        _threading.Thread(target=work, daemon=True, name="eirven-ollama-state").start()

    def _admission_timeout(self) -> float:
        """Сколько ждать управляющее решение — по этой конкретной машине.

        Прежний срок был 8–20 секунд, не больше 20, — рассчитанный на модель,
        уже загруженную в видеокарту. На ноутбуке, где модель думает на
        процессоре, разбор длинной инструкции и ответ в 192 токена занимают
        30–60 секунд. Решение не успевало никогда, и Эрви отвечала отказом на
        каждое сообщение, даже на «как дела».

        Теперь срок складывается из двух частей. Базовый — по типу машины: с
        видеокартой прежний, без неё — до двух минут. И уточнённый — по реальным
        замерам: сколько решение заняло в прошлые разы здесь, с тройным запасом.
        Второе надёжнее: оно само подстраивается под любой ноутбук.
        """
        settings = self.services.settings
        base = min(20.0, max(8.0, float(getattr(settings, "llm_first_token_timeout", 8.0) or 8.0)))
        hardware = getattr(self.services, "hardware", None)
        mode = str(getattr(hardware, "runtime_mode", "") or "")
        if mode and mode != "gpu_resident":
            base = max(base, 90.0)
        measured = float(getattr(self, "_admission_slowest", 0.0) or 0.0)
        if measured > 0:
            base = max(base, min(150.0, measured * 3.0))
        return base

    def _note_admission_seconds(self, seconds: float) -> None:
        """Запомнить, сколько заняло решение — медленнейшее из недавних."""
        try:
            value = float(seconds)
        except Exception:
            return
        previous = float(getattr(self, "_admission_slowest", 0.0) or 0.0)
        # Плавно: одно быстрое решение не должно сразу обнулять запас после медленных.
        self._admission_slowest = max(value, previous * 0.85)
        pace.note_decision(value)
        self._trace("SEMANTIC_DECISION_TIMING", seconds=round(value, 2),
                    next_timeout=round(self._admission_timeout(), 1))

    def _admission_model(self) -> str:
        """Return the resident low-latency model for the single route decision.

        Admission is a tiny typed JSON envelope, not the long-horizon executor.  Keeping
        it on the configured fast checkpoint avoids loading the 9B planner for every
        greeting, follow-up, or mail request while preserving the larger planner for
        actions that actually need UI reasoning.  The prompt and schema remain the
        source of truth; this is only a latency lane selection.
        """
        try:
            installed = {str(x).casefold(): str(x) for x in self.gateway.installed_models()}
        except Exception:
            installed = {}
        fast = str(getattr(self.services.settings, "fast_model", "") or "").strip()
        if fast and (not installed or fast.casefold() in installed):
            return installed.get(fast.casefold(), fast)
        return self._planner_model()

    @staticmethod
    def _hint_from_semantic_decision(decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict):
            return {}
        return {
            "turn_kind": str(decision.get("turn_kind") or ""),
            "action": str(decision.get("action") or "unknown"),
            "target": str(decision.get("target") or ""),
            "target_kind": str(decision.get("target_kind") or "none"),
            "service": str(decision.get("service") or ""),
            "media_content": str(decision.get("media_content") or ""),
            "media_action": str(decision.get("media_action") or "unknown"),
            "single_step": "true" if decision.get("single_step") is True else "false",
            "side_effect_required": bool(decision.get("side_effect_required") is True),
            "platform": str(decision.get("platform") or ""),
            "recipient": str(decision.get("recipient") or ""),
            "message": str(decision.get("message") or ""),
            "note_text": str(decision.get("note_text") or ""),
            "event_title": str(decision.get("event_title") or ""),
            "starts_at": str(decision.get("starts_at") or ""),
            "ends_at": str(decision.get("ends_at") or ""),
            "decision_status": str(decision.get("decision_status") or "ok"),
        }

    @staticmethod
    def _grounded_semantic_span(value: str, utterance: str) -> bool:
        """Accept model-extracted external arguments only when the owner said them."""
        value_n = " ".join(str(value or "").casefold().replace("ё", "е").split())
        utterance_n = " ".join(str(utterance or "").casefold().replace("ё", "е").split())
        return bool(value_n and value_n in utterance_n)

    def semantic_decision(
        self,
        query: str,
        pending_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one uncached route and typed capability decision for this turn.

        Older builds first classified ``computer_action`` and then made another model
        call for the tool hint.  The two calls could disagree, which made a configured
        IMAP connector unreachable and let the conversational model ask for a website.
        This envelope is produced once, validated once and can be threaded through the
        entire executor without a query-key cache or a second semantic interpretation.
        """
        raw = " ".join(str(query or "").split())[:1600]
        empty = {
            "route": "unknown", "turn_kind": "", "confidence": 0.0, "action": "unknown",
            "target": "", "target_kind": "none", "service": "",
            "media_content": "", "media_action": "unknown", "single_step": False,
            "side_effect_required": False,
            "platform": "", "recipient": "", "message": "", "needs_history": True,
            "decision_status": "empty",
        }
        if not raw:
            return empty
        schema = {
            "type": "object",
            "properties": {
                "turn_kind": {"type": "string", "enum": [
                    "action", "clarification_response", "capability_question", "conversation",
                ]},
                "route": {"type": "string", "enum": ["computer_action", "conversation"]},
                "action": {"type": "string", "enum": [
                    "launch_application", "open_service", "application_list",
                    "system_brightness", "media_control", "mail_review", "mail_status",
                    "message_send", "route_search", "code_generation",
                    "organizer_note_create", "organizer_notes_list",
                    "organizer_event_create", "organizer_day_plan",
                    "observe", "unknown",
                ]},
                "target": {"type": "string"},
                "target_kind": {"type": "string", "description": (
                    "Semantic type of target. Use service only for an explicitly named provider/product, "
                    "never for an action modifier such as pause, louder, next, or stop."
                ), "enum": [
                    "service", "content", "application", "path", "location", "recipient", "none",
                ]},
                "service": {"type": "string", "description": (
                    "Exact explicitly named provider/product span, or empty string when no provider was named."
                )},
                "media_content": {"type": "string"},
                "media_action": {"type": "string", "enum": [
                    "list", "status", "play", "pause", "stop", "unknown",
                ]},
                "platform": {"type": "string"},
                "recipient": {"type": "string"},
                "message": {"type": "string"},
                "note_text": {"type": "string"},
                "event_title": {"type": "string"},
                "starts_at": {"type": "string"},
                "ends_at": {"type": "string"},
                "single_step": {"type": "boolean"},
                "side_effect_required": {"type": "boolean"},
                "needs_history": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                # Эмоция Эрви в ответ на реплику и реплика моста — в том же решении,
                # без отдельного вызова модели. Раньше мост делал свой вызов с потолком
                # 0,6 с: на реальной модели он не успевал, уходил в «остывание», и
                # человек видел только дежурное «Выполняю».
                "mood": {"type": "string", "enum": ["calm", "joy", "surprise", "laugh"]},
                "ack": {"type": "string"},
                "question": {"type": "string"},
            },
            # Keep the executor a passive receiver of one stable model contract.
            # Optional semantic slots were silently omitted by small checkpoints
            # (notably ``service`` and ``media_action`` for «Включи Яндекс Музыку»),
            # after which the executor could only ask the owner for information that
            # was already present. Empty strings remain valid when a slot is genuinely
            # absent, but every key must be decided explicitly by the model.
            "required": [
                "turn_kind", "route", "action", "target", "target_kind", "service",
                "media_content", "media_action", "platform", "recipient", "message",
                "note_text", "event_title", "starts_at", "ends_at", "single_step",
                "side_effect_required", "needs_history", "confidence",
                "mood", "ack", "question",
            ],
        }
        mail_configured = False
        try:
            mail = getattr(self.services, "mail", None)
            mail_configured = bool(mail is not None and mail.configured())
        except Exception:
            mail_configured = False
        local_now = datetime.now().astimezone()
        pending_prompt = ""
        if isinstance(pending_context, dict) and pending_context:
            compact_pending = {
                "original_task": str(pending_context.get("original_task") or "")[:600],
                "question": str(pending_context.get("question") or "")[:300],
                "missing_fields": [str(item)[:80] for item in list(pending_context.get("missing_fields") or [])[:12]],
            }
            pending_prompt = (
                " PENDING_CONTEXT=" + json.dumps(compact_pending, ensure_ascii=False, separators=(",", ":"))
                + ". Если реплика отвечает на этот вопрос или заполняет missing_fields, "
                "используй turn_kind=clarification_response, needs_history=true и сохрани "
                "значение в подходящем типизированном поле."
            )
        prompt = (
            "Ты маршрутизатор EIRVEN. Верни один компактный JSON без markdown и пояснений. "
            "Сначала определи turn_kind. action — владелец просит реально выполнить действие; "
            "clarification_response — реплика заполняет поле из PENDING_CONTEXT; "
            "capability_question — владелец только спрашивает, умеет/может ли Эрви что-то делать; "
            "conversation — обычный разговор, объяснение или текст прямо в чат. Вопрос о возможности "
            "никогда не является action: «умеешь монтировать видео?» — capability_question, даже если "
            "такая возможность реально доступна. Только turn_kind=action или clarification_response "
            "может иметь route=computer_action; остальные всегда route=conversation. "
            "route=computer_action для любого действия с ПК, приложением, сайтом, аккаунтом, "
            "почтой, музыкой, сообщением или маршрутом; route=conversation только для обычного "
            "разговора и выдачи кода/текста прямо в чат без сохранения. Для «напиши код/текст "
            "сюда» используй action=code_generation. Если просят положить, "
            "сохранить или создать файл — это computer_action. Личные заметки, календарь и "
            "напоминания выполняются через organizer_note_create|organizer_notes_list|"
            "organizer_event_create|organizer_day_plan и тоже относятся к computer_action. "
            "Для заметки note_text содержит только продиктованный владельцем текст. Для события "
            "event_title содержит его название, starts_at и ends_at — ISO-8601 с часовым поясом; "
            "не выдумывай отсутствующее время и оставь поле пустым, если его нужно уточнить. "
            "action: launch_application|open_service|media_control|mail_review|mail_status|message_send|"
            "route_search|code_generation|organizer_note_create|organizer_notes_list|"
            "organizer_event_create|organizer_day_plan|observe|unknown. target — точная цель из этой "
            "реплики. target_kind описывает её тип. Если владелец назвал сервис, target_kind=service, "
            "а service скопируй ДОСЛОВНО из реплики; не исправляй название. media_content — точное "
            "Service — только название внешнего провайдера/продукта. Направление действия и состояние "
            "плеера («на паузу», «громче», «следующий», «останови») сервисом не являются: если провайдер "
            "не назван, обязательно target_kind=none, service=\"\". "
            "название трека/волны/плейлиста, media_action — "
            "play|pause|stop|status|list|unknown. Для Telegram message_send и заполни platform, "
            "recipient, message только если они сказаны. Не выдумывай сервисы и значения. "
            "needs_history=true лишь для явного продолжения («это», «там», «продолжай»), иначе false. "
            "side_effect_required=true только если нужно изменить состояние: открыть, запустить, "
            "ввести, нажать, создать или изменить. Для чтения, поиска, проверки, справки и просмотра "
            "side_effect_required=false. single_step=true для одного действия; confidence от 0 до 1. "
            "Примеры: «включи музыку»=action/computer_action/media_control/play/side_effect_required=true; «проверь почту" 
            "»=computer_action/mail_review; «сколько ехать до Мытищ»=computer_action/route_search; "
            "«привет»=conversation/conversation/unknown; «включи Яндекс Музыку»="
            "action/computer_action/media_control, target_kind=service, service=\"Яндекс Музыку\". "
            "«поставь музыку на паузу»=action/computer_action/media_control/pause, "
            "target=\"\", target_kind=none, service=\"\". "
            f"LOCAL_NOW={local_now.isoformat(timespec='seconds')}; "
            f"CONNECTED_MAIL_CONFIGURED={str(mail_configured).lower()}. "
            "Ещё три поля. mood — реакция Эрви на реплику: laugh, если человек шутит "
            "или смеётся; surprise, если сообщает что-то неожиданное; joy, если "
            "радуется, благодарит или здоровается; иначе calm. "
            "При route=computer_action поля ack и question ОБЯЗАТЕЛЬНО непустые: ack — "
            "подтверждение в два-три слова по смыслу задачи; question — короткий живой "
            "встречный вопрос человеку, связанный с его задачей или с ним самим. "
            "Образец по смыслу (не копируй, говори своими словами): «найди, кто выиграл "
            "выборы» → ack «Уже ищу», question «А ты как думаешь, кто выиграл?»; "
            "«включи музыку» → ack «Включаю», question «Как у тебя настроение?». "
            "При любом другом route ack и question — пустые строки. "
            f"Реплика: {raw}"
            + pending_prompt
        )
        # Admission is a small structured call, but a resident 9B checkpoint can
        # occasionally miss its short deadline while another turn is unloading a
        # model.  Retry the *same typed envelope* once on the configured fast
        # checkpoint.  This is a transport fallback, not a second intent router:
        # no phrase table or lexical guess is introduced and only a valid JSON
        # envelope can authorize an action.
        admission_messages = [{"role": "user", "content": prompt}]
        # The fast checkpoint is prewarmed at startup.  Allow enough wall-clock time
        # for its structured decoder to finish a difficult JSON turn (the old 10s cap
        # cut off valid music/mail decisions while the model was still resident).
        admission_timeout = self._admission_timeout()
        primary_model = self._admission_model()
        _admission_started = time.monotonic()
        try:
            decision = self.gateway.json(
                admission_messages,
                model=primary_model, temperature=0.0, schema=schema,
                # Match the conversational fast lane's context size. Ollama creates a
                # separate runner when this value changes; using 1024 here forced the
                # very next 3072-token chat to reload the same checkpoint.
                num_ctx=min(int(getattr(self.services.settings, "chat_num_ctx", 3072) or 3072), 3072), num_predict=256,
                keep_alive=self.services.settings.keep_alive,
                timeout_seconds=admission_timeout,
                num_gpu=self._agent_num_gpu(),
            )
        except Exception as exc:
            self._trace("SEMANTIC_DECISION_FAILED", error=str(exc)[:240], model=primary_model)
            self._snapshot_ollama_state("decision_failed")
            fast_model = str(getattr(self.services.settings, "fast_model", "") or "").strip()
            if not fast_model or fast_model.casefold() == str(primary_model).casefold():
                return {**empty, "decision_status": "error"}
            try:
                decision = self.gateway.json(
                    admission_messages,
                    model=fast_model, temperature=0.0, schema=schema,
                    num_ctx=min(int(getattr(self.services.settings, "chat_num_ctx", 3072) or 3072), 3072), num_predict=256,
                    keep_alive=self.services.settings.keep_alive,
                    timeout_seconds=admission_timeout,
                    num_gpu=self._agent_num_gpu(),
                )
                self._trace("SEMANTIC_DECISION_FALLBACK", model=fast_model)
            except Exception as fallback_exc:
                self._trace("SEMANTIC_DECISION_FALLBACK_FAILED", error=str(fallback_exc)[:240], model=fast_model)
                return {**empty, "decision_status": "error"}
        self._note_admission_seconds(time.monotonic() - _admission_started)
        if not isinstance(decision, dict):
            return {**empty, "decision_status": "invalid"}
        route = str(decision.get("route") or "unknown")
        action = str(decision.get("action") or "unknown")
        allowed_actions = {
            "launch_application", "open_service", "application_list",
            "system_brightness", "media_control", "mail_review", "mail_status",
            "message_send", "route_search", "code_generation",
            "organizer_note_create", "organizer_notes_list",
            "organizer_event_create", "organizer_day_plan",
            "observe", "unknown",
        }
        if route not in {"computer_action", "conversation"}:
            route = "unknown"
        if action not in allowed_actions:
            action = "unknown"
        declared_turn_kind = str(decision.get("turn_kind") or "").strip()
        allowed_turn_kinds = {"action", "clarification_response", "capability_question", "conversation"}
        concrete_candidate = action not in {"unknown", "observe", "code_generation"}
        if declared_turn_kind in allowed_turn_kinds:
            turn_kind = declared_turn_kind
        elif concrete_candidate or route == "computer_action":
            turn_kind = "action"
        elif route == "conversation":
            turn_kind = "conversation"
        else:
            turn_kind = ""
        try:
            confidence = float(decision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            confidence = 0.0
        media_action = str(decision.get("media_action") or "unknown")
        if media_action not in {"list", "status", "play", "pause", "stop", "unknown"}:
            media_action = "unknown"
        target = str(decision.get("target") or "").strip()[:240]
        target_kind = str(decision.get("target_kind") or "none").strip()
        if target_kind not in {"service", "content", "application", "path", "location", "recipient", "none"}:
            target_kind = "none"
        service = str(decision.get("service") or "").strip()[:240]
        if service and not self._grounded_semantic_span(service, raw):
            service = ""
        if service and target_kind == "none":
            target_kind = "service"
        if target_kind == "service":
            if not service and self._grounded_semantic_span(target, raw):
                service = target
            if service:
                target = service
            else:
                target_kind = "none"
        media_content = str(decision.get("media_content") or "").strip()[:240]
        platform = str(decision.get("platform") or "").strip()[:80]
        recipient = str(decision.get("recipient") or "").strip()[:160]
        message = str(decision.get("message") or "").strip()[:1200]
        note_text = str(decision.get("note_text") or "").strip()[:12000]
        event_title = str(decision.get("event_title") or "").strip()[:400]
        starts_at = str(decision.get("starts_at") or "").strip()[:80]
        ends_at = str(decision.get("ends_at") or "").strip()[:80]

        # Do not let an organizer-shaped hallucination hijack a normal question.  The
        # organizer is authoritative only when the utterance itself mentions a
        # calendar, reminder, note, event or today's plan.
        organizer_words = (
            "календар", "напомн", "заметк", "событи", "встреч", "совещан",
            "расписан", "план на сегодня", "что сегодня", "по плану",
        )
        organizer_request = any(word in raw.casefold().replace("ё", "е") for word in organizer_words)
        if action.startswith("organizer_") and not organizer_request:
            action = "unknown"
            route = "conversation"
            turn_kind = "conversation"

        # External names must come from the current utterance, never from a previous
        # classifier result or a model's preferred brand.
        if action in {"launch_application", "open_service"} and not self._grounded_semantic_span(target, raw):
            action, target = "unknown", ""
        if action == "message_send" and not all(
            self._grounded_semantic_span(value, raw)
            for value in (platform, recipient, message)
        ):
            action = "unknown"
            platform = recipient = message = ""
        if action == "organizer_note_create" and not self._grounded_semantic_span(
            note_text, raw
        ):
            action, note_text = "unknown", ""
        if action == "organizer_event_create":
            if not self._grounded_semantic_span(event_title, raw):
                action, event_title, starts_at, ends_at = "unknown", "", "", ""
            else:
                parsed_start = None
                parsed_end = None
                try:
                    parsed_start = datetime.fromisoformat(starts_at) if starts_at else None
                    parsed_end = datetime.fromisoformat(ends_at) if ends_at else None
                except (TypeError, ValueError):
                    parsed_start = parsed_end = None
                if starts_at and parsed_start is None:
                    starts_at = ""
                if ends_at and parsed_end is None:
                    ends_at = ""
        if media_content and media_content.casefold().replace("ё", "е") not in raw.casefold().replace("ё", "е"):
            media_content = ""
        if action == "system_brightness":
            try:
                level = float(target.rstrip("%"))
            except (TypeError, ValueError):
                level = -1.0
            if not 0.0 <= level <= 100.0:
                action, target = "unknown", ""

        # A concrete typed capability is more specific than the generic route label.
        # Resolve a contradictory model envelope in the safe direction instead of
        # discarding mail_review/message_send and falling into free-form chat.
        concrete = action not in {"unknown", "observe", "code_generation"}
        if turn_kind in {"capability_question", "conversation"}:
            route = "conversation"
            action, target, target_kind, service, media_action = "unknown", "", "none", "", "unknown"
            platform = recipient = message = ""
            concrete = False
        elif turn_kind in {"action", "clarification_response"}:
            route = "computer_action"
        elif concrete:
            route = "computer_action"
        elif route == "conversation" and action != "code_generation":
            action, target, media_action = "unknown", "", "unknown"
            platform = recipient = message = ""
        if confidence < 0.45 and not concrete:
            route = "unknown"
        # History is a dependency, not a personality setting.  The model owns this
        # boolean in the same typed envelope as the route/action; the engine must not
        # reinterpret natural language with a second lexical/regex classifier.  A
        # conversational greeting or a new topic therefore arrives with
        # ``needs_history=false`` directly from the admission decision, while a
        # genuine continuation can explicitly request the prior turn.
        needs_history = bool(decision.get("needs_history") is True)
        if turn_kind == "clarification_response":
            needs_history = True
        result = {
            "route": route, "turn_kind": turn_kind, "confidence": confidence, "action": action,
            "target": target, "target_kind": target_kind, "service": service,
            "media_content": media_content, "media_action": media_action,
            "single_step": bool(decision.get("single_step") is True),
            "side_effect_required": bool(decision.get("side_effect_required") is True),
            "platform": platform, "recipient": recipient, "message": message,
            "note_text": note_text, "event_title": event_title,
            "starts_at": starts_at, "ends_at": ends_at,
            "needs_history": needs_history,
            "mood": str(decision.get("mood") or "calm") if str(decision.get("mood") or "") in {"calm", "joy", "surprise", "laugh"} else "calm",
            "ack": str(decision.get("ack") or "").strip()[:60],
            "question": str(decision.get("question") or "").strip()[:160],
            "decision_status": "ok",
        }
        self._trace("SEMANTIC_DECISION", **result)
        return result

    def _semantic_admission(self, query: str) -> bool | None:
        decision = self.semantic_decision(query)
        route = str(decision.get("route") or "unknown")
        if route == "computer_action":
            return True
        if route == "conversation":
            return False
        return None

    def semantic_route(self, query: str) -> bool | None:
        """Strict model-only admission used by the production chat front door.

        Unlike ``should_handle`` this method never falls back to lexical intent
        detection.  ``None`` means that the local classifier was unavailable or
        below confidence; callers must then keep the request conversational rather
        than guessing an application, device, service, or action from words.
        """
        return self._semantic_admission(query)

    def semantic_hint(self, query: str) -> dict[str, str]:
        """Return the model-derived typed first-step hint for this utterance."""
        return self._hint_from_semantic_decision(self.semantic_decision(query))

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
        for candidate in (
            self._planner_model(),
            str(self.services.settings.fast_model),
            str(self.services.settings.model),
        ):
            if candidate.casefold() in installed:
                return installed[candidate.casefold()]
        return str(self.services.settings.fast_model)

    def _model_decision(
        self,
        goal: str,
        elements: list[dict[str, Any]],
        *,
        text_to_type: str = "",
        avoid: str = "",
    ) -> dict[str, Any]:
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
            f"ЦЕЛЬ: {goal}\nТЕКСТ_ДЛЯ_ВВОДА: {text_to_type[:500] if text_to_type else '<нет>'}\n"
            f"НЕ ПОВТОРЯЙ БЕЗ ИЗМЕНЕНИЯ ЭКРАНА: {avoid or '<нет>'}\nЭЛЕМЕНТЫ:\n" + self._compact_tree(elements)
        )
        try:
            result = self.gateway.json(
                [{"role":"user","content":prompt}], model=model, temperature=0.0, schema=schema,
                num_ctx=1024, num_predict=64,
                keep_alive=self.services.settings.keep_alive, timeout_seconds=4.8,
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
        a high-level model plan before the deterministic composer primitive.
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
        if re.search(r"\b(?:останов|выключ|отключ|stop)\w*", goal_n):
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
        if re.search(r"\b(?:останов|выключ|отключ|stop)\w*", goal_n):
            return "stopped"
        return ""

    def _media_signature(self, snapshot: dict[str, Any]) -> str:
        """Best available track identity from the active media window/UI tree."""
        title = self._norm((snapshot.get("window") or {}).get("title") or "")
        labels: list[str] = []
        for item in list(snapshot.get("elements") or [])[:260]:
            if self._norm(item.get("control_type")) in {"button", "slider", "progressbar"}:
                continue
            label = self._norm(item.get("name") or "")
            if 3 <= len(label) <= 160 and label not in labels:
                labels.append(label)
        return "|".join([title, *labels[:12]])[:1800]

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
        if action in {"next", "previous"}:
            before_signature = self._media_signature(before)
            deadline = time.monotonic() + 2.6
            after = self._media_snapshot()
            while time.monotonic() < deadline and self._media_signature(after) == before_signature:
                time.sleep(.18)
                after = self._media_snapshot()
            after_signature = self._media_signature(after)
            verified = bool(before_signature and after_signature and after_signature != before_signature)
        else:
            after = self._poll_media_state(desired, timeout=1.8) if desired else self._media_snapshot()
            verified = bool(desired and ((desired == "stopped" and after.get("state") in {"", "paused"}) or after.get("state") == desired))
        self._trace("MEDIA_CONTROL_ENSURE", goal=goal, desired=desired, before=before.get("state"), after=after.get("state"), method=method, verified=verified)
        return {"ok": bool(verified), "completed": True, "verified": bool(verified), "action": action, "desired": desired, "method": method, "before_state": before.get("state") or "unknown", "after_state": after.get("state") or "unknown", "before_signature": self._media_signature(before), "after_signature": self._media_signature(after), "tool_result": sent, "error": "" if verified else "Медиа-команда отправлена один раз, но требуемое состояние не подтверждено"}

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
        # Capture the post-type state before the commit. Seeing text in the composer is
        # proof of typing, not proof of delivery. r51 incorrectly returned verified=True
        # here and therefore declared a frozen Telegram send successful.
        before_rows=[]; before_sig=""
        try:
            before_rows=self.operator._elements(str(acquired.get("title") or ""),limit=420,handle=int(acquired.get("handle") or 0) or None)
            before_sig=self.operator._ui_fingerprint(before_rows)
        except Exception:
            before_rows=[]; before_sig=""
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

        verified=False; evidence=""
        if before_rows and hasattr(self.operator,"_elements"):
            payload_n=self._norm(payload)
            deadline=time.monotonic()+20.0
            while time.monotonic()<deadline:
                rows=self.operator._elements(str(acquired.get("title") or ""),limit=420,handle=int(acquired.get("handle") or 0) or None)
                current_sig=self.operator._ui_fingerprint(rows) if hasattr(self.operator,"_ui_fingerprint") else ""
                composer_found=False; composer_contains=False; outgoing_visible=False
                for el in rows:
                    if hasattr(self.operator,"_is_browser_chrome") and self.operator._is_browser_chrome(el):
                        continue
                    typ=self._norm(el.get("control_type")); blob=self._norm(f"{el.get('name','')} {el.get('value','')} {el.get('class_name','')}")
                    composer_like=typ in {"edit","combobox","group","document"} and any(mark in blob for mark in ("input message","input-message","write a message","сообщение","composer","editable-message","textbox"))
                    if composer_like:
                        composer_found=True
                        if payload_n and payload_n in blob: composer_contains=True
                    elif payload_n and payload_n in blob:
                        rect=el.get("rectangle") or []
                        if len(rect)==4 and int(rect[3])>150: outgoing_visible=True
                changed=bool(before_sig and current_sig and current_sig!=before_sig)
                if outgoing_visible:
                    verified=True; evidence="outgoing_text_visible"; break
                if composer_found and not composer_contains and changed:
                    verified=True; evidence="composer_cleared_and_ui_changed"; break
                time.sleep(.20)
        self._trace("UNIVERSAL_FOCUSED_TEXT",goal=goal,title=acquired.get("title"),chars=len(payload),verified=verified,completed=submitted,focus_verified=acquired.get("focused"),commit_method=commit_method,evidence=evidence)
        return {"ok":True,"completed":submitted,"submitted":True,"verified":verified,"method":"grounded-input","commit_method":commit_method,"title":acquired.get("title"),"text_chars":len(payload),"evidence":evidence or "post_submit_unverified"}

    def accessible_goal(self, goal: str, *, text_to_type: str = "", max_steps: int = 9, stop_event: threading.Event | None = None) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        failed_actions: list[str] = []
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
                if score >= 1.30 and f"click:{idx}:{title}" not in failed_actions:
                    decision = {"action":"click","index":idx,"reason":"semantic-ui-match","score":round(score,3)}
            if not decision:
                decision = self._model_decision(
                    goal,
                    elements,
                    text_to_type=text_to_type,
                    avoid="; ".join(failed_actions[-4:]),
                )
            action = str(decision.get("action") or "fail")
            reason = str(decision.get("reason") or "")[:300]
            try:
                idx = int(decision.get("index", -1))
            except Exception:
                idx = -1
            action_key = (action, idx, title)
            if last_action_key == action_key and previous_signature and signature == previous_signature and action in {"click", "type", "enter"}:
                self._trace("UNIVERSAL_REPEAT_BLOCKED", goal=goal, title=title, action=action, index=idx)
                failed_actions.append(f"{action}:{idx}:{title}")
                last_action_key = None
                time.sleep(.35)
                continue
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
                    failed_actions.append(f"{action}:{idx}:{title}")
                    previous_signature = signature
                    last_action_key = action_key
                    time.sleep(.35)
                    continue
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
        return {
            "ok": False,
            "verified": False,
            "steps": history,
            "method": "uia-agent",
            "error": "Задача повторена с обновлёнными компонентами, но лимит безопасных GUI-шагов достигнут",
        }

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
                                         num_ctx=2048, num_predict=180,
                                         keep_alive=self.services.settings.keep_alive, timeout_seconds=4.0)
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
                "режим полета", "камер",
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
        if any(x in low for x in ("не удалось", "не смогла", "не могу", "не нашла", "не найден", "нужно уточнить", "уточни ")):
            return False
        # Older builds promoted any fluent deterministic answer to verified=true. The direct
        # lane is now deny-by-default: an executor must attach explicit evidence.
        def explicit(value: Any) -> bool:
            if not isinstance(value, dict):
                return False
            if value.get("verified") is True:
                return True
            for key in ("result", "observation", "postcondition"):
                if explicit(value.get(key)):
                    return True
            tools = value.get("tools")
            return bool(isinstance(tools, list) and tools and all(explicit(item) for item in tools if isinstance(item, dict)))
        return explicit(route or {})

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
        normalized = self._norm(query)
        if self._CODE_CONTEXT.search(query) or any(
            token in normalized
            for token in (
                "файл", "папк", "каталог", "директор", "документ", "workspace",
                "репозитор", "исходник", "скрипт", "код", "file", "folder",
            )
        ):
            tool = "files"
        elif any(token in normalized for token in ("powershell", "команд", "терминал", "консол", "git ", "shell")):
            tool = "shell"
        elif any(token in normalized for token in ("брауз", "сайт", "веб", "url", "http://", "https://")):
            tool = "browser"
        elif any(token in normalized for token in ("письм", "почт", "сообщ", "чат", "telegram", "телеграм")):
            tool = "communication"
        else:
            tool = "deterministic"
        return [{
            "goal": query.strip(),
            "mode": "auto",
            "success": "Вся цель пользователя реально достигнута и проверена",
            "text": "",
            "tool": tool,
        }]

    @staticmethod
    def _is_planner_fallback(specs: list[dict[str, Any]]) -> bool:
        return bool(specs) and any(bool(item.get("planner_fallback")) for item in specs)

    def _prepare_executor_specs(self, query: str, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Preserve owner semantics and remove planner-only pseudo resources."""
        query_n = self._norm(query)
        workspace_named = bool(re.search(r"\b(?:рабоч\w*\s+папк\w*|workspace|work\s*folder)\b", query_n, re.I))
        external_communication = any(
            token in query_n
            for token in ("почт", "письм", "telegram", "телеграм", "мессендж", "чат", "получател", "адресат", "отправ")
        )
        explicit_visible = any(token in query_n for token in ("на экране", "в текущем окне", "уведомлен", "покажи окно"))
        file_names = re.findall(
            r"(?<![\w.-])([A-Za-zА-Яа-яЁё0-9_-]{1,120}\.[A-Za-z0-9]{1,12})\b",
            query,
            re.I,
        )
        exact_match = re.search(
            r"(?:точн\w*\s+(?:текст|содержим)\w*)\s*:\s*([\s\S]+?)\s*$",
            query,
            re.I,
        )
        exact_literal = exact_match.group(1) if exact_match and len(file_names) == 1 else None
        literal_target = file_names[0] if exact_literal is not None else ""
        if exact_literal is not None:
            nested_match = re.search(
                rf"(?:каталог|папк\w*)\s+([A-Za-zА-Яа-яЁё0-9_-]{{1,120}})\s*,?\s*(?:а\s+)?внутри\s+файл\w*\s+{re.escape(file_names[0])}",
                query,
                re.I | re.S,
            )
            if nested_match:
                literal_target = f"{nested_match.group(1)}/{file_names[0]}"
        has_file_read_plan = any(
            str(spec.get("tool") or "").casefold() == "files"
            and any(token in self._norm(spec.get("goal") or "") for token in ("read file", "read_file", "прочит"))
            for spec in specs
        )
        user_requested_copy = any(token in query_n for token in ("скопир", "буфер", "copy to clipboard"))

        def clean_workspace_value(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: clean_workspace_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean_workspace_value(item) for item in value]
            if not workspace_named or not isinstance(value, str):
                return value
            normalized = value.replace("\\", "/")
            lowered = normalized.casefold()
            for prefix in ("working_folder/", "work_folder/", "workspace/"):
                if lowered.startswith(prefix):
                    return normalized[len(prefix):]
            return value

        prepared: list[dict[str, Any]] = []
        literal_step_added = False
        for spec in specs:
            spec["original_task"] = query.strip()
            spec["args"] = clean_workspace_value(spec.get("args") or {})
            if isinstance(spec.get("success_condition"), dict):
                spec["success_condition"] = clean_workspace_value(spec["success_condition"])
            goal_n = self._norm(spec.get("goal") or "")
            if exact_literal is not None and workspace_named and str(spec.get("tool") or "").casefold() in {"files", "shell"}:
                if literal_step_added:
                    continue
                literal_step_added = True
                spec["tool"] = "files"
                spec["goal"] = "write_exact_file"
                spec["mode"] = "write"
                spec["args"]["literal_path"] = literal_target
                spec["args"]["literal_content"] = exact_literal
                spec["success_condition"] = {
                    "kind": "file_content_matches",
                    "description": f"Файл {literal_target} существует и точно совпадает с заданным текстом",
                    "args": {"file_path": literal_target, "expected_content": exact_literal},
                }
                goal_n = "write exact file"
            internal_report_goal = any(token in goal_n for token in (
                "report the result", "report result", "display notification", "show notification",
                "сообщ результат", "долож результат", "показ уведомлен",
            ))
            pure_internal_report = (
                str(spec.get("tool") or "").casefold() in {"communication", "desktop_agent"}
                and internal_report_goal
                and not external_communication
                and not explicit_visible
            )
            redundant_copy_after_read = (
                has_file_read_plan
                and str(spec.get("tool") or "").casefold() in {"communication", "desktop_agent"}
                and any(token in goal_n for token in ("copy text", "copy_text", "display text", "show text"))
                and not user_requested_copy
                and not explicit_visible
            )
            if not pure_internal_report and not redundant_copy_after_read:
                prepared.append(spec)
        return prepared or specs[:1]

    def _structured_file_fastpath(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Execute unambiguous workspace file primitives without lossy LLM paraphrase."""
        args = spec.get("args") if isinstance(spec.get("args"), dict) else {}
        condition = spec.get("success_condition") if isinstance(spec.get("success_condition"), dict) else {}
        condition_args = condition.get("args") if isinstance(condition.get("args"), dict) else {}
        goal_n = self._norm(spec.get("goal") or "")
        mode_n = self._norm(spec.get("mode") or "")
        original = str(spec.get("original_task") or "")

        def first_path(*keys: str) -> str:
            for source in (args, condition_args):
                for key in keys:
                    value = str(source.get(key) or "").strip()
                    if value:
                        return value.replace("\\", "/")
            return ""

        literal_path = str(args.get("literal_path") or "").strip().replace("\\", "/")
        if literal_path and "literal_content" in args:
            content = str(args.get("literal_content") or "")
            write = self.tools.execute("write_file", {"path": literal_path, "content": content, "overwrite": True})
            readback = self.tools.execute("read_file", {"path": literal_path}) if write.get("ok") else {}
            actual = str(((readback.get("result") or {}) if isinstance(readback, dict) else {}).get("content") or "")
            verified = bool(write.get("ok") and readback.get("ok") and actual == content)
            return {
                "ok": verified, "completed": bool(write.get("ok")), "verified": verified,
                "answer": (f"Файл {literal_path} записан и точно проверен." if verified else "Точное содержимое файла не подтвердилось после записи."),
                "route": {"action": "structured_file_literal", "model": "deterministic", "write": write, "readback": readback},
            }

        is_read = any(token in goal_n for token in ("read file", "read_file", "прочит")) or mode_n == "read"
        if is_read:
            candidates = re.findall(
                r"(?<![\w.-])([A-Za-zА-Яа-яЁё0-9_-]{1,120}\.[A-Za-z0-9]{1,12})\b",
                original,
                re.I,
            )
            path = candidates[0] if len(candidates) == 1 else first_path("file_path", "path")
            if not path:
                path = str(spec.get("text") or "").strip()
            if path:
                result = self.tools.execute("read_file", {"path": path})
                content = str((result.get("result") or {}).get("content") or "") if result.get("ok") else ""
                return {
                    "ok": bool(result.get("ok")), "completed": False, "verified": bool(result.get("ok")),
                    "answer": content if result.get("ok") else str(result.get("error") or "Файл не прочитан"),
                    "route": {"action": "structured_file_read", "model": "deterministic", "result": result},
                }

        is_count = any(token in goal_n for token in ("count file", "count_files", "посчита"))
        is_list = is_count or any(token in goal_n for token in ("list file", "list_files", "перечисл")) or mode_n == "list files"
        if is_list:
            path = first_path("folder_path", "directory", "path") or str(spec.get("text") or "").strip()
            if path:
                result = self.tools.execute("list_files", {"path": path})
                rows = result.get("result") if isinstance(result.get("result"), list) else []
                count = sum(1 for row in rows if isinstance(row, dict) and row.get("type") == "file")
                return {
                    "ok": bool(result.get("ok")), "completed": False, "verified": bool(result.get("ok")),
                    "answer": str(count) if is_count and result.get("ok") else json.dumps(rows, ensure_ascii=False, default=str),
                    "route": {"action": "structured_file_list", "model": "deterministic", "result": result},
                }
        return None

    def plan(self, query: str) -> list[dict[str, Any]]:
        schema = {
            "type": "object",
            "properties": {
                "schema_version": {"type": "string", "enum": ["eirven.action-plan/v1"]},
                "plan_id": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string", "enum": ["deterministic", "desktop_agent", "files", "shell", "browser", "communication"]},
                    "args": {"type": "object"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "number"},
                    "retries": {"type": "integer"},
                    "failure_strategy": {"type": "string", "enum": ["fail", "replan", "ask_user"]},
                    "requires_confirmation": {"type": "boolean"},
                    "success_condition": {"type": "object", "properties": {
                        "kind": {"type": "string"}, "description": {"type": "string"}, "args": {"type": "object"},
                    }, "required": ["kind", "description", "args"]},
                }, "required": ["id", "tool", "args", "depends_on", "timeout_seconds", "retries", "failure_strategy", "requires_confirmation", "success_condition"]}},
            },
            "required": ["schema_version", "plan_id", "steps"],
        }
        prompt = (
            "Ты планировщик Windows desktop-agent EIRVEN. Верни только eirven.action-plan/v1. "
            "Разбей запрос владельца на 1..10 последовательных проверяемых шагов. Не превращай всю фразу в имя приложения и не придумывай селекторы: "
            "executor сам исследует текущий экран. В args всегда положи goal, mode и text; text заполняй только явно данным текстом. "
            "depends_on ссылается только на более ранние id. success_condition должна описывать машинно наблюдаемое постусловие, а не 'команда отправлена'. "
            "tool=communication для почты/мессенджеров/сообщений, browser для сайта, files/shell для кода и файлов, desktop_agent для GUI, deterministic только для однозначного системного действия. "
            "Не используй список известных приложений: если название разговорное, старое или неточное, исполнитель должен получить application_list и выбрать среди реально установленных приложений; если локального приложения нет, открыть официальный веб-вариант через браузер. Установка или скачивание программ не является fallback и запрещена без отдельной команды владельца. "
            "Для почты предпочитай mail_status/mail_review/mail_drafts/mail_stage_draft: эти инструменты работают с подключённым ящиком и проверяют состояние; фактическая отправка письма всегда требует отдельного подтверждения владельца. "
            "requires_confirmation=true для отправки, звонка, удаления, покупки, оплаты и разрешений. "
            "Если для цели понадобится авторизация/CAPTCHA/UAC, исполнитель сам дойдёт до неё и попросит владельца. "
            "Тебе НЕ нужно анализировать текущий экран: это делает executor после планирования.\n\n"
            f"ЗАПРОС: {query}"
        )
        model = self._planner_model()
        repair = ""
        for attempt in range(2):
            try:
                current_prompt = prompt + repair
                data = self.gateway.json([{"role": "user", "content": current_prompt}], model=model, temperature=0.0, schema=schema,
                                         # 1536 tokens did not even fit the schema, repair
                                         # feedback and a real desktop-state diagnostic.
                                         # Keep room for a valid 10-step plan and for the
                                         # second validation attempt.
                                         num_ctx=max(
                                             4096,
                                             min(int(self.services.settings.task_num_ctx), 8192),
                                         ),
                                         num_predict=700,
                                         # The supported 16B release model can need more
                                         # than seven seconds even while resident. A 7 s
                                         # deadline forced every real plan into the coarse
                                         # deterministic fallback, which then treated file
                                         # goals as foreground-UI work. Keep the bound finite
                                         # while honoring the configured local-model budget.
                                         keep_alive=self.services.settings.keep_alive,
                                         timeout_seconds=max(
                                             30.0,
                                             min(float(self.services.settings.llm_first_token_timeout), 90.0),
                                         ),
                                         num_gpu=self._agent_num_gpu())
                if not isinstance(data, dict):
                    raise ProtocolError("planner returned a non-object")
                if data.get("schema_version") != SCHEMA_VERSION:
                    raise ProtocolError(f"planner returned unsupported schema_version: {data.get('schema_version')!r}")
                plan = plan_from_model(query, list(data.get("steps") or [])[:10], plan_id=str(data.get("plan_id") or ""))
                specs = self._prepare_executor_specs(query, plan.executor_specs())
                self._trace("UNIVERSAL_PLAN", query=query, model=model, plan=plan.to_dict())
                return specs
            except Exception as exc:
                self._trace("UNIVERSAL_PLAN_REPAIR", query=query, model=model, attempt=attempt + 1, error=str(exc)[:900])
                repair = f"\n\nПредыдущий JSON не прошёл validator: {str(exc)[:400]}. Исправь структуру полностью и верни новый plan_id."
        fallback = deterministic_plan(query, self._fallback_goals(query))
        self._trace("UNIVERSAL_PLAN_FALLBACK", query=query, plan=fallback.to_dict())
        # Keep the transport shape identical to an ordinary plan, but make the origin
        # explicit.  The old replan branch compared these versioned executor specs with
        # raw ``_fallback_goals`` dictionaries, so the comparison could never be equal.
        # A planner outage was consequently accepted as a brand-new repair plan and the
        # diagnostic repair prompt itself was executed as a desktop goal.
        specs = self._prepare_executor_specs(query, fallback.executor_specs())
        for spec in specs:
            spec["planner_fallback"] = True
        return specs

    def _pending_key(self, conversation_id: str) -> str:
        return f"universal_agent_pending:{conversation_id or 'voice'}"

    PENDING_MAX_AGE_SECONDS = 900.0

    def has_pending(self, conversation_id: str) -> bool:
        try:
            value = self.services.db.get_setting(self._pending_key(conversation_id), None)
            terminal = {
                TaskState.CANCELLED.value, TaskState.COMPLETED.value, TaskState.FAILED.value,
            }
            if not (
                isinstance(value, dict)
                and not value.get("cleared")
                and str(value.get("state") or "") not in terminal
                and value.get("goals")
            ):
                return False
            # A checkpoint blocks the whole deterministic control plane for this
            # conversation, so it must not outlive the exchange that created it. An
            # interrupted run (crash, app close, killed process) leaves a non-terminal
            # checkpoint behind; without an age limit that silently disabled shutdown,
            # volume, media and messenger commands for good.
            saved_at = float(value.get("saved_at") or 0)
            if saved_at > 0 and (time.time() - saved_at) > self.PENDING_MAX_AGE_SECONDS:
                try:
                    self.services.db.set_setting(self._pending_key(conversation_id), {"cleared": True})
                except Exception:
                    pass
                return False
            return True
        except Exception:
            return False

    def _save_pending(self, conversation_id: str, payload: dict[str, Any], *, replace: bool = False) -> bool:
        """Generation-aware checkpoint CAS; stale workers cannot overwrite a newer turn."""
        lock = getattr(self, "_checkpoint_lock", None) or threading.RLock()
        try:
            with lock:
                key = self._pending_key(conversation_id)
                current = self.services.db.get_setting(key, None)
                incoming_id = str(payload.get("run_id") or "")
                current_id = str(current.get("run_id") or "") if isinstance(current, dict) else ""
                if not replace and current_id and incoming_id != current_id:
                    return False
                if not replace and isinstance(current, dict) and current.get("cleared"):
                    return False
                self.services.db.set_setting(key, payload)
                return True
        except Exception:
            return False

    def _clear_pending(self, conversation_id: str, run_id: str = "") -> bool:
        lock = getattr(self, "_checkpoint_lock", None) or threading.RLock()
        try:
            with lock:
                key = self._pending_key(conversation_id)
                current = self.services.db.get_setting(key, None)
                current_id = str(current.get("run_id") or "") if isinstance(current, dict) else ""
                if current_id and run_id != current_id:
                    return False
                # Keep a tombstone so an older generation cannot repopulate the slot
                # after the current run finishes and clears its goals.
                self.services.db.set_setting(key, {"run_id": run_id or current_id, "cleared": True, "saved_at": time.time()})
                return True
        except Exception:
            return False

    def _execute_goal(self, spec: dict[str, Any], execute_direct: Any, *, stop_event: threading.Event | None = None) -> dict[str, Any]:
        goal = str(spec.get("goal") or "").strip()
        mode = str(spec.get("mode") or "auto")
        text = str(spec.get("text") or "")
        started = time.monotonic()
        model_only = bool(getattr(getattr(self.services, "settings", None), "model_only_mode", False))
        planned_tool = str(spec.get("tool") or "").casefold()
        reactive_v2 = bool(spec.get("reactive_v2"))
        explicit_visible = any(
            x in self._norm(goal)
            for x in ("на экране", "текущем окне", "наж", "клик", "поле", "кнопк", "вкладк")
        )
        structured_non_gui = (not reactive_v2) and planned_tool in {"files", "shell"} and not explicit_visible

        # Once a route question has all required slots, use one bounded authoritative
        # search before touching the desktop. This prevents a stale foreground media tab
        # from capturing the task while keeping route resolution generic and verifiable.
        travel_slots = (
            dict(spec.get("owner_slots") or {})
            if isinstance(spec.get("owner_slots"), dict) else {}
        )
        travel_missing = self._travel_missing_fields(goal, travel_slots)
        if (
            not model_only
            and
            self._TRAVEL_QUERY.search(self._norm(goal))
            and not travel_missing
            and not bool(spec.get("travel_search_preflight"))
        ):
            search_query = self._travel_search_query(goal, travel_slots)
            try:
                search_result = self.tools.execute("web_search", {"query": search_query, "max_results": 5})
            except Exception as exc:
                search_result = {"ok": False, "error": str(exc)}
            spec["travel_search_preflight"] = True
            self._trace(
                "TRAVEL_SEARCH_PREFLIGHT", query=search_query,
                ok=bool(search_result.get("ok")),
            )
            if not search_result.get("ok"):
                return {
                    "ok": False, "completed": False, "goal": goal, "mode": mode,
                    "answer": "Не удалось открыть поиск маршрута; повтор не выполняю.",
                    "route": {"action": "travel_search_preflight", "model": "deterministic", "result": search_result},
                    "verified": False,
                    "error": str(search_result.get("error") or "search unavailable"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            payload = search_result.get("result") if isinstance(search_result, dict) else None
            rows = payload.get("results") if isinstance(payload, dict) else None
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            destination_value = str(travel_slots.get("destination") or "").strip()
            if not destination_value and "назначение " in search_query:
                destination_value = search_query.split("назначение ", 1)[1].split(" способ ", 1)[0].strip()
            duration_row = self._route_duration_row(rows, destination_value)
            route_url = self._route_map_url(rows)
            if duration_row is not None:
                opened = None
                if route_url.startswith(("http://", "https://")):
                    try:
                        opened = self.tools.execute("open_default_url", {"url": route_url})
                    except Exception as exc:
                        opened = {"ok": False, "error": str(exc)}
                snippet = str(duration_row.get("snippet") or "").strip()
                return {
                    "ok": True, "completed": False, "goal": goal, "mode": mode,
                    "answer": f"Маршрут найден и проверен по свежему поиску: {snippet[:420]}",
                    "route": {
                        "action": "travel_search_verified", "model": "web_search",
                        "search": search_result, "map_open": opened,
                    },
                    "verified": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            # Search backends are optional and can time out. Open one focused search in
            # the owner's default browser as a bounded fallback, then ask for a manual
            # check rather than inventing a duration or entering an observer loop.
            try:
                fallback = self.tools.execute("default_search", {"query": search_query})
            except Exception as exc:
                fallback = {"ok": False, "error": str(exc)}
            fallback_ok = bool(fallback.get("ok"))
            fallback_answer = (
                "Веб-поиск сейчас не вернул проверяемое время. Открыла один поиск маршрута "
                "с указанными параметрами; проверь результат на экране и скажи «готово»."
                if fallback_ok else
                "Не удалось получить проверяемое время маршрута: веб-поиск временно недоступен. Повтор не выполняю."
            )
            return {
                "ok": False, "completed": False, "needs_user": fallback_ok,
                "goal": goal, "mode": mode, "answer": fallback_answer,
                "route": {
                    "action": "travel_search_fallback", "model": "default_search",
                    "result": fallback,
                },
                "verified": False,
                "error": str(fallback.get("error") or "route result unavailable"),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }

        # Typed account connectors outrank GUI navigation for exact data reads.  This
        # is a latency adapter over the live capability, not a website scenario: the
        # general reactive engine remains the fallback for every broader mail task.
        goal_n = self._norm(goal)
        if (
            not model_only
            and
            any(token in goal_n for token in ("почт", "письм", "email", "inbox"))
            and any(token in goal_n for token in ("непрочитан", "сколько", "количество"))
        ):
            status = self.tools.execute("mail_status", {})
            payload = status.get("result") if isinstance(status, dict) else None
            payload = payload if isinstance(payload, dict) else {}
            monitor = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
            unread = monitor.get("unread", payload.get("unread"))
            if status.get("ok") and isinstance(unread, int) and unread >= 0:
                return {
                    "ok": True, "completed": False, "goal": goal, "mode": mode,
                    "answer": f"Непрочитанных писем: {unread}.",
                    "route": {"action": "authoritative_mail_status", "model": "none"},
                    "verified": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            # A stopped background monitor legitimately has no cached unread count.
            # Read the connected IMAP inbox once instead of handing this exact factual
            # query to the action model (which used to call mail_status/mail_review
            # repeatedly and could remain in “думаю” for over 30 seconds).
            review = self.tools.execute("mail_review", {"limit": 1})
            review_payload = review.get("result") if isinstance(review, dict) else None
            review_payload = review_payload if isinstance(review_payload, dict) else {}
            if isinstance(review_payload.get("result"), dict):
                review_payload = review_payload["result"]
            unread = review_payload.get("unread")
            if review.get("ok") and isinstance(unread, int) and unread >= 0:
                return {
                    "ok": True, "completed": False, "goal": goal, "mode": mode,
                    "answer": f"Непрочитанных писем: {unread}.",
                    "route": {"action": "authoritative_mail_review", "model": "none"},
                    "verified": bool(review_payload.get("verified", True)),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }

        if (not reactive_v2) and planned_tool == "files" and not explicit_visible:
            file_result = self._structured_file_fastpath(spec)
            if file_result is not None:
                return {"goal": goal, "mode": mode, **file_result, "elapsed_ms": round((time.monotonic()-started)*1000)}

        site_open = None if reactive_v2 else self._open_site_goal(spec, stop_event=stop_event)
        if site_open is not None:
            return {"goal": goal, "mode": mode, **site_open}

        scroll_result = None if reactive_v2 else self._scroll_fastpath(goal, stop_event=stop_event)
        if scroll_result is not None:
            return {"goal": goal, "mode": mode, **scroll_result, "elapsed_ms": round((time.monotonic()-started)*1000)}

        autoplay_result = None if reactive_v2 else self.ensure_autoplay_goal(goal, stop_event=stop_event)
        if autoplay_result is not None:
            return {"goal": goal, "mode": mode, **autoplay_result, "answer": ("Автовоспроизведение переключила и подтвердила." if autoplay_result.get("verified") else str(autoplay_result.get("error") or "Не смогла переключить автовоспроизведение.")), "route": {"action":"autoplay_control","model":"uia"}, "elapsed_ms": round((time.monotonic()-started)*1000)}

        if not structured_non_gui and not reactive_v2:
            named_result = self.click_named_current(goal, stop_event=stop_event)
            if named_result is not None:
                return {"goal": goal, "mode": mode, **named_result, "elapsed_ms": round((time.monotonic()-started)*1000)}

        # Fast adapters are accelerators, never the source of truth. They are safest for
        # compact atomic goals produced by the planner.
        if not reactive_v2 and self._fast_direct_allowed(goal, mode=mode):
            acted, answer, route = execute_direct(goal)
            if self._direct_success(acted, answer, route):
                return {"ok": True, "goal": goal, "mode": mode, "answer": answer, "route": route,
                        "verified": True, "elapsed_ms": round((time.monotonic()-started)*1000)}

        # Media control belongs to the deterministic control plane.  It is state-aware:
        # observe -> toggle only if needed -> verify -> one semantic-button fallback.
        media_result = None if reactive_v2 else self.ensure_media_goal(goal, allow_implicit=False, stop_event=stop_event)
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
        focused = None if (structured_non_gui or reactive_v2) else self._current_window_text_fastpath(goal, text=text)
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

        # GUI-first only when the plan really calls for visible interaction. r59 uses the
        # planner's coarse tool family to keep structured capabilities (mail/files/shell)
        # out of a brittle click-first path. Communication is NOT synonymous with GUI:
        # the generic tool agent may choose IMAP mail tools or a visible messenger from
        # the same goal and live capability set.
        prefer_structured = planned_tool in {"communication", "files", "shell"} and not explicit_visible
        if (not reactive_v2) and (not prefer_structured) and (mode in {"desktop", "web", "communication"} or any(x in self._norm(goal) for x in ("на экране", "текущем окне", "наж", "чат", "браузер"))):
            generic = self.accessible_goal(goal, text_to_type=text, max_steps=10, stop_event=stop_event)
            if generic.get("ok"):
                verified = bool(generic.get("verified"))
                return {"ok": verified, "completed": bool(generic.get("completed", True)), "goal": goal, "mode": mode,
                        "answer": ("Изменение текущего экрана подтверждено." if verified else "Действие выполнено один раз, но изменение экрана не подтвердилось."),
                        "route": {"action":"universal_uia","model":"uia+fast-text","result":generic},
                        "verified": verified, "elapsed_ms": round((time.monotonic()-started)*1000)}

        # General tool agent handles files, terminal, Git, missing prerequisites, web
        # research and can come back to GUI. This is model-guided, not an app template.
        model_only = bool(getattr(getattr(self.services, "settings", None), "model_only_mode", False))
        context = (
            f"STRUCTURED_TASK=true\nWORKSPACE={self.services.settings.workspace_dir}\n"
            "Для файлов workspace используй относительные пути с write_file/read_file."
            if structured_non_gui
            else (
                "СОСТОЯНИЕ НЕ ПРЕДЗАПОЛНЕНО: сначала получи его узким read-only инструментом."
                if model_only else self._state_context(max_chars=3200)
            )
        )
        capability_context = ""
        try:
            capability_context = json.dumps(
                self.services.capabilities.snapshot(), ensure_ascii=False, default=str,
                separators=(",", ":"),
            )[:2500 if bool(getattr(self.services.settings, "model_only_mode", False)) else 9000]
        except Exception:
            capability_context = "{}"
        original_task = str(spec.get("original_task") or goal).strip()
        success_condition = spec.get("success_condition") if isinstance(spec.get("success_condition"), dict) else {}
        saved_hint = spec.get("_semantic_hint")
        model_hint = (
            dict(saved_hint)
            if model_only and isinstance(saved_hint, dict)
            else (self.semantic_hint(original_task) if model_only else {})
        )

        # Notes and calendar are authoritative local capabilities, not websites or
        # UI recipes.  The same semantic envelope that admitted the turn carries the
        # typed payload; the engine validates it, commits once, reads it back and then
        # lets the phone outbox replicate it.  No lexical intent parser is involved.
        organizer_action = str(model_hint.get("action") or "")
        # The semantic model can mention the organizer in its broad context even for a
        # normal conversational question.  Only commit this typed capability when the
        # user's actual wording contains an organizer concept; otherwise continue to
        # the ordinary answer lane instead of replying with an unrelated empty plan.
        organizer_words = (
            "календар", "напомн", "заметк", "событи", "встреч", "совещан",
            "расписан", "план на сегодня", "что сегодня", "по плану",
        )
        organizer_request = any(word in original_task.casefold().replace("ё", "е") for word in organizer_words)
        organizer = getattr(self.services, "phone_sync", None)
        if model_only and organizer is not None and organizer_request and organizer_action.startswith("organizer_"):
            paired_count = len(organizer.devices())
            if organizer_action == "organizer_note_create":
                note_text = str(model_hint.get("note_text") or "").strip()
                if not note_text:
                    prompt = "Что именно записать в заметки?"
                    return {
                        "ok": False, "completed": False, "verified": False,
                        "needs_user": True, "prompt": prompt, "answer": prompt,
                        "route": {"action": organizer_action, "model": "semantic-envelope"},
                    }
                note = organizer.add_note(note_text)
                verified = any(
                    int(item.get("id") or 0) == int(note.get("id") or -1)
                    and str(item.get("text") or "") == str(note.get("text") or "")
                    for item in organizer.recent_notes(30)
                )
                answer = (
                    "Записала в заметки. Телефон получит запись при ближайшей синхронизации."
                    if paired_count else
                    "Записала в заметки на компьютере. Когда телефон будет подключён, запись синхронизируется."
                )
                return {
                    "ok": verified, "completed": True, "verified": verified,
                    "answer": answer,
                    "route": {
                        "action": organizer_action, "model": "semantic-envelope",
                        "note": note, "paired_devices": paired_count,
                    },
                }

            if organizer_action == "organizer_notes_list":
                notes = organizer.recent_notes(20)
                answer = (
                    "В заметках пока пусто."
                    if not notes else
                    "Последние заметки:\n" + "\n".join(
                        f"• {str(item.get('text') or '')[:500]}" for item in notes[:8]
                    )
                )
                return {
                    "ok": True, "completed": False, "verified": True,
                    "answer": answer,
                    "route": {
                        "action": organizer_action, "model": "semantic-envelope",
                        "count": len(notes),
                    },
                }

            if organizer_action == "organizer_day_plan":
                today = organizer.today()
                events = list(today.get("events") or [])
                notes = list(today.get("notes") or [])
                lines: list[str] = []
                if events:
                    lines.append("Сегодня:")
                    for item in events:
                        start = str(item.get("starts_at") or "")
                        try:
                            stamp = datetime.fromisoformat(start).astimezone().strftime("%H:%M")
                        except (TypeError, ValueError):
                            stamp = start
                        lines.append(f"• {stamp} — {str(item.get('title') or '')}")
                else:
                    lines.append("На сегодня событий нет.")
                if notes:
                    lines.append("Недавние заметки:")
                    lines.extend(
                        f"• {str(item.get('text') or '')[:280]}" for item in notes[:5]
                    )
                return {
                    "ok": True, "completed": False, "verified": True,
                    "answer": "\n".join(lines),
                    "route": {
                        "action": organizer_action, "model": "semantic-envelope",
                        "today": today,
                    },
                }

            if organizer_action == "organizer_event_create":
                title = str(model_hint.get("event_title") or "").strip()
                starts_at = str(model_hint.get("starts_at") or "").strip()
                if not title:
                    prompt = "Как назвать событие?"
                    return {
                        "ok": False, "completed": False, "verified": False,
                        "needs_user": True, "prompt": prompt, "answer": prompt,
                        "route": {"action": organizer_action, "model": "semantic-envelope"},
                    }
                if not starts_at:
                    prompt = "На какую дату и точное время поставить событие?"
                    return {
                        "ok": False, "completed": False, "verified": False,
                        "needs_user": True, "prompt": prompt, "answer": prompt,
                        "route": {"action": organizer_action, "model": "semantic-envelope"},
                    }
                try:
                    start = datetime.fromisoformat(starts_at)
                    if start.tzinfo is None:
                        start = start.astimezone()
                    end_raw = str(model_hint.get("ends_at") or "").strip()
                    end = datetime.fromisoformat(end_raw) if end_raw else start + timedelta(hours=1)
                    if end.tzinfo is None:
                        end = end.astimezone()
                except (TypeError, ValueError):
                    prompt = "Не поняла дату события. Назови её и время ещё раз."
                    return {
                        "ok": False, "completed": False, "verified": False,
                        "needs_user": True, "prompt": prompt, "answer": prompt,
                        "route": {"action": organizer_action, "model": "semantic-envelope"},
                    }
                event = organizer.add_event(title, start, end)
                with self.services.db.connect() as conn:
                    stored = conn.execute(
                        "SELECT id,title,starts_at,ends_at,reminders FROM phone_events WHERE id=?",
                        (str(event.get("id") or ""),),
                    ).fetchone()
                verified = bool(stored and str(stored["title"]) == title)
                return {
                    "ok": verified, "completed": True, "verified": verified,
                    "answer": organizer._event_confirmation(event),
                    "route": {
                        "action": organizer_action, "model": "semantic-envelope",
                        "event": event, "paired_devices": paired_count,
                    },
                }

        # A typed route decision has a bounded browser-first lane.  The previous
        # generic executor could see "Яндекс Карты" in a route task, launch the
        # Windows Maps package and then loop through unrelated desktop observers.  A
        # route is a live web fact, so resolve it with one public search and, when a
        # duration is present, optionally open the returned map URL in the default
        # browser.  No package discovery, install or native map launch is involved.
        if (
            model_only
            and str(model_hint.get("action") or "") == "route_search"
            and not bool(spec.get("model_route_browser_preflight"))
        ):
            spec["model_route_browser_preflight"] = True
            route_query = original_task or goal
            try:
                search_result = self.tools.execute(
                    "web_search", {"query": route_query, "max_results": 5},
                )
            except Exception as exc:
                search_result = {"ok": False, "error": str(exc)}
            self._trace(
                "MODEL_ROUTE_BROWSER_PREFLIGHT",
                query=route_query,
                ok=bool(search_result.get("ok")),
            )
            if not search_result.get("ok"):
                try:
                    fallback = self.tools.execute("default_search", {"query": route_query})
                except Exception as exc:
                    fallback = {"ok": False, "error": str(exc)}
                return {
                    "ok": False,
                    "completed": False,
                    "needs_user": bool(fallback.get("ok")),
                    "goal": goal,
                    "mode": mode,
                    "answer": (
                        "Веб-поиск маршрута временно недоступен. Открыла один поиск в браузере; "
                        "проверь результат на экране и скажи «готово»."
                        if fallback.get("ok") else
                        "Не удалось получить веб-результат маршрута; повтор не выполняю."
                    ),
                    "route": {
                        "action": "model_route_browser_fallback",
                        "model": "default_search",
                        "result": fallback,
                    },
                    "verified": False,
                    "error": str(fallback.get("error") or "route search unavailable"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            payload = search_result.get("result") if isinstance(search_result, dict) else None
            rows = payload.get("results") if isinstance(payload, dict) else None
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            duration_row = self._route_duration_row(rows)
            if duration_row is not None:
                route_url = self._route_map_url(rows)
                opened = None
                if route_url.startswith(("http://", "https://")):
                    try:
                        opened = self.tools.execute("open_default_url", {"url": route_url})
                    except Exception as exc:
                        opened = {"ok": False, "error": str(exc)}
                snippet = str(duration_row.get("snippet") or "").strip()
                return {
                    "ok": True,
                    "completed": False,
                    "goal": goal,
                    "mode": mode,
                    "answer": f"Маршрут найден и проверен по свежему веб-поиску: {snippet[:420]}",
                    "route": {
                        "action": "model_route_browser_verified",
                        "model": "web_search",
                        "search": search_result,
                        "map_open": opened,
                    },
                    "verified": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
            try:
                fallback = self.tools.execute("default_search", {"query": route_query})
            except Exception as exc:
                fallback = {"ok": False, "error": str(exc)}
            return {
                "ok": False,
                "completed": False,
                "needs_user": bool(fallback.get("ok")),
                "goal": goal,
                "mode": mode,
                "answer": (
                    "Веб-поиск не вернул проверяемое время. Открыла один поиск маршрута в браузере; "
                    "проверь результат на экране и скажи «готово»."
                    if fallback.get("ok") else
                    "Не удалось получить проверяемое время маршрута; повтор не выполняю."
                ),
                "route": {
                    "action": "model_route_browser_fallback",
                    "model": "default_search",
                    "search": search_result,
                    "result": fallback,
                },
                "verified": False,
                "error": "route duration unavailable",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        if structured_non_gui:
            prompt = (
                f"ИСХОДНАЯ ЗАДАЧА ВЛАДЕЛЬЦА: {original_task}\n"
                f"ТЕКУЩИЙ ШАГ ПЛАНА: {goal}\n"
                f"ПОДСКАЗКА ПЛАНИРОВЩИКА (может быть неполной): {text or '<нет>'}\n"
                f"СТРУКТУРИРОВАННЫЕ АРГУМЕНТЫ ШАГА: {json.dumps(spec.get('args') or {}, ensure_ascii=False, default=str)[:1400]}\n"
                f"КРИТЕРИЙ УСПЕХА: {spec.get('success') or 'цель реально достигнута'}\n"
                f"АРГУМЕНТЫ ПРОВЕРКИ: {json.dumps(success_condition.get('args') or {}, ensure_ascii=False, default=str)[:1400]}\n"
                f"{context}\n\n"
                "Выполни текущий файловый/командный шаг минимальным структурным инструментом, без GUI. "
                "Исходная задача и критерий успеха важнее неполной подсказки планировщика. "
                "Для точной записи в workspace используй write_file с относительным путём и полным содержимым из исходной задачи. "
                "Фразы working_folder/work_folder/workspace обозначают корень WORKSPACE, не создавай папку с таким буквальным именем. "
                "Для каталога и подсчёта его элементов используй list_files, а read_file — только для обычного файла. "
                "После изменения обязательно вызови подходящий read-only инструмент и сравни фактический результат."
            )
        else:
            compact_model_only_rules = (
                "Это model-only контур: слова исходной цели — единственный источник намерения. "
                "Не выбирай EIRVEN, ChatGPT или текущее окно, если владелец явно не попросил именно их. "
                "Если цель просит запустить/открыть названное приложение, первый вызов — launch_application "
                "с названием из цели либо application_list для проверки, а не foreground/window_elements. "
                "Если назван сервис — передай его неизменённым в open_service. После каждого вызова "
                "наблюдай результат и перестраивай следующий шаг; без подтверждённого постусловия не сообщай успех."
                if model_only else ""
            )
            prompt = (
                f"ИСХОДНАЯ ЗАДАЧА ВЛАДЕЛЬЦА: {original_task}\n"
                f"ТЕКУЩИЙ ШАГ ПЛАНА: {goal}\n"
                f"КРИТЕРИЙ УСПЕХА: {spec.get('success') or 'цель реально достигнута'}\n"
                f"МАШИННЫЕ АРГУМЕНТЫ ПРОВЕРКИ: {json.dumps(success_condition.get('args') or {}, ensure_ascii=False, default=str)[:1400]}\n"
                f"ЯВНО РАЗРЕШЁННЫЙ ТЕКСТ ДЛЯ ВВОДА: {text or '<нет>'}\n\n"
                f"{compact_model_only_rules}\n\n"
                "Работай как настоящий desktop-agent. Сначала наблюдай состояние, затем используй минимальный реальный инструмент. "
                "Не считай действие успешным только потому, что команда/клик отработали: проверь состояние после него. "
                "Если пользователь называет приложение разговорно, старым брендом или неточно: сначала проверь живой список установленного ПО. Запускай только однозначно найденное установленное приложение; если подходящего локального приложения нет, сразу найди официальный веб-вариант через браузер. Никогда не скачивай и не устанавливай программу как автоматическое продолжение задачи; это допустимо только по отдельной явной команде владельца. Не держи внутри ответа собственный whitelist приложений. "
                "Если владелец не назвал музыкальный, картографический или иной сервис, задай один открытый вопрос «В каком сервисе?», а не предлагай закрытый фиксированный список. Любой названный сервис передай в open_service и затем наблюдай реально открывшуюся поверхность. "
                "Для маршрута установи только действительно недостающие параметры (откуда, куда, вид транспорта); не выдумывай их. Сначала используй веб-поиск/веб-карты в браузере; не запускай и не устанавливай отдельное приложение карт как обязательный шаг. После ответа прочитай фактическое время из живого веб-результата или попроси только недостающий параметр. "
                "Если задача относится к подключённой почте, предпочитай mail_status/mail_review/mail_drafts/mail_stage_draft вместо кликов по почтовому интерфейсу. Эти инструменты не дают автономно отправлять письмо. "
                "Если отсутствует необходимая утилита или приложение, не устанавливай и не скачивай его автоматически: сначала используй официальный веб-вариант через браузер, а если веб-варианта нет — честно сообщи о недоступности и предложи владельцу отдельную команду установки. Если нужен UAC/login/CAPTCHA/2FA/password, открой нужное место и попроси владельца "
                "сделать только этот ручной шаг; после слова «готово» сценарий продолжится. Для ошибки команды/сайта можешь искать точный текст ошибки через web_search. "
                "Не обходи защиту сайтов, CAPTCHA, UAC или аутентификацию. Покупку/заказ/платёж можно подготовить; "
                "точный финальный commit выполняй только когда deterministic RiskPolicy передал confirmed_action, "
                "никогда не считай обычное согласие в исходной фразе подтверждением. "
                "Для переписки сначала прочитай достаточный локальный контекст чата, чтобы понять адресата и стиль владельца; "
                "не выдумывай факты от имени владельца и не отправляй сообщение, если адресат неоднозначен. "
                "Если интерфейс загружается или кнопка временно недоступна — используй wait/window_wait и повторно наблюдай, а не считай это ошибкой сразу.\n\n"
                f"СТИЛЬ ВЛАДЕЛЬЦА/ЭЙРВЕН:\n{('<не добавлять в model-only>' if model_only else self._style_prompt()[:900])}\n\n"
                f"МОДЕЛЬНАЯ ПОДСКАЗКА ПЕРВОГО ТИПОВОГО ШАГА (не выдумывать сверх неё): {json.dumps(model_hint, ensure_ascii=False)}\n\n"
                f"ТЕКУЩЕЕ СОСТОЯНИЕ:\n{context}"
                f"\n\nLIVE CAPABILITIES (используй доступные authoritative tools прежде GUI):\n{capability_context}"
            )
        # Keep the native-tool prompt small. Tool-schema bloat was a major reason the
        # 2B action model missed its latency budget in r15. This remains a universal
        # primitive set; we merely expose only the primitive families relevant to the goal.
        universal_tools = {
            "foreground_window", "window_list", "window_elements", "window_focus",
            "window_click", "window_type", "click", "type_text", "mouse_move", "mouse_drag",
            "scroll", "press_key", "hotkey",
            "application_list", "launch_application", "open_service", "wait", "window_wait", "media_control", "system_volume", "system_brightness", "system_power",
            "mail_status", "mail_review", "mail_drafts", "mail_stage_draft",
            "screenshot", "desktop_state", "open_default_url", "default_search",
        }
        if (not reactive_v2) and planned_tool == "files" and not explicit_visible:
            universal_tools = {
                "list_files", "read_file", "write_file", "make_directory",
                "system_find", "system_list_files", "system_read_file", "system_write_file",
                "system_batch_rename", "powershell", "command_available",
            }
        elif (not reactive_v2) and planned_tool == "shell" and not explicit_visible:
            # A structured file/shell step must not wander into whichever GUI
            # happened to be foreground. Both workspace-scoped and explicit
            # user-path primitives are available, followed by read-back tools
            # that can prove the exact side effect.
            universal_tools = {
                "list_files", "read_file", "write_file", "make_directory", "run_command",
                "system_find", "system_open_path", "system_list_files", "system_read_file",
                "system_write_file", "powershell", "command_available", "wait",
            }
        goal_n = self._norm(goal)
        if mode in {"web", "communication"} or any(x in goal_n for x in ("брауз", "сайт", "веб", "telegram", "телеграм", "youtube", "ютуб", "музык", "spotify", "яндекс", "чат", "сообщ")):
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
        # Mixed requests such as "проверь сумму и оплати" are still mutations. A
        # leading observation verb must never downgrade the commit half to read-only.
        envelope_fallback = model_only and str(model_hint.get("decision_status") or "ok") in {"error", "invalid"}
        if model_only:
            require_side_effect = bool(model_hint.get("side_effect_required") is True)
        else:
            require_side_effect = bool(re.search(
                r"\b(исправ|почин|измен|замен|помен|допис|дополн|отправ|напиш|скин|ответ|включ|выключ|"
                r"откро|запуст|закро|позвон|переключ|установ|скача|коммит|push|прикреп|загруз|удал|"
                r"очист|убер|созда|перемест|скопир|переимен|заполни|наж|подготов|сортир|разлож|"
                r"оплат|куп|приобрет|закаж|вызов|заброни|постро|create|write|append|delete|move|copy|"
                r"rename|install|download|launch|send|pay|purchase|order|book)\w*",
                self._norm(goal), re.I,
            ))
        agent_steps = 10 if envelope_fallback else (32 if reactive_v2 else (16 if mode in {"communication", "code"} else 12))
        # Reactive-v2 sees the live typed catalogue. ``universal_tools`` remains a
        # latency filter only for legacy planned steps; it is never a capability
        # boundary for an unseen connector or newly installed service.
        agent_allowed_tools = None if reactive_v2 else universal_tools
        # On a clarification resume ``goal`` contains the exact owner's answer.  Feed
        # that enriched surface goal to the agent so media/service/travel slots are
        # grounded in the answer instead of silently reverting to the original query.
        surface_goal = goal if re.search(r"(?:ответ\s+владельца|owner\s+answer)", goal, re.I) else original_task
        # Action turns use the same configured main/planner checkpoint as admission.
        # The previous model-only branch silently selected ``fast_model`` (on this
        # machine qwen3.5:4b) for the actual desktop agent after the 9B envelope had
        # already understood the request.  That split was both slower (model reload)
        # and less reliable on live UI.  Conversational turns still use their own
        # fast text model in ChatService; only the executor is unified here.
        planner_model = self._planner_model()
        require_tool_action = not envelope_fallback
        report = self.services.agent.run(
            prompt,
            model=planner_model,
            max_steps=agent_steps,
            external_stop_event=stop_event,
            allowed_tools=agent_allowed_tools,
            auto_vision=True,
            require_tool_action=require_tool_action,
            require_side_effect=require_side_effect,
            require_verification=((model_only and not envelope_fallback) or require_side_effect),
            num_gpu=self._agent_num_gpu(),
            confirmed_action=str(spec.get("confirmed_action") or ""),
            confirmed_tool=str(spec.get("confirmed_tool") or ""),
            confirmed_arguments=(
                dict(spec.get("confirmed_arguments") or {})
                if isinstance(spec.get("confirmed_arguments"), dict) else None
            ),
            confirmed_surface=(
                dict(spec.get("confirmed_surface") or {})
                if isinstance(spec.get("confirmed_surface"), dict) else None
            ),
            task_deadline_seconds=float(spec.get("task_deadline_seconds") or 180.0),
            owner_goal=surface_goal,
            owner_slots=(
                dict(spec.get("owner_slots") or {})
                if isinstance(spec.get("owner_slots"), dict) else None
            ),
            model_first_action=model_hint,
        )
        if stop_event and stop_event.is_set():
            return {"ok": False, "cancelled": True, "completed": False, "goal": goal, "mode": mode,
                    "answer": "Остановлено пользователем.", "verified": False,
                    "route": {"action":"reactive_agent_v2","model":planner_model},
                    "elapsed_ms": round((time.monotonic()-started)*1000)}
        low = self._norm(report)
        if any(x in low for x in ("нужно войти", "нужна авторизация", "авторизуй", "captcha", "2fa", "uac", "подтверди вход", "введите пароль", "введи пароль")):
            raise TaskNeedsUser("Я дошла до шага авторизации/подтверждения в открытом окне. Заверши его вручную")
        bad = any(x in low for x in (
            "не удалось", "не смог", "остановилась", "остановлено пользователем", "остановлен пользователем",
            "лимит шагов", "модель остановилась", "не было выполнено", "не подтвержден", "не подтверждён",
            "не удалось надёжно подтвердить", "стратегии исчерпаны",
        ))
        outcome = self.services.agent.last_run_outcome() if hasattr(self.services.agent, "last_run_outcome") else {}
        verified = bool(outcome.get("goal_verified", outcome.get("verified")))
        # If the typed admission was unavailable, the bounded model fallback may
        # legitimately answer a greeting without a tool. A normal conversational
        # completion is successful; an actual action still needs its own evidence.
        ok = bool(not bad and (verified or not require_tool_action))
        if not ok and not bad and require_side_effect:
            report = "Действие выполнено один раз, но специализированное постусловие не подтвердилось. Повтор не выполняю."
        return {
                "ok": ok,
                # ``completed`` is reserved for an irreversible/external commit attempt;
                # reversible navigation may still be recovered through another route.
                "completed": bool(outcome.get("commit_attempted")),
                "effect_attempted": bool(outcome.get("used_side_effect")),
                "goal": goal, "mode": mode, "answer": report,
                "route": {"action":"reactive_agent_v2","model":planner_model},
                "verified": verified, "elapsed_ms": round((time.monotonic()-started)*1000)}

    def execute_task(
        self,
        query: str,
        execute_direct: Any,
        *,
        conversation_id: str = "",
        stop_event: threading.Event | None = None,
        semantic_envelope: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        model_only = bool(getattr(getattr(self.services, "settings", None), "model_only_mode", False))
        # The chat front door passes the exact envelope it already used for admission.
        # Direct/test callers still get one fresh decision here, but no task ever
        # reclassifies the same utterance into a conflicting tool hint.
        if model_only:
            decision = (
                dict(semantic_envelope)
                if isinstance(semantic_envelope, dict)
                else self.semantic_decision(query)
            )
            model_hint = self._hint_from_semantic_decision(decision)
        else:
            model_hint = {}
        # Camera is a sensor in r14, not a second desktop. A normal desktop task suspends
        # capture first so OpenCV never competes with the agent/ASR for resources.
        try:
            camera = self.services.camera
            if camera is not None and camera.status().get("running") and "камер" not in self._norm(query):
                camera.stop()
                self._trace("CAMERA_AUTO_SUSPEND_FOR_DESKTOP", query=query)
        except Exception:
            pass
        # Resume from a typed checkpoint. A free-form answer to a clarification is not
        # the same protocol as approval of an exact risky action or "ready" after auth.
        pending = None
        pending_clarification = False
        if conversation_id:
            try:
                candidate = self.services.db.get_setting(self._pending_key(conversation_id), None)
            except Exception:
                candidate = None
            if isinstance(candidate, dict) and (
                candidate.get("cleared")
                or str(candidate.get("state") or "") in {
                    TaskState.CANCELLED.value, TaskState.COMPLETED.value, TaskState.FAILED.value,
                }
            ):
                candidate = None
            if isinstance(candidate, dict) and candidate.get("goals"):
                checkpoint_expires = float(candidate.get("expires_at") or 0.0)
                if candidate.get("clarification") and checkpoint_expires and time.time() > checkpoint_expires:
                    self._clear_pending(conversation_id, str(candidate.get("run_id") or ""))
                    candidate = None
            if isinstance(candidate, dict) and candidate.get("goals"):
                if is_cancel_confirmation(query):
                    self._clear_pending(conversation_id, str(candidate.get("run_id") or ""))
                    self._trace("UNIVERSAL_CHECKPOINT_CANCEL", query=query)
                    return WorkflowResult(False, "Отменила ожидающий шаг. Ничего не подтверждала и не продолжала.", [])
                pending_clarification = bool(candidate.get("clarification"))
                approval_pending = bool(
                    candidate.get("risk_confirmation_fingerprint")
                    or candidate.get("confirmation_step_id")
                )
                if pending_clarification and not is_cancel_confirmation(query):
                    pending = candidate
                elif approval_pending and (
                    is_affirmative_confirmation(query) or is_resume_confirmation(query)
                ):
                    pending = candidate
                elif not approval_pending and is_resume_confirmation(query):
                    pending = candidate
        if isinstance(pending, dict) and pending.get("goals"):
            run_id = str(pending.get("run_id") or uuid.uuid4().hex)
            task = str(pending.get("task") or query)
            goals = list(pending.get("goals") or [])
            plan_id = str(pending.get("plan_id") or "")
            start_index = int(pending.get("index") or 0)
            results = list(pending.get("results") or [])
            replans = int(pending.get("replans") or 0)
            confirmed_steps = {str(item) for item in (pending.get("confirmed_steps") or []) if str(item)}
            confirmation_step_id = str(pending.get("confirmation_step_id") or "")
            approved_reply = is_affirmative_confirmation(query) or is_resume_confirmation(query)
            if confirmation_step_id and approved_reply:
                confirmed_steps.add(confirmation_step_id)
            risk_fingerprint = str(pending.get("risk_confirmation_fingerprint") or "")
            risk_not_expired = time.time() <= float(pending.get("expires_at") or 0.0)
            if risk_fingerprint and risk_not_expired and approved_reply and 0 <= start_index < len(goals):
                confirmed_tool = str(pending.get("risk_confirmation_tool") or "")
                confirmed_arguments = pending.get("risk_confirmation_arguments")
                confirmed_surface = pending.get("risk_confirmation_surface")
                # A token without its exact typed payload is not resumable approval.
                if confirmed_tool and isinstance(confirmed_arguments, dict):
                    goals[start_index]["confirmed_action"] = risk_fingerprint
                    goals[start_index]["confirmed_tool"] = confirmed_tool
                    goals[start_index]["confirmed_arguments"] = dict(confirmed_arguments)
                    goals[start_index]["confirmed_surface"] = (
                        dict(confirmed_surface) if isinstance(confirmed_surface, dict) else {}
                    )
                else:
                    for key in ("confirmed_action", "confirmed_tool", "confirmed_arguments", "confirmed_surface"):
                        goals[start_index].pop(key, None)
            elif 0 <= start_index < len(goals):
                for key in ("confirmed_action", "confirmed_tool", "confirmed_arguments", "confirmed_surface"):
                    goals[start_index].pop(key, None)
            if pending_clarification and 0 <= start_index < len(goals):
                clarification_prompt = str(pending.get("clarification_prompt") or pending.get("prompt") or "").strip()
                clarification_answer = str(query or "").strip()
                task = (
                    f"{task}\n\nУточняющий вопрос Эрви: {clarification_prompt}\n"
                    f"Ответ владельца: {clarification_answer}"
                ).strip()
                goals[start_index]["goal"] = task
                saved_semantic_hint = goals[start_index].get("_semantic_hint")
                if (
                    model_only
                    and isinstance(saved_semantic_hint, dict)
                    and str(saved_semantic_hint.get("action") or "").startswith("organizer_")
                ):
                    # The follow-up supplies a missing semantic slot (usually date or
                    # time). Re-run the same typed envelope over the original request,
                    # the exact question and the owner's answer. This is model parsing,
                    # not a date regex or scenario-specific command router.
                    refreshed = self.semantic_decision(task)
                    if (
                        str(refreshed.get("decision_status") or "") == "ok"
                        and str(refreshed.get("action") or "")
                        == str(saved_semantic_hint.get("action") or "")
                    ):
                        goals[start_index]["_semantic_hint"] = (
                            self._hint_from_semantic_decision(refreshed)
                        )
                missing_fields = [
                    str(item).strip() for item in (pending.get("missing_fields") or [])
                    if str(item).strip()
                ]
                owner_slots = {
                    str(key): str(value).strip()
                    for key, value in dict(goals[start_index].get("owner_slots") or {}).items()
                    if str(key).strip() and str(value).strip()
                }
                clarification_kind = str(pending.get("clarification_kind") or "")
                if clarification_kind == "travel_owner":
                    owner_slots.update(self._travel_answer_slots(clarification_answer, missing_fields))
                elif len(missing_fields) == 1 and clarification_answer:
                    # Preserve the answer as a typed owner slot. Otherwise a short
                    # provider name can lose the category carried by the question.
                    owner_slots[missing_fields[0]] = clarification_answer
                if owner_slots:
                    goals[start_index]["owner_slots"] = owner_slots
                if pending.get("clarification_kind"):
                    goals[start_index]["clarification_kind"] = str(pending.get("clarification_kind"))
                for key in ("confirmed_action", "confirmed_tool", "confirmed_arguments", "confirmed_surface"):
                    goals[start_index].pop(key, None)
            self._trace("UNIVERSAL_RESUME", task=task, index=start_index, replans=replans)
        else:
            run_id = uuid.uuid4().hex
            task = query
            # Action ownership is no longer split between a regex fast lane and the
            # general engine. Atomic accelerators are capabilities the reactive policy
            # may call, so their failure cannot terminate or hijack the whole request.
            # Reactive v2 owns the complete natural-language goal.  It does not buy an
            # ungrounded 1..10 step JSON plan before seeing the desktop: the model chooses
            # exactly one typed action from the current observation, acts, re-observes and
            # continues in the same context.  This is also what makes new services and UI
            # changes survivable instead of adding another regex recipe.
            if model_only:
                # The model-only front door must preserve the owner's utterance as-is.
                # No lexical intent splitter, app whitelist, or route recipe is allowed
                # to manufacture a different task before the reactive planner sees it.
                goals = [{
                    "goal": str(query or "").strip(),
                    "mode": "auto",
                    "text": "",
                    "success": "Исходная цель владельца достигнута и подтверждена свежим независимым наблюдением",
                    "reactive_v2": True,
                    "_semantic_hint": dict(model_hint),
                }]
                plan_id = uuid.uuid4().hex
            else:
                goals = self._fallback_goals(query)
                for goal_spec in goals:
                    goal_spec["reactive_v2"] = True
                    goal_spec["success"] = "Исходная цель владельца достигнута и подтверждена свежим независимым наблюдением"
                self._trace("REACTIVE_V2_GOAL", query=query, goals=goals)
                if goals and not all(isinstance(item, dict) and item.get("plan_step_id") for item in goals):
                    protocol = deterministic_plan(task, goals)
                    plan_id = protocol.plan_id
                    goals = protocol.executor_specs()
                    for goal_spec in goals:
                        goal_spec["reactive_v2"] = True
                else:
                    plan_id = str((goals[0] if goals else {}).get("plan_id") or "")
                goals = self._prepare_executor_specs(task, goals)
            start_index = 0
            replans = 0
            results: list[dict[str, Any]] = []
            confirmed_steps: set[str] = set()
            if conversation_id and goals:
                self._save_pending(conversation_id, {
                    "task": task, "plan_id": plan_id, "goals": goals, "index": 0, "results": [], "replans": 0,
                    "state": TaskState.RUNNING.value, "confirmed_steps": [], "saved_at": time.time(), "run_id": run_id,
                }, replace=True)

        # Route questions are not actionable until the owner supplies the route slots
        # that materially change the answer. Ask once, before any desktop observer, and
        # retain the typed checkpoint for a free-form follow-up. This keeps a foreground
        # media tab from hijacking an under-specified travel request and prevents long
        # observer loops with no possible route to a verified result.
        pending_travel_guard = bool(
            pending_clarification
            and isinstance(pending, dict)
            and (
                str(pending.get("clarification_kind") or "") == "travel_owner"
                or re.search(
                    r"(?:точку\s+отправления|способ\s+передвижения|параметр\w*\s+маршрут)",
                    str(pending.get("clarification_prompt") or ""),
                    re.I,
                )
            )
        )
        # In model-only mode the admission hint is the typed authority for a route
        # request.  Apply the same slot guard there; never fabricate a duration from
        # a conversational answer when origin/transport are still missing.
        route_hint_authorized = bool(model_only and str(model_hint.get("action") or "") == "route_search")
        if (not model_only or route_hint_authorized) and goals and 0 <= start_index < len(goals) and (not pending_clarification or pending_travel_guard):
            goal_owner_slots = (
                dict(goals[start_index].get("owner_slots") or {})
                if isinstance(goals[start_index].get("owner_slots"), dict) else {}
            )
            travel_missing = self._travel_missing_fields(task, goal_owner_slots)
            if travel_missing:
                clarification_prompt = self._travel_clarification_prompt(travel_missing)
                if conversation_id:
                    self._save_pending(conversation_id, {
                        "task": task, "plan_id": plan_id, "goals": goals, "index": start_index,
                        "results": results, "replans": replans,
                        "state": TaskState.WAITING_USER.value, "confirmed_steps": sorted(confirmed_steps),
                        "clarification": True, "clarification_prompt": clarification_prompt,
                        "clarification_kind": "travel_owner", "missing_fields": travel_missing,
                        "prompt": clarification_prompt, "saved_at": time.time(),
                        "expires_at": time.time() + 300.0, "run_id": run_id,
                    })
                self._runtime_step(
                    "Жду владельца: " + clarification_prompt,
                    stage="waiting_user", index=start_index + 1, total=len(goals),
                )
                self._trace(
                    "UNIVERSAL_WAIT_USER", task=task, index=start_index + 1,
                    prompt=clarification_prompt,
                )
                return WorkflowResult(
                    False, clarification_prompt, results,
                    needs_user=True, prompt=clarification_prompt,
                )

        for index in range(start_index, len(goals)):
            if stop_event and stop_event.is_set():
                if conversation_id:
                    self._clear_pending(conversation_id, run_id)
                return WorkflowResult(False, "Остановила текущую задачу. Ожидающий шаг завершён и сам не возобновится.", results)
            spec = goals[index]
            step_id = str(spec.get("plan_step_id") or f"step_{index + 1}")
            if bool(spec.get("requires_confirmation")) and step_id not in confirmed_steps:
                prompt = f"Подтверди внешний шаг: {str(spec.get('goal') or task).strip()[:360]}"
                if conversation_id:
                    self._save_pending(conversation_id, {
                        "task": task, "plan_id": plan_id, "goals": goals, "index": index,
                        "results": results, "replans": replans,
                        "state": TaskState.WAITING_USER.value,
                        "confirmation_step_id": step_id,
                        "confirmed_steps": sorted(confirmed_steps),
                        "prompt": prompt, "saved_at": time.time(), "run_id": run_id,
                    })
                self._runtime_step("Жду подтверждения владельца", stage="waiting_user", index=index + 1, total=len(goals))
                return WorkflowResult(False, prompt + ". Скажи «да, продолжай» или «отмена». Причём подтверждение относится только к этому шагу.", results, needs_user=True, prompt=prompt)
            self._runtime_step(
                f"Этап {index + 1}/{len(goals)}: {str(spec.get('goal') or '')[:180]}",
                stage="desktop_agent", index=index + 1, total=len(goals), goal=spec.get("goal"),
            )
            self._trace("UNIVERSAL_GOAL_BEGIN", task=task, index=index+1, goal=spec)
            try:
                # UI work from every engine is serialized on the same physical-desktop
                # lock.  A stale model response can no longer click into a newer task.
                with self._desktop_lock:
                    if stop_event and stop_event.is_set():
                        result = {"ok": False, "cancelled": True, "completed": False,
                                  "answer": "Остановлено пользователем.", "verified": False}
                    else:
                        result = self._execute_goal(spec, execute_direct, stop_event=stop_event)
            except TaskNeedsUser as exc:
                agent_outcome = self.services.agent.last_run_outcome() if hasattr(self.services.agent, "last_run_outcome") else {}
                checkpoint = {"task": task, "plan_id": plan_id, "goals": goals, "index": index, "results": results, "prompt": exc.prompt, "replans": replans, "confirmed_steps": sorted(confirmed_steps), "state": TaskState.WAITING_USER.value, "saved_at": time.time(), "run_id": run_id}
                if agent_outcome.get("needs_confirmation"):
                    # Drop a consumed/stale approval payload before writing the newly
                    # grounded confirmation checkpoint.
                    for key in ("confirmed_action", "confirmed_tool", "confirmed_arguments", "confirmed_surface"):
                        goals[index].pop(key, None)
                    checkpoint["risk_confirmation_fingerprint"] = str(agent_outcome.get("confirmation_fingerprint") or "")
                    checkpoint["risk_confirmation_summary"] = str(agent_outcome.get("confirmation_summary") or "")
                    checkpoint["risk_confirmation_tool"] = str(agent_outcome.get("confirmation_tool") or "")
                    checkpoint["risk_confirmation_arguments"] = dict(agent_outcome.get("confirmation_arguments") or {})
                    checkpoint["risk_confirmation_surface"] = dict(agent_outcome.get("confirmation_surface") or {})
                    checkpoint["created_at"] = float(agent_outcome.get("confirmation_created_at") or time.time())
                    checkpoint["expires_at"] = time.time() + 120.0
                if agent_outcome.get("clarification"):
                    checkpoint["clarification"] = True
                    checkpoint["clarification_prompt"] = str(agent_outcome.get("clarification_prompt") or exc.prompt)
                    checkpoint["clarification_kind"] = str(agent_outcome.get("clarification_kind") or "")
                    checkpoint["missing_fields"] = [
                        str(item).strip() for item in (agent_outcome.get("missing_fields") or [])
                        if str(item).strip()
                    ]
                    checkpoint["allow_free_text"] = bool(agent_outcome.get("allow_free_text"))
                    checkpoint["created_at"] = time.time()
                    checkpoint["expires_at"] = time.time() + 300.0
                if conversation_id:
                    self._save_pending(conversation_id, checkpoint)
                self._runtime_step("Жду владельца: " + exc.prompt[:220], stage="waiting_user", index=index + 1, total=len(goals))
                self._trace("UNIVERSAL_WAIT_USER", task=task, index=index+1, prompt=exc.prompt)
                if agent_outcome.get("needs_confirmation") or agent_outcome.get("clarification"):
                    # A grounded risk confirmation expects an explicit yes/no, while a
                    # natural clarification expects the user's actual free-form answer.
                    # Neither should be rewritten into the legacy "say ready" protocol.
                    answer = exc.prompt
                else:
                    answer = exc.prompt + " После этого скажи «готово»."
                return WorkflowResult(False, answer, results, needs_user=True, prompt=exc.prompt)
            if (stop_event and stop_event.is_set()) or result.get("cancelled"):
                result = {**result, "ok": False, "verified": False, "cancelled": True, "answer": "Остановлено пользователем."}
            # One central deny-by-default postcondition gate.  Executors may attempt an
            # action, but only independently observable evidence may promote it to ok.
            if result.get("completed"):
                verifier = getattr(self.services, "verifier", None)
                explicitly_verified = bool(result.get("verified"))
                if verifier is not None:
                    try:
                        explicitly_verified = explicitly_verified or bool(verifier.explicit_verified(result))
                    except Exception:
                        pass
                result["verified"] = bool(explicitly_verified)
                if result.get("ok") and not explicitly_verified:
                    result["ok"] = False
                    result.setdefault("error", "Изменение выполнено, но независимое постусловие отсутствует")
            results.append(result)
            self._trace("UNIVERSAL_GOAL_END", task=task, index=index+1, ok=result.get("ok"), result=result)
            if result.get("needs_user"):
                prompt = str(result.get("prompt") or result.get("answer") or "Проверь результат на экране и скажи «готово».").strip()
                if conversation_id:
                    self._save_pending(conversation_id, {
                        "task": task, "plan_id": plan_id, "goals": goals, "index": index,
                        "results": results, "replans": replans,
                        "state": TaskState.WAITING_USER.value, "clarification": True,
                        "clarification_prompt": prompt, "clarification_kind": "owner_check",
                        "missing_fields": [], "allow_free_text": True,
                        "prompt": prompt, "saved_at": time.time(),
                        "expires_at": time.time() + 300.0, "run_id": run_id,
                    })
                self._runtime_step("Жду владельца: " + prompt[:220], stage="waiting_user", index=index + 1, total=len(goals))
                return WorkflowResult(False, prompt, results, needs_user=True, prompt=prompt)
            if result.get("cancelled"):
                if conversation_id:
                    self._clear_pending(conversation_id, run_id)
                self._runtime_step("Остановлено пользователем", stage="desktop_agent_cancelled", index=index + 1, total=len(goals))
                return WorkflowResult(False, "Остановила текущую задачу.", results)
            if result.get("ok"):
                self._runtime_step(f"Этап {index + 1}/{len(goals)} подтверждён", stage="desktop_agent_verified", index=index + 1, total=len(goals))
                if conversation_id and index + 1 < len(goals):
                    self._save_pending(conversation_id, {
                        "task": task, "plan_id": plan_id, "goals": goals, "index": index + 1, "results": results, "replans": replans,
                        "state": TaskState.RUNNING.value, "confirmed_steps": sorted(confirmed_steps), "saved_at": time.time(), "run_id": run_id,
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
                # Several genuinely different plans are allowed, each from fresh desktop state.
                # The bound prevents infinite clicking while still surviving slow apps/modals.
                if not spec.get("reactive_v2") and replans < self.MAX_REPLANS and not model_timed_out:
                    observation = self.tools.execute("desktop_state", {})
                    current_state = self._state_context(max_chars=5200)
                    completed_steps = [str(item.get("goal") or "") for item in goals[:index] if item]
                    backlog = [str(item.get("goal") or "") for item in goals[index + 1:] if item]
                    repair_query = (
                        f"Исходная задача: {task}\nНе удалось выполнить промежуточную цель: {spec.get('goal')}\n"
                        f"Ошибка/отчёт: {result.get('answer') or result.get('error')}\n"
                        f"Выполненные этапы: {json.dumps(completed_steps, ensure_ascii=False)}\n"
                        f"Оставшийся backlog: {json.dumps(backlog, ensure_ascii=False)}\n"
                        f"Наблюдение desktop_state: {json.dumps(observation, ensure_ascii=False, default=str)[:5000]}\n"
                        f"Текущее окно и accessibility tree:\n{current_state}\n"
                        f"Перепланирование {replans + 1}/{self.MAX_REPLANS}: продолжи именно с текущего состояния, предложи только оставшиеся цели; используй новое наблюдение и не повторяй уже провалившийся подход."
                    )
                    self._trace(
                        "UNIVERSAL_REPLAN_CONTEXT", plan_id=plan_id, failed_step=spec.get("plan_step_id"),
                        error=result.get("answer") or result.get("error"), completed=completed_steps,
                        backlog=backlog, observation=observation,
                    )
                    replacement = self.plan(repair_query)
                    if replacement and not self._is_planner_fallback(replacement):
                        for replacement_spec in replacement:
                            replacement_spec["original_task"] = task
                        goals = goals[:index+1] + replacement
                        replans += 1
                        if conversation_id:
                            self._save_pending(conversation_id, {"task":task,"plan_id":plan_id,"goals":goals,"index":index+1,"results":results,"replans":replans,"confirmed_steps":sorted(confirmed_steps),"state":TaskState.REPLANNING.value,"saved_at":time.time(),"run_id":run_id})
                            return self.execute_task("готово", execute_direct, conversation_id=conversation_id, stop_event=stop_event)
                break

        if conversation_id:
            self._clear_pending(conversation_id, run_id)
        done = sum(1 for r in results if r.get("ok"))
        completed = sum(1 for r in results if r.get("completed"))
        total = len(goals)
        ok = bool(total and done == total)
        if ok:
            facts = [str(item.get("answer") or "").strip() for item in results if item.get("ok") and str(item.get("answer") or "").strip()]
            summary = (facts[-1] if facts else f"Результат подтверждён по {done} из {total} постусловий.")
        elif done:
            summary = f"Подтверждены {done} этапов из {total}; следующий результат не удалось надёжно подтвердить."
        elif completed:
            summary = "Команда была отправлена один раз, но постусловие не подтвердилось; повтор заблокирован, чтобы не дублировать действие."
        else:
            summary = self._useful_fallback(task)
        self._trace("UNIVERSAL_FINAL", plan_id=plan_id, state=(TaskState.COMPLETED.value if ok else TaskState.PARTIAL.value if done or completed else TaskState.FAILED.value), verified_steps=done, completed_unverified=completed, total=total)
        return WorkflowResult(ok, summary, results)

    _ACTION_VERBS = (
        "выключ", "включ", "открой", "откр", "закрой", "запусти", "запуст", "останов",
        "поставь", "нажми", "кликни", "введи", "напиши", "создай", "сделай", "сгенерир",
        "сохрани", "удали", "переимен", "перемест", "скопир", "отправь", "ответь",
        "запомни", "забудь", "покажи", "найди файл", "смонтируй", "склей", "обрежь",
        "конвертир", "увеличь", "уменьш", "переведи", "установи", "обнови", "добавь",
        "напомни", "позвони", "включи музыку", "собери", "закажи", "купи",
    )

    def _search_links(self, query: str, limit: int = 4) -> str:
        """Материалы по теме. Раньше поиск стоял только в ветке вопросов,
        поэтому неудачное действие оставляло человека вообще без помощи."""
        try:
            search = self.tools.execute("web_search", {"query": query, "max_results": limit})
            payload = (search.get("result") or {}) if isinstance(search, dict) else {}
            items = payload.get("results") or payload.get("items") or []
            lines = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                body = str(item.get("body") or item.get("snippet") or "").strip()
                href = str(item.get("href") or item.get("url") or "").strip()
                if not title and not body:
                    continue
                piece = title or body[:120]
                if href:
                    piece = f"{piece} — {href}"
                lines.append("• " + piece[:300])
            return "\n".join(lines)
        except Exception:
            return ""

    def _looks_like_action(self, task: str) -> bool:
        low = " ".join(str(task or "").casefold().replace("ё", "е").split())
        return any(verb in low for verb in self._ACTION_VERBS)

    def _useful_fallback(self, task: str) -> str:
        """Return something the person can use when execution could not be verified.

        Refusing to claim unverified success is right, but what helps next depends
        on what was asked. For a question, looking it up is genuinely useful. For an
        action -- shut down, open, save, remember, show -- search results are not a
        partial result, they are a non-sequitur that reads as "she googles instead of
        doing", so say plainly what failed instead.
        """
        query = str(task or "").strip()
        # Формулировка без оправданий: «не смогла надёжно подтвердить» звучит как
        # отговорка, хотя поведение правильное — не выдавать неудачу за успех.
        # Спокойная констатация плюс следующий шаг читается как надёжность.
        honest = "Результат не подтвердился, поэтому не отмечаю задачу выполненной."
        if not query:
            return honest
        try:
            # Remember what failed so "обучи выполнению" needs no repetition of the goal.
            self.services.db.set_setting("last_failed_goal", query[:300])
        except Exception:
            pass
        if self._looks_like_action(query):
            # Даже когда действие не вышло, человек не должен остаться ни с чем:
            # ссылки по теме часто решают вопрос быстрее, чем повторная попытка.
            links = self._search_links(query, limit=3)
            tail = ("Нажми «Обучить выполнению», чтобы показать мне нужные шаги."
                    if not links else
                    "Нажми «Обучить выполнению», чтобы показать мне шаги.\n\n"
                    "А пока — что нашла по теме:\n" + links)
            return f"{honest}\n\n{tail}"
        findings = self._search_links(query, limit=4)
        if findings:
            findings = "Нашла в интернете:\n" + findings
        if not findings:
            return f"{honest}\n\nПопробуй переформулировать — я не нашла, за что зацепиться."
        return f"{honest}\n\n{findings}"

    # Backward-compatible wrapper used by old tests/call sites.
    def execute_compound(self, query: str, execute_direct: Any, *, stop_event: threading.Event | None = None) -> WorkflowResult:
        return self.execute_task(query, execute_direct, stop_event=stop_event)
