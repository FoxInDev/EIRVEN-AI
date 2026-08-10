from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .hardware import HardwareProfile
from .llm import ModelGateway


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
        r"\b(максимально подробно|глубокий анализ|глубоко проанализ|исследование|"
        r"сложная архитектура|стратегия|доказательство)\b",
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

    def __init__(
        self,
        settings: Settings,
        gateway: ModelGateway,
        hardware: HardwareProfile,
    ):
        self.settings = settings
        self.gateway = gateway
        self.hardware = hardware

    def installed(self) -> list[str]:
        method = getattr(self.gateway, "installed_models", None)
        return method() if callable(method) else self.gateway.models()

    @staticmethod
    def _base(model: str) -> str:
        return model.split(":", 1)[0]

    @staticmethod
    def _fast_candidates(configured: str) -> list[str]:
        # The generic qwen3:4b tag can still emit a long internal draft. Prefer the
        # final-answer-only instruct build when it is installed.
        if configured.lower() in {"qwen3:4b", "qwen3:latest"}:
            return ["qwen3:4b-instruct", configured]
        return [configured, "qwen3:4b-instruct"]

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
        if explicit_model and explicit_model not in {"auto", "Автоматически"}:
            complex_task = bool(self.COMPLEX_MARKERS.search(query))
            return ModelRoute(
                model=explicit_model,
                think=complex_task and "instruct" not in explicit_model.lower(),
                num_ctx=self.settings.task_num_ctx if complex_task else self.settings.chat_num_ctx,
                num_predict=(
                    self.settings.task_num_predict
                    if complex_task
                    else self.settings.chat_num_predict
                ),
                temperature=0.55 if complex_task else 0.72,
                reason="Модель выбрана пользователем",
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
                    + [self.hardware.recommended_fast_model, "qwen3.5:2b", self.settings.model]
                )
                return ModelRoute(
                    model=selected, think=False, num_ctx=min(self.settings.chat_num_ctx, 3072),
                    num_predict=min(self.settings.chat_num_predict, 384), temperature=0.32,
                    reason="Интерактивный код на малой VRAM: быстрый резидентный контур",
                )
            selected = self._choose_existing([self.settings.code_model, self.hardware.recommended_code_model, self.settings.model])
            return ModelRoute(
                model=selected,
                think=(is_complex and self._base(selected).lower() == "qwen3" and "instruct" not in selected.lower()),
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
                think=(deep_requested and self._base(selected).lower() == "qwen3" and "instruct" not in selected.lower()),
                num_ctx=self.settings.task_num_ctx,
                num_predict=min(self.settings.task_num_predict, 2048),
                temperature=0.5,
                reason="Нужно глубокое рассуждение",
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
        """Use a small model for GUI/tool routing; reserve coder models for actual coding."""
        code_heavy = bool(re.search(r"\b(напиши код|исправь код|рефактор|реализуй функц|создай файл с кодом)\w*", query, re.IGNORECASE))
        if code_heavy:
            return self._choose_existing([self.settings.code_model, self.settings.model])
        if self._base(self.settings.fast_model).lower() in {"qwen3.5", "gemma4"}:
            candidates = self._fast_candidates(self.settings.fast_model) + [self.hardware.recommended_fast_model, "qwen3.5:2b", "qwen3:1.7b", self.settings.model]
        else:
            # The stock Gemma 3 Ollama model is our fast chat/vision lane, not the
            # native tool router. Keep a tiny Qwen tool-capable model as an on-demand
            # reserve so GUI planning does not degrade when chat moves to Gemma.
            candidates = ["qwen3.5:2b", "qwen3:1.7b"] + self._fast_candidates(self.settings.fast_model) + [self.hardware.recommended_fast_model, self.settings.model]
        return self._choose_existing(candidates)

    def task_model(self, kind: str) -> str:
        if kind == "project":
            return self._choose_existing(
                [
                    self.settings.code_model,
                    self.hardware.recommended_code_model,
                    self.settings.model,
                ]
            )
        if kind in {"vision", "screen", "image"}:
            # Image/screen analysis is deliberately isolated from the resident chat model.
            # On 4-GB GPUs a 4B VLM can reserve a huge CUDA arena and stall ASR/chat for
            # tens of seconds. Prefer the disposable 0.8B multimodal lane.
            return self._choose_existing([
                "qwen3.5:0.8b",
                self.settings.vision_model,
                self.hardware.recommended_vision_model,
                "qwen3.5:2b",
            ])
        return self._choose_existing(
            [self.settings.model, self.hardware.recommended_main_model]
        )
