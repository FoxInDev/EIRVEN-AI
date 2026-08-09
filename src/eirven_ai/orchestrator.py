from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class IntentResult:
    kind: str
    title: str
    payload: dict[str, Any]
    confidence: float


class IntentRouter:
    GIT_ACTION = re.compile(
        r"\b(закоммить|закоммитить|коммитни|commit|запушь|запушить|push)\b",
        re.IGNORECASE,
    )
    PROJECT = re.compile(
        r"(?:\b(создай|сделай|разработай|напиши|собери|реализуй)\b.{0,140}"
        r"\b(проект|приложение|сайт|сервис|бот|api|программу|игру)\b"
        r"|\b(проект|приложение|сайт|сервис|бот|api|программа|игра)\b.{0,180}"
        r"\b(с нуля|тз|требован|реализ|созда|разработ|должн|нужно сделать)\w*)",
        re.IGNORECASE | re.DOTALL,
    )
    BROWSER = re.compile(
        r"\b(открой|посмотри|найди|проверь|узнай|погугли)\b.{0,100}"
        r"\b(в браузере|сайт|страниц|цену|курс|битк|bitcoin|btc|новост|интернет)\w*",
        re.IGNORECASE | re.DOTALL,
    )
    CRYPTO = re.compile(r"\b(битк(?:а|оин)?|bitcoin|btc|эфир|ethereum|eth)\b", re.IGNORECASE)
    TELEGRAM = re.compile(
        r"\b(телеграм|telegram|тг)\b.{0,160}\b(монитор|отвеч|ответ|следи)\w*",
        re.IGNORECASE | re.DOTALL,
    )
    SCREEN = re.compile(
        r"\b(что|посмотри|покажи|прочитай|проанализируй|оцени)\b.{0,90}\b(на экране|экран|тут написано|сейчас открыто|график|окно)\w*|"
        r"\b(что тут написано|что у меня на экране|посмотри на экран)\b",
        re.IGNORECASE | re.DOTALL,
    )
    COMPUTER = re.compile(
        r"\b(на компьютере|на экране|мышк|клик|рабочий стол|браузер)\b",
        re.IGNORECASE,
    )
    APPLICATION = re.compile(
        r"\b(запусти|запустишь|открой|отрой|откроешь|открыть|включи|включишь|вруби|зайди|перейди)\b\s+(?:мне\s+)?"
        r"(minecraft|майнкрафт|майн|telegram|телеграм|тг|vscode|visual studio code|код|"
        r"блокнот|калькулятор|браузер|chrome|edge|firefox)\b",
        re.IGNORECASE,
    )
    GENERIC_APPLICATION = re.compile(
        r"^\s*(?:пожалуйста[, ]+)?(?:запусти|запустишь|открой|отрой|откроешь|открыть|включи|включишь|вруби|зайди|перейди)\s+(?:мне\s+)?(.{2,80}?)\s*[.!]?\s*$",
        re.IGNORECASE,
    )
    MOVIE_RECOMMEND = re.compile(
        r"\b(посоветуй|подбери|предложи|хочу посмотреть)\b.{0,80}\b(фильм|кино|сериал)\w*",
        re.IGNORECASE | re.DOTALL,
    )
    CREATIVE = re.compile(
        r"\b(сгенерируй|создай|нарисуй|сделай)\b.{0,100}\b(картинк|изображен|аватар|персонаж)\w*",
        re.IGNORECASE | re.DOTALL,
    )
    GAME = re.compile(
        r"\b(поиграй|играй|поиграть|найди алмаз|добудь|покрафти|пройди)\w*\b.{0,120}"
        r"\b(майн|minecraft|майнкрафт|за меня|ресурс|алмаз|моб)\w*",
        re.IGNORECASE | re.DOTALL,
    )
    GENERAL_ACTION = re.compile(
        r"(?:^\s*(?:пожалуйста[, ]+)?|\b(?:надо|нужно|можешь|пожалуйста)\s+)"
        r"(сделай|выполни|открой|запусти|включи|закрой|найди|проверь|скачай|установи|"
        r"настрой|перемести|скопируй|создай папку|введи|напиши в|ответь в|заполни|нажми|"
        r"перейди|подключись|закоммить|закоммитить|запушь|запушить|клонируй|удали|переименуй)\b",
        re.IGNORECASE,
    )
    SYSTEM_TASK = re.compile(
        r"\b(git|github|репозитор|коммит|commit|push|powershell|терминал|процесс|служб|"
        r"папк[аеуы]?|файл[а-я]*|рабоч(?:ий|ем) стол|desktop|ssh|docker|compose|kubernetes|"
        r"telegram|телеграм|браузер|окн[оа]|приложен)\b", re.IGNORECASE
    )

    def route(self, text: str) -> IntentResult | None:
        clean = text.strip()
        if not clean:
            return None
        if self.GIT_ACTION.search(clean):
            remote = ""
            match = re.search(r"(?:git@[^\s]+|https://(?:www\.)?github\.com/[^\s]+)", clean, re.IGNORECASE)
            if match:
                remote = match.group(0).rstrip(".,)")
            return IntentResult(
                "git_action",
                "Сохранить изменения в Git",
                {"task": clean, "remote": remote},
                0.99,
            )
        if self.SCREEN.search(clean):
            active_screen_action = bool(re.search(
                r"\b(переключ|поменя|нажм|клик|двиг|выдел|прокрут|скрол|введ|открой|закрой|перетащ)\w*",
                clean, re.IGNORECASE,
            ))
            if not active_screen_action:
                return IntentResult(
                    "screen_query",
                    "Посмотреть экран",
                    {"question": clean},
                    0.99,
                )
            return IntentResult(
                "agent",
                "Посмотреть и выполнить действие на экране",
                {"task": clean, "observe_screen_first": True},
                0.97,
            )
        if self.TELEGRAM.search(clean):
            return IntentResult(
                "agent",
                "Выполнить задачу в Telegram",
                {"task": clean},
                0.94,
            )
        if self.CREATIVE.search(clean):
            use_as_avatar = bool(re.search(r"рабоч(?:ий|ем) стол|посади|персонаж", clean, re.IGNORECASE))
            return IntentResult(
                "creative_image",
                "Сгенерировать изображение",
                {"prompt": clean, "use_as_avatar": use_as_avatar, "width": 768, "height": 768, "steps": 24},
                0.9,
            )
        if self.GAME.search(clean):
            return IntentResult(
                "game",
                "Игровая задача в Minecraft",
                {"goal": clean, "window_title": "Minecraft", "max_minutes": 15},
                0.91,
            )
        application = self.APPLICATION.search(clean)
        if application:
            return IntentResult(
                "application_launch",
                f"Запустить {application.group(2)}",
                {"application": application.group(2)},
                0.97,
            )
        generic_app = self.GENERIC_APPLICATION.match(clean)
        if generic_app:
            candidate = generic_app.group(1).strip().strip('"«»')
            if not re.search(
                r"^(?:https?://|www\.|папк|файл|проект|репозитор|сайт|страниц|ссылк|"
                r"рабоч(?:ий|ем) стол|чат|диалог|настройк)",
                candidate,
                re.IGNORECASE,
            ):
                return IntentResult(
                    "application_launch",
                    f"Запустить {candidate}",
                    {"application": candidate},
                    0.93,
                )
        if self.MOVIE_RECOMMEND.search(clean):
            return IntentResult(
                "media_recommend",
                "Подобрать фильм",
                {"request": clean},
                0.94,
            )
        if self.PROJECT.search(clean):
            name = self._project_name(clean)
            return IntentResult(
                "project",
                f"Создать проект {name}",
                {"name": name, "description": clean, "overwrite": False},
                0.93,
            )
        if self.CRYPTO.search(clean) and re.search(
            r"\b(цен|курс|сколько|сейчас|посмотри|узнай)\w*", clean, re.IGNORECASE
        ):
            symbol = "ethereum" if re.search(r"эфир|ethereum|eth", clean, re.IGNORECASE) else "bitcoin"
            currency = "rub" if re.search(r"руб|rub", clean, re.IGNORECASE) else (
                "eur" if re.search(r"евро|eur", clean, re.IGNORECASE) else "usd"
            )
            return IntentResult(
                "crypto_price",
                f"Узнать цену {symbol}",
                {"symbol": symbol, "currency": currency, "open_browser": True},
                0.98,
            )
        if (
            self.BROWSER.search(clean)
            or self.COMPUTER.search(clean)
            or self.GENERAL_ACTION.search(clean)
            or (self.SYSTEM_TASK.search(clean) and re.search(r"\b(надо|нужно|сделай|выполни|хочу чтобы|пожалуйста)\b", clean, re.IGNORECASE))
        ):
            return IntentResult(
                "agent",
                "Выполнить задачу на компьютере",
                {"task": clean},
                0.92 if self.GENERAL_ACTION.search(clean) else 0.84,
            )
        return None

    @staticmethod
    def _project_name(text: str) -> str:
        quoted = re.search(r"[«\"']([^»\"']{2,50})[»\"']", text)
        source = quoted.group(1) if quoted else "eirven-project"
        name = re.sub(r"[^a-zA-Z0-9_-]+", "-", source).strip("-").lower()
        return (name or "eirven-project")[:50]
