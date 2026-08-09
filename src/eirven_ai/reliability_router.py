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
        ("telegram", re.compile(r"\b(?:telegram|телеграм\w*|телегр\w*|телега\w*|тг)\b", re.I)),
        ("yandex_music", re.compile(r"\b(?:яндекс\s*музык\w*|yandex\s*music)\b", re.I)),
        ("youtube", re.compile(r"\b(?:youtube|ютуб\w*)\b", re.I)),
        ("spotify", re.compile(r"\b(?:spotify|спотифай\w*)\b", re.I)),
        ("discord", re.compile(r"\b(?:discord|дискорд\w*)\b", re.I)),
    )
    ACTION = re.compile(
        r"\b(?:открой|запусти|зайди|перейди|найди|отыщи|добавь|положи|отправь|ответь|"
        r"напиши|включи|выключи|поставь|продолжи|нажми|выбери|прокрути|полистай)\b",
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

    @staticmethod
    def norm(text: Any) -> str:
        value = str(text or "").casefold().replace("ё", "е")
        value = re.sub(r"[^a-zа-я0-9._@:+/-]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def apps(self, text: str) -> list[str]:
        return [name for name, pattern in self.APP_PATTERNS if pattern.search(text)]

    def _app_open(self, clean: str) -> tuple[str, str]:
        """Return explicit app target and the unconsumed tail.

        Only an app named immediately after an open/start verb qualifies.  Merely
        mentioning Telegram later in a sentence must not steal a cross-app mission.
        """
        for app, pattern in self.APP_PATTERNS:
            m = re.match(r"^\s*(?:открой|запусти|включи)\s+(.+)$", clean, re.I)
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
        if re.search(r"\b(?:все|всем|кажд\w*)\b.{0,80}\b(?:непрочитан\w*|чат\w*|сообщен\w*)", clean, re.I) and re.search(r"\b(?:ответ|напиш|обработ)\w*", clean, re.I):
            return ReliabilityDecision("mission", reason="bounded collection task", confidence=.99)
        explicit_app, tail = self._app_open(clean)
        if len(set(mentioned)) >= 2:
            return ReliabilityDecision("mission", reason="multiple app surfaces", confidence=.99)
        if explicit_app:
            if tail and self.ACTION.search(tail):
                return ReliabilityDecision("app_compound", app=explicit_app, remainder=tail, reason="explicit app then action", confidence=.99)
            # Natural content phrases can omit a second imperative: ``open YouTube, any video``.
            if tail and re.search(r"\b(?:любое\s+видео|любой\s+ролик|любую\s+песн\w*|любой\s+трек)\b", tail, re.I):
                return ReliabilityDecision("app_compound", app=explicit_app, remainder=tail, reason="explicit app with content request", confidence=.97)
            return ReliabilityDecision("app_open", app=explicit_app, reason="explicit app open", confidence=.99)

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

        # ``open <entity>`` with no app/site keyword is external navigation unless the
        # object is a known page/system primitive.  The site resolver can then choose the
        # official destination generically; no merchant-specific recipe is needed.
        ext = re.fullmatch(r"(?:открой|запусти)\s+(.+)", clean, re.I)
        if ext:
            raw_target = ext.group(1).strip()
            # Literal site/domain commands already have a mature deterministic opener with
            # verification.  Leave them to that handler; r22 only resolves bare entities.
            if re.match(r"^(?:мне\s+)?сайт\s+", raw_target, re.I) or re.search(r"(?:https?://|\b[a-z0-9-]+\.(?:ru|com|net|org|io|app|ai)\b)", raw_target, re.I):
                return ReliabilityDecision("unknown", reason="literal site handled by site opener", confidence=.95)
            target = raw_target
            if target and not self.PAGE_LOCAL.fullmatch(target) and not self.SYSTEM_LOCAL.search(target):
                return ReliabilityDecision("external_open", target=target, reason="named external entity", confidence=.90)

        # Media ownership is only allowed when no explicit new app surface was requested.
        if re.search(r"\b(?:поставь\s+на\s+паузу|пауза|продолжи|играй|воспроизведи|следующ\w*|предыдущ\w*)\b", clean):
            return ReliabilityDecision("media", reason="transport command without new app", confidence=.94)

        actions = len(self.ACTION.findall(clean))
        if actions >= 3 or re.search(r"\b(?:потом|затем|после\s+этого|параллельно|одновременно)\b", clean):
            return ReliabilityDecision("mission", reason="long-horizon structure", confidence=.92)

        return ReliabilityDecision("unknown", reason="legacy fallback", confidence=.0)
