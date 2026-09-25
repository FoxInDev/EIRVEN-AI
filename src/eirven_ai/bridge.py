# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
"""Разговорный мост.

Идея простая: между просьбой и ответом всегда есть пауза, и она тем заметнее,
чем дольше работает модель. Убрать паузу нельзя — железо не станет быстрее. Но
её можно занять: сразу подтвердить, что задача принята, и задать встречный
вопрос. Пока человек отвечает, настоящая работа идёт в фоне.

Это не ускорение, а перераспределение внимания. Честно говоря об этом: секунды
те же, но проведены они иначе — в разговоре, а не в тишине перед пустым экраном.

Важное ограничение устройства: подтверждение обязано появиться мгновенно, иначе
приём бессмысленен. Поэтому оно берётся из уже вычисленного решения
маршрутизатора, а вопрос формулирует модель самым дешёвым вызовом, какой
возможен — двадцать токенов на быстрой модели. Списков заготовленных фраз здесь
нет: решает контур.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from .trace import log_event

# Дольше этого ждать молча нельзя: пауза начинает читаться как зависание.
BRIDGE_AFTER_SECONDS = 0.35

# Короче этого мост не нужен: задача решилась раньше, чем человек прочитал бы
# вопрос, и встречная реплика будет выглядеть навязчивой.
MIN_TASK_SECONDS = 1.2

# Маркер «не успела»: отличает таймаут от честного ответа модели «моста не надо».
_TIMEOUT = object()


def _within(seconds: float, fn, default):
    """Выполнить fn не дольше seconds, иначе вернуть _TIMEOUT.

    Вызов модели безопасен в отдельном потоке: он не трогает ни инструменты,
    ни интерфейс Windows, только текст. Поток-демон не держит программу.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = default

    worker = threading.Thread(target=run, daemon=True, name="eirven-bridge-call")
    worker.start()
    worker.join(timeout=seconds)
    if worker.is_alive() or "value" not in box:
        return _TIMEOUT
    return box["value"]


