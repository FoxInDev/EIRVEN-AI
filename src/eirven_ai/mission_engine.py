from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .trace import log_event


@dataclass(slots=True)
class MissionNode:
    id: str
    goal: str
    kind: str = "ui"  # ui | app | open_target | media | telegram_message | web | system | resolve_file | telegram_file | telegram_unread | background
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | done | failed | waiting_user | cancelled
    attempts: int = 0
    app: str = ""
    parallel_group: str = ""
    commit: bool = False
    verified: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MissionResult:
    ok: bool
    summary: str
    mission_id: str
    nodes: list[dict[str, Any]]
    needs_user: bool = False
    prompt: str = ""


class MissionEngine:
    """r19 long-horizon, cross-app mission coordinator.

    r16-r18 learned to act reliably in the *current* surface. r19 adds the layer above it:
    a persistent task graph whose nodes may move between apps, survive a restart, and run
    independent non-GUI work in parallel. The desktop remains a single shared physical
    resource, so GUI nodes are intentionally serialized behind one desktop lock while
    background/system nodes may run concurrently.

    The graph stores typed artifacts (for example a resolved file path) separately from
    natural-language goals. This prevents a transfer request such as "send file log2 from
    the open folder to Saved Messages in Telegram" from collapsing into one search string.
    """

    # Broad stem detector is only used to decide whether a request belongs to the
    # mission layer. The exact imperative token below is used for splitting so past
    # tense words inside message text ("что включил музыку") never become new nodes.
    _ACTION = re.compile(
        r"\b(откро|зайд|запуст|перейд|найд|отыщ|добав|полож|отправ|ответ|напиш|"
        r"скача|сохран|закро|заверш|включ|выключ|постав|продолж|увелич|уменьш|игра)\w*",
        re.I,
    )
    _ACTION_TOKEN = re.compile(
        r"\b(?:открой(?:те)?|зайди(?:те)?|запусти(?:те)?|перейди(?:те)?|"
        r"найди(?:те)?|отыщи(?:те)?|добавь(?:те)?|положи(?:те)?|отправь(?:те)?|"
        r"ответь(?:те)?|напиши(?:те)?|скачай(?:те)?|сохрани(?:те)?|закрой(?:те)?|"
        r"заверши(?:те)?|включи(?:те)?|выключи(?:те)?|поставь(?:те)?|"
        r"продолжи(?:те)?|увеличь(?:те)?|уменьши(?:те)?|играй(?:те)?)\b", re.I,
    )
    _LONG_HINT = re.compile(
        r"\b(потом|затем|после\s+этого|после\s+чего|дальше|параллельно|одновременно|"
        r"все\s+непрочитан|всем\s+непрочитан|до\s+конца|до\s+результата|самостоятельно|"
        r"не\s+останавливайся|несколько\s+приложен|в\s+фоне)\w*",
        re.I,
    )
    _APPS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("telegram", re.compile(r"\b(?:telegram|телеграм\w*|телегр\w*|телега\w*|тг|избранн\w*|saved messages)\b", re.I)),
        ("yandex_music", re.compile(r"\b(яндекс\s*музык\w*|yandex\s*music|моя\s+волна|трек|альбом)\b", re.I)),
        ("browser", re.compile(r"\b(сайт|страниц|браузер|каталог|корзин|товар|магазин)\w*", re.I)),
        ("files", re.compile(r"\b(файл|папк|проводник|explorer|директор)\w*", re.I)),
        ("system", re.compile(r"\b(системн\w*|windows|процесс\w*|громкост|звук)\b", re.I)),
    )
    _PARALLEL = re.compile(r"\b(?:параллельно|одновременно|тем\s+временем|заодно)\b", re.I)
    _SEPARATOR = re.compile(
        r"\s*(?:;|\n+|\b(?:а\s+затем|и\s+затем|затем|потом|после\s+этого|после\s+чего|далее)\b)\s*",
        re.I,
    )

    def __init__(self, services: Any):
        self.services = services
        self.tools = services.tools
        self.gateway = services.gateway
        self.db = services.db
        self.autonomous = getattr(services, "autonomous_workflow", None)
        self._desktop_lock = threading.RLock()
        self._state_lock = threading.RLock()

    @staticmethod
    def _norm(text: Any) -> str:
        s = str(text or "").casefold().replace("ё", "е")
        s = re.sub(r"[^a-zа-я0-9._@:+/-]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def _trace(self, event: str, **data: Any) -> None:
        try:
            log_event(self.services.settings.root_dir, event, **data)
        except Exception:
            pass

    def should_handle(self, goal: str) -> bool:
        text = str(goal or "").strip()
        if not text or not self._ACTION.search(text):
            return False
        apps = {name for name, pattern in self._APPS if pattern.search(text)}
        actions = len(self._ACTION.findall(text))
        file_transfer = bool(
            re.search(r"\bотправ\w*\b.{0,80}\bфайл\w*\b", text, re.I)
            and re.search(r"\b(?:telegram|телеграм\w*|телегр\w*|тг|почт\w*|discord)\b", text, re.I)
        )
        quantified_loop = bool(re.search(r"\b(?:все|всем|кажд\w*)\b.{0,60}\b(?:непрочитан|чат|сообщен)\w*", text, re.I))
        explicit_parallel = bool(self._PARALLEL.search(text))
        cross_app = len(apps) >= 2
        # Two-step same-page shopping remains on r18. r19 owns long chains, typed
        # artifact transfer, collection loops, explicit parallelism and cross-app work.
        return file_transfer or quantified_loop or explicit_parallel or cross_app or actions >= 3 or bool(self._LONG_HINT.search(text))

    def capture_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {"captured_at": time.time()}
        try:
            fg = self.tools.execute("foreground_window", {})
            if fg.get("ok"):
                foreground = dict(fg.get("result") or {})
                context["foreground"] = foreground
                # Browser tab titles often contain only the current peer/product name
                # (for example "Кирилл - Samsung Browser"). Sample a small UIA slice so
                # the mission remembers which web app owned the surface, not just title.
                cls = self._norm(foreground.get("class_name"))
                if any(mark in cls for mark in ("chrome widgetwin", "mozilla", "applicationframewindow")):
                    try:
                        elems = self.tools.execute("window_elements", {
                            "title_contains": str(foreground.get("title") or ""),
                            "max_elements": 120,
                            "handle": int(foreground.get("handle") or 0) or None,
                        })
                        rows = list(elems.get("result") or []) if elems.get("ok") else []
                        blob = self._norm(" ".join(
                            f"{row.get('name','')} {row.get('automation_id','')} {row.get('class_name','')}"
                            for row in rows[:120] if isinstance(row, dict)
                        ))
                        if any(mark in blob for mark in ("web.telegram.org", "telegram search input", "telegram-search-input", "telegram web")):
                            context["surface_app"] = "telegram"
                        elif any(mark in blob for mark in ("music.yandex", "яндекс музыка", "yandex music")):
                            context["surface_app"] = "yandex_music"
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            folder = self.tools.execute("explorer_current_folder", {})
            if folder.get("ok"):
                context["explorer_folder"] = str((folder.get("result") or {}).get("path") or "")
        except Exception:
            pass
        return context

    def _state_key(self, mission_id: str) -> str:
        return f"r19_mission_state:{mission_id}"

    def _save(self, mission: dict[str, Any]) -> None:
        with self._state_lock:
            try:
                self.db.set_setting(self._state_key(str(mission["id"])), mission)
            except Exception:
                pass

    def _load(self, mission_id: str) -> dict[str, Any] | None:
        try:
            raw = self.db.get_setting(self._state_key(mission_id), None)
            return dict(raw) if isinstance(raw, dict) and raw.get("id") else None
        except Exception:
            return None

    @staticmethod
    def _node_dict(node: MissionNode) -> dict[str, Any]:
        return asdict(node)

    @staticmethod
    def _node_from_dict(data: dict[str, Any]) -> MissionNode:
        allowed = {f.name for f in MissionNode.__dataclass_fields__.values()}
        return MissionNode(**{k: v for k, v in dict(data).items() if k in allowed})

    def _apps_for(self, text: str) -> set[str]:
        return {name for name, pattern in self._APPS if pattern.search(text)}

    @staticmethod
    def _strip_discourse_prefix(text: str) -> str:
        value = str(text or "").strip()
        # Spoken continuations are graph-control language, never a UI target. Repeatedly
        # peel them because ASR often produces "ещё после этого ..." as one clause.
        pattern = re.compile(
            r"^\s*(?:(?:и\s+)?(?:ещ[её]|заодно|дальше|далее)|(?:и\s+)?после\s+этого|"
            r"(?:и\s+)?после\s+чего|(?:и\s+)?потом|(?:и\s+)?затем)\s*[,;:.-]*\s*",
            re.I,
        )
        previous = None
        while value and value != previous:
            previous = value
            value = pattern.sub("", value, count=1).strip()
        return value

    def _context_app(self, context: dict[str, Any] | None) -> str:
        ctx = dict(context or {})
        explicit_surface = str(ctx.get("surface_app") or "").strip()
        if explicit_surface in {"telegram", "yandex_music", "browser", "files", "system"}:
            return explicit_surface
        fg = dict(ctx.get("foreground") or {})
        title = self._norm(fg.get("title"))
        cls = self._norm(fg.get("class_name"))
        if re.search(r"(?:telegram|web telegram|телеграм)", title):
            return "telegram"
        if re.search(r"(?:яндекс музык|yandex music|music yandex)", title):
            return "yandex_music"
        if "cabinetwclass" in cls or re.search(r"(?:проводник|explorer)", title):
            return "files"
        if any(mark in cls for mark in ("chrome widgetwin", "mozilla", "applicationframewindow")) or re.search(r"(?:browser|браузер|chrome|firefox|opera|samsung)", title):
            return "browser"
        return ""

    def _file_transfer_plan(self, goal: str) -> list[MissionNode] | None:
        if not (
            re.search(r"\bотправ\w*\b", goal, re.I)
            and re.search(r"\bфайл\w*\b", goal, re.I)
            and re.search(r"\b(?:telegram|телеграм\w*|телегр\w*|тг)\b", goal, re.I)
        ):
            return None
        # Capture the object independently from the destination. The filename may be a
        # bare stem (log2) or a full name. Stop before source/destination prepositions.
        m = re.search(
            r"\bфайл\w*\s+[«\"']?(.+?)[»\"']?(?=\s+(?:из|в|во|к|для)\b|[,.!?]|$)",
            goal,
            re.I,
        )
        file_name = (m.group(1) if m else "").strip(" «»\"'.,!?")
        if not file_name:
            file_name = "файл"
        recipient = "Избранное" if re.search(r"\b(?:избранн\w*|saved\s+messages|сохраненн\w*\s+сообщен)\b", goal, re.I) else ""
        if not recipient:
            rm = re.search(r"\b(?:в|для|к)\s+([A-Za-zА-Яа-яЁё0-9_ .-]{2,60})\s+(?:в\s+)?(?:telegram|телеграм\w*|телегр\w*|тг)\b", goal, re.I)
            if rm:
                recipient = rm.group(1).strip(" ,.")
        recipient = recipient or "Избранное"
        resolve = MissionNode(
            id="n1",
            goal=f"Найди файл {file_name} в исходной открытой папке",
            kind="resolve_file",
            metadata={"file_name": file_name, "source": "captured_explorer"},
        )
        send = MissionNode(
            id="n2",
            goal=f"Отправь найденный файл в Telegram, получатель {recipient}",
            kind="telegram_file",
            dependencies=["n1"],
            app="telegram",
            commit=True,
            metadata={"artifact_from": "n1", "recipient": recipient},
        )
        return [resolve, send]

    def _split_action_clauses(self, goal: str) -> list[str]:
        text = self._strip_discourse_prefix(goal)
        if not text:
            return []
        matches = list(self._ACTION_TOKEN.finditer(text))
        if len(matches) <= 1:
            return [text.strip(" ,.;")]
        parts: list[str] = []
        for index, match in enumerate(matches):
            stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            part = text[match.start():stop]
            part = re.sub(r"(?:,|;|\b(?:и|а|потом|затем|далее|после\s+этого)\b)\s*$", "", part, flags=re.I).strip(" ,.;")
            if part:
                parts.append(part)
        return parts or [text.strip(" ,.;")]

    @staticmethod
    def _open_target_text(part: str) -> str:
        value = re.sub(r"^\s*(?:открой(?:те)?|зайди(?:те)?|запусти(?:те)?)\s+", "", str(part or ""), flags=re.I)
        value = re.sub(r"^\s*(?:мне\s+)?(?:сайт|приложение)\s+", "", value, flags=re.I)
        return value.strip(" ,.;:«»\"'")

    @staticmethod
    def _is_telegram_collection(text: str) -> bool:
        q = str(text or "")
        return bool(
            re.search(r"\b(?:все|всем|кажд\w*)\b.{0,80}\b(?:непрочитан\w*|чат\w*|сообщен\w*)", q, re.I)
            and re.search(r"\b(?:ответ\w*|напиш\w*|обработ\w*)", q, re.I)
        )

    def _deterministic_plan(self, goal: str, context: dict[str, Any] | None = None) -> list[MissionNode]:
        cleaned_goal = self._strip_discourse_prefix(goal)
        special = self._file_transfer_plan(cleaned_goal)
        if special:
            return special

        context_app = self._context_app(context)
        # A collection request can omit the word Telegram when Telegram is already the
        # captured foreground surface. Keep this as one bounded collection node.
        telegram_collection_hint = bool(
            "telegram" in self._apps_for(cleaned_goal) or context_app == "telegram"
            or re.search(r"\bв\s+мо[её]м\s+стиле\b", cleaned_goal, re.I)
            or re.search(r"\bканал\w*\b", cleaned_goal, re.I)
        )
        if self._is_telegram_collection(cleaned_goal) and telegram_collection_hint:
            collection = MissionNode(
                id="n1", goal=cleaned_goal, kind="telegram_unread", app="telegram",
                commit=True, metadata={"skip_non_personal": True, "source": "visible_unread"},
            )
            # Preserve an explicit open step as a visible checkpoint; when Telegram is
            # already the captured surface the collection node can run directly.
            if "telegram" in self._apps_for(cleaned_goal) and re.search(r"^\s*(?:открой|запусти|зайди)\w*", cleaned_goal, re.I):
                opening = MissionNode(id="n1", goal="Открой Telegram", kind="app", app="telegram")
                collection.id = "n2"
                collection.dependencies = ["n1"]
                return [opening, collection]
            return [collection]

        parts = self._split_action_clauses(cleaned_goal)
        nodes: list[MissionNode] = []
        previous = ""
        inherited_app = context_app
        explicit_parallel = bool(self._PARALLEL.search(cleaned_goal))
        for index, raw_part in enumerate(parts[:14], 1):
            part = self._strip_discourse_prefix(raw_part).strip(" ,.;")
            if not part:
                continue
            apps = self._apps_for(part)
            explicit_app = next(iter(apps), "") if len(apps) == 1 else ""
            target = self._open_target_text(part) if re.match(r"^\s*(?:открой|запусти|зайди)\w*\b", part, re.I) else ""
            page_local_open = bool(target and re.match(r"^(?:раздел|каталог|корзин|товар|страниц|поиск|меню)\w*", self._norm(target), re.I))
            unknown_open_target = bool(target and not explicit_app and not page_local_open)
            # An explicit "open X" introduces a new surface and must not inherit the
            # previous app merely because X is unknown to the canonical app dictionary.
            app = "" if unknown_open_target else explicit_app
            if explicit_app:
                inherited_app = explicit_app
            elif not unknown_open_target and inherited_app:
                app = inherited_app

            kind = "ui"
            if unknown_open_target:
                kind = "open_target"
            elif app == "system":
                kind = "system"
            elif app == "telegram" and self._is_telegram_collection(part):
                kind = "telegram_unread"
            elif app == "telegram" and re.match(r"^\s*(?:напиши|отправь)\w*\b", part, re.I) and not re.search(r"\bфайл\w*", part, re.I):
                kind = "telegram_message"
            elif app == "yandex_music" and re.match(r"^\s*(?:включи|играй|продолжи|поставь)\w*\b", part, re.I):
                kind = "media"
            elif re.match(r"^\s*(?:открой|запусти|зайди)\w*\b", part, re.I) and app in {"telegram", "yandex_music"}:
                kind = "app"
            elif re.search(r"\b(?:исследуй|собери|сравни|проанализируй)\w*\b", part, re.I) and not app:
                kind = "background"

            deps = [] if (explicit_parallel and index > 1) else ([previous] if previous else [])
            node = MissionNode(
                id=f"n{len(nodes)+1}", goal=part, kind=kind, dependencies=deps,
                app=app, parallel_group="p1" if explicit_parallel else "",
                commit=bool(kind in {"telegram_message", "telegram_unread"} or re.search(r"\b(?:отправ|ответ|напиш|сообщ|удал|добав|полож|оплат|куп)\w*", part, re.I)),
            )
            if kind == "open_target":
                node.metadata["target"] = self._open_target_text(part)
            nodes.append(node)
            previous = node.id
        return nodes or [MissionNode(id="n1", goal=cleaned_goal or goal, kind="ui", app=context_app)]

    def _model_plan(self, goal: str, fallback: list[MissionNode]) -> list[MissionNode]:
        # Deterministic special plans are already semantically typed and should not be
        # rewritten by a small model.
        if any(n.kind in {"resolve_file", "telegram_file", "telegram_unread", "telegram_message", "media", "open_target"} for n in fallback):
            return fallback
        try:
            installed = list(self.gateway.installed_models())
        except Exception:
            installed = []
        if not installed:
            return fallback
        preferred = [
            getattr(self.services.settings, "action_model", ""),
            getattr(self.services.settings, "fast_model", ""),
            "qwen3:1.7b",
        ]
        model = next((m for p in preferred if p for m in installed if str(m).casefold() == str(p).casefold()), "")
        if not model:
            return fallback
        prompt = (
            "Разложи конечную цель владельца на небольшой DAG исполнимых подзадач. "
            "Нельзя придумывать приложение-специфичные координаты или скрытые данные. Один node = одна локальная цель, "
            "после которой можно наблюдать/проверить состояние. Сохраняй ссылки вида 'его/там/после этого' через dependencies. "
            "kind только ui|app|web|system|background. Если шаги должны идти последовательно, dependencies содержит id предыдущего. "
            "Если они явно независимы и пользователь сказал параллельно/одновременно — dependencies может быть пустым. "
            "Не добавляй оплату/покупку сверх цели. Максимум 12 nodes. Верни JSON {nodes:[{id,goal,kind,dependencies,app,commit}]}\n"
            f"ЦЕЛЬ: {goal}"
        )
        try:
            data = self.gateway.json(
                [{"role": "system", "content": "Ты планировщик долгих задач EIRVEN. Верни только JSON."}, {"role": "user", "content": prompt}],
                model=model, temperature=0.0, think=False, num_ctx=1500, num_predict=550, keep_alive="2h", timeout_seconds=6,
            )
            rows = list(data.get("nodes") or []) if isinstance(data, dict) else []
            if not rows:
                return fallback
            nodes: list[MissionNode] = []
            known: set[str] = set()
            for i, row in enumerate(rows[:12], 1):
                if not isinstance(row, dict):
                    continue
                nid = str(row.get("id") or f"n{i}")[:32]
                goal_text = str(row.get("goal") or "").strip()
                if not goal_text:
                    continue
                kind = str(row.get("kind") or "ui")
                if kind not in {"ui", "app", "web", "system", "background"}:
                    kind = "ui"
                deps = [str(x) for x in (row.get("dependencies") or []) if str(x) in known]
                nodes.append(MissionNode(
                    id=nid, goal=goal_text, kind=kind, dependencies=deps,
                    app=str(row.get("app") or "")[:60], commit=bool(row.get("commit")),
                ))
                known.add(nid)
            return nodes or fallback
        except Exception as exc:
            self._trace("R19_PLAN_FALLBACK", goal=goal, error=str(exc))
            return fallback

    def plan(self, goal: str, *, mission_id: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        nodes = self._deterministic_plan(goal, context=context)
        # Only ask the local planner when deterministic splitting cannot express a
        # multi-action goal cleanly. This keeps ordinary cross-app plans fast.
        if len(nodes) == 1 and len(self._ACTION.findall(goal)) >= 3:
            nodes = self._model_plan(goal, nodes)
        mid = mission_id or uuid.uuid4().hex
        mission = {
            "id": mid,
            "goal": str(goal or "").strip(),
            "status": "queued",
            "nodes": [self._node_dict(n) for n in nodes],
            "artifacts": {},
            "context": dict(context or {}),
            "created_at": time.time(),
            "updated_at": time.time(),
            "revision": 1,
        }
        self._trace("R19_PLAN", mission_id=mid, goal=goal, nodes=mission["nodes"])
        return mission

    def _resolve_file(self, node: MissionNode, mission: dict[str, Any]) -> dict[str, Any]:
        name = str(node.metadata.get("file_name") or "").strip()
        context = dict(mission.get("context") or {})
        roots: list[Path] = []
        captured = str(context.get("explorer_folder") or "").strip()
        if captured:
            roots.append(Path(captured))
        if not roots:
            try:
                live = self.tools.execute("explorer_current_folder", {})
                if live.get("ok"):
                    roots.append(Path(str((live.get("result") or {}).get("path") or "")))
            except Exception:
                pass
        needle = self._norm(name)
        compact_needle = re.sub(r"[^a-zа-я0-9]+", "", needle)
        try:
            from difflib import SequenceMatcher
        except Exception:
            SequenceMatcher = None  # type: ignore[assignment]
        candidates: list[tuple[float, Path]] = []
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for path in root.iterdir():
                    if not path.is_file():
                        continue
                    label = self._norm(path.name)
                    stem = self._norm(path.stem)
                    score = 0.0
                    if label == needle or stem == needle:
                        score = 100.0
                    elif needle and (needle in label or needle in stem):
                        score = 70.0 - abs(len(label) - len(needle)) * 0.1
                    elif label and needle and all(tok in label for tok in needle.split()):
                        score = 55.0
                    elif compact_needle and SequenceMatcher is not None:
                        compact_label = re.sub(r"[^a-zа-я0-9]+", "", stem or label)
                        ratio = SequenceMatcher(None, compact_needle, compact_label).ratio() if compact_label else 0.0
                        # ASR frequently turns short Latin filenames into phonetic-looking
                        # tokens (log2 -> Loce 2). Fuzzy matching is constrained to the
                        # captured folder and requires a high ratio, so it cannot roam the PC.
                        if ratio >= .58:
                            score = 42.0 + ratio * 22.0
                    if score:
                        candidates.append((score, path))
            except Exception:
                continue
        if not candidates:
            try:
                found = self.tools.execute("system_find", {"name": name})
                rows = list(found.get("result") or []) if found.get("ok") else []
                for row in rows:
                    p = Path(str((row or {}).get("path") or ""))
                    if p.is_file():
                        candidates.append((40.0, p))
            except Exception:
                pass
        if not candidates:
            return {"ok": False, "verified": False, "error": f"Файл «{name}» не найден в исходной папке"}
        candidates.sort(key=lambda x: (x[0], -len(str(x[1]))), reverse=True)
        best = candidates[0][1].resolve()
        artifact = {"type": "file", "path": str(best), "name": best.name, "size": best.stat().st_size}
        mission.setdefault("artifacts", {})[node.id] = artifact
        return {"ok": True, "verified": True, "completed": True, "artifact": artifact}

    def _send_telegram_file(self, node: MissionNode, mission: dict[str, Any]) -> dict[str, Any]:
        source_id = str(node.metadata.get("artifact_from") or "")
        artifact = dict((mission.get("artifacts") or {}).get(source_id) or {})
        path = str(artifact.get("path") or "")
        recipient = str(node.metadata.get("recipient") or "Избранное")
        if not path or not Path(path).is_file():
            return {"ok": False, "verified": False, "error": "Артефакт файла потерян до шага отправки"}
        operator = getattr(self.services, "desktop_operator", None)
        sender = getattr(operator, "telegram_send_file", None)
        if not callable(sender):
            return {"ok": False, "verified": False, "error": "Контур отправки файлов Telegram недоступен"}
        result = dict(sender(recipient, path) or {})
        return {"ok": bool(result.get("ok") or result.get("sent")), "verified": bool(result.get("verified")), "completed": bool(result.get("sent") or result.get("completed")), **result}

    def _telegram_message(self, node: MissionNode) -> dict[str, Any]:
        chat = getattr(self.services, "chat", None)
        sender = getattr(chat, "_telegram_send_turn", None)
        if not callable(sender):
            return {"ok": False, "verified": False, "error": "Telegram message lane unavailable"}
        target = re.sub(r"^\s*(?:напиши|отправь)\w*\s+", "", node.goal, flags=re.I).strip()
        if not re.search(r"\b(?:telegram|телеграм\w*|телегр\w*|тг)\b", target, re.I):
            target = f"{target} в telegram"
        acted, answer, route = sender(target)
        route = dict(route or {})
        result = dict(route.get("result") or {})
        completed = bool(result.get("sent") or result.get("completed") or route.get("completed") or route.get("action") in {"telegram_send_verified", "telegram_send_unverified"})
        verified = bool(result.get("verified") or route.get("verified") or route.get("action") == "telegram_send_verified")
        return {
            "ok": bool(acted and completed), "completed": completed, "verified": verified,
            "summary": str(answer or ""), "route": route,
            "error": "" if completed else str(result.get("error") or answer or "Telegram message failed"),
        }

    def _media_node(self, node: MissionNode, stop_event: threading.Event) -> dict[str, Any]:
        workflow = getattr(self.services, "universal_workflow", None)
        ensure = getattr(workflow, "ensure_media_goal", None)
        if not callable(ensure):
            return {"ok": False, "verified": False, "error": "Media state lane unavailable"}
        result = ensure(node.goal, allow_implicit=True, stop_event=stop_event)
        if not isinstance(result, dict):
            return {"ok": False, "verified": False, "error": "Current surface is not recognized as media"}
        return dict(result)

    def _open_target(self, node: MissionNode) -> dict[str, Any]:
        target = str(node.metadata.get("target") or self._open_target_text(node.goal)).strip()
        if not target:
            return {"ok": False, "verified": False, "error": "Не распознана цель открытия"}
        skills = getattr(self.services, "app_skills", None)
        if skills is not None:
            try:
                result = dict(skills.open(target) or {})
                if result.get("ok"):
                    return {"ok": True, "completed": True, "verified": bool(result.get("verified", True)), "result": result, "target": target}
            except Exception:
                pass
        applications = getattr(self.services, "applications", None)
        if applications is not None:
            try:
                result = dict(applications.web_fallback(target) or {})
                return {"ok": bool(result.get("url")), "completed": bool(result.get("url")), "verified": bool(result.get("url")), "result": result, "target": target}
            except Exception as exc:
                return {"ok": False, "verified": False, "error": str(exc), "target": target}
        return {"ok": False, "verified": False, "error": f"Не удалось открыть {target}"}

    @staticmethod
    def _telegram_row_unread_count(raw: str) -> int:
        # Telegram Web A often exposes unread badges only as a trailing integer in the
        # accessible ListItem name. Keep it bounded to the left chat-list row parser.
        m = re.search(r"(?:^|\s)(\d{1,3})\s*$", str(raw or "").strip())
        return int(m.group(1)) if m else 0

    @staticmethod
    def _telegram_header_kind(rows: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for el in rows:
            rect = el.get("rectangle") or []
            if len(rect) == 4 and int(rect[1]) <= 430 and int(rect[0]) >= 850:
                parts.append(str(el.get("name") or ""))
        blob = " ".join(parts).casefold().replace("ё", "е")
        if re.search(r"\b(?:subscribers?|подписчик\w*|channel|канал\w*|members?|участник\w*|group|группа)\b", blob, re.I):
            return "non_personal"
        if re.search(r"\b(?:last seen|online|был(?:а)?|в сети|заходил(?:а)?)\b", blob, re.I):
            return "personal"
        return "unknown"

    def _telegram_unread_replies(self, node: MissionNode, stop_event: threading.Event) -> dict[str, Any]:
        """Reply once to visible unread *personal* chats, skipping channels/groups.

        Telegram Web A does not reliably expose the literal word "unread". Its UIA row
        commonly ends with the unread badge number, so discovery accepts that bounded
        semantic and then validates the opened chat header before any message is sent.
        """
        operator = getattr(self.services, "desktop_operator", None)
        chat = getattr(self.services, "chat", None)
        reply = getattr(chat, "_telegram_style_reply", None)
        if operator is None or not callable(reply):
            return {"ok": False, "verified": False, "error": "Telegram style-reply lane unavailable"}
        win = operator.wait_window(["Telegram", "web.telegram", "Телеграм"], .5)
        if not win:
            opened = self._open_app(MissionNode(id="open-tg", goal="Открой Telegram", kind="app", app="telegram"))
            if not opened.get("ok"):
                return {"ok": False, "verified": False, "error": "Telegram не открыт"}
            win = operator.wait_window(["Telegram", "web.telegram", "Телеграм"], 6.0)
        if not win:
            return {"ok": False, "verified": False, "error": "Telegram не появился"}
        handle = int(win.get("handle") or 0) or None
        title = str(win.get("title") or "Telegram")
        replied: set[str] = set()
        skipped: set[str] = set()
        outcomes: list[dict[str, Any]] = []

        def back_to_list() -> None:
            try:
                rows = operator._elements(title, limit=180, handle=handle)
                back = next((el for el in rows if el.get("visible", True) and operator._norm(el.get("name")) in {"return to chat list", "назад к списку чатов", "назад"}), None)
                if back:
                    operator.click_element(title, back, goal="r20_telegram_back_to_chat_list")
                    time.sleep(.18)
            except Exception:
                pass

        back_to_list()
        for _round in range(28):
            if stop_event.is_set():
                return {"ok": False, "verified": False, "error": "cancelled", "replied": sorted(replied)}
            rows = operator._elements(title, limit=650, handle=handle)
            candidates: list[tuple[int, dict[str, Any], str]] = []
            for el in rows:
                if not el.get("visible", True) or not el.get("enabled", True):
                    continue
                rect = el.get("rectangle") or []
                if len(rect) != 4 or int(rect[0]) > 950 or int(rect[1]) < 380:
                    continue
                ctype = operator._norm(el.get("control_type"))
                cls = operator._norm(el.get("class_name"))
                if ctype not in {"button", "listitem", "hyperlink"}:
                    continue
                raw = str(el.get("name") or "").strip()
                blob = operator._norm(f"{raw} {cls} {el.get('automation_id','')}")
                explicit = any(m in blob for m in ("unread", "непрочитан", "badge unread", "has unread"))
                numeric = self._telegram_row_unread_count(raw) if "listitem button" in cls or "listitem-button" in str(el.get("class_name") or "").casefold() else 0
                if not raw or not explicit and not numeric:
                    continue
                name = re.split(r"\s{2,}|\b(?:unread|непрочитан)\b", raw, maxsplit=1, flags=re.I)[0].strip()
                key = self._norm(name)
                if not key or key in replied or key in skipped:
                    continue
                candidates.append((int(rect[1]), el, name))
            if not candidates:
                break
            candidates.sort(key=lambda x: x[0])
            _y, element, recipient = candidates[0]
            key = self._norm(recipient)
            if not operator.click_element(title, element, goal="r20_open_unread_candidate"):
                skipped.add(key)
                continue
            time.sleep(.25)
            opened_rows = operator._elements(title, limit=260, handle=handle)
            kind = self._telegram_header_kind(opened_rows)
            if kind != "personal":
                skipped.add(key)
                self._trace("R20_TG_SKIP_NONPERSONAL", recipient=recipient, kind=kind)
                back_to_list()
                continue
            acted, answer, route = reply(recipient=recipient)
            route = dict(route or {})
            inner = dict(route.get("result") or {}) if isinstance(route.get("result"), dict) else {}
            completed = bool(route.get("completed") or inner.get("completed") or route.get("action") == "telegram_style_reply")
            verified = bool(route.get("verified") or inner.get("verified"))
            outcomes.append({"recipient": recipient, "answer": answer, "completed": completed, "verified": verified})
            replied.add(key)
            if completed and not verified:
                return {"ok": False, "completed": True, "verified": False, "error": f"Ответ {recipient} отправлен один раз, но не подтверждён; повтор заблокирован", "outcomes": outcomes}
            back_to_list()
        return {"ok": True, "completed": True, "verified": True, "replied_count": len(outcomes), "skipped_count": len(skipped), "outcomes": outcomes, "collection_exhausted": True}

    def _open_app(self, node: MissionNode) -> dict[str, Any]:
        app = node.app or node.goal
        target = {
            "telegram": "Telegram",
            "yandex_music": "Яндекс Музыка",
        }.get(app, app)
        skills = getattr(self.services, "app_skills", None)
        try:
            if skills is not None:
                result = dict(skills.open(target) or {})
                return {"ok": bool(result.get("ok")), "verified": bool(result.get("verified", result.get("ok"))), "completed": bool(result.get("ok")), **result}
            result = self.tools.execute("launch_application", {"application": target})
            return {"ok": bool(result.get("ok")), "verified": bool(result.get("ok")), "completed": bool(result.get("ok")), "result": result}
        except Exception as exc:
            return {"ok": False, "verified": False, "error": str(exc)}

    def _system_node(self, node: MissionNode) -> dict[str, Any]:
        q = self._norm(node.goal)
        amount = re.search(r"\bна\s+(\d{1,2})\b", q)
        steps = max(1, min(int(amount.group(1)), 10)) if amount else 2
        if re.search(r"\b(?:увелич|прибав|повыс|погромч)\w*\b.{0,40}\b(?:системн\w*\s+)?(?:громк|звук)\w*", q):
            r = self.tools.execute("system_volume", {"action": "up", "steps": steps})
            return {"ok": bool(r.get("ok")), "verified": bool(r.get("ok")), "completed": bool(r.get("ok")), "result": r}
        if re.search(r"\b(?:уменьш|убав|пониз|потиш)\w*\b.{0,40}\b(?:системн\w*\s+)?(?:громк|звук)\w*", q):
            r = self.tools.execute("system_volume", {"action": "down", "steps": steps})
            return {"ok": bool(r.get("ok")), "verified": bool(r.get("ok")), "completed": bool(r.get("ok")), "result": r}
        if re.search(r"\b(?:закрой|заверши|убей|останови)\w*.{0,30}\b(?:python|пайтон)\b", q):
            r = self.tools.execute("process_terminate", {"name_contains": "python", "all_matches": True, "protect_eirven": True})
            payload = dict(r.get("result") or {}) if r.get("ok") else {}
            return {"ok": bool(r.get("ok")), "verified": bool(payload.get("verified")), "completed": bool(r.get("ok")), "result": r}
        return {"ok": False, "verified": False, "error": "Нет детерминированного системного примитива для узла"}

    def _ui_node(self, node: MissionNode, mission_id: str, stop_event: threading.Event) -> dict[str, Any]:
        # A node gets a private checkpoint namespace. Window anchors are reset by the
        # autonomous engine between calls, so cross-app transitions are intentional.
        engine = self.autonomous or getattr(self.services, "autonomous_workflow", None)
        if engine is not None:
            result = engine.execute_goal(
                node.goal, conversation_id=f"mission:{mission_id}:{node.id}",
                stop_event=stop_event, max_steps=32,
            )
            return {
                "ok": bool(result.ok), "verified": bool(result.ok), "completed": bool(result.ok),
                "summary": result.summary, "needs_user": bool(result.needs_user),
                "prompt": result.prompt, "steps": result.steps,
            }
        return {"ok": False, "verified": False, "error": "Autonomous workflow unavailable"}

    def _background_node(self, node: MissionNode, stop_event: threading.Event) -> dict[str, Any]:
        agent = getattr(self.services, "agent", None)
        router = getattr(self.services, "router", None)
        if agent is None or router is None:
            return {"ok": False, "verified": False, "error": "Фоновый агент недоступен"}
        try:
            report = agent.run(node.goal, model=router.agent_model(node.goal), max_steps=10, external_stop_event=stop_event)
            return {"ok": True, "verified": True, "completed": True, "report": str(report)}
        except Exception as exc:
            return {"ok": False, "verified": False, "error": str(exc)}

    def _execute_node(self, node: MissionNode, mission: dict[str, Any], stop_event: threading.Event) -> dict[str, Any]:
        if stop_event.is_set():
            return {"ok": False, "verified": False, "error": "cancelled"}
        if node.kind == "resolve_file":
            return self._resolve_file(node, mission)
        if node.kind == "telegram_file":
            with self._desktop_lock:
                return self._send_telegram_file(node, mission)
        if node.kind == "telegram_unread":
            with self._desktop_lock:
                return self._telegram_unread_replies(node, stop_event)
        if node.kind == "telegram_message":
            with self._desktop_lock:
                return self._telegram_message(node)
        if node.kind == "media":
            with self._desktop_lock:
                return self._media_node(node, stop_event)
        if node.kind == "open_target":
            with self._desktop_lock:
                return self._open_target(node)
        if node.kind == "app":
            with self._desktop_lock:
                return self._open_app(node)
        if node.kind == "system":
            return self._system_node(node)
        if node.kind == "background":
            return self._background_node(node, stop_event)
        with self._desktop_lock:
            return self._ui_node(node, str(mission["id"]), stop_event)

    def _apply_live_instructions(self, context: Any, mission: dict[str, Any], consumed: int) -> int:
        try:
            instructions = list(context.manager.live_instructions(context.task_id))
        except Exception:
            return consumed
        if consumed >= len(instructions):
            return consumed
        pending = instructions[consumed:]
        nodes = [self._node_from_dict(x) for x in mission.get("nodes") or []]
        last_id = nodes[-1].id if nodes else ""
        for instruction in pending:
            text = str(instruction or "").strip()
            if not text:
                continue
            extras = self._deterministic_plan(text, context=dict(mission.get("context") or {}))
            for extra in extras:
                new_id = f"n{len(nodes)+1}"
                extra.id = new_id
                if not self._PARALLEL.search(text) and last_id:
                    extra.dependencies = [last_id]
                nodes.append(extra)
                last_id = new_id
            mission["revision"] = int(mission.get("revision") or 1) + 1
            self._trace("R19_LIVE_UPDATE", mission_id=mission.get("id"), instruction=text, revision=mission["revision"])
        mission["nodes"] = [self._node_dict(n) for n in nodes]
        mission["updated_at"] = time.time()
        self._save(mission)
        return len(instructions)

    def run_task(self, context: Any, payload: dict[str, Any]) -> dict[str, Any]:
        goal = str(payload.get("goal") or payload.get("task") or "").strip()
        if not goal:
            raise ValueError("Пустая миссия")
        mission_id = str(payload.get("mission_id") or context.task_id)
        mission = self._load(mission_id)
        if mission is None:
            mission = self.plan(goal, mission_id=mission_id, context=dict(payload.get("context") or {}))
        else:
            previous_status = str(mission.get("status") or "")
            repaired_nodes = []
            for raw in list(mission.get("nodes") or []):
                row = dict(raw)
                status = str(row.get("status") or "pending")
                result = dict(row.get("result") or {})
                commit_uncertain = bool(row.get("commit") and result.get("completed") and not result.get("verified"))
                # A crash can leave a node marked running; a user checkpoint can leave it
                # waiting_user. Both resume from a fresh observation. A manually retried
                # failed mission gets one new attempt except for uncertain committed
                # side effects, which remain single-shot forever.
                if status in {"running", "waiting_user"}:
                    row["status"] = "pending"
                elif previous_status == "failed" and status == "failed" and not commit_uncertain:
                    row["status"] = "pending"
                    row["attempts"] = max(0, min(int(row.get("attempts") or 0), 1))
                repaired_nodes.append(row)
            mission["nodes"] = repaired_nodes
        mission["status"] = "running"
        mission["updated_at"] = time.time()
        self._save(mission)

        nodes = [self._node_from_dict(x) for x in mission.get("nodes") or []]
        context.set_total(max(1, len(nodes)))
        consumed_instructions = 0
        completed = sum(1 for n in nodes if n.status == "done")
        context.update("Восстанавливаю граф миссии" if completed else "Запускаю миссию", completed_steps=completed, progress=(completed / max(1, len(nodes))) * .95)
        self._trace("R19_BEGIN", mission_id=mission_id, goal=goal, resumed=bool(completed), nodes=len(nodes))

        while True:
            context.check_cancelled()
            consumed_instructions = self._apply_live_instructions(context, mission, consumed_instructions)
            nodes = [self._node_from_dict(x) for x in mission.get("nodes") or []]
            by_id = {n.id: n for n in nodes}
            if all(n.status == "done" for n in nodes):
                break
            ready = [
                n for n in nodes if n.status in {"pending", "failed"}
                and n.attempts < 2
                and all(by_id.get(dep) and by_id[dep].status == "done" for dep in n.dependencies)
            ]
            if not ready:
                failed = [n for n in nodes if n.status == "failed"]
                mission["status"] = "failed"
                mission["updated_at"] = time.time()
                mission["nodes"] = [self._node_dict(n) for n in nodes]
                self._save(mission)
                reason = str((failed[0].result if failed else {}).get("error") or "Нет исполнимых узлов")
                self._trace("R19_FAIL", mission_id=mission_id, reason=reason)
                return {"ok": False, "mission_id": mission_id, "summary": f"Миссия остановилась: {reason}", "nodes": mission["nodes"], "artifacts": mission.get("artifacts", {})}

            # Independent background nodes may run concurrently. UI nodes are still
            # protected by _desktop_lock even if this wave contains several of them.
            results: dict[str, dict[str, Any]] = {}
            workers = min(3, len(ready))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eirven-mission") as pool:
                futures = {}
                for node in ready:
                    node.status = "running"
                    node.attempts += 1
                    self._trace("R19_NODE_BEGIN", mission_id=mission_id, node=self._node_dict(node))
                    futures[pool.submit(self._execute_node, node, mission, context.stop_event)] = node
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        results[node.id] = dict(future.result() or {})
                    except Exception as exc:
                        results[node.id] = {"ok": False, "verified": False, "error": str(exc)}

            needs_user = None
            for node in ready:
                result = results.get(node.id, {"ok": False, "verified": False, "error": "no result"})
                node.result = result
                node.verified = bool(result.get("verified"))
                if result.get("needs_user"):
                    node.status = "waiting_user"
                    needs_user = (node, str(result.get("prompt") or result.get("summary") or "Нужен ручной шаг"))
                elif result.get("ok") and (result.get("verified") or not node.commit):
                    node.status = "done"
                elif result.get("completed") and node.commit:
                    # A committed side effect happened once; uncertainty must never turn
                    # into an automatic duplicate on retry.
                    node.status = "failed"
                    result.setdefault("error", "Committed action completed once but could not be verified; retry blocked")
                else:
                    node.status = "failed"
                    error_text = self._norm(result.get("error") or result.get("summary") or "")
                    terminal_policy_failure = node.kind == "ui" and any(mark in error_text for mark in ("local policy unavailable", "локальная модель не ответила", "local policy"))
                    if terminal_policy_failure:
                        node.attempts = 2
                        self._trace("R20_NO_BLIND_RETRY", mission_id=mission_id, node_id=node.id, error=str(result.get("error") or result.get("summary") or ""))
                    elif node.attempts < 2:
                        self._trace("R19_RECOVER", mission_id=mission_id, node_id=node.id, strategy="fresh-observation-retry", error=str(result.get("error") or result.get("summary") or ""))
                self._trace("R19_NODE_END", mission_id=mission_id, node_id=node.id, status=node.status, result=result)

            mission["nodes"] = [self._node_dict(n) for n in nodes]
            mission["updated_at"] = time.time()
            self._save(mission)
            completed = sum(1 for n in nodes if n.status == "done")
            context.set_total(max(1, len(nodes)))
            context.update(
                f"Миссия: {completed}/{len(nodes)} шагов подтверждено",
                completed_steps=completed,
                progress=min(.97, completed / max(1, len(nodes))),
                data={"mission_id": mission_id, "revision": mission.get("revision", 1)},
            )
            if needs_user:
                node, prompt = needs_user
                mission["status"] = "waiting_user"
                self._save(mission)
                # TaskManager understands TaskNeedsUser, but importing it here would make
                # this engine tightly coupled. Raise the same public exception lazily.
                from .tasks import TaskNeedsUser
                raise TaskNeedsUser(prompt)

        mission["status"] = "completed"
        mission["updated_at"] = time.time()
        mission["nodes"] = [self._node_dict(n) for n in nodes]
        self._save(mission)
        self._trace("R19_DONE", mission_id=mission_id, goal=goal, nodes=len(nodes))
        return {
            "ok": True,
            "mission_id": mission_id,
            "summary": f"Миссия выполнена: {len(nodes)} из {len(nodes)} шагов подтверждены.",
            "nodes": mission["nodes"],
            "artifacts": mission.get("artifacts", {}),
            "revision": mission.get("revision", 1),
        }
