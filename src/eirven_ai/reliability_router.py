from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ReliabilityDecision:
    kind: str = "unknown"
    target: str = ""
    app: str = ""
    remainder: str = ""
    reason: str = ""
    confidence: float = 0.0
    verb: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReliabilityRouter:
    """r22 front-door arbitration for voice/text commands.

    The historical stack accumulated several individually useful fast paths.  The live
    traces show that they can still compete for the same utterance (for example a media
    fast path stealing ``open YouTube and play a video`` while Yandex Music is focused).
    This router does *not* execute anything.  It only assigns ownership before any legacy
    surface-specific handler gets a chance to act.
    """

    APP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("telegram", re.compile(r"\b(?:telegram|т?елеграм\w*|телег\w*|тг)\b", re.I)),
        ("yandex_music", re.compile(r"\b(?:яндекс\s*музык\w*|yandex\s*music)\b", re.I)),
        ("youtube", re.compile(r"\b(?:youtube|ютуб\w*)\b", re.I)),
        ("spotify", re.compile(r"\b(?:spotify|спотифай\w*)\b", re.I)),
        ("discord", re.compile(r"\b(?:discord|дискорд\w*)\b", re.I)),
        ("mesh", re.compile(r"\b(?:м[эе]ш|mesh)\b", re.I)),
    )
    ACTION = re.compile(
        r"\b(?:открой|запусти|зайди|перейди|найди|отыщи|добавь|положи|отправь|ответь|"
        r"напиши|скинь|посмотри|проверь|прочитай|включи|вруби|воспроизведи|выключи|поставь|"
        r"продолжи|нажми|выбери|прокрути|полистай|удали|переустанови|почини|исправь|сделай)\b",
        re.I,
    )
    PAGE_LOCAL = re.compile(
        r"^(?:раздел\s+)?(?:каталог|корзин\w*|меню|новинк\w*|категори\w*|поиск|акци\w*|"
        r"избранн\w*|профил\w*|заказ\w*|доставк\w*|бренд\w*)$",
        re.I,
    )
    SYSTEM_LOCAL = re.compile(
        r"\b(?:папк\w*|файл\w*|проводник|explorer|настройк\w*\s+windows|параметр\w*\s+windows|"
        r"браузер|меню\s+пуск|поиск\s+windows|командн\w*\s+строк\w*|powershell|терминал)\b",
        re.I,
    )
    WEB_ENTITY = re.compile(
        r"^(?:кворк|kwork|золотое\s+яблоко|goldapple|ozon|wildberries|авито|avito)$",
        re.I,
    )
    AMBIGUOUS_BRAND = re.compile(
        r"^(?:microsoft|майкрософт|google|гугл|яндекс|yandex|apple|эппл)$",
        re.I,
    )

    @staticmethod
    def norm(text: Any) -> str:
        value = str(text or "").casefold().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9._@:+/-]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(
            r"^(?:(?:eirven|eirwen|эрви|эйрви|эйрвен|эйрвэн|эрвен|ирвен)\s+)+",
            "", value, flags=re.I,
        )
        value = re.sub(r"^(?:(?:пожалуйста|плиз|слушай|ну|эй)\s+)+", "", value, flags=re.I)
        value = re.sub(r"\s+(?:пожалуйста|плиз)$", "", value, flags=re.I)
        return value.strip()

    def apps(self, text: str) -> list[str]:
        return [name for name, pattern in self.APP_PATTERNS if pattern.search(text)]

    def _app_open(self, clean: str) -> tuple[str, str]:
        """Return explicit app target and the unconsumed tail.

        Only an app named immediately after an open/start verb qualifies.  Merely
        mentioning Telegram later in a sentence must not steal a cross-app mission.
        """
        for app, pattern in self.APP_PATTERNS:
            m = re.match(
                r"^\s*(?:открой|запусти|включи|вруби|зайди|перейди)\s+(?:в\s+|на\s+)?(.+)$",
                clean, re.I,
            )
            if not m:
                continue
            rest = m.group(1).strip()
            app_match = pattern.search(rest)
            if app_match and app_match.start() <= 3:
                tail = rest[app_match.end():].strip(" ,.;:-")
                tail = re.sub(r"^(?:и|а\s+потом|потом|затем|после\s+этого)\s+", "", tail, flags=re.I).strip()
                return app, tail
        return "", ""

    def classify(self, query: str, *, foreground_title: str = "") -> ReliabilityDecision:
        clean = self.norm(query)
        if not clean:
            return ReliabilityDecision()

        if re.fullmatch(r"(?:что\s+ты\s+умеешь|что\s+умеешь|что\s+ты\s+можешь|какие\s+у\s+тебя\s+возможности)", clean):
            return ReliabilityDecision("capabilities", reason="fixed capability question", confidence=1.0)

        if re.fullmatch(
            r"(?:пожалуйста\s+)?(?:выключись|отключись|закройся|заверши\s+работу|"
            r"(?:выключи|закрой|заверши|останови)\s+(?:себя|эрви|eirven)(?:\s+полностью)?)",
            clean,
        ):
            return ReliabilityDecision("self_shutdown", reason="explicit assistant shutdown", confidence=1.0)

        mentioned = self.apps(clean)
        # Bounded collection work (all unread chats/messages) is a mission even when it
        # names only one app.  A one-recipient fast path must never steal this loop.
        collection = bool(
            re.search(r"\b(?:все|всем|кажд\w*)\b.{0,80}\b(?:непрочитан\w*|чат\w*|сообщен\w*)", clean, re.I)
            or re.search(r"\bвсем\b.{0,80}\bкому\b.{0,40}\b(?:я\s+)?не\s+(?:ответил|ответила|написал|написала)\b", clean, re.I)
        )
        if collection and re.search(r"\b(?:ответ|напиш|обработ)\w*", clean, re.I):
            return ReliabilityDecision("mission", reason="bounded collection task", confidence=.99)
        explicit_app, tail = self._app_open(clean)
        if (
            not explicit_app and mentioned == ["telegram"]
            and re.search(r"\b(?:напиши|отправь|скинь|пошли)\w*", clean, re.I)
        ):
            # Natural voice order can put the platform at the end: ``открой и напиши
            # Тиме привет в Telegram``.  It is still one deterministic Telegram action,
            # not an application literally named ``и напиши ...``.
            remainder=re.sub(
                r"^(?:открой|запусти|включи|вруби)\w*\s*(?:и\s+)?", "", clean,
                count=1, flags=re.I,
            ).strip()
            return ReliabilityDecision(
                "app_compound", app="telegram", remainder=remainder,
                reason="telegram platform follows send action", confidence=.98,
            )
        if len(set(mentioned)) >= 2:
            return ReliabilityDecision("mission", reason="multiple app surfaces", confidence=.99)
        if explicit_app:
            if tail and self.ACTION.search(tail):
                return ReliabilityDecision("app_compound", app=explicit_app, remainder=tail, reason="explicit app then action", confidence=.99)
            # Natural content phrases can omit a second imperative: ``open YouTube, any video``.
            if tail and re.search(r"\b(?:любое\s+видео|любой\s+ролик|любую\s+песн\w*|любой\s+трек)\b", tail, re.I):
                return ReliabilityDecision("app_compound", app=explicit_app, remainder=tail, reason="explicit app with content request", confidence=.97)
            return ReliabilityDecision("app_open", app=explicit_app, reason="explicit app open", confidence=.99)

        action_matches = list(self.ACTION.finditer(clean))
        actions = len(action_matches)
        # Two owner imperatives joined in one utterance are already a mission. The old
        # three-action threshold collapsed ``включи музыку и открой тг`` into a fuzzy
        # request to open an application literally called "музыку и открой тг".
        two_action_chain = any(
            re.search(r"\b(?:и|а\s+потом|потом|затем|далее)\b", clean[left.end():right.start()], re.I)
            for left, right in zip(action_matches, action_matches[1:])
        )
        if two_action_chain:
            return ReliabilityDecision("mission", reason="two explicit joined actions", confidence=.98)
        if actions >= 3 or re.search(r"\b(?:потом|затем|после\s+этого|параллельно|одновременно)\b", clean):
            return ReliabilityDecision("mission", reason="long-horizon structure", confidence=.96)

        # Page-local ownership is explicit.  This prevents ``open Golden Apple`` from
        # being interpreted as a fuzzy button in whatever browser page happens to be frontmost.
        page_m = re.match(
            r"^(?:зайди|перейди|открой|нажми|выбери)\s+(?:на\s+этом\s+сайте\s+|"
            r"на\s+текущем\s+сайте\s+|на\s+этой\s+странице\s+|в\s+)?(?:раздел\s+)?(.+)$",
            clean,
            re.I,
        )
        if page_m:
            target = page_m.group(1).strip()
            # ASR hesitation before a noun ("э-э раздел новинки") is not part of the
            # semantic destination. Keep filler removal local to the extracted target.
            target = re.sub(r"^(?:(?:э|эм|мм)\s+)+", "", target, flags=re.I).strip()
            target = re.sub(r"^раздел\s+", "", target, flags=re.I).strip()
            if "на этом сайте" in clean or "на текущем сайте" in clean or "на этой странице" in clean or re.search(r"\bраздел\b", clean) or self.PAGE_LOCAL.fullmatch(target):
                return ReliabilityDecision("page_navigation", target=target, reason="explicit current-page destination", confidence=.98)

        # A bare request to start music is a media goal, not an application-name lookup.
        # It uses the configured/default music surface and is therefore deterministic.
        if re.fullmatch(r"(?:включи|вруби|запусти|поставь|воспроизведи)\s+(?:мне\s+)?(?:музык\w*|песн\w*|трек\w*)", clean, re.I):
            return ReliabilityDecision("media_start", app="yandex_music", reason="generic music playback", confidence=.98, verb="включи")

        # Player transport/control phrases must remain controls even while a media page
        # is foreground.  Without this guard, ``поставь лайк`` and ``поставь на паузу``
        # were misread as song titles and sent into Yandex search.
        if re.search(r"\b(?:поставь\s+на\s+паузу|пауза|продолжи|играй|следующ\w*|предыдущ\w*)\b", clean):
            return ReliabilityDecision("media", reason="transport command without new app", confidence=.94)

        content = re.fullmatch(r"(?:включи|воспроизведи|поставь)\s+[«\"']?(.+?)[»\"']?", clean, re.I)
        if content and re.search(r"(?:яндекс\s*музык|yandex\s*music|music\.yandex)", foreground_title, re.I):
            target = content.group(1).strip()
            player_control = bool(re.match(
                r"^(?:на\s+)?(?:лайк|дизлайк|не\s+нравится|нравится|пауз\w*|поиск\w*|"
                r"автовоспроизвед\w*|автоплей\w*|громк\w*|звук\w*)\b",
                target, re.I,
            ))
            if not player_control:
                return ReliabilityDecision("media_content", target=target, app="yandex_music", reason="foreground media context", confidence=.97, verb="включи")

        explicit_application = re.fullmatch(
            r"(открой|запусти|включи)\s+(?:приложение|программу)\s+(.+)", clean, re.I,
        )
        if explicit_application:
            return ReliabilityDecision(
                "application_open", target=explicit_application.group(2).strip(),
                reason="explicit application noun", confidence=.99,
                verb=explicit_application.group(1).casefold(),
            )

        # ``зайди в/на X`` describes navigation to a service.  Unlike ``открой X`` it
        # has a strong web meaning and should never fuzzy-launch a Start-menu app.
        enter = re.fullmatch(r"(зайди|перейди)\s+(?:в|на)\s+(.+)", clean, re.I)
        if enter:
            target = enter.group(2).strip()
            if target and not self.PAGE_LOCAL.fullmatch(target) and not self.SYSTEM_LOCAL.search(target):
                return ReliabilityDecision("external_open", target=target, reason="service navigation verb", confidence=.96, verb=enter.group(1).casefold())

        # A bare entity can name a website, an installed app, or both.  Keep known web
        # services fast; all other entities are resolved against the installed-app index
        # by ChatService and clarified when both meanings remain plausible.
        ext = re.fullmatch(r"(открой|запусти|включи)\s+(.+)", clean, re.I)
        if ext:
            verb = ext.group(1).casefold()
            raw_target = ext.group(2).strip()
            # Literal site/domain commands already have a mature deterministic opener with
            # verification.  Leave them to that handler; r22 only resolves bare entities.
            if re.match(r"^(?:мне\s+)?сайт\s+", raw_target, re.I) or re.search(r"(?:https?://|\b[a-z0-9-]+\.(?:ru|com|net|org|io|app|ai)\b)", raw_target, re.I):
                return ReliabilityDecision("unknown", reason="literal site handled by site opener", confidence=.95)
            target = raw_target
            if target and not self.PAGE_LOCAL.fullmatch(target) and not self.SYSTEM_LOCAL.search(target):
                if self.WEB_ENTITY.fullmatch(target):
                    return ReliabilityDecision("external_open", target=target, reason="known web service", confidence=.95, verb=verb)
                return ReliabilityDecision("contextual_open", target=target, reason="bare entity requires app/web resolution", confidence=.90, verb=verb)

        actions = len(self.ACTION.findall(clean))
        if actions >= 3 or re.search(r"\b(?:потом|затем|после\s+этого|параллельно|одновременно)\b", clean):
            return ReliabilityDecision("mission", reason="long-horizon structure", confidence=.92)

        return ReliabilityDecision("unknown", reason="legacy fallback", confidence=.0)
