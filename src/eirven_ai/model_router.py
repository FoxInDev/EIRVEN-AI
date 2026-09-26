from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .hardware import HardwareProfile
from .llm import ModelGateway
from .release_policy import TEXT_MODEL, VISION_MODEL


@dataclass(slots=True)
class ModelRoute:
    model: str
    think: bool
    num_ctx: int
    num_predict: int
    temperature: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "think": self.think,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
            "temperature": self.temperature,
            "reason": self.reason,
        }


class ModelRouter:
    COMPLEX_MARKERS = re.compile(
        r"\b(архитектур|спроектир|проанализир|сравни|докажи|рассчитай|"
        r"сложн|подробн|план|стратег|исслед|рефактор|отлад|ошибк|тест)\w*",
        re.IGNORECASE,
    )
    CODE_MARKERS = re.compile(
        r"\b(код|python|проект|приложен|api|база|fastapi|тест|git|docker|"
        r"файл|функц|класс|баг|ошибк|разработ)\w*",
        re.IGNORECASE,
    )
    DEEP_MARKERS = re.compile(
        r"\b(?:максимально\s+подробно|"
        r"глубок\w*\s+(?:анализ\w*|рассужд\w*|исследован\w*)|"
        r"глубоко\s+(?:проанализ\w*|подумай|разбери)|"
        r"подумай\s+(?:глубоко|тщательно|подольше)|"
        r"тщательно\s+(?:проанализ\w*|исследуй|разбери)|"
        r"включи\s+(?:режим\s+)?thinking|с\s+(?:подробным\s+)?рассуждением)\b",
        re.IGNORECASE,
    )
    SIMPLE_CHAT = re.compile(
        r"^\s*(привет|здравствуй|здравствуйте|доброе утро|добрый день|добрый вечер|"
        r"как дела|ты тут|готов|спасибо|ок|понял|ясно|пока)[!?. ,]*$",
        re.IGNORECASE,
    )
    PROJECT_STATUS_CHAT = re.compile(
        r"\b(как там|что с|статус|как дела у|что происходит с|почему .* завис|"
        r"где результат|сколько осталось|что ты сейчас делаешь).*\b(проект|задач|сборк|work)\b|"
        r"\b(проект|задач|сборк|work)\b.*\b(как там|статус|завис|готов|прогресс|осталось)\b",
        re.IGNORECASE,
    )

    _EXPLICIT_ITEMS = re.compile(
        r"\b(?:ровно\s+|список(?:а|\s+из)?\s+|дай\s+|напиши\s+)?"
        r"(\d{1,4})\s+"
        r"(?:(?:коротк|кратк|нумерован|подробн|отдельн|разн)\w*\s+){0,4}"
        r"(?:пункт|способ|идей|вариант|пример|шаг|строк)\w*\b",
        re.IGNORECASE,
    )
    _EXPLICIT_WORDS = re.compile(r"\b(\d{2,5})\s+слов\w*\b", re.IGNORECASE)
    _LONG_OUTPUT = re.compile(
        r"\b(?:не\s+сокращай|без\s+сокращений|максимально\s+подробно|"
        r"исчерпывающ\w*|развёрнут\w*|развернут\w*|полн\w*\s+ответ)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        settings: Settings,
        gateway: ModelGateway,
        hardware: HardwareProfile,
    ):
        self.settings = settings
        self.gateway = gateway
        self.hardware = hardware
        self._installed_cache: tuple[float, list[str]] | None = None

    def installed(self) -> list[str]:
        now = __import__("time").monotonic()
        cached = self._installed_cache
        if cached is not None and now - cached[0] < 30.0:
            return list(cached[1])
        method = getattr(self.gateway, "installed_models", None)
        values = method() if callable(method) else self.gateway.models()
        self._installed_cache = (now, list(values or []))
        return list(values or [])

    def adapt_output_budget(self, query: str, route: ModelRoute) -> ModelRoute:
        """Size output to the requested artifact, while keeping ordinary chat cheap.

        The old fixed 384/1536 ceilings cut an explicitly requested 80-item answer at
        item 71.  This estimator raises the ceiling only when the owner requested a
        measurable/long artifact; ``task_num_predict`` remains the hard local cap.
        """
        base = max(64, int(route.num_predict))
        hard_cap = max(base, int(self.settings.task_num_predict))
        wanted = base
        item_match = self._EXPLICIT_ITEMS.search(str(query or ""))
        if item_match:
            count = max(1, min(int(item_match.group(1)), 1000))
            wanted = max(wanted, 192 + count * 32)
        word_match = self._EXPLICIT_WORDS.search(str(query or ""))
        if word_match:
            words = max(1, min(int(word_match.group(1)), 20_000))
            wanted = max(wanted, 160 + words * 2)
        if self._LONG_OUTPUT.search(str(query or "")):
            wanted = max(wanted, min(hard_cap, max(1024, base * 2)))
        route.num_predict = min(hard_cap, wanted)
        if route.num_predict > base:
            # Reserve context for both prompt/history and the requested output.
            route.num_ctx = min(
                int(self.settings.task_num_ctx),
                max(int(route.num_ctx), min(int(self.settings.task_num_ctx), route.num_predict + 3072)),
            )
            route.reason += f"; адаптивный бюджет ответа {route.num_predict} токенов"
        return route

    @staticmethod
    def _base(model: str) -> str:
        return model.split(":", 1)[0]

    @staticmethod
    def _fast_candidates(configured: str) -> list[str]:
        return [TEXT_MODEL, configured]

    def _choose_existing(self, candidates: list[str]) -> str:
        installed = self.installed()
        if not installed:
            return candidates[0]
        exact = {name.lower(): name for name in installed}
        for candidate in candidates:
            if candidate.lower() in exact:
                return exact[candidate.lower()]
        for candidate in candidates:
            base = self._base(candidate).lower()
            for item in installed:
                if self._base(item).lower() == base:
                    return item
        # The configured model may not yet be downloaded; returning it gives a useful Ollama error.
        return candidates[0]

    def chat_route(self, query: str, explicit_model: str | None = None) -> ModelRoute:
        if self.settings.strict_release_model:
            complex_task = bool(self.COMPLEX_MARKERS.search(query)) or len(query) > 900
            deep_requested = bool(self.DEEP_MARKERS.search(query))
            low_memory = bool(self.hardware.ram_gb and self.hardware.ram_gb < 24)
            task_ctx = min(self.settings.task_num_ctx, 8192 if low_memory else 16384)
            selected = self._choose_existing(
                [self.hardware.recommended_main_model, self.settings.model]
                if (complex_task or deep_requested)
                else [self.hardware.recommended_fast_model, self.settings.fast_model, self.settings.model]
            )
            return ModelRoute(
                model=selected,
                think=deep_requested,
                num_ctx=task_ctx if complex_task else min(self.settings.chat_num_ctx, 3072),
                num_predict=min(self.settings.task_num_predict, 1536) if complex_task else self.settings.chat_num_predict,
                temperature=0.45 if complex_task else 0.68,
                reason=("Глубокий режим на основной модели" if deep_requested else
                        "Основная модель для сложной задачи" if complex_task else
                        "Адаптивная быстрая модель без скрытого thinking"),
            )
        if explicit_model and explicit_model not in {"auto", "Автоматически"}:
            complex_task = bool(self.COMPLEX_MARKERS.search(query))
            deep_requested = bool(self.DEEP_MARKERS.search(query))
            return ModelRoute(
                model=explicit_model,
                think=deep_requested and "instruct" not in explicit_model.lower(),
                num_ctx=self.settings.task_num_ctx if complex_task else self.settings.chat_num_ctx,
                num_predict=(
                    self.settings.task_num_predict
                    if complex_task
                    else self.settings.chat_num_predict
                ),
                temperature=0.55 if complex_task else 0.72,
                reason=(
                    "Модель выбрана пользователем; глубокое рассуждение запрошено явно"
                    if deep_requested else "Модель выбрана пользователем; быстрый режим"
                ),
            )

        if self.SIMPLE_CHAT.match(query):
            selected = self._choose_existing(
                self._fast_candidates(self.settings.fast_model)
                + [self.hardware.recommended_fast_model, self.settings.model]
            )
            return ModelRoute(
                model=selected,
                think=False,
                num_ctx=min(self.settings.chat_num_ctx, 4096),
                num_predict=96,
                temperature=0.55,
                reason="Короткая реплика: мгновенный режим без рассуждений",
            )

        if self.PROJECT_STATUS_CHAT.search(query):
            selected = self._choose_existing(
                self._fast_candidates(self.settings.fast_model)
                + [self.hardware.recommended_fast_model, self.settings.model]
            )
            return ModelRoute(
                model=selected,
                think=False,
                num_ctx=self.settings.chat_num_ctx,
                num_predict=min(self.settings.chat_num_predict, 320),
                temperature=0.55,
                reason="Вопрос о состоянии текущей работы: быстрый разговорный ответ",
            )

        is_code = bool(re.search(
            r"\b(напиши|реализуй|исправь|почини|рефактор|добавь|измени|сгенерируй)\w*.{0,80}"
            r"\b(код|функц|класс|модул|файл|python|api|тест)\w*|"
            r"\b(код|python|функц|класс|traceback|stack trace)\b",
            query, re.IGNORECASE | re.DOTALL
        ))
        is_complex = bool(self.COMPLEX_MARKERS.search(query)) or len(query) > 900
        if is_code:
            # On small GPUs interactive code discussion must not evict the resident fast
            # model. Heavy project/code execution is handled by background task routing.
            if self.hardware.vram_gb and self.hardware.vram_gb <= 6.0:
                selected = self._choose_existing(
                    self._fast_candidates(self.settings.fast_model)
                    + [self.hardware.recommended_fast_model, self.settings.model]
                )
                return ModelRoute(
                    model=selected, think=False, num_ctx=min(self.settings.chat_num_ctx, 3072),
                    num_predict=min(self.settings.chat_num_predict, 384), temperature=0.32,
                    reason="Интерактивный код на малой VRAM: быстрый резидентный контур",
                )
            selected = self._choose_existing([self.settings.code_model, self.hardware.recommended_code_model, self.settings.model])
            return ModelRoute(
                model=selected,
                think=(bool(self.DEEP_MARKERS.search(query)) and "instruct" not in selected.lower()),
                num_ctx=self.settings.task_num_ctx,
                num_predict=min(self.settings.task_num_predict, 1536),
                temperature=0.25,
                reason="Запрос связан с кодом или проектом",
            )
        if is_complex:
            deep_requested = bool(self.DEEP_MARKERS.search(query))
            selected = self._choose_existing(
                ([self.settings.deep_model] if deep_requested else [])
                + [self.settings.model, self.hardware.recommended_main_model]
            )
            return ModelRoute(
                model=selected,
                think=False,
                num_ctx=self.settings.task_num_ctx,
                num_predict=min(self.settings.task_num_predict, 2048),
                temperature=0.5,
                reason=("Глубокий режим с Thinking" if deep_requested else "Сложная задача без лишнего скрытого thinking"),
            )
        selected = self._choose_existing(
            self._fast_candidates(self.settings.fast_model)
            + [self.hardware.recommended_fast_model, self.settings.model]
        )
        return ModelRoute(
            model=selected,
            think=False,
            num_ctx=self.settings.chat_num_ctx,
            num_predict=self.settings.chat_num_predict,
            temperature=0.72,
            reason="Обычный разговор: быстрый режим без скрытого рассуждения",
        )

    def agent_model(self, query: str) -> str:
        """Choose planner model; official release never downgrades text intelligence by hardware."""
        if self.settings.strict_release_model:
            return self.settings.model
        code_heavy = bool(re.search(r"\b(напиши код|исправь код|рефактор|реализуй функц|создай файл с кодом)\w*", query, re.IGNORECASE))
        if code_heavy:
            return self._choose_existing([self.settings.code_model, self.settings.model])
        candidates = self._fast_candidates(self.settings.fast_model) + [
            self.hardware.recommended_fast_model,
            self.settings.model,
        ]
        return self._choose_existing(candidates)

    def task_model(self, kind: str) -> str:
        if self.settings.strict_release_model and kind not in {"vision", "screen", "image"}:
            return self.settings.model
        if kind == "project":
            return self._choose_existing(
                [
                    self.settings.code_model,
                    self.hardware.recommended_code_model,
                    self.settings.model,
                ]
            )
        if kind in {"vision", "screen", "image"}:
            # Textual UI trees stay on DeepSeek-Coder-V2; Qwen3-VL is loaded only
            # for a real screenshot, scan or image attachment.
            return self._choose_existing([
                self.settings.vision_model,
                self.hardware.recommended_vision_model,
                VISION_MODEL,
            ])
        return self._choose_existing(
            [self.settings.model, self.hardware.recommended_main_model]
        )