class ConversationBridge:
    """Занимает паузу разговором, пока идёт настоящая работа."""

    def __init__(self, settings: Any, gateway: Any, memory: Any = None, *, fast_hardware: bool = True) -> None:
        self.settings = settings
        self.gateway = gateway
        self.memory = memory
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}
        # До какого момента мост молчит, не трогая модель. Брошенный по сроку
        # вызов не отменяется — он продолжает занимать Ollama, и на слабой
        # машине, где запросы идут по одному, настоящая задача ждала за ним:
        # 3 секунды превращались в 6. Раз не уложилась — значит машина для
        # моста медленная, и звать модель ради реплики только вредит.
        self._backoff_until = 0.0
        # На машине без видеокарты мост никогда не уложится в срок: генерация
        # идёт 5–10 токенов в секунду. Там незачем даже пробовать — каждая
        # попытка занимала бы очередь Ollama перед настоящей задачей. Выключаем
        # сразу, без разведки ценой первой задачи.
        if not fast_hardware:
            self._backoff_until = float("inf")

    # ------------------------------------------------------------- открытие
    # Сколько ждать реплику, прежде чем начать задачу без неё. Больше нельзя:
    # мост затевался, чтобы убрать паузу, а не добавить новую. На машине без
    # видеокарты генерация идёт со скоростью 5–10 токенов в секунду, и без
    # этого потолка «включи музыку» ждало бы 6–12 секунд до начала работы.
    OPENER_DEADLINE = 0.6
    BACKOFF_SECONDS = 600.0

    def _cooling(self) -> bool:
        return time.monotonic() < self._backoff_until

    def _note_timeout(self, where: str) -> None:
        self._backoff_until = time.monotonic() + self.BACKOFF_SECONDS
        log_event(self.settings.root_dir, "BRIDGE_BACKOFF", where=where,
                  minutes=int(self.BACKOFF_SECONDS // 60))

    def opener_within_deadline(self, query: str, domain: str, restated: str = "") -> dict[str, str]:
        """Реплика, если модель успела её придумать, иначе пусто.

        Задача никогда не ждёт реплику дольше срока: на слабом компьютере мост
        просто не появится, а работа начнётся сразу — это лучше, чем заставить
        человека ждать фразу «уже делаю».
        """
        box: dict[str, dict[str, str]] = {}

        def run() -> None:
            box["value"] = self.opener(query, domain, restated)

        worker = threading.Thread(target=run, daemon=True, name="eirven-bridge-opener")
        worker.start()
        worker.join(timeout=self.OPENER_DEADLINE)
        if worker.is_alive() or "value" not in box:
            log_event(self.settings.root_dir, "BRIDGE_SKIPPED_SLOW", domain=domain)
            return {"ack": "", "question": ""}
        return box["value"]

    def decide_and_open(self, query: str) -> dict[str, str]:
        """На входе хода: нужен ли мост, и если да — какая реплика.

        Решает модель, без списков: «выключи компьютер» или «привет» моста не
        заслуживают, «включи музыку» и «найди новости» — да. Одним вызовом,
        чтобы не платить дважды, и со сроком, чтобы не стать паузой самой.
        """
        if self._cooling():
            return {"ack": "", "question": ""}
        system = (
            "Владелец дал Эрви запрос. Реши, нужна ли короткая реплика до ответа.\n"
            "Нужна, если Эрви предстоит что-то сделать или поискать: включить, "
            "открыть, найти, разобрать, собрать. Тогда верни подтверждение в "
            "два-три слова и лёгкий встречный вопрос, связанный с запросом.\n"
            "НЕ нужна для приветствий, простых вопросов, на которые отвечают сразу, "
            "и для выключения или перезагрузки компьютера.\n"
            "Верни ТОЛЬКО JSON: {\"bridge\": true|false, \"ack\": \"...\", \"question\": \"...\"}"
        )

        def call() -> dict[str, str]:
            message = self.gateway.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Запрос: {query}"}],
                model=self.settings.fast_model, temperature=0.8, think=False,
                response_format={
                    "type": "object",
                    "properties": {"bridge": {"type": "boolean"},
                                   "ack": {"type": "string"},
                                   "question": {"type": "string"}},
                    "required": ["bridge"],
                },
                num_ctx=1024, num_predict=32,
            )
            raw = str(message.get("content") or "").strip()
            a, b = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[a:b + 1]) if a >= 0 and b > a else {}
            if not data.get("bridge"):
                return {"ack": "", "question": ""}
            return {"ack": str(data.get("ack") or "").strip(),
                    "question": str(data.get("question") or "").strip()}

        result = _within(self.OPENER_DEADLINE, call, None)
        if result is _TIMEOUT:
            self._note_timeout("opener")
            return {"ack": "", "question": ""}
        if not isinstance(result, dict) or not result.get("ack"):
            log_event(self.settings.root_dir, "BRIDGE_NOT_USED")
            return {"ack": "", "question": ""}
        return result

    def is_reply(self, question: str, reply: str) -> bool:
        """Ответ ли это на встречный вопрос — или новая задача.

        Если модель не успела решить, считаем задачей: проглотить задачу хуже,
        чем не откликнуться на ответ. «Открой ютуб» вместо ответа должно
        открыть ютуб, а не получить в ответ «понимаю».
        """
        if self._cooling():
            return False
        system = (
            "Эрви задала владельцу мимоходный вопрос. Он что-то ответил.\n"
            "Это ответ на вопрос или новая просьба что-то сделать?\n"
            "Верни ТОЛЬКО JSON: {\"reply\": true|false}"
        )

        def call() -> bool:
            message = self.gateway.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Вопрос: {question}\nОн ответил: {reply}"}],
                model=self.settings.fast_model, temperature=0.0, think=False,
                response_format={"type": "object",
                                 "properties": {"reply": {"type": "boolean"}},
                                 "required": ["reply"]},
                num_ctx=512, num_predict=8,
            )
            raw = str(message.get("content") or "")
            a, b = raw.find("{"), raw.rfind("}")
            return bool(json.loads(raw[a:b + 1]).get("reply")) if a >= 0 and b > a else False

        result = _within(self.OPENER_DEADLINE, call, False)
        if result is _TIMEOUT:
            self._note_timeout("reply")
            return False
        return bool(result)

    def opener(self, query: str, domain: str, restated: str = "") -> dict[str, str]:
        """Подтверждение и встречный вопрос.

        Обе части формулирует модель: заготовленные фразы выдают себя на третьем
        повторении, а человек разговаривает с Эрви каждый день. Вызов предельно
        дешёвый — короткий промпт и жёсткий потолок в сорок токенов.
        """
        system = (
            "Ты — Эрви, помощник на компьютере владельца. Он только что дал задачу.\n"
            "Ответь ДВУМЯ короткими частями:\n"
            "1) подтверждение, что взялась — два-три слова, по смыслу задачи;\n"
            "2) встречный вопрос владельцу, связанный с задачей или с ним самим.\n\n"
            "Вопрос нужен, чтобы занять паузу, пока задача выполняется. Он должен "
            "быть лёгким, без нажима, на который отвечают одним предложением.\n"
            "Не спрашивай разрешения и не уточняй задачу — она уже понятна.\n\n"
            "Верни ТОЛЬКО JSON: {\"ack\": \"...\", \"question\": \"...\"}"
        )
        user = f"Задача: {restated or query}\nОбласть: {domain or 'неизвестна'}"
        try:
            message = self.gateway.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=self.settings.fast_model,
                temperature=0.8,          # выше обычного: реплики не должны повторяться
                think=False,
                response_format={
                    "type": "object",
                    "properties": {"ack": {"type": "string"}, "question": {"type": "string"}},
                    "required": ["ack", "question"],
                },
                num_ctx=1024, num_predict=28,
            )
            raw = str(message.get("content") or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start:end + 1])
                ack = str(data.get("ack") or "").strip()
                question = str(data.get("question") or "").strip()
                if ack:
                    return {"ack": ack, "question": question}
        except Exception as exc:
            log_event(self.settings.root_dir, "BRIDGE_OPENER_FAILED", error=str(exc)[:160])
        # Без моста задача всё равно выполнится — просто молча.
        return {"ack": "", "question": ""}

    # ---------------------------------------------------------- когда нужен
    # Области, где мост уместен. Музыка сюда входит, хотя включается быстро:
    # это как раз тот случай, где встречный вопрос работает на ощущение живого
    # собеседника, а не только прикрывает паузу.
    # Не входят питание и экран: «выключи компьютер» с вопросом «как настроение?»
    # звучит издевательски, а яркость меняется быстрее, чем прочитаешь реплику.
    SLOW_DOMAINS = frozenset({
        "web", "mail", "video", "food", "task", "code", "telegram", "phone", "media",
    })

    def worth_bridging(self, domain: str) -> bool:
        """Стоит ли занимать паузу разговором.

        Решение по области от маршрутизатора, а не по словам в запросе. Мост на
        быстрой задаче выглядит навязчиво: человек получил бы вопрос и сразу
        ответ, не успев прочитать первое.
        """
        return str(domain or "") in self.SLOW_DOMAINS

    # ------------------------------------------------------- ответ владельца
    def remember_reply(self, conversation_id: str, question: str, reply: str) -> None:
        """Сохранить ответ на встречный вопрос — без отдельной реакции.

        Человек отвечает, чтобы занять паузу, а не чтобы начать беседу. Поэтому
        сказанное уходит в память и в контекст, но не порождает новый ход: иначе
        он получит два ответа подряд и запутается, на какой из них смотреть.
        """
        text = str(reply or "").strip()
        if not text or self.memory is None:
            return
        try:
            self.memory.add_message(
                conversation_id, "user", text,
                metadata={"bridge_reply": True, "asked": question[:120]},
            )
            log_event(self.settings.root_dir, "BRIDGE_REPLY_STORED", length=len(text))
        except Exception as exc:
            log_event(self.settings.root_dir, "BRIDGE_REPLY_FAILED", error=str(exc)[:160])

    def closing_note_within_deadline(self, question: str, reply: str) -> str:
        """Отклик на ответ, если модель успела. Иначе молчание — это нормально:
        ответ всё равно сохранён в памяти и повлияет на следующие реплики."""
        if self._cooling():
            return ""
        result = _within(1.2, lambda: self.closing_note(question, reply), "")
        if result is _TIMEOUT:
            self._note_timeout("closing")
            return ""
        return str(result or "")

    def closing_note(self, question: str, reply: str) -> str:
        """Короткий отклик на сказанное владельцем.

        Одна фраза, не вопрос: цель — показать, что его услышали, и закрыть тему,
        а не продолжить разговор. Если человек ответил чем-то тяжёлым, отклик
        должен это признать, а не пожелать хорошего дня поверх.
        """
        text = str(reply or "").strip()
        if not text:
            return ""
        system = (
            "Владелец ответил на твой мимоходный вопрос. Откликнись ОДНОЙ короткой "
            "фразой: покажи, что услышала, и на этом закончи.\n"
            "Не задавай встречных вопросов и не развивай тему.\n"
            "Если в ответе усталость, раздражение или что-то тяжёлое — отзовись "
            "на это по-человечески, без бодрых пожеланий.\n"
            "Только сама фраза, без кавычек и пояснений."
        )
        user = f"Твой вопрос: {question}\nЕго ответ: {text}"
        try:
            message = self.gateway.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=self.settings.fast_model,
                temperature=0.7, think=False,
                num_ctx=1024, num_predict=40,
            )
            return str(message.get("content") or "").strip().strip('"')
        except Exception:
            return ""

    # ------------------------------------------------------------- состояние
    def mark_waiting(self, conversation_id: str, question: str) -> None:
        """Запомнить, что задан встречный вопрос и ждём ответа."""
        with self._lock:
            self._pending[conversation_id] = {"question": question, "at": time.time()}

    def take_waiting(self, conversation_id: str, ttl: float = 180.0) -> str:
        """Забрать заданный вопрос, если ответ пришёл вовремя.

        Через три минуты вопрос перестаёт считаться открытым: человек давно
        переключился, и его новая реплика — это новая задача, а не ответ.
        """
        with self._lock:
            entry = self._pending.get(conversation_id)
            if not entry:
                return ""
            if time.time() - entry["at"] > ttl:
                self._pending.pop(conversation_id, None)
                return ""
            self._pending.pop(conversation_id, None)
            return str(entry.get("question") or "")

    def is_waiting(self, conversation_id: str) -> bool:
        # Вызывается в самом условии на входе каждого сообщения — вне защиты
        # try. Поэтому не имеет права упасть ни при каком раскладе: при любой
        # неожиданности считаем, что ответа не ждём, и сообщение идёт обычно.
        try:
            with self._lock:
                return conversation_id in self._pending
        except Exception:
            return False
