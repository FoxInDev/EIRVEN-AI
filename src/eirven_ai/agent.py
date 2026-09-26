from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import pace
from .config import Settings
from .llm import LLMError, ModelGateway
from .resilience import AdaptiveRecovery
from .risk_policy import RiskPolicy
from .style import StyleStore
from .tasks import TaskNeedsUser
from .trace import log_event
from .tools import ToolExecutor


@dataclass(slots=True)
class SurfaceLease:
    """One concrete Windows surface owned by a single reactive task."""

    handle: int = 0
    title: str = ""
    pid: int = 0
    rectangle: tuple[int, int, int, int] | None = None
    observed_at: float = 0.0
    generation: int = 0

    @property
    def bound(self) -> bool:
        return bool(self.handle)


@dataclass(slots=True)
class MediaUIRecovery:
    """Bounded recovery after a transport command finds no GSMTC session.

    The state is deliberately engine-owned.  The policy model may choose a service or
    an initial transport command, but it cannot reuse a stale accessibility id, click a
    preview card, or retry the same no-session command indefinitely.
    """

    action: str = "play"
    target: str = ""
    service: str = ""
    phase: str = "need_focus"
    handle: int = 0
    pid: int = 0
    preflight: bool = False
    clicks: int = 0
    fresh_stable_ids: tuple[str, ...] = ()
    surface_generation: int = -1
    scan_serial: int = -1
    candidate: dict[str, Any] = field(default_factory=dict)
    before_session_ids: tuple[str, ...] = ()
    content_required: bool = False
    content_selected: bool = False
    exact_surface: bool = False
    verified_by: str = ""
    vision_analysis: dict[str, Any] = field(default_factory=dict)
    vision_meta: dict[str, Any] = field(default_factory=dict)
    refresh_attempts: int = 0


class LocalAgent:
    """Native tool-calling computer agent.

    The model never emits an intermediate JSON plan that another personality interprets.
    It sees the real tool catalogue, calls tools, receives observations, and continues in
    the same conversation until it has a factual final result.
    """

    @staticmethod
    def _coerce_id(value: Any) -> int:
        """Accept integer HWND/PID values and JSON/model scientific notation."""
        try:
            if isinstance(value, str) and any(mark in value.casefold() for mark in ("e", ".")):
                return int(float(value))
            return int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    def __init__(
        self,
        settings: Settings,
        gateway: ModelGateway,
        tools: ToolExecutor,
        style: StyleStore,
    ):
        self.settings = settings
        self.gateway = gateway
        self.tools = tools
        self.style = style
        self._run_outcome = threading.local()

    def _set_run_outcome(
        self, *, used_tool: bool = False, used_side_effect: bool = False,
        verified: bool = False, **extra: Any,
    ) -> None:
        self._run_outcome.value = {
            "used_tool": bool(used_tool),
            "used_side_effect": bool(used_side_effect),
            "verified": bool(verified),
            **extra,
        }

    def last_run_outcome(self) -> dict[str, Any]:
        value = getattr(self._run_outcome, "value", None)
        return dict(value) if isinstance(value, dict) else {"used_tool": False, "used_side_effect": False, "verified": False}

    @staticmethod
    def _surface_allowed(task: str, row: dict[str, Any]) -> bool:
        title = str(row.get("title") or row.get("window") or "").casefold()
        try:
            handle = LocalAgent._coerce_id(row.get("handle") or row.get("surface_handle"))
        except (TypeError, ValueError):
            handle = 0
        if not title or not handle:
            return False
        protected = ("eirven", "эрви", "codex", "chatgpt")
        task_n = str(task or "").casefold()
        return not (any(token in title for token in protected) and not any(token in task_n for token in protected))

    @classmethod
    def _bind_surface(cls, lease: SurfaceLease, task: str, row: dict[str, Any]) -> bool:
        try:
            handle = LocalAgent._coerce_id(row.get("handle") or row.get("surface_handle"))
        except (TypeError, ValueError):
            return False
        same_surface = bool(handle and handle == lease.handle)
        previous_handle = lease.handle
        candidate = {
            "handle": handle,
            "title": str(row.get("title") or row.get("window") or (lease.title if same_surface else "")),
            "pid": row.get("pid") or (lease.pid if same_surface else 0),
            "rectangle": row.get("rectangle") or (lease.rectangle if same_surface else None),
        }
        try:
            candidate["pid"] = LocalAgent._coerce_id(candidate["pid"])
        except (TypeError, ValueError):
            candidate["pid"] = 0
        if not cls._surface_allowed(task, candidate):
            return False
        rect = candidate.get("rectangle")
        lease.handle = handle
        lease.title = str(candidate["title"])
        lease.pid = LocalAgent._coerce_id(candidate["pid"])
        lease.rectangle = (
            tuple(int(x) for x in rect[:4])  # type: ignore[assignment]
            if isinstance(rect, (list, tuple)) and len(rect) >= 4 else None
        )
        lease.observed_at = time.monotonic()
        if handle != previous_handle:
            lease.generation += 1
        return True

    @classmethod
    def _adopt_related_surface(
        cls,
        lease: SurfaceLease,
        task: str,
        row: dict[str, Any],
        *,
        allow_unbound: bool = False,
    ) -> bool:
        """Adopt only the same surface or a causally related popup.

        Ambient foreground is not ownership evidence: a notification, Codex or the
        EIRVEN shell may steal focus between an action and its observation.  A new HWND
        is adopted automatically only when it belongs to the same process.  Cross-process
        dialogs remain visible to the model through ``foreground_window/window_list`` and
        require an explicit ``window_focus`` decision.
        """
        if not cls._surface_allowed(task, row):
            return False
        try:
            handle = LocalAgent._coerce_id(row.get("handle") or row.get("surface_handle"))
            pid = LocalAgent._coerce_id(row.get("pid"))
        except (TypeError, ValueError):
            return False
        if not lease.bound:
            return bool(allow_unbound and cls._bind_surface(lease, task, row))
        if handle == lease.handle:
            return cls._bind_surface(lease, task, row)
        if lease.pid and pid and lease.pid == pid:
            return cls._bind_surface(lease, task, row)
        return False

    @classmethod
    def _verified_target_surface(
        cls,
        tool_name: str,
        tool_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Extract the surface causally returned by a verified opener/launcher.

        ``open_service`` and ``launch_application`` already perform their own launch
        observation and often return the real target under ``window`` or
        ``observation.window``.  That evidence outranks whatever happened to be
        foreground a few milliseconds later.
        """
        if tool_name not in {
            "open_service", "launch_application", "open_default_url", "default_search",
            "system_open_named", "system_open_path",
        }:
            return None
        outer_verified = cls._explicitly_verified(tool_result)
        if not outer_verified:
            return None

        def walk(value: Any, depth: int = 0) -> dict[str, Any] | None:
            if depth > 5:
                return None
            if isinstance(value, dict):
                try:
                    handle = LocalAgent._coerce_id(value.get("handle") or value.get("surface_handle") or value.get("hwnd"))
                except (TypeError, ValueError):
                    handle = 0
                title = str(value.get("title") or value.get("window") or value.get("name") or "").strip()
                if handle and title:
                    return {
                        "handle": handle,
                        "title": title,
                        "pid": value.get("pid") or value.get("process_id") or 0,
                        "rectangle": value.get("rectangle") or value.get("bounds"),
                    }
                for key in ("window", "observation", "surface", "target", "result", "route"):
                    candidate = walk(value.get(key), depth + 1)
                    if candidate:
                        return candidate
            elif isinstance(value, list):
                for item in value[:12]:
                    candidate = walk(item, depth + 1)
                    if candidate:
                        return candidate
            return None

        return walk(tool_result)

    @staticmethod
    def _lease_context(lease: SurfaceLease) -> str:
        if not lease.bound:
            return "SURFACE_LEASE=<none: call window_list, then window_focus or open_service>"
        return (
            f"SURFACE_LEASE=handle:{lease.handle};title:{lease.title};pid:{lease.pid};"
            f"rectangle:{lease.rectangle or '<unknown>'};generation:{lease.generation};age_seconds:"
            f"{max(0.0, time.monotonic() - lease.observed_at):.1f}"
        )

    def stop(self) -> None:
        # Emergency stop only. Normal task cancellation is passed as a scoped token.
        self.tools.stop()

    @staticmethod
    def _vision_schema() -> dict[str, Any]:
        target = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "label": {"type": "string", "maxLength": 120},
                "role": {"type": "string", "maxLength": 48},
                "action": {"type": "string", "maxLength": 48},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "string", "maxLength": 240},
            },
            "required": ["label", "role", "action", "x", "y", "confidence", "evidence"],
        }
        blocker = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "label": {"type": "string", "maxLength": 120},
                "evidence": {"type": "string", "maxLength": 240},
            },
            "required": ["label", "evidence"],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "maxLength": 240},
                "screen_state": {"type": "string", "maxLength": 180},
                "goal_reached": {"type": "boolean"},
                "primary_target": target,
                "controls": {"type": "array", "maxItems": 4, "items": target},
                "blockers": {"type": "array", "maxItems": 3, "items": blocker},
            },
            "required": [
                "summary", "screen_state", "goal_reached", "primary_target", "controls", "blockers",
            ],
        }

    @staticmethod
    def _empty_vision(reason: str = "") -> dict[str, Any]:
        evidence = str(reason or "Структурированный vision-анализ недоступен").strip()[:180]
        return {
            "summary": "Vision не дал надёжного результата",
            "screen_state": "unknown",
            "goal_reached": False,
            "primary_target": {
                "label": "", "role": "", "action": "", "x": 0, "y": 0,
                "confidence": 0.0, "evidence": "",
            },
            "controls": [],
            "blockers": [{"label": "vision_unavailable", "evidence": evidence}],
        }

    @staticmethod
    def _normalize_vision_analysis(raw: Any, width: int, height: int) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return LocalAgent._empty_vision("Ответ vision был обрезан или не являлся JSON")
        required = {"summary", "screen_state", "goal_reached", "primary_target", "controls", "blockers"}
        if not isinstance(raw, dict) or not required <= set(raw) or width <= 0 or height <= 0:
            return LocalAgent._empty_vision("Ответ vision не соответствует обязательной схеме или геометрии")

        def text(value: Any, limit: int) -> str:
            return str(value or "").strip()[:limit]

        def target(value: Any) -> tuple[dict[str, Any] | None, bool]:
            if not isinstance(value, dict):
                return None, False
            try:
                x = int(value.get("x"))
                y = int(value.get("y"))
                confidence = float(value.get("confidence"))
            except (TypeError, ValueError):
                return None, False
            if not (0.0 <= confidence <= 1.0):
                return None, False
            normalized = {
                "label": text(value.get("label"), 120),
                "role": text(value.get("role"), 48),
                "action": text(value.get("action"), 48),
                "x": x,
                "y": y,
                "confidence": round(confidence, 4),
                "evidence": text(value.get("evidence"), 240),
            }
            described = bool(
                normalized["label"] or normalized["role"] or normalized["action"]
                or normalized["evidence"] or confidence > 0
            )
            if described and not (0 <= x < width and 0 <= y < height):
                return None, False
            return normalized, True

        primary, primary_valid = target(raw.get("primary_target"))
        if primary is None:
            primary = LocalAgent._empty_vision()["primary_target"]
        controls: list[dict[str, Any]] = []
        source_controls = raw.get("controls") if isinstance(raw.get("controls"), list) else []
        for item in source_controls[:4]:
            normalized, valid = target(item)
            if valid and normalized is not None:
                controls.append(normalized)
        blockers: list[dict[str, str]] = []
        source_blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
        for item in source_blockers[:3]:
            if not isinstance(item, dict):
                continue
            blockers.append({
                "label": text(item.get("label"), 120),
                "evidence": text(item.get("evidence"), 240),
            })
        if not primary_valid:
            blockers = ([{
                "label": "invalid_primary_target",
                "evidence": "Координата или формат primary_target не прошли проверку",
            }] + blockers)[:3]
        summary = text(raw.get("summary"), 240)
        screen_state = text(raw.get("screen_state"), 180)
        reached = bool(raw.get("goal_reached") is True)
        if reached and not (
            primary_valid and summary and screen_state and str(primary.get("evidence") or "").strip()
        ):
            reached = False
        return {
            "summary": summary,
            "screen_state": screen_state,
            "goal_reached": reached,
            "primary_target": primary,
            "controls": controls,
            "blockers": blockers,
        }

    @staticmethod
    def _vision_uia_context(
        rows: list[dict[str, Any]], screenshot_meta: dict[str, Any], goal: str,
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        try:
            compact = json.loads(LocalAgent._compact_result(
                {"ok": True, "result": rows}, 7000, goal,
            ))
            ranked = compact.get("result") if isinstance(compact, dict) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            ranked = []
        origin = screenshot_meta.get("coordinate_origin")
        origin = origin if isinstance(origin, dict) else {}
        try:
            origin_x, origin_y = int(origin.get("x") or 0), int(origin.get("y") or 0)
        except (TypeError, ValueError):
            origin_x = origin_y = 0
        prepared: list[dict[str, Any]] = []
        for row in list(ranked or [])[:16]:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            rect = item.pop("rectangle", None)
            if isinstance(rect, (list, tuple)) and len(rect) >= 4:
                try:
                    desktop_rect = [int(value) for value in rect[:4]]
                    item["desktop_rectangle"] = desktop_rect
                    item["png_rectangle"] = [
                        desktop_rect[0] - origin_x, desktop_rect[1] - origin_y,
                        desktop_rect[2] - origin_x, desktop_rect[3] - origin_y,
                    ]
                except (TypeError, ValueError):
                    pass
            prepared.append(item)
        return prepared

    @staticmethod
    def _fresh_vision_uia_rows(context: dict[str, Any], lease: SurfaceLease) -> list[dict[str, Any]]:
        if not lease.bound:
            return []
        try:
            same_surface = (
                LocalAgent._coerce_id(context.get("handle")) == lease.handle
                and int(context.get("generation", -1)) == lease.generation
                and int(context.get("scan_serial", -1)) >= 0
            )
        except (TypeError, ValueError):
            return []
        if not same_surface or not isinstance(context.get("rows"), list):
            return []
        return [row for row in context["rows"] if isinstance(row, dict)]

    def _describe_screenshot(
        self,
        path: str,
        question: str = "Опиши экран для следующего действия",
        screenshot_meta: dict[str, Any] | None = None,
        uia_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        meta = dict(screenshot_meta or {})
        try:
            width, height = int(meta.get("width") or 0), int(meta.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        if width <= 0 or height <= 0:
            return self._empty_vision("Снимок не содержит исходную ширину и высоту")
        origin = meta.get("coordinate_origin") if isinstance(meta.get("coordinate_origin"), dict) else {}
        geometry = {
            "width": width,
            "height": height,
            "coordinate_origin": {"x": int(origin.get("x") or 0), "y": int(origin.get("y") or 0)},
            "surface_handle": LocalAgent._coerce_id(meta.get("surface_handle")),
        }
        uia_context = self._vision_uia_context(list(uia_rows or []), meta, question)
        prompt = (
            f"Цель: {question}\n"
            f"Геометрия исходного PNG: width={width}, height={height}, "
            f"origin={geometry['coordinate_origin']['x']},{geometry['coordinate_origin']['y']}, "
            f"surface_handle={geometry['surface_handle']}.\n"
            "Верни только компактный JSON по схеме. x/y всегда относительно PNG: "
            f"0<=x<{width}, 0<=y<{height}. Не пиши рассуждения. Не угадывай скрытое. "
            "Приоритет: прямой control, создающий требуемый результат, выше навигации, карточек и preview. "
            "UIA_CONTEXT — приоритетное структурное основание; визуально сопоставь элемент с его "
            "stable_id и png_rectangle, но координатный клик не предлагай при неоднозначности. "
            "Не более 4 controls и 3 blockers.\n"
            f"UIA_CONTEXT={json.dumps(uia_context, ensure_ascii=False, separators=(',', ':'))}"
        )
        try:
            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            raw = self.gateway.json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Ты структурированное компьютерное зрение локального агента. "
                            "Анализируй только приложенный снимок и свежий UIA_CONTEXT."
                        ),
                    },
                    {"role": "user", "content": prompt, "images": [encoded]},
                ],
                model=self.settings.vision_model,
                temperature=0.0,
                schema=self._vision_schema(),
                num_ctx=min(self.settings.chat_num_ctx, 4096),
                num_predict=360,
                timeout_seconds=12,
            )
            return self._normalize_vision_analysis(raw, width, height)
        except Exception as exc:
            return self._empty_vision(f"Vision-анализ не удался: {exc}")

    @staticmethod
    def _vision_goal_reached(
        effect_args: dict[str, Any], observation_args: dict[str, Any], inner: dict[str, Any],
    ) -> bool:
        analysis = inner.get("vision_analysis")
        if not isinstance(analysis, dict) or analysis.get("goal_reached") is not True:
            return False
        target = analysis.get("primary_target")
        if not isinstance(target, dict) or not str(target.get("evidence") or "").strip():
            return False
        if not str(analysis.get("screen_state") or "").strip():
            return False
        try:
            captured_handle = LocalAgent._coerce_id(inner.get("surface_handle"))
            observed_handle = LocalAgent._coerce_id(observation_args.get("handle"))
            effect_handle = LocalAgent._coerce_id(effect_args.get("handle"))
        except (TypeError, ValueError):
            return False
        return bool(
            captured_handle
            and observed_handle == captured_handle
            and effect_handle == captured_handle
        )

    @staticmethod
    def _vision_actionable_target(
        analysis: dict[str, Any], screenshot_meta: dict[str, Any], expected_handle: int,
    ) -> bool:
        target = analysis.get("primary_target") if isinstance(analysis, dict) else None
        if not isinstance(target, dict) or not str(target.get("evidence") or "").strip():
            return False
        try:
            confidence = float(target.get("confidence") or 0)
            x, y = int(target.get("x")), int(target.get("y"))
            width = int(screenshot_meta.get("width") or 0)
            height = int(screenshot_meta.get("height") or 0)
            handle = LocalAgent._coerce_id(screenshot_meta.get("surface_handle"))
        except (TypeError, ValueError):
            return False
        action = str(target.get("action") or "").casefold().replace("ё", "е")
        role = str(target.get("role") or "").casefold()
        actionable = any(mark in action for mark in (
            "click", "press", "activate", "play", "resume", "start",
            "нажа", "актив", "воспроиз", "запуст", "включ",
        ))
        return bool(
            confidence >= 0.78
            and actionable
            and role in {"button", "hyperlink", "control", "кнопка", "ссылка"}
            and width > 0 and height > 0 and 0 <= x < width and 0 <= y < height
            and expected_handle and handle == expected_handle
        )

    @staticmethod
    def _vision_media_uia_candidate(
        rows: list[dict[str, Any]],
        analysis: dict[str, Any],
        screenshot_meta: dict[str, Any],
        goal: str,
        action: str,
        expected_handle: int,
    ) -> dict[str, Any] | None:
        """Map a visual hint to one fresh UIA stable id; never click coordinates."""
        if not LocalAgent._vision_actionable_target(analysis, screenshot_meta, expected_handle):
            return None
        target = dict(analysis.get("primary_target") or {})
        origin = screenshot_meta.get("coordinate_origin")
        origin = origin if isinstance(origin, dict) else {}
        try:
            point_x = int(target.get("x")) + int(origin.get("x") or 0)
            point_y = int(target.get("y")) + int(origin.get("y") or 0)
        except (TypeError, ValueError):
            return None
        wanted = str(action or "play").casefold()
        ranked: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("visible") is False or row.get("enabled") is False:
                continue
            stable_id = str(row.get("stable_id") or "").strip()
            if not stable_id:
                continue
            try:
                row_handle = LocalAgent._coerce_id(row.get("surface_handle") or expected_handle)
            except (TypeError, ValueError):
                continue
            if row_handle != expected_handle:
                continue
            role = str(row.get("control_type") or "").casefold()
            if role not in {"button", "hyperlink", "menuitem", "listitem"}:
                continue
            rect = row.get("rectangle") if isinstance(row.get("rectangle"), (list, tuple)) else []
            if len(rect) < 4:
                continue
            try:
                left, top, right, bottom = (int(value) for value in rect[:4])
            except (TypeError, ValueError):
                continue
            if not (left <= point_x < right and top <= point_y < bottom):
                continue
            name = str(row.get("name") or "")
            automation_id = str(row.get("automation_id") or "")
            class_name = str(row.get("class_name") or "")
            context = str(row.get("parent_name") or row.get("context") or "")
            blob = f"{name} {automation_id} {class_name} {context}".casefold().replace("ё", "е")
            if any(mark in blob for mark in (
                "autoplay", "auto play", "автовоспроиз", "preview", "card", "tile", "cover",
                "превью", "карточ", "browser toolbar", "omnibox", "address and search",
            )):
                continue
            if wanted in {"play", "resume", "play_pause"}:
                semantic = any(mark in blob for mark in (
                    "play", "resume", "start playback", "воспроиз", "запуст", "включ",
                ))
            elif wanted == "pause":
                semantic = any(mark in blob for mark in ("pause", "пауза", "приостанов"))
            elif wanted == "stop":
                semantic = any(mark in blob for mark in ("stop", "останов"))
            else:
                semantic = True
            if not semantic:
                continue
            strict = LocalAgent._media_ui_candidate([row], goal, action)
            primary_marks = any(mark in f"{context} {class_name} {automation_id}".casefold() for mark in (
                "playercontrols", "player controls", "playerbar", "player bar", "playback",
                "transportcontrols", "media controls", "плеер", "проигрывател",
            ))
            rank = 3 if strict else (2 if primary_marks else 1)
            area = max(1, (right - left) * (bottom - top))
            ranked.append((rank, -area, -index, {
                "stable_id": stable_id,
                "surface_handle": expected_handle,
                "element_text": name,
                "control_type": str(row.get("control_type") or ""),
                "automation_id": automation_id,
                "class_name": class_name,
                "context": context,
                "primary_player": bool(strict or primary_marks),
                "vision_grounded": True,
                "rectangle": [left, top, right, bottom],
            }))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2]))
        best = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best[0]:
            return None
        return best[3]

    @staticmethod
    def _vision_content_uia_candidate(
        rows: list[dict[str, Any]],
        analysis: dict[str, Any],
        screenshot_meta: dict[str, Any],
        target: str,
        expected_handle: int,
    ) -> dict[str, Any] | None:
        if not LocalAgent._vision_actionable_target(analysis, screenshot_meta, expected_handle):
            return None
        primary = dict(analysis.get("primary_target") or {})
        origin = screenshot_meta.get("coordinate_origin")
        origin = origin if isinstance(origin, dict) else {}
        try:
            point_x = int(primary.get("x")) + int(origin.get("x") or 0)
            point_y = int(primary.get("y")) + int(origin.get("y") or 0)
        except (TypeError, ValueError):
            return None
        matches: list[dict[str, Any]] = []
        for row in rows:
            candidate = LocalAgent._media_content_ui_candidate([row], target, expected_handle)
            if not candidate:
                continue
            rect = row.get("rectangle") if isinstance(row.get("rectangle"), (list, tuple)) else []
            if len(rect) < 4:
                continue
            try:
                left, top, right, bottom = (int(value) for value in rect[:4])
            except (TypeError, ValueError):
                continue
            if left <= point_x < right and top <= point_y < bottom:
                matches.append(candidate)
        unique = {str(item.get("stable_id") or ""): item for item in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    @staticmethod
    def _tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        return name, dict(arguments) if isinstance(arguments, dict) else {}

    @staticmethod
    def _needs_user(tool_name: str, result: dict[str, Any]) -> str | None:
        if result.get("ok"):
            inner = result.get("result") or {}
            if isinstance(inner, dict) and (inner.get("ok") is False or int(inner.get("returncode", 0) or 0) != 0):
                text = json.dumps(inner, ensure_ascii=False).lower()
            else:
                return None
        else:
            text = str(result.get("error") or "").lower()
        if tool_name in {"git_publish", "powershell", "browser_fill", "window_type"} and any(
            marker in text
            for marker in (
                "auth", "authentication", "login", "sign in", "permission denied",
                "publickey", "credential", "captcha", "uac", "access denied",
                "авториз", "войд", "доступ запрещ",
            )
        ):
            return "Нужно вручную завершить авторизацию/подтверждение в открытом окне. После этого напиши «готово»."
        return None

    @staticmethod
    def _compact_result(result: dict[str, Any], limit: int = 12000, goal: str = "") -> str:
        """Keep actionable UI evidence intact instead of cutting a raw tree mid-row.

        Chromium accessibility trees begin with thousands of characters of browser
        chrome and layout containers.  A blind string slice therefore hid the actual
        page buttons from the action model (including Play/My Wave) even though UIA had
        found them.  Rank and structurally compact visible controls first; this remains
        application-agnostic and preserves stable ids/rectangles for grounded actions.
        """
        payload = result.get("result") if isinstance(result, dict) else None
        if isinstance(payload, list) and payload and all(isinstance(row, dict) for row in payload):
            # ``window_list`` is also a list of dictionaries, but it is not a UIA tree.
            # Treating it as one dropped every row because windows have title/handle/pid
            # rather than name/stable_id.  Preserve the structural surface identity so
            # the policy can distinguish "service is not open" from "I saw no data".
            is_uia_tree = any(
                any(key in row for key in ("control_type", "stable_id", "automation_id", "parent_name"))
                for row in payload
            )
            is_window_list = not is_uia_tree and all(
                any(key in row for key in ("handle", "hwnd"))
                and any(key in row for key in ("title", "name"))
                for row in payload
            )
            if is_window_list:
                compact_windows: list[dict[str, Any]] = []
                envelope = {key: value for key, value in result.items() if key != "result"}
                envelope.update({"result": compact_windows, "total_windows": len(payload), "prioritized": True})
                for row in payload:
                    compact = {
                        key: row.get(key) for key in (
                            "title", "handle", "pid", "process_name", "class_name",
                            "rectangle", "visible", "enabled",
                        ) if row.get(key) not in (None, "", [])
                    }
                    compact_windows.append(compact)
                    rendered = json.dumps(envelope, ensure_ascii=False, default=str)
                    if len(rendered) > max(500, limit - 1):
                        compact_windows.pop()
                        break
                return json.dumps(envelope, ensure_ascii=False, default=str)
            if not is_uia_tree:
                text = json.dumps(result, ensure_ascii=False, default=str)
                return text if len(text) <= limit else text[:limit] + "…"
            goal_n = str(goal or "").casefold().replace("ё", "е")
            goal_words = {
                word for word in re.findall(r"[a-zа-яё0-9]{3,}", str(goal or "").casefold())
                if word not in {
                    "который", "которая", "после", "перед", "текущем", "экране", "ничего",
                    "уже", "открытой", "открытом", "открыта", "открыт", "нужно",
                }
            }
            media_goal = any(mark in goal_n for mark in ("музык", "трек", "песн", "аудио", "видео", "волна", "player", "play"))
            play_goal = media_goal and any(mark in goal_n for mark in ("включ", "запуст", "воспроиз", "играй", "play", "resume"))
            interactive = {
                "button", "hyperlink", "edit", "combobox", "checkbox", "radiobutton",
                "listitem", "treeitem", "menuitem", "tabitem", "slider", "progressbar",
            }
            chrome_marks = {
                "omnibox", "address and search", "адресная строка", "tabstrip", "tab strip",
                "browser toolbar", "toolbarview", "chrome app menu", "caption button",
            }
            ranked: list[tuple[float, int, dict[str, Any]]] = []
            seen: set[tuple[Any, ...]] = set()
            for index, row in enumerate(payload):
                if row.get("visible") is False:
                    continue
                name = str(row.get("name") or "").strip()
                role = str(row.get("control_type") or "").casefold()
                stable_id = str(row.get("stable_id") or "").strip()
                automation_id = str(row.get("automation_id") or "").strip()
                value = str(row.get("value") or "").strip()
                class_name = str(row.get("class_name") or "").strip()
                parent_name = str(row.get("parent_name") or "").strip()
                if not (name or stable_id or automation_id or value):
                    continue
                rect = row.get("rectangle") if isinstance(row.get("rectangle"), (list, tuple)) else []
                key = (name.casefold(), role, stable_id, automation_id, tuple(rect or ()))
                if key in seen:
                    continue
                seen.add(key)
                blob = f"{name} {parent_name} {automation_id} {class_name}".casefold()
                is_interactive = role in interactive
                score = 100.0 if is_interactive else 12.0
                if row.get("enabled", True):
                    score += 10.0
                if stable_id:
                    score += 18.0
                if automation_id:
                    score += 8.0
                if goal_words:
                    score += 45.0 * sum(1 for word in goal_words if word in blob)
                if play_goal and any(mark in blob for mark in ("воспроизвед", "воспроизвести", " play", "playbutton", "resume")):
                    score += 170.0
                    if any(mark in parent_name.casefold() for mark in ("плеер", "player", "проигрыватель")):
                        score += 75.0
                    if any(mark in class_name.casefold() for mark in ("playercontrols", "playerbar", "playback")):
                        score += 45.0
                if len(rect) == 4 and int(rect[1]) >= 90:
                    score += 16.0
                chrome_class = any(mark in class_name.casefold() for mark in (
                    "windowscaptionbutton", "toolbarview", "tabstrip", "tabclosebutton",
                    "browserappmenubutton", "omniboxview", "backforwardbutton",
                )) or (role == "tabitem" and len(rect) == 4 and int(rect[1]) < 90)
                if chrome_class or any(mark in blob for mark in chrome_marks):
                    score -= 180.0
                compact = {
                    key_name: value_item for key_name, value_item in (
                        ("name", name), ("control_type", str(row.get("control_type") or "")),
                        ("stable_id", stable_id), ("automation_id", automation_id),
                        ("value", value[:160]), ("class_name", class_name[:140]),
                        ("context", parent_name[:140]),
                        ("rectangle", list(rect) if rect else None),
                        ("enabled", row.get("enabled") if row.get("enabled") is False else None),
                    ) if value_item not in ("", None, [])
                }
                ranked.append((score, index, compact))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            selected: list[dict[str, Any]] = []
            envelope = {key: value for key, value in result.items() if key != "result"}
            envelope.update({"result": selected, "total_elements": len(payload), "prioritized": True})
            for _score, _index, row in ranked:
                selected.append(row)
                rendered = json.dumps(envelope, ensure_ascii=False, default=str)
                if len(rendered) > max(500, limit - 1):
                    selected.pop()
                    break
            return json.dumps(envelope, ensure_ascii=False, default=str)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    @staticmethod
    def _semantic_observation_signature(tool_name: str, result: dict[str, Any]) -> str:
        """Hash observable meaning while ignoring capture-specific volatile fields."""
        volatile = {
            "path", "screenshot_path", "created_at", "captured_at", "observed_at",
            "timestamp", "ts", "cursor", "elapsed_ms", "duration_ms", "age_seconds",
            "request_id", "trace_id",
        }

        def stable(value: Any, key: str = "") -> Any:
            if str(key).casefold() in volatile:
                return None
            if isinstance(value, dict):
                return {
                    str(item_key): normalized
                    for item_key, item_value in value.items()
                    if (normalized := stable(item_value, str(item_key))) is not None
                }
            if isinstance(value, list):
                rows = [stable(item) for item in value]
                rows = [item for item in rows if item is not None]
                if rows and all(isinstance(item, dict) for item in rows):
                    rows.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
                return rows
            return value

        payload = result.get("result") if isinstance(result, dict) else result
        # A window-list observation is a semantic inventory, not a geometry
        # measurement.  The EIRVEN dock/sphere can move by a few pixels while the
        # same list is being read; treating each rectangle as a new scene made the
        # policy model loop forever on window_list.  Keep title/handle/pid/class so a
        # real surface change still invalidates the signature.
        if str(tool_name or "").casefold() == "window_list" and isinstance(payload, list):
            payload = [
                {
                    str(key): value for key, value in row.items()
                    if str(key).casefold() not in {"rectangle", "rect", "bounds"}
                }
                if isinstance(row, dict) else row
                for row in payload
            ]
        return AdaptiveRecovery.signature(str(tool_name or ""), stable(payload))

    @staticmethod
    def _media_control_is_read(arguments: dict[str, Any]) -> bool:
        action = str(arguments.get("action") or "status").strip().casefold().replace("-", "_")
        return action in {"status", "list", "sessions", "inspect"}

    @staticmethod
    def _bare_media_start(goal: str) -> bool:
        """True only when playback was requested without a service/content owner."""
        clean = str(goal or "").casefold().replace("ё", "е")
        words = set(re.findall(r"[a-zа-я0-9]+", clean))
        starts = bool(words & {
            "включи", "включить", "запусти", "запустить", "возобнови", "возобновить",
            "поставь", "поставить", "вруби", "врубить", "проиграй", "проиграть",
            "play", "resume", "start",
        })
        media = bool(words & {
            "музыка", "музыку", "музыки", "песня", "песню", "песни", "трек",
            "треки", "аудио", "music", "song", "songs", "track",
        })
        if not starts or not media:
            return False
        generic = {
            "эрви", "пожалуйста", "мне", "сейчас", "давай", "просто", "хочу",
            "послушать", "какую", "какую-нибудь", "нибудь", "что", "что-нибудь",
            "включи", "включить", "запусти", "запустить", "возобнови", "возобновить",
            "поставь", "поставить", "вруби", "врубить", "проиграй", "проиграть",
            "play", "resume", "start",
            "музыка", "музыку", "музыки", "песня", "песню", "песни", "трек",
            "треки", "аудио", "music", "song", "songs", "track",
        }
        return not (words - generic)

    @staticmethod
    def _media_request(goal: str) -> bool:
        clean = str(goal or "").casefold().replace("ё", "е")
        return bool(re.search(
            r"\b(?:музык\w*|песн\w*|трек\w*|аудио\w*|плеер\w*|"
            r"воспроизвес\w*|пауз\w*|радио\w*|подкаст\w*|видео\w*|волна\w*|"
            r"music|song|track|audio|player|play|pause|radio|podcast|video)\b",
            clean,
        ))

    @staticmethod
    def _media_service_purpose(goal: str) -> str:
        """Return the provider-neutral media category carried by the owner request."""
        raw = re.split(
            r"(?:уточняющий\s+вопрос\s+эрви|owner\s+question)\s*:",
            str(goal or ""), maxsplit=1, flags=re.I,
        )[0]
        clean = raw.casefold().replace("ё", "е")
        categories = (
            (r"\b(?:подкаст\w*|podcast\w*)\b", "подкаст"),
            (r"\b(?:радио\w*|radio\w*)\b", "радио"),
            (r"\b(?:видео\w*|video\w*)\b", "видео"),
            (
                r"\b(?:музык\w*|песн\w*|трек\w*|аудио\w*|волна\w*|"
                r"music|song|track|audio)\b",
                "музыка",
            ),
        )
        for pattern, purpose in categories:
            if re.search(pattern, clean, re.I):
                return purpose
        return ""

    @staticmethod
    def _explicit_media_service_slot(goal: str, proposed: str = "") -> str:
        """Extract an exact owner-owned service span without a provider catalogue.

        High-confidence explicit relations can seed the first opener.  Lower-confidence
        spans are used only to repair a model proposal that overlaps the owner's words;
        this keeps phrases such as "в хорошем качестве" from becoming invented services.
        """
        raw = str(goal or "").strip()
        if not raw or not LocalAgent._media_request(raw):
            return ""

        def clean_span(value: str) -> str:
            return str(value or "").strip().strip(" \t\r\n.,!?;:«»\"'")

        proposal_words = {
            word for word in re.findall(r"[a-zа-яё0-9]+", str(proposed or "").casefold())
        }
        owner_answer = re.search(
            r"(?:ответ\s+владельца|owner\s+answer)\s*:\s*([^\r\n!?;]+)", raw, re.I,
        )
        if owner_answer:
            answer = clean_span(owner_answer.group(1))
            if answer and (
                not proposal_words
                or proposal_words.intersection(re.findall(r"[a-zа-яё0-9]+", answer.casefold()))
            ):
                return answer

        tokens = list(re.finditer(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._+-]*", raw))
        relations = {"в", "во", "на", "через", "using", "via", "in", "on", "with"}
        descriptors = {
            "сервис", "сервисе", "плеер", "плеере", "приложение", "приложении",
            "сайт", "сайте", "service", "player", "application", "app", "website", "web",
        }
        stops = {
            "и", "а", "но", "потом", "затем", "после", "чтобы", "пожалуйста", "сейчас",
            "трек", "треки", "песню", "песни", "плейлист", "радиостанцию",
        }
        for index, token_match in enumerate(tokens):
            relation = token_match.group(0).casefold().replace("ё", "е")
            prefix_text = raw[:token_match.start()]
            explicit_start = bool(re.search(
                r"\b(?:включи|включить|запусти|запустить|проиграй|проиграть|поставь|поставить|"
                r"возобнови|возобновить|play|start|resume)\b",
                prefix_text.casefold().replace("ё", "е"), re.I,
            ))
            if relation not in relations or not (
                LocalAgent._media_request(prefix_text)
                or (LocalAgent._media_request(raw) and explicit_start)
            ):
                continue
            cursor = index + 1
            had_descriptor = False
            while cursor < len(tokens) and tokens[cursor].group(0).casefold().replace("ё", "е") in descriptors:
                had_descriptor = True
                cursor += 1
            start = cursor
            while cursor < len(tokens) and cursor - start < 4:
                word = tokens[cursor].group(0).casefold().replace("ё", "е")
                if word in stops or (cursor > start and word in relations):
                    break
                cursor += 1
            if cursor <= start:
                continue
            candidate = clean_span(raw[tokens[start].start():tokens[cursor - 1].end()])
            if not candidate or not LocalAgent._media_service_is_grounded(raw, candidate):
                continue
            candidate_words = set(re.findall(r"[a-zа-яё0-9]+", candidate.casefold()))
            if proposal_words:
                if proposal_words.intersection(candidate_words):
                    return candidate
                continue
            quoted = bool(re.search(rf"[«\"']\s*{re.escape(candidate)}\s*[»\"']", raw, re.I))
            latin_or_digit = bool(re.search(r"[A-Za-z0-9]", candidate))
            title_words = sum(1 for word in candidate.split() if word[:1].isupper())
            if had_descriptor or quoted or latin_or_digit or title_words >= 2:
                return candidate
        return ""

    @staticmethod
    def _media_service_is_grounded(goal: str, target: str) -> bool:
        """Reject a model-invented player while allowing any owner-named new service.

        Mere word overlap is not enough: a content title such as ``Моя волна`` must not
        silently become an ``open_service`` argument.  The target has to occur in an
        owner-written service/application relation.  This is deliberately lexical and
        catalogue-free, so future brands remain valid without teaching the engine their
        names.
        """
        goal_n = re.sub(r"[^a-zа-яё0-9]+", " ", str(goal or "").casefold()).strip()
        target_n = re.sub(r"[^a-zа-яё0-9]+", " ", str(target or "").casefold()).strip()
        if not target_n:
            return False
        goal_words = goal_n.split()
        target_words = target_n.split()

        def same_word(left: str, right: str) -> bool:
            return left == right or (
                len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5]
            )

        start = -1
        for index in range(0, len(goal_words) - len(target_words) + 1):
            if all(same_word(goal_words[index + offset], word) for offset, word in enumerate(target_words)):
                start = index
                break
        if start < 0:
            return False
        prefix = goal_words[max(0, start - 4):start]
        previous = prefix[-1] if prefix else ""
        relations = {
            "в", "во", "на", "через", "using", "via", "in", "on", "with",
        }
        service_words = {
            "сервис", "сервисе", "сервиса", "плеер", "плеере", "плеера",
            "приложение", "приложении", "приложения", "сайт", "сайте",
            "service", "player", "application", "app", "website",
        }
        explicit_openers = {
            "открой", "открыть", "запусти", "запустить", "open", "launch",
        }
        return bool(
            previous in relations
            or any(word in service_words for word in prefix)
            or previous in explicit_openers
            or ({"ответ", "владельца"} <= set(prefix))
        )

    @staticmethod
    def _media_content_for_service(goal: str, service: str) -> str:
        """Extract owner content separately from an exact service relation."""
        raw = str(goal or "").strip()
        service_raw = str(service or "").strip()
        if not raw or not service_raw:
            return ""
        start = raw.casefold().find(service_raw.casefold())
        if start < 0:
            return ""
        prefix = raw[:start]
        # A service supplied as a clarification answer does not turn the protocol text
        # into requested content; only the original owner utterance may own this slot.
        if re.search(r"(?:ответ\s+владельца|owner\s+answer)\s*:\s*$", prefix, re.I):
            prefix = re.split(r"(?:уточняющий\s+вопрос|owner\s+question)", prefix, maxsplit=1, flags=re.I)[0]
        prefix = re.sub(r"\b(?:в|во|на|через|using|via|in|on|with)\s*$", "", prefix, flags=re.I).strip()
        prefix = re.sub(
            r"^\s*(?:(?:эрви|пожалуйста|please)[,\s]+)*"
            r"(?:включи|включить|запусти|запустить|проиграй|проиграть|поставь|поставить|"
            r"возобнови|возобновить|play|start|resume)\b",
            "", prefix, count=1, flags=re.I,
        ).strip(" \t\r\n.,!?;:«»\"'")
        prefix = re.sub(
            r"^(?:музыку?|трек(?:и|ов)?|песн(?:ю|и)|плейлист|радио|аудио|видео)\b",
            "", prefix, count=1, flags=re.I,
        ).strip(" \t\r\n.,!?;:«»\"'")
        words = set(re.findall(r"[a-zа-яё0-9]+", prefix.casefold()))
        non_content = {
            "", "в", "на", "хорошем", "высоком", "лучшем", "максимальном",
            "качестве", "качеством", "сейчас", "пожалуйста",
        }
        return "" if not words or words <= non_content else prefix

    @staticmethod
    def _media_content_ui_candidate(
        rows: list[dict[str, Any]], target: str, expected_handle: int,
    ) -> dict[str, Any] | None:
        target_words = [
            word for word in re.findall(r"[a-zа-яё0-9]{3,}", str(target or "").casefold())
            if word not in {"мою", "моя", "мой", "мое", "моё", "the", "трек", "песня"}
        ]
        if not target_words or not expected_handle:
            return None

        def matches(word: str, candidate: str) -> bool:
            return bool(
                word == candidate
                or (len(word) >= 5 and len(candidate) >= 5 and word[:4] == candidate[:4])
                or SequenceMatcher(None, word, candidate).ratio() >= 0.76
            )

        # Some players expose a station/playlist as a page heading while the
        # actual actionable element is the main transport button.  Ground that
        # button to a compact heading on the same live surface; do not infer it
        # from recommendation/card text.
        page_grounded = False
        for row in rows:
            if not isinstance(row, dict) or row.get("visible") is False:
                continue
            row_role = str(row.get("control_type") or "").casefold()
            if row_role not in {"text", "heading", "title", "statictext"}:
                continue
            row_name = str(row.get("name") or "").strip()
            row_words = re.findall(r"[a-zа-яё0-9]{3,}", row_name.casefold())
            if (
                row_name
                and len(row_words) <= len(target_words) + 1
                and all(any(matches(word, candidate) for candidate in row_words) for word in target_words)
                and not str(row.get("parent_name") or row.get("context") or "").strip()
            ):
                page_grounded = True
                break
        if not page_grounded:
            # Chromium often omits the heading from a bounded accessibility probe,
            # while still exposing the selected item as a compact menu link/wrapper.
            # Treat that pair as page grounding only when it is clearly navigation,
            # never when it is a recommendation/card or a transport button.
            compact_navigation: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict) or row.get("visible") is False:
                    continue
                row_role = str(row.get("control_type") or "").casefold()
                if row_role not in {"hyperlink", "listitem", "treeitem", "menuitem", "tabitem"}:
                    continue
                row_name = str(row.get("name") or "").strip()
                row_words = re.findall(r"[a-zа-яё0-9]{3,}", row_name.casefold())
                row_context = str(row.get("parent_name") or row.get("context") or "").casefold()
                if (
                    row_name
                    and len(row_words) <= len(target_words) + 1
                    and all(any(matches(word, candidate) for candidate in row_words) for word in target_words)
                ):
                    compact_navigation.append(row)
            for index, row in enumerate(compact_navigation):
                row_rect = tuple(row.get("rectangle") or ())
                row_name = str(row.get("name") or "").casefold()
                if any(
                    other is not row
                    and str(other.get("name") or "").casefold() == row_name
                    and tuple(other.get("rectangle") or ()) == row_rect
                    and (
                        any(mark in str(row.get("parent_name") or row.get("context") or "").casefold() for mark in ("меню", "menu", "навигац", "navigation"))
                        or any(mark in str(other.get("parent_name") or other.get("context") or "").casefold() for mark in ("меню", "menu", "навигац", "navigation"))
                    )
                    for other in compact_navigation[index + 1:]
                ):
                    page_grounded = True
                    break

        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("visible") is False or row.get("enabled") is False:
                continue
            stable_id = str(row.get("stable_id") or "").strip()
            role = str(row.get("control_type") or "").casefold()
            if not stable_id or role not in {
                "button", "hyperlink", "listitem", "treeitem", "menuitem", "tabitem",
            }:
                continue
            try:
                row_handle = LocalAgent._coerce_id(row.get("surface_handle") or expected_handle)
            except (TypeError, ValueError):
                continue
            if row_handle != expected_handle:
                continue
            name = str(row.get("name") or "").strip()
            context = str(row.get("parent_name") or row.get("context") or "").strip()
            value = str(row.get("value") or "").strip()
            blob_words = re.findall(r"[a-zа-яё0-9]{3,}", f"{name} {value} {context}".casefold())
            chrome_blob = f"{name} {context} {row.get('class_name') or ''}".casefold()
            if any(mark in chrome_blob for mark in (
                "tab close", "close tab", "address and search", "omnibox", "browser toolbar",
                "закрыть вкладку", "адресная строка",
            )):
                continue
            name_words = re.findall(r"[a-zа-яё0-9]{3,}", name.casefold())
            direct_matches = sum(
                1 for word in target_words if any(matches(word, candidate) for candidate in name_words)
            )
            direct_transport = role == "button" and any(mark in chrome_blob for mark in (
                "плеер", "player", "transport", "playback", "playercontrols",
            )) and not any(mark in chrome_blob for mark in (
                "preview", "карточ", "превью", "recommend", "recommendation",
            )) and any(mark in chrome_blob for mark in (
                "воспроизвед", "включить", "playbutton", "play_button", "play button",
            ))
            playing_transport = direct_transport and any(mark in chrome_blob for mark in (
                "пауза", "pause", "playing",
            ))
            if not all(any(matches(word, candidate) for candidate in blob_words) for word in target_words):
                if not (page_grounded and direct_transport):
                    continue
            score = direct_matches * 60.0 + (25.0 if role in {"button", "hyperlink"} else 10.0)
            # A navigation item is commonly exposed twice by Chromium/UIA (a
            # ListItem wrapper and its inner Hyperlink) at the same rectangle.
            # Prefer the actionable inner link while keeping genuinely distinct
            # controls ambiguous and therefore fail-closed.
            if role == "hyperlink":
                score += 20.0
            if page_grounded and direct_transport:
                score += 180.0
            # Prefer a compact, directly named content link over a wrapper row or a
            # broad action card that merely mentions the same words.  Reset/clear
            # controls are navigation/collection mutations, not content selection.
            if len(name_words) <= max(2, len(target_words) + 1):
                score += 18.0
            if any(mark in f"{name} {row.get('class_name') or ''}".casefold() for mark in (
                "сброс", "reset", "clear collection", "очистить коллекци",
            )):
                score -= 45.0
            if any(mark in context.casefold() for mark in ("навигац", "navigation", "menu")):
                score += 12.0
            if any(mark in context.casefold() for mark in ("recommend", "preview", "карточ", "превью")):
                score -= 8.0
            ranked.append((score, index, {
                "stable_id": stable_id,
                "surface_handle": expected_handle,
                "element_text": name,
                "control_type": str(row.get("control_type") or ""),
                "automation_id": str(row.get("automation_id") or ""),
                "class_name": str(row.get("class_name") or ""),
                "context": context,
                "rectangle": list(row.get("rectangle") or []),
                "content_grounded": True,
                "page_grounded": bool(page_grounded and direct_transport),
                "already_playing": bool(page_grounded and playing_transport),
            }))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 18.0:
            return None
        return ranked[0][2]

    @staticmethod
    def _media_blocker_ui_candidate(
        rows: list[dict[str, Any]], expected_handle: int,
    ) -> dict[str, Any] | None:
        """Find one safe, visible control that dismisses a blocking overlay.

        This is intentionally semantic and provider-agnostic.  A named item may be
        hidden behind a consent/promo/interstitial surface; dismissing one uniquely
        grounded ``back/close/skip`` control is safe navigation, while arbitrary
        preview/card clicks remain forbidden.
        """
        if not expected_handle:
            return None
        markers = (
            "вернуться", "назад", "закрыть", "пропустить", "отмена", "не сейчас",
            "back", "close", "dismiss", "skip", "cancel", "not now",
        )
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("visible") is False or row.get("enabled") is False:
                continue
            stable_id = str(row.get("stable_id") or "").strip()
            role = str(row.get("control_type") or "").casefold()
            if not stable_id or role not in {"button", "hyperlink", "menuitem", "tabitem"}:
                continue
            try:
                row_handle = LocalAgent._coerce_id(row.get("surface_handle") or expected_handle)
            except (TypeError, ValueError):
                continue
            if row_handle != expected_handle:
                continue
            name = str(row.get("name") or "").strip()
            context = str(row.get("parent_name") or row.get("context") or "").strip()
            class_name = str(row.get("class_name") or "")
            blob = f"{name} {context} {class_name}".casefold().replace("ё", "е")
            if any(mark in blob for mark in (
                "windowcaptionbutton", "tabclosebutton", "backforwardbutton",
                "browser toolbar", "browserrootview", "locationbarview", "tabstrip",
                "omnibox", "preview", "карточ", "превью", "address and search",
            )):
                continue
            matched = [mark for mark in markers if mark in blob]
            if not matched:
                continue
            score = 70.0 + max(0, 20.0 - len(name) * 0.2)
            if any(mark in name.casefold().replace("ё", "е") for mark in matched):
                score += 18.0
            if any(mark in context.casefold().replace("ё", "е") for mark in ("диалог", "dialog", "overlay", "promo", "слайд")):
                score += 8.0
            ranked.append((score, index, {
                "stable_id": stable_id,
                "surface_handle": expected_handle,
                "element_text": name,
                "control_type": str(row.get("control_type") or ""),
                "automation_id": str(row.get("automation_id") or ""),
                "class_name": class_name,
                "context": context,
                "blocker_dismiss": True,
            }))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 12.0):
            return None
        return ranked[0][2]

    @staticmethod
    def _media_mutation_action(goal: str, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action") or "play").strip().casefold().replace("-", "_")
        aliases = {
            "resume": "play", "playing": "play", "paused": "pause",
            "toggle": "play_pause", "playpause": "play_pause",
            "nexttrack": "next", "prev": "previous", "previoustrack": "previous",
        }
        action = aliases.get(action, action)
        if action == "play_pause":
            goal_n = str(goal or "").casefold().replace("ё", "е")
            if any(mark in goal_n for mark in ("пауз", "приостанов", "pause")):
                return "pause"
            return "play"
        return action

    @staticmethod
    def _media_sessions_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
        inner = result.get("result") if isinstance(result, dict) else None
        if not isinstance(inner, dict):
            return []
        for key in ("after", "selected"):
            row = inner.get(key)
            if isinstance(row, dict):
                return [row]
        sessions = inner.get("sessions")
        if isinstance(sessions, list):
            return [row for row in sessions if isinstance(row, dict)]
        row = inner.get("before")
        if isinstance(row, dict):
            return [row]
        return []

    @staticmethod
    def _media_session_state_verifies(
        action: str,
        result: dict[str, Any],
        *,
        before_session_ids: tuple[str, ...] = (),
    ) -> bool:
        action_n = str(action or "").casefold()
        desired = {
            "play": {"playing"},
            "pause": {"paused", "stopped"},
            "stop": {"stopped"},
        }.get(action_n)
        if not desired:
            return False
        rows = LocalAgent._media_sessions_from_result(result)
        if not rows:
            return False
        inner = result.get("result") if isinstance(result, dict) else None
        selected_result = bool(
            isinstance(inner, dict)
            and isinstance(inner.get("selected") or inner.get("after"), dict)
        )
        candidates = rows
        before_ids = {value for value in before_session_ids if value}
        if before_ids and not selected_result:
            candidates = [
                row for row in rows
                if str(row.get("session_id") or "") not in before_ids
            ]
        if not selected_result and len(candidates) != 1:
            return False
        return len(candidates) == 1 and str(candidates[0].get("state") or "").casefold() in desired

    @staticmethod
    def _media_ui_state_verifies(
        rows: list[dict[str, Any]],
        goal: str,
        action: str,
    ) -> dict[str, Any] | None:
        expected_control = {
            "play": "pause",
            "pause": "play",
            "stop": "play",
        }.get(str(action or "").casefold())
        if not expected_control:
            return None
        return LocalAgent._media_ui_candidate(rows, goal, expected_control)

    @staticmethod
    def _media_ui_candidate_state_changed(
        rows: list[dict[str, Any]], recovery: MediaUIRecovery,
    ) -> dict[str, Any] | None:
        """Verify Play→Pause (or the inverse) on the exact clicked geometry.

        A page heading can ground the main transport without appearing in the
        transport button's own accessible name.  In that case the normal goal-word
        scorer is intentionally too strict; matching the fresh button to the prior
        candidate's rectangle/semantic parent keeps verification exact and avoids
        touching any preview controls.
        """
        expected = {
            "play": ("пауза", "pause", "playing", "воспроизведение остановить"),
            "pause": ("воспроизведение", "play", "resume", "начать воспроизведение"),
            "stop": ("воспроизведение", "play", "resume"),
        }.get(str(recovery.action or "").casefold())
        if not expected:
            return None
        prior = recovery.candidate if isinstance(recovery.candidate, dict) else {}
        prior_rect = tuple(prior.get("rectangle") or ())
        prior_context = str(prior.get("context") or "").casefold()
        prior_class = str(prior.get("class_name") or "").casefold()
        for row in rows:
            if not isinstance(row, dict) or row.get("visible") is False or row.get("enabled") is False:
                continue
            if str(row.get("control_type") or "").casefold() != "button":
                continue
            try:
                if LocalAgent._coerce_id(row.get("surface_handle") or recovery.handle) != recovery.handle:
                    continue
            except (TypeError, ValueError):
                continue
            rect = tuple(row.get("rectangle") or ())
            context = str(row.get("parent_name") or row.get("context") or "").casefold()
            class_name = str(row.get("class_name") or "").casefold()
            blob = f"{row.get('name') or ''} {class_name} {context}".casefold().replace("ё", "е")
            if prior_rect and rect != prior_rect:
                continue
            if prior_context and context and prior_context not in context and context not in prior_context:
                continue
            if prior_class and not any(part and part in class_name for part in re.split(r"[_\s]+", prior_class) if len(part) >= 8):
                continue
            if any(mark in blob for mark in expected):
                return dict(row)
        return None

    @staticmethod
    def _media_goal_requires_content_match(goal: str, target: str = "") -> bool:
        """Whether transport state alone is insufficient for the owner's media goal."""
        if target:
            return not LocalAgent._media_service_is_grounded(goal, target)
        if LocalAgent._bare_media_start(goal):
            return False
        goal_n = str(goal or "").casefold().replace("ё", "е")
        if any(mark in goal_n for mark in (
            "пауз", "приостанов", "останови", "выключи", "громк", "тише",
            "pause", "stop", "volume", "mute",
        )):
            return False
        return LocalAgent._media_request(goal)

    @staticmethod
    def _media_result_content_matches(target: str, result: dict[str, Any]) -> bool:
        target_words = [
            word for word in re.findall(r"[a-zа-яё0-9]{3,}", str(target or "").casefold())
            if word not in {
                "музыка", "музыку", "music", "трек", "track",
                # Possessive determiners carry no identity for the requested item
                # and commonly change case (моя/мою/мой/мое) between speech and
                # live media metadata.
                "моя", "мою", "мой", "мое", "моё", "твой", "твоя", "твое", "твои",
            }
        ]
        if not target_words:
            return False
        rows = LocalAgent._media_sessions_from_result(result)
        if len(rows) != 1:
            return False
        metadata = " ".join(
            str(rows[0].get(key) or "") for key in ("title", "artist", "album", "subtitle")
        ).casefold().replace("ё", "е")
        metadata_words = re.findall(r"[a-zа-я0-9]{3,}", metadata)

        def present(word: str) -> bool:
            return any(
                word == candidate
                or (len(word) >= 5 and len(candidate) >= 5 and word[:5] == candidate[:5])
                # Metadata often uses a different grammatical case than the
                # owner's utterance (for example ``волну`` vs ``волна``).  A
                # conservative fuzzy token match handles inflection without
                # turning unrelated words into a claimed content match.
                or (len(word) >= 4 and len(candidate) >= 4 and SequenceMatcher(None, word, candidate).ratio() >= 0.78)
                for candidate in metadata_words
            )

        return bool(target_words and all(present(word) for word in target_words))

    @staticmethod
    def _media_no_session_result(arguments: dict[str, Any], result: dict[str, Any]) -> bool:
        if LocalAgent._media_control_is_read(arguments):
            return False
        inner = result.get("result") if isinstance(result, dict) else None
        return bool(
            isinstance(inner, dict)
            and inner.get("selection") == "no_sessions"
            and not inner.get("attempted")
            and not inner.get("executed")
        )

    @staticmethod
    def _media_ui_candidate(
        rows: list[dict[str, Any]],
        goal: str,
        action: str,
    ) -> dict[str, Any] | None:
        """Choose only a high-confidence live UIA media/content control.

        The selector uses accessibility semantics and words from the owner's goal, not
        site identities.  Ambiguity returns ``None`` so the action model can inspect the
        same compact live rows; coordinates are never manufactured here.
        """
        goal_n = str(goal or "").casefold().replace("ё", "е")
        stop = {
            "включи", "включить", "запусти", "запустить", "открой", "открыть",
            "музыку", "музыка", "плеер", "плеере", "сервис", "сервисе", "уже",
            "ничего", "открытой", "открытом", "пожалуйста", "сейчас", "играть",
        }
        goal_words = {
            word for word in re.findall(r"[a-zа-яё0-9]{3,}", goal_n)
            if word not in stop
        }
        wanted_action = str(action or "play").casefold()
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("visible") is False or row.get("enabled") is False:
                continue
            stable_id = str(row.get("stable_id") or "").strip()
            if not stable_id:
                continue
            role = str(row.get("control_type") or "").casefold()
            if role not in {"button", "hyperlink", "listitem", "menuitem", "treeitem"}:
                continue
            name = str(row.get("name") or "").strip()
            automation_id = str(row.get("automation_id") or "").strip()
            class_name = str(row.get("class_name") or "").strip()
            context = str(row.get("parent_name") or row.get("context") or "").strip()
            value = str(row.get("value") or "").strip()
            blob = f"{name} {automation_id} {class_name} {context} {value}".casefold().replace("ё", "е")
            words = set(re.findall(r"[a-zа-яё0-9]{3,}", blob))
            overlap = len(goal_words & words)
            score = 52.0 if role == "button" else 24.0
            score += min(4, overlap) * 38.0
            autoplay = any(mark in blob for mark in ("autoplay", "auto play", "автовоспроиз"))
            play_semantic = not autoplay and any(mark in blob for mark in (
                "воспроизвести", "воспроизвед", "включить", "запустить", "слушать",
                "playbutton", "play button", "play_button", "resume", "start playback", "playback start",
            ))
            if not autoplay and re.search(r"(?:^|[^a-z])play(?:$|[^a-z])", blob):
                play_semantic = True
            pause_semantic = any(mark in blob for mark in (
                "пауза", "приостанов", "pause", "stop playback",
            ))
            if wanted_action in {"play", "play_pause", "resume"}:
                if play_semantic:
                    score += 118.0
                if pause_semantic and not play_semantic:
                    score -= 180.0
            elif wanted_action == "pause":
                if pause_semantic:
                    score += 118.0
                if play_semantic and not pause_semantic:
                    score -= 120.0
            elif wanted_action == "stop" and any(mark in blob for mark in (
                "останов", "stop playback", "stopbutton",
            )):
                score += 118.0
            elif wanted_action == "next" and any(mark in blob for mark in (
                "следующ", "next", "skip forward",
            )):
                score += 118.0
            elif wanted_action == "previous" and any(mark in blob for mark in (
                "предыдущ", "previous", "skip back",
            )):
                score += 118.0
            primary_player = any(mark in f"{context} {class_name} {automation_id}".casefold() for mark in (
                "playercontrols", "player controls", "playerbar", "player bar", "playback",
                "transportcontrols", "transport controls", "media controls", "плеер", "проигрывател",
            ))
            preview = any(mark in f"{context} {class_name}".casefold() for mark in (
                "preview", "card", "tile", "cover", "превью", "карточ",
            ))
            if preview:
                primary_player = False
                score -= 180.0
            if primary_player:
                score += 72.0
            if autoplay:
                score -= 220.0
            if any(mark in blob for mark in (
                "tab close", "close tab", "new tab", "address and search", "omnibox",
                "закрыть вкладку", "новая вкладка", "адресная строка", "browser toolbar",
            )):
                score -= 220.0
            rect = row.get("rectangle") if isinstance(row.get("rectangle"), (list, tuple)) else []
            if len(rect) == 4 and int(rect[1]) >= 80:
                score += 10.0
            ranked.append((score, index, {
                "stable_id": stable_id,
                "surface_handle": row.get("surface_handle"),
                "element_text": name,
                "control_type": str(row.get("control_type") or ""),
                "automation_id": automation_id,
                "class_name": class_name,
                "context": context,
                "primary_player": primary_player,
                "score": round(score, 2),
            }))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked or ranked[0][0] < 180.0 or not ranked[0][2].get("primary_player"):
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 24.0:
            return None
        return ranked[0][2]

    def _visible_media_players(
        self,
        goal: str,
        *,
        action: str = "play",
        max_windows: int = 12,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Find visible players from current UIA semantics without a service catalogue.

        This is intentionally a bounded read-only preflight.  It looks at live top-level
        windows, prioritises browser/media-looking surfaces, and counts only windows with
        one high-confidence accessible play control.  The returned stable id is evidence
        only: callers must focus the window and read ``window_elements`` again before a
        click so a popup/layout update cannot turn it into a stale target.
        """
        listing = self.tools.execute("window_list", {"max_windows": 120})
        raw_windows = listing.get("result") if isinstance(listing, dict) else None
        if not listing.get("ok") or not isinstance(raw_windows, list):
            return [], listing

        ranked: list[tuple[int, int, dict[str, Any]]] = []
        media_marks = (
            "music", "музык", "player", "плеер", "media", "audio", "аудио",
            "radio", "радио", "stream", "listen", "video", "видео",
        )
        browser_classes = ("chrome_widgetwin", "mozilla", "cef", "applicationframewindow")
        rejected_marks = (
            "program manager", "taskbar", "панель задач", "overlay", "оверлей",
            "desktopwindow", "shell_traywnd", "progman", "workerw", "osc-widget",
        )
        for index, row in enumerate(raw_windows):
            if not isinstance(row, dict) or not self._surface_allowed(goal, row):
                continue
            title = str(row.get("title") or "").casefold()
            class_name = str(row.get("class_name") or "").casefold()
            blob = f"{title} {class_name}"
            if any(mark in blob for mark in rejected_marks):
                continue
            rect = row.get("rectangle") if isinstance(row.get("rectangle"), (list, tuple)) else []
            if len(rect) >= 4:
                try:
                    if int(rect[2]) - int(rect[0]) < 300 or int(rect[3]) - int(rect[1]) < 180:
                        continue
                except (TypeError, ValueError):
                    continue
            priority = 0 if any(mark in title for mark in media_marks) else (
                1 if any(mark in class_name for mark in browser_classes) else 2
            )
            ranked.append((priority, index, row))
        ranked.sort(key=lambda item: (item[0], item[1]))

        scan_limit = max(1, min(int(max_windows), 12))
        scan_incomplete = len(ranked) > scan_limit
        matches: list[dict[str, Any]] = []
        for _priority, _index, window in ranked[:scan_limit]:
            if (cancel_event and cancel_event.is_set()) or (
                deadline is not None and time.monotonic() >= deadline
            ):
                interrupted = dict(listing)
                interrupted.update({"media_scan_cancelled": bool(cancel_event and cancel_event.is_set()), "media_scan_incomplete": True})
                return [], interrupted
            try:
                handle = LocalAgent._coerce_id(window.get("handle"))
                elements = self.tools.execute(
                    "window_elements",
                    {"handle": handle, "title_contains": str(window.get("title") or ""), "max_elements": 500},
                )
            except Exception:
                continue
            rows = elements.get("result") if isinstance(elements, dict) else None
            if not elements.get("ok") or not isinstance(rows, list):
                continue
            candidate = self._media_ui_candidate(rows, goal, action)
            if candidate:
                matches.append({"window": dict(window), "candidate": candidate})
                if len(matches) > 1:
                    break
        if scan_incomplete and len(matches) < 2:
            incomplete = dict(listing)
            incomplete["media_scan_incomplete"] = True
            return [], incomplete
        return matches, listing

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        return str((schema.get("function") or {}).get("name") or schema.get("name") or "")

    def _compat_tool_response(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        *,
        model: str,
        decision_tokens: int,
        timeout_seconds: float,
        num_gpu: int | None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Emulate tool calls for Ollama models without the native ``tools`` capability.

        The r60 release model advertises only ``completion``/``insert`` in Ollama. Sending
        native tool schemas therefore returns HTTP 400 before the model can act. Structured
        JSON generation *is* supported, so use the same validated action envelope and
        normalize it back to the response shape consumed by the executor.
        """
        catalog: list[dict[str, Any]] = []
        names: list[str] = []
        for schema in tool_schemas:
            function = schema.get("function") or schema
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
            catalog.append({
                "name": name,
                "description": str(function.get("description") or "")[:260],
                "arguments_schema": parameters,
            })
        decision_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tool_calls": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": names},
                            "arguments": {"type": "object"},
                        },
                        "required": ["name", "arguments"],
                    },
                },
            },
            "required": ["content", "tool_calls"],
        }
        compat_messages: list[dict[str, Any]] = []
        instruction = (
            "У этой модели нет нативного function calling. Верни строго JSON по данной схеме. "
            "Чтобы действовать, заполни tool_calls реальными вызовами из каталога; arguments должны точно "
            "соответствовать arguments_schema. Если задача действительно завершена, верни tool_calls=[] и "
            "краткий content. Не печатай вызов инструмента как обычный текст.\n"
            f"КАТАЛОГ ИНСТРУМЕНТОВ: {json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))}"
        )
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "")
            if role == "tool":
                role = "user"
                content = f"РЕЗУЛЬТАТ ИНСТРУМЕНТА {message.get('tool_name') or ''}: {content}"
            elif role == "assistant" and message.get("tool_calls"):
                calls = []
                for call in list(message.get("tool_calls") or []):
                    name, arguments = self._tool_call(call)
                    calls.append({"name": name, "arguments": arguments})
                content = f"ПРЕДЫДУЩИЕ ВЫЗОВЫ ИНСТРУМЕНТОВ: {json.dumps(calls, ensure_ascii=False)}"
            compat_messages.append({"role": role, "content": content})
        compat_messages.insert(0, {"role": "system", "content": instruction})
        data = self.gateway.json(
            compat_messages,
            model=model,
            temperature=0.0,
            schema=decision_schema,
            num_ctx=min(max(int(self.settings.task_num_ctx), 4096), 8192),
            num_predict=max(220, decision_tokens),
            keep_alive=self.settings.keep_alive,
            timeout_seconds=timeout_seconds,
            num_gpu=num_gpu,
            cancel_event=cancel_event,
        )
        if not isinstance(data, dict):
            raise LLMError("модель вернула не-объект вместо JSON-вызова инструмента")
        calls: list[dict[str, Any]] = []
        allowed = set(names)
        for raw in list(data.get("tool_calls") or [])[:1]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            arguments = raw.get("arguments") or {}
            if name in allowed and isinstance(arguments, dict):
                calls.append({"function": {"name": name, "arguments": arguments}})
        return {"content": str(data.get("content") or ""), "tool_calls": calls}

    @staticmethod
    def _screen_only(task: str) -> bool:
        q = task.casefold()
        return any(token in q for token in (
            "текущем экране", "на экране", "это окно", "текущее окно", "здесь напис",
            "нажми", "кликни", "кнопк", "поле", "прокрут", "скролл", "видишь экран",
        ))

    def _catalog_model_choice(
        self, requested: str, candidates: list[dict[str, Any]],
    ) -> str:
        """Resolve a spoken/localized app name against the live installed catalog.

        This is deliberately a model judgment over observed catalog data.  It keeps
        cross-language names (e.g. ``calculator`` → ``Калькулятор``) working without
        embedding an alias table or keyword route in the executor.
        """
        names = [
            str(row.get("name") or "").strip()
            for row in candidates
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        ]
        if not names:
            return ""
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["name", "confidence"],
        }
        prompt = (
            "Выбери из наблюдаемого списка ровно одно установленное приложение, "
            "которое лучше всего соответствует запросу владельца. Учитывай перевод, "
            "локализацию и разговорное имя, но не выдумывай приложение. Если нет "
            "надёжного соответствия, верни пустое name и confidence 0. Верни только JSON.\n"
            f"Запрос: {str(requested or '').strip()[:180]}\n"
            f"Установленные приложения: {json.dumps(names[:220], ensure_ascii=False)}"
        )
        try:
            result = self.gateway.json(
                [{"role": "user", "content": prompt}],
                model=self.settings.fast_model or self.settings.model,
                temperature=0.0, schema=schema, num_ctx=3072, num_predict=64,
                keep_alive=self.settings.keep_alive,
                timeout_seconds=pace.scale(min(8.0, max(3.0, float(self.settings.llm_first_token_timeout))), cap=90.0),
            )
            chosen = str(result.get("name") or "").strip()
            confidence = float(result.get("confidence") or 0.0)
            exact = next((name for name in names if name.casefold() == chosen.casefold()), "")
            return exact if exact and confidence >= 0.70 else ""
        except Exception:
            return ""

    def _embedded_action_completes_goal(
        self, goal: str, tool_name: str, result: dict[str, Any],
    ) -> bool:
        """Ask the model whether a verified typed action is the whole goal.

        A launch/open result is often only the first step of a compound request.  This
        bounded judgment prevents an eager shortcut from ending ``open X and then ...``
        while still allowing atomic actions to finish without observer loops.
        """
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {"complete": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["complete", "reason"],
        }
        prompt = (
            "Реши только один вопрос: полностью ли выполнена исходная цель владельца "
            "после этого подтверждённого typed-действия? Если цель содержит следующий "
            "шаг, адресата, текст или другой результат — false. Верни только JSON.\n"
            f"Цель: {str(goal or '').strip()[:500]}\n"
            f"Инструмент: {tool_name}\n"
            f"Фактический результат: {self._compact_result(result, 1800, goal)}"
        )
        try:
            verdict = self.gateway.json(
                [{"role": "user", "content": prompt}],
                model=self.settings.fast_model or self.settings.model,
                temperature=0.0, schema=schema, num_ctx=1536, num_predict=40,
                keep_alive=self.settings.keep_alive,
                timeout_seconds=pace.scale(min(5.0, max(2.5, float(self.settings.llm_first_token_timeout))), cap=60.0),
            )
            return bool(verdict.get("complete") is True)
        except Exception:
            return False

    @staticmethod
    def _looks_read_only_tool(tool_name: str, description: str = "") -> bool:
        """Classify newly added capabilities without making a name whitelist a boundary.

        Unknown tools fail conservative (state-changing).  A tool is read-only only
        when its typed name or description clearly says that it observes/retrieves
        state.  This lets future connectors participate in the reactive loop while an
        unseen ``send_*``/``delete_*`` capability can never be mistaken for evidence.
        """
        name = str(tool_name or "").strip().casefold()
        desc = str(description or "").strip().casefold().replace("ё", "е")
        if not name:
            return False
        mutating_prefixes = (
            "open_", "launch_", "close_", "set_", "toggle_", "write_", "create_",
            "update_", "delete_", "remove_", "send_", "publish_", "post_", "upload_",
            "install_", "reinstall_", "move_", "rename_", "click", "type_", "press_",
        )
        if name.startswith(mutating_prefixes):
            return False
        read_prefixes = (
            "get_", "list_", "read_", "find_", "search_", "inspect_", "observe_",
            "describe_", "query_", "check_", "fetch_", "lookup_",
        )
        read_suffixes = (
            "_status", "_list", "_snapshot", "_search", "_find", "_details",
            "_info", "_price", "_drafts", "_review", "_state",
        )
        if name.startswith(read_prefixes) or name.endswith(read_suffixes):
            return True
        return bool(
            re.search(
                r"\b(?:получить|показать|прочитать|найти|проверить|статус|состояние|список|"
                r"наблюдение|read|retrieve|fetch|inspect|observe|status|snapshot|list)\b",
                desc,
            )
            and not re.search(
                r"\b(?:изменить|удалить|отправить|опубликовать|загрузить|создать|нажать|"
                r"change|delete|send|publish|upload|create|click)\b",
                desc,
            )
        )

    @staticmethod
    def _trusted_embedded_postcondition(tool_name: str, result: dict[str, Any]) -> bool:
        """Accept a postcondition observed inside a typed executor, never mere ``ok``.

        These primitives perform their own before/after or read-back verification.  UI
        clicks and generic commands are intentionally absent: they still require a
        separate observation from the reactive loop.
        """
        if not LocalAgent._explicitly_verified(result):
            return False
        trusted = {
            "write_file", "system_write_file", "system_batch_rename",
            "launch_application", "system_open_named", "system_open_path",
            "media_control", "system_brightness", "process_terminate", "system_power",
        }
        if tool_name not in trusted:
            return False
        inner = result.get("result") or {}
        if not isinstance(inner, dict):
            return False
        if tool_name in {"write_file", "system_write_file"}:
            return bool(inner.get("readback_verified") is True)
        if tool_name == "system_batch_rename":
            return "verification" in inner or inner.get("dry_run") is True
        if tool_name in {"launch_application", "system_open_named", "system_open_path"}:
            observation = inner.get("observation")
            return bool(isinstance(observation, dict) and observation.get("verified") is True)
        if tool_name == "media_control":
            return bool(
                isinstance(inner.get("after"), dict)
                and inner.get("after", {}).get("state") not in {None, "", "unknown", "none"}
            )
        if tool_name == "system_brightness":
            return bool(inner.get("verified") is True)
        if tool_name == "process_terminate":
            return isinstance(inner.get("remaining"), list)
        if tool_name == "system_power":
            return bool(inner.get("scheduled") is True or inner.get("cancelled") is True)
        return False

    @staticmethod
    def _deterministic_evidence_verdict(goal: str, evidence: dict[str, Any]) -> tuple[bool, str]:
        """Bind a few exact typed postconditions without another fallible LLM hop."""
        q = str(goal or "").casefold().replace("ё", "е")
        rows = evidence.get("observations") if isinstance(evidence, dict) else None
        rows = rows if isinstance(rows, list) else [evidence]
        trusted_rows = [row for row in rows if isinstance(row, dict) and row.get("trusted_postcondition")]
        if not trusted_rows:
            transport_rows = [
                row for row in rows
                if isinstance(row, dict) and isinstance(row.get("media_transport_postcondition"), dict)
            ]
            if transport_rows:
                marker = dict(transport_rows[-1].get("media_transport_postcondition") or {})
                return False, (
                    "Транспортное состояние подтверждено, но точный материал владельца не подтверждён"
                    if marker.get("content_verified") is False else ""
                )
            return False, ""
        last = trusted_rows[-1]
        name = str(last.get("tool") or "")
        args = dict(last.get("arguments") or {})
        result = dict(last.get("result") or {})
        inner = result.get("result") if isinstance(result.get("result"), dict) else {}
        media_ui_marker = last.get("media_ui_postcondition")
        if isinstance(media_ui_marker, dict):
            action = str(media_ui_marker.get("action") or "").casefold()
            if media_ui_marker.get("content_verified") is not True:
                return False, "Точный материал владельца не подтверждён"
            if action == "play":
                return True, "Свежая GSMTC/UIA-проверка подтверждает воспроизведение"
            if action in {"pause", "stop"}:
                return True, "Свежая GSMTC/UIA-проверка подтверждает требуемое transport-состояние"
        if name == "media_control":
            # A generic transport request is exactly proven by GSMTC state. A named
            # material still needs matching live metadata; playing something else is not
            # completion.  This also covers names such as ``Моя волна`` without a noun
            # from a fixed content catalogue.
            target = str(args.get("target") or "")
            if LocalAgent._media_goal_requires_content_match(goal, target) and not LocalAgent._media_result_content_matches(target, result):
                return False, "Медиасессия подтверждает состояние плеера, но не выбранный материал"
            action = str(args.get("action") or inner.get("action") or "").casefold()
            after = inner.get("after") if isinstance(inner.get("after"), dict) else {}
            state = str(after.get("state") or "").casefold()
            if action in {"play", "resume"} and state == "playing":
                return True, "Системная медиасессия подтверждает воспроизведение"
            if action == "pause" and state in {"paused", "stopped"}:
                return True, "Системная медиасессия подтверждает паузу"
            if action == "stop" and state == "stopped":
                return True, "Системная медиасессия подтверждает остановку"
        if name == "system_brightness" and inner.get("verified") is True:
            return True, "Системный адаптер подтвердил фактическую яркость"
        if name in {"write_file", "system_write_file"} and re.search(r"\b(?:созда|запиш|сохран|измени)\w*\b", q):
            return True, "Инструмент перечитал точные байты записанного файла"
        if name == "process_terminate" and re.search(r"\b(?:закро|заверш|останов)\w*\b", q):
            return True, "Повторное перечисление процессов не нашло целевые процессы"
        if name == "system_power" and re.search(r"\b(?:выключ|отмен)\w*\b", q):
            return True, "Windows приняла и подтвердила точную команду питания"
        return False, ""

    @staticmethod
    def _explicitly_verified(result: dict[str, Any]) -> bool:
        inner = result.get("result") or {}
        return bool(result.get("verified") is True or (isinstance(inner, dict) and inner.get("verified") is True))

    @staticmethod
    def _observation_verifies(
        effect_name: str,
        effect_args: dict[str, Any],
        observation_name: str,
        observation_args: dict[str, Any],
        observation_result: dict[str, Any],
    ) -> bool:
        """Accept only an observation that can prove this specific side effect."""
        if not observation_result.get("ok"):
            return False
        inner = observation_result.get("result") or {}
        if effect_name in {"write_file", "system_write_file"}:
            if not isinstance(inner, dict):
                return False
            if observation_name not in {"read_file", "system_read_file"}:
                return False
            expected_path = str(effect_args.get("path") or "").replace("\\", "/").casefold()
            observed_path = str(inner.get("path") or observation_args.get("path") or "").replace("\\", "/").casefold()
            expected_content = str(effect_args.get("content") or "")
            return bool(expected_path and observed_path.endswith(expected_path) and str(inner.get("content") or "") == expected_content)
        if effect_name in {"launch_application", "open_service", "system_open_named", "system_open_path"}:
            if observation_name not in {"foreground_window", "window_list", "process_list", "window_wait"}:
                return False
            target = str(effect_args.get("application") or effect_args.get("service") or effect_args.get("name") or effect_args.get("path") or "").casefold()
            tokens = [token for token in target.replace("\\", "/").split("/")[-1].replace(".exe", "").split() if len(token) >= 3]
            return bool(tokens and any(token in str(inner).casefold() for token in tokens))
        if effect_name in {"open_default_url", "browser_open"}:
            if observation_name not in {"browser_snapshot", "foreground_window", "window_wait"}:
                return False
            from urllib.parse import urlparse
            host = urlparse(str(effect_args.get("url") or "")).netloc.casefold()
            haystack = str(inner).casefold()
            if host and host in haystack:
                return True
            # foreground_window/window_wait only ever see the OS window title, i.e. the
            # page's <title> text -- not its URL. A real site's title is its brand/
            # description ("Kwork - фриланс биржа услуг"), essentially never the bare
            # "domain.tld" string, so the exact-host check above only ever succeeds
            # through browser_snapshot's real url field. Fall back to the registrable
            # label without the TLD (and without a leading "www."), which is what
            # actually tends to appear in a title when navigation genuinely worked.
            label = host[4:] if host.startswith("www.") else host
            label = label.split(".")[0] if label else ""
            return bool(label and len(label) >= 3 and label in haystack)
        if effect_name in {"window_click", "window_type", "browser_fill", "type_text", "click"}:
            media_action = str(effect_args.get("_media_recovery_action") or "")
            if effect_name in {"window_click", "click"} and media_action:
                if observation_name == "media_control":
                    return LocalAgent._media_session_state_verifies(
                        media_action, observation_result,
                    )
                if observation_name == "window_elements" and isinstance(inner, list):
                    return bool(LocalAgent._media_ui_state_verifies(
                        [row for row in inner if isinstance(row, dict)],
                        str(effect_args.get("_media_goal") or ""),
                        media_action,
                    ))
                return False
            if observation_name == "window_wait" and isinstance(inner, dict):
                return bool(
                    inner.get("ready") is True
                    and any(str(observation_args.get(key) or "").strip() for key in ("element_text", "automation_id"))
                )
            if observation_name == "window_elements":
                return bool(inner)
            if observation_name == "screenshot" and isinstance(inner, dict):
                return LocalAgent._vision_goal_reached(effect_args, observation_args, inner)
            return False
        if effect_name in {"media_control", "scroll", "press_key", "hotkey", "mouse_drag"}:
            if observation_name == "window_wait" and isinstance(inner, dict):
                return bool(inner.get("ready") is True and (observation_args.get("element_text") or observation_args.get("automation_id")))
            if effect_name == "media_control":
                return False
            if observation_name == "window_elements":
                return bool(inner)
            if observation_name == "screenshot" and isinstance(inner, dict):
                return LocalAgent._vision_goal_reached(effect_args, observation_args, inner)
            return False
        if effect_name == "window_focus":
            return bool(observation_name == "foreground_window" and isinstance(inner, dict) and inner.get("handle"))
        if effect_name == "powershell":
            return observation_name in {"system_read_file", "system_find", "command_available", "process_list", "window_wait"}
        # Media/session state and destructive communication require their dedicated
        # executors. A generic read-only call is never enough to declare them successful.
        return False

    @staticmethod
    def _effect_matches_goal(goal: str, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Reject preparatory UI motion as completion of an unrelated owner goal."""
        q = str(goal or "").casefold().replace("ё", "е")
        name = str(tool_name or "")
        if name == "window_focus":
            return any(token in q for token in ("сфокус", "переключись на окно", "активируй окно"))
        if name == "mouse_move":
            return any(token in q for token in ("курсор", "указатель", "навед"))
        if name == "scroll":
            return any(token in q for token in ("прокрут", "пролист", "полист", "скрол"))
        if name in {"launch_application", "open_service", "open_default_url", "system_open_named", "system_open_path"}:
            opens = any(token in q for token in ("открой", "открыть", "запусти", "запустить", "перейди", "перейти"))
            additional_change = any(token in q for token in (
                "включ", "воспроиз", "поставь", "отправ", "напиш", "ответь", "оплат",
                "куп", "закаж", "вызов", "удал", "заполн", "нажм", "скача",
            ))
            return opens and not additional_change
        if name == "default_search":
            return any(token in q for token in ("найди", "поищи", "загугл", "поиск"))
        if name in {"window_type", "type_text", "browser_fill"}:
            typing_requested = any(token in q for token in ("введ", "напечат", "заполн", "напиш", "впиши"))
            external_commit = any(token in q for token in ("отправ", "скинь", "ответь", "опубли", "запости"))
            return typing_requested and not external_commit
        if name == "media_control":
            return any(token in q for token in ("музык", "включ", "выключ", "пауз", "трек", "громк", "звук", "воспроиз"))
        if name in {"window_click", "click", "press_key", "hotkey", "mouse_drag"}:
            return True
        if name in {
            "write_file", "make_directory", "system_write_file", "system_batch_rename",
            "powershell", "reinstall_application", "git_publish", "close_application",
            "close_browsers", "close_user_apps", "set_dark_theme", "toggle_quick_setting",
            "system_power",
        }:
            return True
        # A newly installed typed capability is not excluded merely because this build
        # has never seen its name. Read-like tools remain observations; every other
        # unknown call is conservatively an attempted transition and still needs a
        # goal-bound postcondition before success.
        return bool(name and not LocalAgent._looks_read_only_tool(name))

    def _goal_evidence_verdict(
        self,
        goal: str,
        evidence: dict[str, Any],
        reported_answer: str,
        *,
        model_only: bool = False,
    ) -> tuple[bool, str]:
        """Use a separate bounded judgment to bind evidence to the whole owner goal."""
        if not evidence:
            return False, "Нет свежего наблюдаемого доказательства"
        if not model_only:
            deterministic, deterministic_reason = self._deterministic_evidence_verdict(goal, evidence)
            if deterministic or deterministic_reason:
                return deterministic, deterministic_reason
        schema = {
            "type": "object",
            "properties": {
                "achieved": {"type": "boolean"},
                "reason": {"type": "string"},
                "missing": {"type": "string"},
            },
            "required": ["achieved", "reason", "missing"],
        }
        prompt = (
            "Ты независимый проверяющий desktop-agent. Реши, доказывает ли СВЕЖЕЕ ФАКТИЧЕСКОЕ "
            "наблюдение выполнение ВСЕЙ исходной цели владельца. Не доверяй отчёту исполнителя, "
            "факту клика, фокусу окна или просто наличию нужного приложения. Для включения музыки "
            "нужен наблюдаемый playing/pause state; для отправки — доставленное/отправленное состояние; "
            "для настройки — новое значение; для чтения — фактические данные источника. Если доказана "
            "только подготовка или часть цели, achieved=false. Верни строго JSON по схеме.\n\n"
            f"ЦЕЛЬ: {goal}\n"
            f"СВЕЖЕЕ ДОКАЗАТЕЛЬСТВО: {self._compact_result(evidence, 6500)}\n"
            f"ОТЧЁТ ИСПОЛНИТЕЛЯ (не является доказательством): {reported_answer[:800]}"
        )
        try:
            verdict = self.gateway.json(
                [{"role": "user", "content": prompt}],
                model=self.settings.fast_model or self.settings.model,
                temperature=0.0,
                schema=schema,
                num_ctx=3072,
                num_predict=96,
                keep_alive=self.settings.keep_alive,
                timeout_seconds=pace.scale(min(7.0, max(3.0, float(self.settings.llm_first_token_timeout))), cap=90.0),
            )
            return bool(verdict.get("achieved")), str(verdict.get("reason") or verdict.get("missing") or "")[:500]
        except Exception as exc:
            return False, f"Проверяющая модель недоступна: {exc}"

    @staticmethod
    def _authoritative_observation_answer(
        goal: str,
        tool_name: str,
        tool_result: dict[str, Any],
    ) -> str:
        """Finish narrow read requests from typed source data without another LLM turn.

        This is deliberately a capability contract, not an application recipe.  When a
        connector returns the exact scalar requested by the owner, asking the action
        model to paraphrase it adds latency and can turn a completed read into an
        endless policy loop.  Only fields with an unambiguous typed meaning are eligible.
        """
        q = str(goal or "").casefold().replace("ё", "е")
        payload = tool_result.get("result") if isinstance(tool_result, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        if tool_name == "mail_status" and any(
            token in q for token in ("непрочитан", "сколько писем", "количество писем")
        ):
            monitor = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
            unread = monitor.get("unread", payload.get("unread"))
            if isinstance(unread, int) and unread >= 0:
                return f"Непрочитанных писем: {unread}."
        return ""

    def run(
        self,
        task: str,
        model: str | None = None,
        max_steps: int | None = None,
        external_stop_event: threading.Event | None = None,
        allowed_tools: set[str] | None = None,
        auto_vision: bool = False,
        require_tool_action: bool = False,
        require_side_effect: bool = False,
        require_verification: bool = False,
        num_gpu: int | None = None,
        confirmed_action: str = "",
        confirmed_tool: str = "",
        confirmed_arguments: dict[str, Any] | None = None,
        confirmed_surface: dict[str, Any] | None = None,
        task_deadline_seconds: float = 180.0,
        owner_goal: str = "",
        owner_slots: dict[str, str] | None = None,
        model_first_action: dict[str, str] | None = None,
    ) -> str:
        task = task.strip()
        self._set_run_outcome()
        if not task:
            return "Пустая задача."
        surface_task = str(owner_goal or task).strip()
        typed_owner_slots = {
            str(key).strip(): str(value).strip()
            for key, value in dict(owner_slots or {}).items()
            if str(key).strip() and str(value).strip()
        }
        steps = max(1, min(max_steps or self.settings.max_agent_steps, 32))
        tool_schemas = self.tools.native_descriptions()  # type: ignore[attr-defined]
        if allowed_tools is not None:
            tool_schemas = [s for s in tool_schemas if self._schema_name(s) in allowed_tools]
        # ``task`` contains the engine's policy prose (including words such as
        # "button"), so classifying that blob made every reactive goal screen-only and
        # silently removed authoritative mail/web/service tools. Classify only the
        # owner's original natural-language goal.
        # In model-only mode the model's typed dispatch is the sole intent source.
        # The legacy lexical screen detector remains available to compatibility tests,
        # but must not narrow the live capability catalogue for production turns.
        model_only_mode = bool(getattr(self.settings, "model_only_mode", False))
        # A validated structured dispatch is authoritative even when a compatibility
        # test/runtime profile has ``model_only_mode`` disabled.  Re-running the
        # lexical screen classifier here can silently remove the very capability the
        # model selected (for example ``open_service`` for a named music provider).
        # Keep legacy narrowing only for turns that truly lack a typed decision.
        has_typed_dispatch = bool(
            isinstance(model_first_action, dict)
            and str(model_first_action.get("decision_status") or "ok").strip().casefold() == "ok"
            and str(model_first_action.get("action") or "").strip()
        )
        screen_only = False if (model_only_mode or has_typed_dispatch) else self._screen_only(surface_task)
        if screen_only:
            allowed = {
                "screenshot", "desktop_state", "window_list", "window_elements",
                "window_focus", "window_click", "window_type", "window_wait", "mouse_move", "mouse_drag",
                "scroll", "press_key", "hotkey", "click", "type_text", "launch_application",
                "foreground_window", "media_control",
            }
            tool_schemas = [s for s in tool_schemas if self._schema_name(s) in allowed]
        selected = model or self.settings.fast_model or self.settings.model
        code_heavy = any(word in task.lower() for word in ("код", "файл", "проект", "исправ", "рефактор", "тест"))
        decision_tokens = 260 if code_heavy else 120
        # Interactive desktop work must remain bounded even for code. Long work is split
        # into multiple tool turns rather than one 120-second model call.
        decision_timeout = pace.scale(max(5.0, min(float(self.settings.llm_first_token_timeout), 18.0)), cap=150.0)
        deadline = time.monotonic() + max(0.05, min(float(task_deadline_seconds), 900.0))
        native_tool_calling = not tool_schemas or "tools" in set(self.gateway.model_capabilities(selected))
        available_tool_names = {self._schema_name(schema) for schema in tool_schemas}
        tool_descriptions = {
            self._schema_name(schema): str((schema.get("function") or schema).get("description") or "")
            for schema in tool_schemas
        }
        system = (
            "Ты EIRVEN — один локальный ИИ и одновременно исполнитель на компьютере владельца. "
            "Инструменты ниже — твои собственные руки, глаза, терминал и доступ к файлам. "
            "Если пользователь просит действие, не объясняй, что ты текстовая модель, и не проси "
            "его сделать то, что доступно инструментами: вызови инструмент. Не сочиняй результат. "
            "Сначала сопоставь цель с capability-инструментом: если названо приложение, сервис, "
            "файл или настройка, не выбирай случайное активное окно — вызови соответствующий "
            "authoritative инструмент (application_list/launch_application, open_service, "
            "системный или файловый connector), затем наблюдай его результат. "
            "Для незнакомого приложения сначала попробуй launch_application; для неизвестного пути — "
            "system_find; для существующего пути — system_open_path. Для сайта используй open_default_url, "
            "если URL очевиден, иначе default_search. Для уже открытого интерфейса предпочитай window_list/"
            "window_elements перед координатным кликом. Для визуальной задачи screenshot — только снимок; "
            "Окна с заголовком «Эрви», «EIRVEN» или «ChatGPT» являются служебными поверхностями самого ассистента: "
            "не выбирай их как пользовательскую цель, не кликай и не проси владельца взаимодействовать с ними. "
            "если vision не подключён, опирайся на UI Automation/структурные данные, а не выдумывай содержимое пикселей. PowerShell используй как универсальный способ "
            "системной работы, когда структурного инструмента нет. При полном доступе PowerShell может управлять службами, сетью, Wi-Fi/VPN, реестром, темой и настройками Windows. Для поиска сбоев используй system_diagnostics. Автокликеры, локальные утилиты, Git, Docker "
            "и обычная автоматизация разрешены, но установка или скачивание любых программ/зависимостей без отдельной команды владельца запрещены. Если локального приложения или утилиты нет, не ставь её: для сервиса используй официальный веб-вариант через браузер (open_default_url/default_search/web_search), а для системной утилиты честно сообщи, что она недоступна. "
            "Если нужен логин/CAPTCHA/UAC/2FA/пароль, открой нужное место, "
            "остановись и попроси владельца выполнить только этот ручной шаг. Не обходи защиту. Действуй кратчайшим практическим путём и проверяй результат. "
            "После изменяющего действия ОБЯЗАТЕЛЬНО снова наблюдай состояние подходящим read-only инструментом и только затем сообщай об успехе. "
            "Не перечисляй все процессы/файлы без причины: сначала используй самый узкий инструмент, который проверяет текущую гипотезу. "
            "Если интерфейс ещё грузится, подожди и посмотри снова вместо преждевременного отказа. "
            "Работай реактивно: за один ответ вызывай РОВНО ОДИН инструмент, затем заново наблюдай состояние. "
            "Перед кликом/вводом сначала привяжись к одному окну через window_focus; все следующие действия "
            "относятся только к его handle. Если SURFACE_LEASE уже содержит нужный handle, НЕ вызывай window_focus "
            "повторно: сразу выбери действие по свежему window_elements. После смены окна или popup сначала прочитай новую поверхность. "
            "Если UIA не показывает нужный элемент, сделай screenshot привязанного окна: vision_analysis уже "
            "видит изображение. Координаты снимка относительны coordinate_origin; исполнитель безопасно переводит их. "
            "Предпочитай stable_id из свежего window_elements. Нельзя считать собственное слово done, успешный клик "
            "или просто изменение экрана доказательством цели: используй конкретное read-only постусловие. "
            "Для любого названного владельцем сервиса доступен open_service; он не ограничен фиксированным списком. "
            "Не выдумывай музыкальный сервис: если владелец его не назвал, используй только "
            "одну реальную Windows media session или один уже видимый плеер; при неоднозначности задай один свободный вопрос."
        )
        if screen_only:
            system += (
                " ВАЖНО: задача относится к уже открытому текущему экрану. Не открывай новый браузер, "
                "поиск, сайт, проект или файл. Смотри screenshot/window_elements и действуй только в "
                "текущем foreground-интерфейсе."
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        lease = SurfaceLease()
        if screen_only:
            try:
                initial = self.tools.execute("foreground_window", {})
                if initial.get("ok"):
                    self._bind_surface(lease, surface_task, dict(initial.get("result") or {}))
            except Exception:
                pass

        # A confirmed external commit resumes from the exact grounded call stored in
        # the typed checkpoint.  It is never regenerated from natural language.
        pending_exact_call: dict[str, Any] | None = None
        forced_recovery_call: dict[str, Any] | None = None
        forced_model_call: dict[str, Any] | None = None
        # Set when the live application catalog proves that the requested launch
        # target is absent and the next typed call has been redirected to the browser.
        # This lets a verified web surface finish the atomic request instead of handing
        # control back to the policy model, which could otherwise keep exploring apps.
        browser_fallback_in_flight = False
        # A bare "включи музыку" is resolved to the owner's durable Yandex default
        # when no live media session exists.  Keep this as typed recovery state rather
        # than teaching the front-door router a brand-specific regex.
        default_media_service = False
        model_media_start = False
        model_media_content = ""
        # Service identity is already grounded by the single semantic envelope. Keep
        # it typed all the way into execution instead of parsing word order again.
        model_media_service = ""
        model_single_step = bool(
            isinstance(model_first_action, dict)
            and str(model_first_action.get("single_step") or "").casefold() == "true"
        )
        first_action = ""
        target = ""
        if isinstance(model_first_action, dict):
            first_action = str(model_first_action.get("action") or "").strip()
            target = str(model_first_action.get("target") or "").strip()
            target_kind = str(model_first_action.get("target_kind") or "").strip().casefold()
            typed_service = str(model_first_action.get("service") or "").strip()
            if typed_service and first_action == "media_control":
                model_media_service = typed_service
            elif first_action == "media_control" and target_kind == "service" and target:
                model_media_service = target
            if first_action == "launch_application" and target:
                # ``open_service`` is the single live resolver for both installed
                # applications and arbitrary web services.  Starting with a raw
                # launch_application call forces a slow fuzzy Start-menu scan and can
                # then make a model choose an unrelated row when the app is absent.
                # Let the typed resolver inspect the exact current catalog once and
                # fall back to the browser without any installer path.
                forced_model_call = {
                    "function": {"name": "open_service", "arguments": {"service": target}}
                }
                browser_fallback_in_flight = True
            elif first_action == "open_service" and target:
                forced_model_call = {
                    "function": {"name": "open_service", "arguments": {"service": target}}
                }
            elif first_action == "application_list":
                forced_model_call = {
                    "function": {"name": "application_list", "arguments": {}}
                }
            elif first_action == "system_brightness" and target:
                try:
                    level = max(0, min(100, int(float(target.strip().rstrip("%")))))
                except (TypeError, ValueError):
                    level = None
                if level is not None:
                    forced_model_call = {
                        "function": {"name": "system_brightness", "arguments": {"level": level}}
                    }
            elif first_action == "route_search":
                # Route requests are read-only lookups.  Seed the first grounded
                # capability call from the validated semantic envelope so the
                # desktop policy cannot spend its budget inspecting an unrelated
                # foreground app before consulting a current route source.
                forced_model_call = {
                    "function": {
                        "name": "web_search",
                        "arguments": {"query": surface_task, "max_results": 5},
                    }
                }
            elif first_action == "media_control":
                media_action = str(model_first_action.get("media_action") or "unknown").strip().casefold()
                candidate_media_content = str(model_first_action.get("media_content") or "").strip()
                if candidate_media_content and candidate_media_content.casefold().replace("ё", "е") in surface_task.casefold().replace("ё", "е"):
                    model_media_content = candidate_media_content
                # A provider name is not a Windows media-session id. Resolve the exact
                # service first, then observe its real surface and operate the player.
                if model_media_service and media_action in {"play", "pause", "stop", "unknown"}:
                    pass
                elif media_action in {"list", "status"}:
                    forced_model_call = {
                        "function": {"name": "media_control", "arguments": {"action": media_action}}
                    }
                elif media_action in {"play", "pause", "stop"}:
                    media_arguments: dict[str, Any] = {"action": media_action}
                    # ``target`` is a semantic slot, not automatically a session id.
                    # For a transport-only request the model may preserve natural
                    # wording such as «на паузу» while correctly declaring
                    # target_kind=none. Passing that phrase to GSMTC makes a unique
                    # live player look unmatched. Only typed content/application
                    # targets participate in transport selection; named services are
                    # already handled by the open_service branch above.
                    if target and target_kind in {"content", "application"}:
                        media_arguments["target"] = target
                    forced_model_call = {
                        "function": {
                            "name": "media_control",
                            "arguments": media_arguments,
                        }
                    }
                elif not target and media_action in {"play", "unknown"}:
                    # The ownership preflight below uses the live session/player and
                    # asks one open question when there is no unique owner.  It is
                    # enabled by the structured model decision, never by word regexes.
                    model_media_start = True
        opening_only = bool(
            first_action in {"open_service", "launch_application", "open_default_url"}
            and model_single_step
            and not re.search(r"\b(?:и|затем|потом|чтобы)\b|[;\n]", surface_task, re.I)
        )
        media_ui_recovery: MediaUIRecovery | None = None
        if confirmed_action and confirmed_tool and isinstance(confirmed_arguments, dict):
            pending_exact_call = {
                "function": {
                    "name": str(confirmed_tool),
                    "arguments": dict(confirmed_arguments),
                }
            }
            saved_surface = dict(confirmed_surface or {})
            try:
                saved_handle = LocalAgent._coerce_id(saved_surface.get("handle") or saved_surface.get("surface_handle"))
            except (TypeError, ValueError):
                saved_handle = 0
            if saved_handle:
                try:
                    listing = self.tools.execute("window_list", {"max_windows": 120})
                    row = next(
                        (
                            item for item in list(listing.get("result") or [])
                            if isinstance(item, dict) and LocalAgent._coerce_id(item.get("handle")) == saved_handle
                        ),
                        None,
                    )
                except Exception:
                    row = None
                if not row or not self._bind_surface(lease, surface_task, row):
                    pending_exact_call = None
                    confirmed_action = ""
        messages.append({"role": "user", "content": self._lease_context(lease)})
        transcript: list[str] = []
        used_tool = False
        used_action = False
        successful_transition_count = 0
        used_side_effect = False
        goal_effect_seen = False
        commit_attempted = False
        verified_after_effect = False
        last_successful_observation = False
        last_goal_evidence: dict[str, Any] = {}
        evidence_ledger: list[dict[str, Any]] = []
        must_observe_after_action = False
        last_tool_ok: bool | None = None
        last_effect_name = ""
        last_effect_args: dict[str, Any] = {}
        recovery = AdaptiveRecovery(attempts_per_strategy=4, max_strategy_changes=3)
        executed_single_shot: set[str] = set()
        side_effect_tools = {
            "write_file", "make_directory", "system_write_file", "system_batch_rename", "system_open_path", "system_open_named",
            "powershell", "reinstall_application", "git_publish", "open_default_url", "default_search", "launch_application", "open_service", "close_application",
            "close_browsers", "close_user_apps", "set_dark_theme", "toggle_quick_setting", "window_focus", "window_click",
            "window_type", "mouse_move", "mouse_drag", "scroll", "press_key", "hotkey", "click", "type_text", "media_control",
            "system_volume", "system_brightness", "system_power",
            "browser_open", "browser_search", "browser_click_text", "browser_fill",
            "browser_press", "browser_upload", "process_terminate",
        }
        state_transition_tools = set(side_effect_tools)
        observation_tools = {
            "desktop_state", "access_status", "foreground_window", "window_list", "window_elements", "window_wait", "process_list",
            "system_find", "system_list_files", "system_read_file", "read_file", "browser_snapshot",
            "system_diagnostics", "command_available", "web_search", "screenshot", "application_list",
            "mail_status", "mail_drafts",
            "wait", "browser_screenshot", "crypto_price",
        }
        irreversible_tools = {
            "system_power", "process_terminate", "close_user_apps", "reinstall_application", "git_publish",
        }
        never_repeat_tools = {
            "write_file", "system_write_file", "system_batch_rename", "reinstall_application", "git_publish",
            "browser_upload", "process_terminate",
            "system_power",
        }
        scene_single_shot_tools = {
            "browser_fill", "window_type", "type_text", "window_click", "click", "media_control",
            "press_key", "hotkey", "browser_click_text", "browser_press",
        }
        last_observation_signature = ""
        observation_scene_ledger: dict[tuple[str, int, int], str] = {}
        last_semantic_observation_key: tuple[str, str] | None = None
        repeated_observations = 0
        successful_observation_count = 0
        observation_only_streak = 0
        observation_epoch = (lease.handle, lease.generation)
        stagnation_warned = False
        stagnation_terminal = False
        media_no_session_attempts: set[str] = set()
        media_ui_click_budget: set[str] = set()
        uia_scan_serial = 0
        last_uia_context: dict[str, Any] = {
            "handle": 0, "generation": -1, "scan_serial": -1, "rows": [],
        }

        with self.tools.task_scope(external_stop_event):
            # Ownership is a hard engine invariant, not a model preference.  A bare
            # "play music" never grants authority to pick Spotify/Yandex/another
            # provider.  Bind the only concrete live session/player, otherwise ask one
            # open question before the policy model has a chance to invent a service.
            if model_media_start or (not model_only_mode and self._bare_media_start(surface_task)):
                media_probe = (
                    self.tools.execute("media_control", {"action": "list"})
                    if "media_control" in available_tool_names else {"ok": True, "result": {"sessions": []}}
                )
                used_tool = True
                media_payload = media_probe.get("result") if isinstance(media_probe, dict) else None
                sessions = list(media_payload.get("sessions") or []) if isinstance(media_payload, dict) else []
                sessions = [row for row in sessions if isinstance(row, dict)]
                if len(sessions) == 1:
                    session_id = str(sessions[0].get("session_id") or "").strip()
                    play_arguments: dict[str, Any] = {"action": "play"}
                    if session_id:
                        play_arguments["session_id"] = session_id
                    forced_recovery_call = {
                        "function": {
                            "name": "media_control",
                            "arguments": play_arguments,
                        }
                    }
                    messages.append({
                        "role": "user",
                        "content": "MEDIA_OWNERSHIP: найдена ровно одна реальная Windows media session; действую точно по её session_id.",
                    })
                elif len(sessions) > 1:
                    question = "Какой плеер или сервис использовать?"
                    self._set_run_outcome(
                        used_tool=True, used_side_effect=False, verified=False,
                        needs_user=True, clarification=True, clarification_prompt=question,
                        clarification_kind="media_owner", missing_fields=["service_or_player"],
                        allow_free_text=True, media_sessions=len(sessions),
                    )
                    raise TaskNeedsUser(question)
                else:
                    if external_stop_event and external_stop_event.is_set():
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=False, verified=False,
                            cancelled=True,
                        )
                        return "Остановлено пользователем."
                    if {"window_list", "window_elements"} <= available_tool_names:
                        visible_players, window_probe = self._visible_media_players(
                            surface_task,
                            action="play",
                            cancel_event=external_stop_event,
                            deadline=deadline,
                        )
                    else:
                        visible_players, window_probe = [], {"media_scan_unavailable": True}
                    if window_probe.get("media_scan_cancelled"):
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=False, verified=False,
                            cancelled=True,
                        )
                        return "Остановлено пользователем."
                    if len(visible_players) == 1:
                        window = dict(visible_players[0].get("window") or {})
                        media_ui_recovery = MediaUIRecovery(
                            action="play",
                            phase="need_focus",
                            handle=LocalAgent._coerce_id(window.get("handle")),
                            pid=LocalAgent._coerce_id(window.get("pid")),
                            preflight=True,
                        )
                        forced_recovery_call = {
                            "function": {
                                "name": "window_focus",
                                "arguments": {
                                    "handle": LocalAgent._coerce_id(window.get("handle")),
                                    "title_contains": str(window.get("title") or ""),
                                },
                            }
                        }
                        messages.append({
                            "role": "user",
                            "content": "MEDIA_OWNERSHIP: найден ровно один уже видимый плеер; после фокуса обязательно получи свежий stable_id.",
                        })
                    elif not visible_players and model_only_mode and self._bare_media_start(surface_task):
                        default_media_service = True
                        media_ui_recovery = MediaUIRecovery(
                            action="play", service="yandex_music", phase="opening",
                            exact_surface=True,
                        )
                        forced_recovery_call = {
                            "function": {
                                "name": "open_service",
                                "arguments": {"service": "Яндекс Музыка", "purpose": "музыка"},
                            }
                        }
                        messages.append({
                            "role": "user",
                            "content": (
                                "MEDIA_DEFAULT_PROVIDER: живой плеер не найден. Владелец ранее выбрал "
                                "Яндекс Музыку как музыкальный сервис по умолчанию; открой её веб-версию "
                                "одним open_service и продолжи по свежей UI-поверхности."
                            ),
                        })
                    else:
                        question = (
                            "Какой открытый плеер или сервис использовать?"
                            if len(visible_players) > 1 or window_probe.get("media_scan_incomplete") else
                            "В каком сервисе или плеере включить музыку?"
                        )
                        self._set_run_outcome(
                            used_tool=True, used_side_effect=False, verified=False,
                            needs_user=True, clarification=True, clarification_prompt=question,
                            clarification_kind="media_owner", missing_fields=["service_or_player"],
                            allow_free_text=True,
                            visible_media_players=len(visible_players),
                        )
                        raise TaskNeedsUser(question)
            elif (
                pending_exact_call is None
                and not opening_only
                and "open_service" in available_tool_names
                and (
                    explicit_media_service := (
                        model_media_service
                        or typed_owner_slots.get("service_or_player", "")
                        or self._explicit_media_service_slot(surface_task)
                        or (
                            target
                            if (
                                first_action == "media_control"
                                and target
                                and self._media_service_is_grounded(surface_task, target)
                            )
                            else ""
                        )
                    )
                )
            ):
                # The exact service span belongs to the owner, not to the policy model.
                # Seed one general capability call so a small model cannot expand
                # ``SoundCloud`` into an ungrounded brand phrase or observe forever.
                opener_arguments = {"service": explicit_media_service}
                if typed_owner_slots.get("service_or_player"):
                    purpose = self._media_service_purpose(surface_task)
                    if purpose:
                        opener_arguments["purpose"] = purpose
                forced_recovery_call = {
                    "function": {
                        "name": "open_service",
                        "arguments": opener_arguments,
                    }
                }
                # The semantic envelope carries a grounded media_content span when
                # the owner named a wave/track/playlist.  Use it as the authoritative
                # target; the legacy extractor remains only a compatibility fallback
                # for resumes produced by older checkpoints.
                explicit_media_content = model_media_content or self._media_content_for_service(
                    surface_task, explicit_media_service,
                )
                media_ui_recovery = MediaUIRecovery(
                    action="play",
                    target=explicit_media_content,
                    service=explicit_media_service,
                    phase="opening",
                    content_required=bool(explicit_media_content),
                    exact_surface=True,
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "MEDIA_SERVICE_BINDING: владелец явно назвал сервис точным текстом "
                        f"{explicit_media_service!r}. Первый переход — ровно один open_service "
                        "с этим неизменённым аргументом; затем прочитай свежую поверхность."
                    ),
                })
            for step in range(1, steps + 1):
                if external_stop_event and external_stop_event.is_set():
                    self._set_run_outcome(
                        used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                        used_action=used_action, goal_effect_seen=goal_effect_seen,
                        commit_attempted=commit_attempted, cancelled=True,
                    )
                    return "Остановлено пользователем."
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._set_run_outcome(
                        used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                        used_action=used_action, goal_effect_seen=goal_effect_seen,
                        commit_attempted=commit_attempted, timed_out=True,
                    )
                    return "Задача остановлена по общему лимиту времени; последнее необратимое действие автоматически не повторяю."
                # Surface state is data, not a hidden global.  Keep the model informed
                # after every popup/focus transition without asking it to remember a
                # stale title from the beginning of the task.
                messages.append({"role": "user", "content": self._lease_context(lease)})
                try:
                    if pending_exact_call is not None:
                        response = {"content": "", "tool_calls": [pending_exact_call]}
                        pending_exact_call = None
                    elif forced_recovery_call is not None:
                        # A failed native transport with no GSMTC session has one safe
                        # generic recovery: re-observe the already leased player and use
                        # a fresh UIA stable_id. This still executes exactly one tool per
                        # reactive turn and never invents coordinates.
                        response = {"content": "", "tool_calls": [forced_recovery_call]}
                        forced_recovery_call = None
                    elif forced_model_call is not None:
                        # This is a typed action selected by the model-only dispatcher;
                        # it is never inferred from lexical patterns in the engine.
                        response = {"content": "", "tool_calls": [forced_model_call]}
                        forced_model_call = None
                    elif native_tool_calling:
                        response = self.gateway.chat(
                            messages,
                            model=selected,
                            temperature=0.05,
                            tools=tool_schemas,
                            think=False,
                            num_ctx=min(max(self.settings.task_num_ctx, 4096), 8192),
                            num_predict=decision_tokens,
                            keep_alive=self.settings.keep_alive,
                            timeout_seconds=min(decision_timeout, max(0.05, remaining)),
                            num_gpu=num_gpu,
                            cancel_event=external_stop_event,
                        )
                    else:
                        response = self._compat_tool_response(
                            messages,
                            tool_schemas,
                            model=selected,
                            decision_tokens=decision_tokens,
                            timeout_seconds=min(decision_timeout, max(0.05, remaining)),
                            num_gpu=num_gpu,
                            cancel_event=external_stop_event,
                        )
                except LLMError as exc:
                    directive = recovery.record_failure(
                        signature=AdaptiveRecovery.signature("model", type(exc).__name__, str(exc)[:200]),
                        reason=f"local model error: {exc}",
                    )
                    if directive.action in {"continue", "switch_strategy"} and step < steps:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Предыдущий вызов модели/маршрут не сработал. Снова наблюдай фактическое состояние и продолжи. "
                                + ("Четыре подхода исчерпаны — смени класс решения и не повторяй прежние команды. " if directive.action == "switch_strategy" else "")
                                + recovery.prompt_context()
                            ),
                        })
                        continue
                    if transcript:
                        return "\n".join(transcript + [f"Все доступные стратегии модели исчерпаны: {exc}"])
                    return f"Все доступные стратегии модели исчерпаны: {exc}"

                if time.monotonic() >= deadline:
                    self._set_run_outcome(
                        used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                        used_action=used_action, goal_effect_seen=goal_effect_seen,
                        commit_attempted=commit_attempted, timed_out=True,
                    )
                    return "Задача остановлена по общему лимиту времени до следующего действия."

                # A reactive policy never emits a batch against stale UI.  Execute the
                # first grounded action, observe again, then let the model decide anew.
                tool_calls = list(response.get("tool_calls") or [])[:1]
                content = str(response.get("content") or "").strip()
                assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                messages.append(assistant_message)

                if not tool_calls:
                    content_n = content.casefold().replace("ё", "е")
                    clarification_markers = (
                        "уточни", "укажите", "укажи", "назови", "выбери", "какой", "какая",
                        "какое", "какие", "где", "кому", "что именно", "откуда", "куда",
                        "на чем", "в каком сервисе", "каким способом", "разрешаешь",
                    )
                    clarification = bool(
                        not commit_attempted and content
                        and any(token in content_n for token in clarification_markers)
                        and (
                            "?" in content
                            or bool(re.match(r"\s*(?:уточни|укажите|укажи|назови|выбери)", content_n))
                        )
                    )
                    if clarification:
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                            used_action=used_action, goal_effect_seen=goal_effect_seen,
                            commit_attempted=commit_attempted,
                            needs_user=True, clarification=True, clarification_prompt=content,
                        )
                        raise TaskNeedsUser(content)
                    if used_tool and last_tool_ok is False:
                        directive = recovery.record_failure(
                            signature=AdaptiveRecovery.signature("failed-tool-final", transcript[-1:] or content[:300]),
                            reason="the most recent tool failed and no later observation recovered it",
                        )
                        if directive.action in {"continue", "switch_strategy"} and step < steps:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Последний инструмент завершился ошибкой. Нельзя выдавать эту ошибку за выполненную задачу. "
                                    "Выбери другой подходящий инструмент, исправь аргументы и получи успешное фактическое наблюдение. "
                                    + recovery.prompt_context()
                                ),
                            })
                            continue
                        return "Все доступные стратегии исчерпаны: последний реальный инструмент завершился ошибкой."
                    if require_tool_action and not used_tool:
                        directive = recovery.record_failure(
                            signature=AdaptiveRecovery.signature("no-tool", content[:300]),
                            reason="model selected no real computer action",
                        )
                        if directive.action in {"continue", "switch_strategy"} and step < steps:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Ответ без инструмента не выполняет задачу. Наблюдай состояние и вызови реальный инструмент. "
                                    + ("После четырёх неудач полностью смени подход. " if directive.action == "switch_strategy" else "")
                                    + recovery.prompt_context()
                                ),
                            })
                            continue
                        return "Все доступные стратегии исчерпаны: модель не выбрала реального действия на компьютере."
                    if must_observe_after_action and step < steps:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Последнее действие изменило поверхность, но после него не было отдельного свежего наблюдения. "
                                "Следующий вызов обязан быть read-only: window_elements/window_wait/screenshot/foreground_window "
                                "или другим узким инструментом состояния."
                            ),
                        })
                        continue
                    if must_observe_after_action:
                        return "Не удалось получить обязательное свежее наблюдение после действия."
                    if require_side_effect and not goal_effect_seen:
                        directive = recovery.record_failure(
                            signature=AdaptiveRecovery.signature("no-side-effect", content[:300]),
                            reason="no action plausibly satisfying the owner's requested change was observed",
                        )
                        if directive.action in {"continue", "switch_strategy"} and step < steps:
                            messages.append({
                                "role": "user",
                                "content": "Наблюдения недостаточно: выполни требуемое изменение другим безопасным способом. " + recovery.prompt_context(),
                            })
                            continue
                        return "Все доступные стратегии исчерпаны: требуемое изменение не было выполнено."
                    if require_verification and goal_effect_seen and not verified_after_effect and step < steps:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Изменяющее действие уже было выполнено, но результат ещё не проверен. "
                                "Не завершай задачу. Используй read-only наблюдение (window_elements/window_list/process_list/"
                                "system_read_file/command_available/window_wait и т.п.) и подтверди фактическое состояние."
                            ),
                        })
                        continue
                    if require_verification and goal_effect_seen and not verified_after_effect:
                        return "Не удалось надёжно подтвердить результат после выполненного действия."
                    baseline_verified = bool(
                        verified_after_effect if require_side_effect else last_successful_observation
                    )
                    evidence_verified = False
                    evidence_reason = ""
                    if baseline_verified:
                        evidence_verified, evidence_reason = self._goal_evidence_verdict(
                            surface_task, last_goal_evidence, content,
                            model_only=model_only_mode,
                        )
                    if baseline_verified and not evidence_verified and step < steps:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Независимая проверка не доказала всю исходную цель: "
                                f"{evidence_reason or 'доказательство относится только к промежуточному шагу'}. "
                                "Получи более узкое фактическое постусловие и не повторяй уже выполненный commit."
                            ),
                        })
                        continue
                    final_verified = bool(baseline_verified and evidence_verified)
                    self._set_run_outcome(
                        used_tool=used_tool,
                        used_side_effect=used_side_effect,
                        verified=final_verified,
                        used_action=used_action,
                        goal_effect_seen=goal_effect_seen,
                        effect_verified=verified_after_effect,
                        goal_verified=final_verified,
                        commit_attempted=commit_attempted,
                    )
                    return content or ("\n".join(transcript) if transcript else "Задача завершена без текстового результата.")

                for call in tool_calls:
                    name, arguments = self._tool_call(call)
                    if not name:
                        continue
                    arguments = dict(arguments)
                    mail_review_mutates = bool(name == "mail_review" and arguments.get("move_spam"))
                    media_control_reads = bool(
                        name == "media_control" and self._media_control_is_read(arguments)
                    )
                    is_observation_call = bool(
                        name in observation_tools
                        or media_control_reads
                        or (name == "mail_review" and not mail_review_mutates)
                        or (
                            name not in side_effect_tools
                            and self._looks_read_only_tool(name, tool_descriptions.get(name, ""))
                        )
                    )
                    # Unknown future capabilities are conservatively mutations unless
                    # their typed contract clearly identifies them as read-only.
                    is_side_effect_call = bool(
                        (name in side_effect_tools and not media_control_reads)
                        or mail_review_mutates or not is_observation_call
                    )
                    is_transition_call = bool(
                        (name in state_transition_tools and not media_control_reads)
                        or mail_review_mutates or is_side_effect_call
                    )
                    requested_handle = 0
                    try:
                        requested_handle = LocalAgent._coerce_id(arguments.get("handle"))
                    except (TypeError, ValueError):
                        requested_handle = 0
                    requested_title = str(arguments.get("title_contains") or "").casefold()
                    redundant_focus = bool(
                        name == "window_focus" and lease.bound
                        and (
                            requested_handle == lease.handle
                            or (not requested_handle and requested_title and requested_title in lease.title.casefold())
                        )
                    )
                    if redundant_focus:
                        # The lease is the grounding authority; window_click/type also
                        # re-focus that exact HWND before input.  Treat repeated focus as
                        # a read-only acknowledgement so the small policy cannot get
                        # trapped in focus -> screenshot -> focus loops.
                        is_observation_call = True
                        is_side_effect_call = False
                        is_transition_call = False

                    # Ground all UI work against one concrete surface.  A stale model
                    # title can no longer click whichever application happened to become
                    # foreground while it was thinking.
                    surface_tools = {"window_elements", "window_click", "window_type", "window_wait", "screenshot"}
                    foreground_tools = {"scroll", "press_key", "hotkey", "click", "type_text", "mouse_move", "mouse_drag"}
                    grounding_error = ""
                    if name not in available_tool_names:
                        grounding_error = f"Инструмент {name!r} отсутствует в текущем каталоге возможностей."
                    media_provider_tools = {
                        "open_service", "launch_application", "system_open_named", "system_open_path",
                        "open_default_url", "default_search", "browser_open", "browser_search",
                    }
                    media_goal = bool(
                        model_media_start
                        or (isinstance(model_first_action, dict) and str(model_first_action.get("action") or "") == "media_control")
                        or (not model_only_mode and self._media_request(surface_task))
                    )
                    # Route tasks are browser facts, not native-app installation tasks.
                    # If a recovery or an older planner still proposes a map package,
                    # reject that call and force the model back to web_search/default_search.
                    typed_action = str(
                        (model_first_action or {}).get("action") or ""
                    ) if isinstance(model_first_action, dict) else ""
                    if typed_action == "route_search" and name in {
                        "launch_application", "open_service", "system_open_named",
                    }:
                        grounding_error = (
                            "Для route_search не запускай нативные карты и не устанавливай программы. "
                            "Используй web_search или default_search, затем открой только веб-результат в браузере."
                        )
                    # Model-only turns never install software as an implicit recovery.
                    # Keep the lower-level escape hatches available for explicit owner
                    # workflows, but reject package-manager mutations that a confused
                    # model might invent after an application lookup fails.
                    if model_only_mode and name == "reinstall_application":
                        grounding_error = (
                            "Автоматическая установка и переустановка программ запрещена. "
                            "Для сервиса используй браузерный вариант; отдельную установку владелец попросит явно."
                        )
                    if model_only_mode and name == "powershell":
                        command_text = str(arguments.get("command") or "").casefold()
                        package_mutation_markers = (
                            "winget install", "winget upgrade", "winget uninstall", "choco install",
                            "scoop install", "pip install", "npm install", "pnpm install", "msiexec",
                        )
                        if any(marker in command_text for marker in package_mutation_markers):
                            grounding_error = (
                                "Установка программ/пакетов через PowerShell запрещена без отдельной команды владельца. "
                                "Используй официальный веб-сервис в браузере или сообщи о недоступности."
                            )
                    if name in media_provider_tools and media_goal:
                        proposed_player = str(
                            arguments.get("service") or arguments.get("application") or arguments.get("name")
                            or arguments.get("path") or arguments.get("url") or arguments.get("query") or ""
                        ).strip()
                        literal_url = bool(
                            proposed_player
                            and proposed_player.casefold() in surface_task.casefold()
                            and re.match(r"^https?://", proposed_player, re.I)
                        )
                        typed_service_match = bool(
                            model_media_service
                            and proposed_player
                            and proposed_player.casefold() == model_media_service.casefold()
                        )
                        if (
                            not literal_url
                            and not typed_service_match
                            and not (
                                default_media_service and name == "open_service" and
                                re.sub(r"\s+", " ", str(proposed_player or "").casefold().replace("ё", "е")).strip()
                                in {"яндекс музыка", "yandex music", "yandex_music"}
                            )
                            and not self._media_service_is_grounded(surface_task, proposed_player)
                        ):
                            exact_slot = self._explicit_media_service_slot(surface_task, proposed_player)
                            if exact_slot and name == "open_service":
                                arguments["service"] = exact_slot
                                proposed_player = exact_slot
                            else:
                                grounding_error = (
                                    "Владелец не называл этот музыкальный сервис. Не выбирай провайдер сама: "
                                    "используй единственную живую media session/видимый плеер, либо open_service "
                                    "с точным свободным ответом владельца, либо задай один свободный вопрос."
                                )
                    recovery_phase = media_ui_recovery.phase if media_ui_recovery else ""
                    if (
                        media_ui_recovery
                        and name == "media_control"
                        and not media_control_reads
                    ):
                        grounding_error = (
                            "У плеера ещё нет Windows media session. Сначала используй свежий "
                            "window_elements и window_click по stable_id видимого элемента плеера."
                        )
                    if media_ui_recovery and name in {"click", "mouse_move", "mouse_drag", "press_key", "hotkey"}:
                        grounding_error = (
                            "Recovery плеера запрещает координаты/клавиши: выбери свежий stable_id "
                            "из последнего window_elements и вызови window_click."
                        )
                    if (
                        media_ui_recovery
                        and (
                            media_ui_recovery.phase == "need_click"
                            or (
                                media_ui_recovery.phase == "select_content"
                                and bool(media_ui_recovery.fresh_stable_ids)
                            )
                            or (
                                media_ui_recovery.phase == "dismiss_blocker"
                                and bool(media_ui_recovery.fresh_stable_ids)
                            )
                        )
                        and name == "window_click"
                    ):
                        stable_id = str(arguments.get("stable_id") or "").strip()
                        fresh_ids = set(media_ui_recovery.fresh_stable_ids)
                        try:
                            click_handle = LocalAgent._coerce_id(arguments.get("handle"))
                        except (TypeError, ValueError):
                            click_handle = 0
                        recovery_click_key = AdaptiveRecovery.signature(
                            media_ui_recovery.action,
                            media_ui_recovery.target,
                            media_ui_recovery.handle,
                        )
                        if (
                            not stable_id
                            or stable_id not in fresh_ids
                            or click_handle != lease.handle
                            or media_ui_recovery.surface_generation != lease.generation
                            or media_ui_recovery.scan_serial != uia_scan_serial
                            or media_ui_recovery.handle != lease.handle
                            or bool(media_ui_recovery.pid and lease.pid and media_ui_recovery.pid != lease.pid)
                            or (
                                media_ui_recovery.phase == "need_click"
                                and (
                                    media_ui_recovery.clicks >= 1
                                    or recovery_click_key in media_ui_click_budget
                                )
                            )
                        ):
                            grounding_error = (
                                "Для media recovery нужны точный HWND и stable_id из самого свежего "
                                "window_elements этой же версии окна; сначала перечитай элементы."
                            )
                    media_attempt_signature = ""
                    if name == "media_control" and not media_control_reads:
                        media_attempt_signature = AdaptiveRecovery.signature(
                            "media-no-session",
                            self._media_mutation_action(surface_task, arguments),
                            str(arguments.get("target") or "").casefold(),
                            str(arguments.get("session_id") or "").casefold(),
                            lease.handle,
                            lease.generation,
                            last_observation_signature,
                        )
                        if media_attempt_signature in media_no_session_attempts:
                            grounding_error = (
                                "Эта media_control команда уже вернула no_sessions в неизменной сцене. "
                                "Повтор запрещён: наблюдай UI, открой только названный сервис или уточни владельца."
                            )
                    if must_observe_after_action and not is_observation_call:
                        grounding_error = (
                            "После предыдущего действия требуется отдельное свежее read-only наблюдение; "
                            "сначала вызови window_elements/window_wait/screenshot/foreground_window."
                        )
                    if lease.bound and name in surface_tools:
                        arguments["handle"] = lease.handle
                        if "title_contains" in arguments or name != "screenshot":
                            arguments["title_contains"] = lease.title
                    if name in {"window_click", "window_type"} and not lease.bound:
                        grounding_error = "Нет привязанного окна: сначала вызови window_list и window_focus либо open_service."
                    if name in foreground_tools and not grounding_error:
                        if not lease.bound:
                            grounding_error = "Координатное/клавиатурное действие запрещено без SurfaceLease."
                        else:
                            focus = self.tools.execute("window_focus", {"handle": lease.handle})
                            if not focus.get("ok"):
                                grounding_error = "Привязанное окно исчезло или не принимает фокус; заново наблюдай window_list."
                            else:
                                focused_row = focus.get("result") or {}
                                if isinstance(focused_row, dict):
                                    self._bind_surface(lease, surface_task, focused_row)
                            if not grounding_error and name in {"click", "mouse_move", "mouse_drag"} and not lease.rectangle:
                                grounding_error = "У привязанного окна нет подтверждённых границ; вызови window_list или screenshot заново."
                            elif not grounding_error and name in {"click", "mouse_move", "mouse_drag"} and time.monotonic() - lease.observed_at > 12.0:
                                grounding_error = "Геометрия окна устарела; вызови window_list или screenshot перед координатным действием."
                            elif not grounding_error and name in {"click", "mouse_move", "mouse_drag"} and lease.rectangle:
                                left, top, right, bottom = lease.rectangle
                                x, y = int(arguments.get("x") or 0), int(arguments.get("y") or 0)
                                # Vision coordinates are relative to the cropped leased
                                # screenshot. Convert them using the real virtual origin.
                                if not (left <= x <= right and top <= y <= bottom):
                                    if 0 <= x <= right - left and 0 <= y <= bottom - top:
                                        x, y = left + x, top + y
                                if not (left <= x <= right and top <= y <= bottom):
                                    grounding_error = "Координата находится вне привязанного окна; сделай свежий screenshot."
                                else:
                                    arguments["x"], arguments["y"] = x, y

                    if name == "window_focus":
                        requested_handle = LocalAgent._coerce_id(arguments.get("handle"))
                        requested_title = str(arguments.get("title_contains") or "")
                        protected = ("eirven", "эрви", "codex", "chatgpt")
                        task_n = surface_task.casefold()
                        if any(token in requested_title.casefold() for token in protected) and not any(token in task_n for token in protected):
                            grounding_error = "Служебное окно EIRVEN/Codex не является поверхностью этой задачи."
                        elif requested_handle:
                            try:
                                listing = self.tools.execute("window_list", {"max_windows": 120})
                                row = next(
                                    (item for item in list(listing.get("result") or []) if LocalAgent._coerce_id(item.get("handle")) == requested_handle),
                                    None,
                                )
                                if row and not self._surface_allowed(surface_task, row):
                                    grounding_error = "Выбранное служебное окно не разрешено для этой задачи."
                            except Exception:
                                pass

                    media_recovery_click_action = ""
                    if (
                        name == "window_click"
                        and media_ui_recovery
                        and media_ui_recovery.phase == "need_click"
                    ):
                        media_recovery_click_action = media_ui_recovery.action
                    tool_signature = AdaptiveRecovery.signature(name, arguments)
                    dedupe_signature = (
                        AdaptiveRecovery.signature(
                            name, arguments, last_observation_signature, lease.handle, lease.generation,
                        )
                        if name in scene_single_shot_tools
                        else tool_signature
                    )
                    risk = None
                    if grounding_error:
                        result = {"ok": False, "error": grounding_error, "grounding_failed": True}
                    elif (
                        (name in never_repeat_tools or name in scene_single_shot_tools)
                        and dedupe_signature in executed_single_shot
                    ):
                        result = {
                            "ok": False,
                            "error": "Это изменяющее действие уже выполнялось; повтор заблокирован во избежание дубля.",
                            "completed": True,
                            "verified": False,
                        }
                    elif redundant_focus:
                        result = {
                            "ok": True,
                            "verified": True,
                            "result": {
                                "handle": lease.handle, "title": lease.title, "pid": lease.pid,
                                "rectangle": lease.rectangle, "already_bound": True, "verified": True,
                            },
                        }
                    else:
                        risk = RiskPolicy.evaluate(
                            surface_task, name, arguments,
                            context={
                                "surface_handle": lease.handle,
                                "surface_title": lease.title,
                                "surface_pid": lease.pid,
                                "media_recovery": bool(media_recovery_click_action),
                                "confirmation_mode": str(getattr(self.settings, "confirmation_mode", "full") or "full"),
                                "desktop_control_enabled": bool(getattr(self.settings, "enable_desktop_control", True)),
                            },
                        )
                        if risk.blocked:
                            result = {
                                "ok": False,
                                "error": risk.reason or "Управление компьютером отключено в настройках",
                                "permission_required": "desktop_control",
                                "completed": False,
                                "verified": False,
                            }
                        elif risk.requires_confirmation and risk.fingerprint != str(confirmed_action or ""):
                            self._set_run_outcome(
                                used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                                used_action=used_action, goal_effect_seen=goal_effect_seen,
                                commit_attempted=commit_attempted,
                                needs_confirmation=True,
                                confirmation_fingerprint=risk.fingerprint,
                                confirmation_summary=risk.summary,
                                confirmation_category=risk.category,
                                confirmation_tool=name,
                                confirmation_arguments=arguments,
                                confirmation_surface={
                                    "handle": lease.handle, "title": lease.title, "pid": lease.pid,
                                    "rectangle": lease.rectangle, "generation": lease.generation,
                                },
                                confirmation_created_at=time.time(),
                            )
                            confirmation_prefix = (
                                "Перед этим изменяющим действием нужно подтверждение"
                                if risk.category == "action_confirmation"
                                else "Перед необратимым действием нужно точное подтверждение"
                            )
                            raise TaskNeedsUser(
                                f"{confirmation_prefix}: {risk.summary}. "
                                "Скажи «да, продолжай» или «отмена»"
                            )
                        else:
                            result = self.tools.execute(name, arguments)
                            if risk.requires_confirmation and risk.fingerprint == str(confirmed_action or ""):
                                confirmed_action = ""

                    inner_result = result.get("result") or {}
                    if name == "window_focus" and result.get("ok") and isinstance(inner_result, dict):
                        self._bind_surface(lease, surface_task, inner_result)
                    elif name == "foreground_window" and result.get("ok") and isinstance(inner_result, dict):
                        self._adopt_related_surface(
                            lease, surface_task, inner_result,
                            allow_unbound=screen_only,
                        )

                    target_surface = self._verified_target_surface(name, result)
                    target_surface_bound = bool(
                        target_surface and self._bind_surface(lease, surface_task, target_surface)
                    )
                    if (
                        media_ui_recovery
                        and media_ui_recovery.phase == "opening"
                        and name == "open_service"
                    ):
                        if bool(result.get("ok")) and target_surface_bound:
                            media_ui_recovery.handle = lease.handle
                            media_ui_recovery.pid = lease.pid
                            media_ui_recovery.phase = "need_elements"
                        elif lease.bound and media_ui_recovery.service:
                            # A generic resolver may report failure after it has still
                            # left the explicitly requested web surface foreground
                            # (for example a transient search timeout). Keep that
                            # causal surface, perform one fresh UIA read, and never
                            # hand failed recovery back for blind model retries.
                            media_ui_recovery.handle = lease.handle
                            media_ui_recovery.pid = lease.pid
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": lease.handle},
                                }
                            }
                        else:
                            media_ui_recovery.phase = "failed"
                    if (
                        target_surface_bound
                        and name in {"open_service", "launch_application", "open_default_url", "default_search"}
                        and "window_elements" in available_tool_names
                    ):
                        forced_recovery_call = {
                            "function": {
                                "name": "window_elements",
                                "arguments": {"handle": lease.handle},
                            }
                        }

                    # Every mutating UI action receives a fresh, automatic surface
                    # observation before the next policy turn. A newly focused popup is
                    # adopted only when it is not an EIRVEN/Codex control surface.
                    if name in {
                        "window_click", "window_type", "click", "type_text", "press_key", "hotkey", "scroll",
                        "launch_application", "open_service", "open_default_url", "default_search",
                    }:
                        try:
                            if name in {"launch_application", "open_service", "open_default_url", "default_search"}:
                                time.sleep(0.16)
                            post_surface = self.tools.execute("foreground_window", {})
                            if isinstance(result, dict):
                                result["post_surface"] = post_surface
                            if post_surface.get("ok"):
                                candidate = dict(post_surface.get("result") or {})
                                self._adopt_related_surface(
                                    lease,
                                    surface_task,
                                    candidate,
                                    allow_unbound=(
                                        not target_surface_bound
                                        and name in {"launch_application", "open_service", "open_default_url", "default_search"}
                                    ),
                                )
                        except Exception:
                            pass
                    inner = result.get("result") or {}
                    tool_ok = bool(result.get("ok")) and not (
                        isinstance(inner, dict)
                        and (inner.get("ok") is False or int(inner.get("returncode", 0) or 0) != 0)
                    )
                    media_recovery_verified = False
                    media_recovery_verified_by = ""
                    media_transport_verified = False
                    if name == "window_elements" and tool_ok:
                        uia_scan_serial += 1
                        raw_rows = result.get("result")
                        context_rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
                        try:
                            observed_handle = LocalAgent._coerce_id(arguments.get("handle"))
                            if not observed_handle and context_rows:
                                observed_handle = LocalAgent._coerce_id(context_rows[0].get("surface_handle"))
                        except (TypeError, ValueError):
                            observed_handle = 0
                        if not observed_handle:
                            observed_handle = lease.handle
                        if lease.bound and observed_handle == lease.handle:
                            last_uia_context = {
                                "handle": lease.handle,
                                "generation": lease.generation,
                                "scan_serial": uia_scan_serial,
                                "rows": context_rows[:500],
                            }
                        else:
                            last_uia_context = {
                                "handle": 0, "generation": -1, "scan_serial": -1, "rows": [],
                            }

                    # Enrich the screenshot before any recovery state consumes it.  Only
                    # UIA observed on this exact leased generation is forwarded.
                    if auto_vision and name == "screenshot" and tool_ok:
                        shot = result.get("result")
                        if isinstance(shot, dict) and not isinstance(shot.get("vision_analysis"), dict):
                            path = str(shot.get("path") or "")
                            context_rows = self._fresh_vision_uia_rows(last_uia_context, lease)
                            shot["vision_analysis"] = (
                                self._describe_screenshot(path, surface_task, shot, context_rows)
                                if path else self._empty_vision("Screenshot не вернул путь к PNG")
                            )

                    # ``no_sessions`` is the only transport failure that proves no
                    # command was attempted.  Every other failed/ambiguous transport is
                    # left to normal fail-closed recovery and is never followed by an
                    # automatic click.
                    if name == "media_control" and self._media_no_session_result(arguments, result):
                        if media_attempt_signature:
                            media_no_session_attempts.add(media_attempt_signature)
                        action = self._media_mutation_action(surface_task, arguments)
                        target = str(arguments.get("target") or "").strip()
                        requested_media_content = model_media_content
                        # The semantic target "music" is a category, not a requested
                        # track/playlist.  Treat it as empty for UI recovery so the
                        # engine looks for the service's real Play control instead of
                        # asking the owner to open a fictional item named "music".
                        recovery_target = requested_media_content or (
                            "" if self._bare_media_start(surface_task) else target
                        )
                        before_sessions = self._media_sessions_from_result(result)
                        before_ids = tuple(
                            str(row.get("session_id") or "").strip()
                            for row in before_sessions
                            if str(row.get("session_id") or "").strip()
                        )
                        if action not in {"play", "pause", "stop"}:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "MEDIA_RECOVERY: no_sessions подтверждён, но это действие нельзя "
                                    "надёжно проверить одним UI-кликом. Не повторяй media_control; "
                                    "наблюдай интерфейс или запроси недостающую информацию."
                                ),
                            })
                        else:
                            visible_players: list[dict[str, Any]] = []
                            window_probe: dict[str, Any] = {}
                            if not lease.bound and {"window_list", "window_elements"} <= available_tool_names:
                                visible_players, window_probe = self._visible_media_players(
                                    surface_task,
                                    action=action,
                                    cancel_event=external_stop_event,
                                    deadline=deadline,
                                )
                                if target and len(visible_players) > 1:
                                    target_words = {
                                        word for word in re.findall(r"[a-zа-яё0-9]{3,}", target.casefold())
                                    }
                                    grounded_matches = [
                                        item for item in visible_players
                                        if target_words and target_words <= {
                                            word for word in re.findall(
                                                r"[a-zа-яё0-9]{3,}",
                                                str((item.get("window") or {}).get("title") or "").casefold(),
                                            )
                                        }
                                    ]
                                    if len(grounded_matches) == 1:
                                        visible_players = grounded_matches
                            if window_probe.get("media_scan_cancelled"):
                                self._set_run_outcome(
                                    used_tool=True, used_side_effect=used_side_effect, verified=False,
                                    used_action=used_action, goal_effect_seen=goal_effect_seen,
                                    commit_attempted=commit_attempted, cancelled=True,
                                )
                                return "Остановлено пользователем."
                            if lease.bound:
                                media_ui_recovery = MediaUIRecovery(
                                    action=action,
                                    target=recovery_target,
                                    phase="need_elements",
                                    handle=lease.handle,
                                    pid=lease.pid,
                                    before_session_ids=before_ids,
                                    content_required=bool(requested_media_content) or self._media_goal_requires_content_match(surface_task, recovery_target),
                                )
                                forced_recovery_call = {
                                    "function": {"name": "window_elements", "arguments": {"handle": lease.handle}}
                                }
                            elif len(visible_players) == 1:
                                window = dict(visible_players[0].get("window") or {})
                                media_ui_recovery = MediaUIRecovery(
                                    action=action,
                                    target=recovery_target,
                                    phase="need_focus",
                                    handle=LocalAgent._coerce_id(window.get("handle")),
                                    pid=LocalAgent._coerce_id(window.get("pid")),
                                    before_session_ids=before_ids,
                                    content_required=bool(requested_media_content) or self._media_goal_requires_content_match(surface_task, recovery_target),
                                )
                                forced_recovery_call = {
                                    "function": {
                                        "name": "window_focus",
                                        "arguments": {"handle": media_ui_recovery.handle},
                                    }
                                }
                            else:
                                service_grounded = bool(target and self._media_service_is_grounded(surface_task, target))
                                if not service_grounded or len(visible_players) > 1 or window_probe.get("media_scan_incomplete"):
                                    question = (
                                        "Какой открытый плеер или сервис использовать?"
                                        if len(visible_players) > 1 or window_probe.get("media_scan_incomplete")
                                        else "В каком сервисе или плеере продолжить?"
                                    )
                                    self._set_run_outcome(
                                        used_tool=True, used_side_effect=used_side_effect, verified=False,
                                        used_action=used_action, goal_effect_seen=goal_effect_seen,
                                        commit_attempted=commit_attempted, needs_user=True,
                                        clarification=True, clarification_prompt=question,
                                        clarification_kind="media_owner",
                                        missing_fields=["service_or_player"], allow_free_text=True,
                                    )
                                    raise TaskNeedsUser(question)
                                messages.append({
                                    "role": "user",
                                    "content": (
                                        "MEDIA_RECOVERY: у точно названного сервиса ещё нет live session и "
                                        "видимого плеера. Не повторяй media_control; открой ровно этот сервис "
                                        "через open_service и затем прочитай его реальную поверхность."
                                    ),
                                })

                    if media_ui_recovery and media_ui_recovery.phase == "need_focus" and name == "window_focus":
                        if (
                            tool_ok
                            and lease.handle == media_ui_recovery.handle
                            and not (
                                media_ui_recovery.pid and lease.pid
                                and media_ui_recovery.pid != lease.pid
                            )
                        ):
                            media_ui_recovery.phase = "need_elements"
                            media_ui_recovery.pid = lease.pid or media_ui_recovery.pid
                            forced_recovery_call = {
                                "function": {"name": "window_elements", "arguments": {"handle": lease.handle}}
                            }
                        else:
                            media_ui_recovery.phase = "failed"
                            messages.append({
                                "role": "user",
                                "content": "MEDIA_RECOVERY: выбранное окно исчезло или сменило процесс; клик запрещён. Наблюдай окна заново.",
                            })
                    elif media_ui_recovery and media_ui_recovery.phase == "need_elements" and name == "window_elements":
                        rows = result.get("result") if tool_ok else None
                        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                        needs_content_selection = bool(
                            media_ui_recovery.content_required
                            and not media_ui_recovery.content_selected
                        )
                        candidate = (
                            None if needs_content_selection else
                            self._media_ui_candidate(rows, surface_task, media_ui_recovery.action)
                        )
                        try:
                            candidate_handle = LocalAgent._coerce_id((candidate or {}).get("surface_handle") or lease.handle)
                        except (TypeError, ValueError):
                            candidate_handle = 0
                        if needs_content_selection:
                            content_candidate = self._media_content_ui_candidate(
                                rows, media_ui_recovery.target, media_ui_recovery.handle,
                            )
                            if content_candidate:
                                media_ui_recovery.candidate = dict(content_candidate)
                                if content_candidate.get("already_playing"):
                                    # The requested station/playlist is visibly the
                                    # current page and its exact main transport is
                                    # already in Pause state.  Do not toggle it just to
                                    # manufacture an effect; the live postcondition is
                                    # already satisfied and is safe to report.
                                    media_ui_recovery.content_selected = True
                                    media_ui_recovery.phase = "verified"
                                    media_ui_recovery.verified_by = "uia_page_content_already_playing"
                                    media_recovery_verified = True
                                    media_recovery_verified_by = media_ui_recovery.verified_by
                                    goal_effect_seen = True
                                    verified_after_effect = True
                                    media_ui_recovery.fresh_stable_ids = ()
                                    forced_recovery_call = None
                                else:
                                    media_ui_recovery.fresh_stable_ids = (
                                        str(content_candidate["stable_id"]),
                                    )
                                    media_ui_recovery.surface_generation = lease.generation
                                    media_ui_recovery.scan_serial = uia_scan_serial
                                    media_ui_recovery.phase = "select_content"
                                    forced_recovery_call = {
                                        "function": {
                                            "name": "window_click",
                                            "arguments": {
                                                "handle": lease.handle,
                                                "title_contains": lease.title,
                                                "stable_id": str(content_candidate["stable_id"]),
                                            },
                                        }
                                    }
                            elif (
                                blocker_candidate := self._media_blocker_ui_candidate(
                                    rows, media_ui_recovery.handle,
                                )
                            ):
                                media_ui_recovery.candidate = dict(blocker_candidate)
                                media_ui_recovery.fresh_stable_ids = (
                                    str(blocker_candidate["stable_id"]),
                                )
                                media_ui_recovery.surface_generation = lease.generation
                                media_ui_recovery.scan_serial = uia_scan_serial
                                media_ui_recovery.phase = "dismiss_blocker"
                                forced_recovery_call = {
                                    "function": {
                                        "name": "window_click",
                                        "arguments": {
                                            "handle": lease.handle,
                                            "title_contains": lease.title,
                                            "stable_id": str(blocker_candidate["stable_id"]),
                                        },
                                    }
                                }
                            elif (
                                auto_vision and "screenshot" in available_tool_names
                                and lease.bound and lease.handle == media_ui_recovery.handle
                            ):
                                media_ui_recovery.surface_generation = lease.generation
                                media_ui_recovery.scan_serial = uia_scan_serial
                                media_ui_recovery.fresh_stable_ids = ()
                                media_ui_recovery.phase = "content_need_vision"
                                forced_recovery_call = {
                                    "function": {
                                        "name": "screenshot",
                                        "arguments": {"handle": lease.handle},
                                    }
                                }
                            else:
                                question = (
                                    f"Не вижу на текущем экране материал {media_ui_recovery.target!r}. "
                                    "Открой его страницу в выбранном сервисе и напиши «готово»."
                                )
                                self._set_run_outcome(
                                    used_tool=True, used_side_effect=used_side_effect, verified=False,
                                    used_action=used_action, goal_effect_seen=goal_effect_seen,
                                    commit_attempted=commit_attempted, needs_user=True,
                                    clarification=True, clarification_prompt=question,
                                    clarification_kind="media_content_surface",
                                    missing_fields=["visible_requested_content"], allow_free_text=True,
                                )
                                raise TaskNeedsUser(question)
                        elif (
                            candidate
                            and lease.handle == media_ui_recovery.handle
                            and candidate_handle == lease.handle
                            and not (
                                media_ui_recovery.pid and lease.pid
                                and media_ui_recovery.pid != lease.pid
                            )
                        ):
                            media_ui_recovery.candidate = dict(candidate)
                            media_ui_recovery.fresh_stable_ids = (str(candidate["stable_id"]),)
                            media_ui_recovery.surface_generation = lease.generation
                            media_ui_recovery.scan_serial = uia_scan_serial
                            media_ui_recovery.phase = "need_click"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_click",
                                    "arguments": {
                                        "handle": lease.handle,
                                        "title_contains": lease.title,
                                        "stable_id": str(candidate["stable_id"]),
                                    },
                                }
                            }
                        elif (
                            auto_vision
                            and "screenshot" in available_tool_names
                            and lease.bound
                            and lease.handle == media_ui_recovery.handle
                        ):
                            media_ui_recovery.surface_generation = lease.generation
                            media_ui_recovery.scan_serial = uia_scan_serial
                            media_ui_recovery.phase = "need_vision"
                            media_ui_recovery.fresh_stable_ids = ()
                            forced_recovery_call = {
                                "function": {
                                    "name": "screenshot",
                                    "arguments": {"handle": lease.handle},
                                }
                            }
                            messages.append({
                                "role": "user",
                                "content": (
                                    "MEDIA_RECOVERY: UIA неоднозначна. Получи один структурированный vision hint "
                                    "этой поверхности, затем ОБЯЗАТЕЛЬНО перечитай UIA и сопоставь hint с новым "
                                    "stable_id. Координатный клик запрещён."
                                ),
                            })
                        else:
                            media_ui_recovery.phase = "ambiguous"
                            media_ui_recovery.fresh_stable_ids = ()
                            messages.append({
                                "role": "user",
                                "content": (
                                    "MEDIA_RECOVERY: свежая UIA-сцена не содержит одного однозначного "
                                    "главного transport-control. Не нажимай preview/card и не повторяй transport; "
                                    "перейди к screenshot/vision или честному уточнению."
                                ),
                            })
                    elif media_ui_recovery and media_ui_recovery.phase == "content_need_vision" and name == "screenshot":
                        shot = result.get("result") if tool_ok else None
                        shot = shot if isinstance(shot, dict) else {}
                        analysis = shot.get("vision_analysis")
                        analysis = analysis if isinstance(analysis, dict) else {}
                        if (
                            lease.handle == media_ui_recovery.handle
                            and lease.generation == media_ui_recovery.surface_generation
                            and self._vision_actionable_target(
                                analysis, shot, media_ui_recovery.handle,
                            )
                        ):
                            media_ui_recovery.vision_analysis = dict(analysis)
                            media_ui_recovery.vision_meta = {
                                key: shot.get(key) for key in (
                                    "width", "height", "coordinate_origin", "surface_handle",
                                )
                            }
                            media_ui_recovery.phase = "content_vision_refresh"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                        else:
                            question = (
                                f"Не вижу на текущем экране материал {media_ui_recovery.target!r}. "
                                "Открой его страницу в выбранном сервисе и напиши «готово»."
                            )
                            self._set_run_outcome(
                                used_tool=True, used_side_effect=used_side_effect, verified=False,
                                used_action=used_action, goal_effect_seen=goal_effect_seen,
                                commit_attempted=commit_attempted, needs_user=True,
                                clarification=True, clarification_prompt=question,
                                clarification_kind="media_content_surface",
                                missing_fields=["visible_requested_content"], allow_free_text=True,
                            )
                            raise TaskNeedsUser(question)
                    elif media_ui_recovery and media_ui_recovery.phase == "content_vision_refresh" and name == "window_elements":
                        rows = result.get("result") if tool_ok else None
                        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                        candidate = (
                            self._vision_content_uia_candidate(
                                rows,
                                media_ui_recovery.vision_analysis,
                                media_ui_recovery.vision_meta,
                                media_ui_recovery.target,
                                media_ui_recovery.handle,
                            )
                            if lease.handle == media_ui_recovery.handle
                            and lease.generation == media_ui_recovery.surface_generation
                            else None
                        )
                        if candidate:
                            media_ui_recovery.candidate = dict(candidate)
                            media_ui_recovery.fresh_stable_ids = (str(candidate["stable_id"]),)
                            media_ui_recovery.scan_serial = uia_scan_serial
                            media_ui_recovery.phase = "select_content"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_click",
                                    "arguments": {
                                        "handle": lease.handle,
                                        "title_contains": lease.title,
                                        "stable_id": str(candidate["stable_id"]),
                                    },
                                }
                            }
                        else:
                            question = (
                                f"Не удалось сопоставить материал {media_ui_recovery.target!r} "
                                "с одним свежим элементом интерфейса. Открой его страницу и напиши «готово»."
                            )
                            self._set_run_outcome(
                                used_tool=True, used_side_effect=used_side_effect, verified=False,
                                used_action=used_action, goal_effect_seen=goal_effect_seen,
                                commit_attempted=commit_attempted, needs_user=True,
                                clarification=True, clarification_prompt=question,
                                clarification_kind="media_content_surface",
                                missing_fields=["visible_requested_content"], allow_free_text=True,
                            )
                            raise TaskNeedsUser(question)
                    elif media_ui_recovery and media_ui_recovery.phase == "need_vision" and name == "screenshot":
                        shot = result.get("result") if tool_ok else None
                        shot = shot if isinstance(shot, dict) else {}
                        analysis = shot.get("vision_analysis")
                        analysis = analysis if isinstance(analysis, dict) else {}
                        if (
                            lease.handle == media_ui_recovery.handle
                            and lease.generation == media_ui_recovery.surface_generation
                            and self._vision_actionable_target(
                                analysis, shot, media_ui_recovery.handle,
                            )
                        ):
                            media_ui_recovery.vision_analysis = dict(analysis)
                            media_ui_recovery.vision_meta = {
                                key: shot.get(key) for key in (
                                    "width", "height", "coordinate_origin", "surface_handle",
                                )
                            }
                            media_ui_recovery.phase = "vision_refresh"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                        else:
                            # A freshly opened web application often exposes its
                            # transport controls only after the first skeleton frame.
                            # Vision latency itself gives the page time to settle, so
                            # re-read the same leased UIA surface before involving the
                            # owner. This is a bounded generic state-machine retry, not
                            # a provider-specific selector or phrase rule.
                            if media_ui_recovery.refresh_attempts < 2:
                                media_ui_recovery.refresh_attempts += 1
                                media_ui_recovery.phase = "need_elements"
                                media_ui_recovery.fresh_stable_ids = ()
                                forced_recovery_call = {
                                    "function": {
                                        "name": "window_elements",
                                        "arguments": {"handle": media_ui_recovery.handle},
                                    }
                                }
                                messages.append({
                                    "role": "user",
                                    "content": (
                                        "MEDIA_RECOVERY: снимок был сделан во время загрузки или не дал "
                                        "однозначной цели. Повторно прочитай свежую UIA-сцену того же "
                                        "leased handle; не проси владельца до исчерпания bounded refresh."
                                    ),
                                })
                            else:
                                media_ui_recovery.phase = "ambiguous"
                                question = (
                                    "На странице выбранного сервиса не вижу однозначной основной кнопки "
                                    "воспроизведения. Открой в нём готовый экран плеера или убери блокирующий "
                                    "экран и напиши «готово»."
                                )
                                self._set_run_outcome(
                                    used_tool=True, used_side_effect=used_side_effect, verified=False,
                                    used_action=used_action, goal_effect_seen=goal_effect_seen,
                                    commit_attempted=commit_attempted, needs_user=True,
                                    clarification=True, clarification_prompt=question,
                                    clarification_kind="media_surface", missing_fields=["ready_player_surface"],
                                    allow_free_text=True,
                                )
                                raise TaskNeedsUser(question)
                    elif media_ui_recovery and media_ui_recovery.phase == "vision_refresh" and name == "window_elements":
                        rows = result.get("result") if tool_ok else None
                        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                        candidate = (
                            self._vision_media_uia_candidate(
                                rows,
                                media_ui_recovery.vision_analysis,
                                media_ui_recovery.vision_meta,
                                surface_task,
                                media_ui_recovery.action,
                                media_ui_recovery.handle,
                            )
                            if lease.handle == media_ui_recovery.handle
                            and lease.generation == media_ui_recovery.surface_generation
                            else None
                        )
                        if candidate:
                            media_ui_recovery.candidate = dict(candidate)
                            media_ui_recovery.fresh_stable_ids = (str(candidate["stable_id"]),)
                            media_ui_recovery.scan_serial = uia_scan_serial
                            media_ui_recovery.phase = "need_click"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_click",
                                    "arguments": {
                                        "handle": lease.handle,
                                        "title_contains": lease.title,
                                        "stable_id": str(candidate["stable_id"]),
                                    },
                                }
                            }
                        else:
                            media_ui_recovery.phase = "ambiguous"
                            media_ui_recovery.fresh_stable_ids = ()
                            question = (
                                "На странице выбранного сервиса нет одного подтверждённого основного "
                                "элемента воспроизведения. Открой готовый экран плеера или убери "
                                "блокирующий экран и напиши «готово»."
                            )
                            self._set_run_outcome(
                                used_tool=True, used_side_effect=used_side_effect, verified=False,
                                used_action=used_action, goal_effect_seen=goal_effect_seen,
                                commit_attempted=commit_attempted, needs_user=True,
                                clarification=True, clarification_prompt=question,
                                clarification_kind="media_surface", missing_fields=["ready_player_surface"],
                                allow_free_text=True,
                            )
                            raise TaskNeedsUser(question)
                    elif media_ui_recovery and media_ui_recovery.phase == "dismiss_blocker" and name == "window_click":
                        media_ui_recovery.fresh_stable_ids = ()
                        if tool_ok:
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                        else:
                            media_ui_recovery.phase = "failed"
                    elif media_ui_recovery and media_ui_recovery.phase == "select_content" and name == "window_click":
                        media_ui_recovery.fresh_stable_ids = ()
                        if tool_ok and media_ui_recovery.candidate.get("page_grounded"):
                            # The click was the main transport control on a page whose
                            # compact heading grounded the requested content.  Verify
                            # the resulting Play→Pause transition directly; GSMTC
                            # metadata may expose only the current track, not the
                            # station/playlist identity.
                            media_ui_recovery.content_selected = True
                            media_ui_recovery.phase = "verify_uia"
                            forced_recovery_call = {
                                "function": {"name": "window_elements", "arguments": {"handle": media_ui_recovery.handle}}
                            }
                        elif tool_ok and "media_control" in available_tool_names:
                            media_ui_recovery.phase = "verify_content_session"
                            forced_recovery_call = {
                                "function": {"name": "media_control", "arguments": {"action": "list"}}
                            }
                        elif tool_ok:
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                        else:
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                    elif (
                        media_ui_recovery
                        and media_ui_recovery.phase == "verify_content_session"
                        and name == "media_control"
                        and media_control_reads
                    ):
                        content_matches = self._media_result_content_matches(
                            media_ui_recovery.target, result,
                        )
                        if content_matches and self._media_session_state_verifies("play", result):
                            media_ui_recovery.phase = "verified"
                            media_ui_recovery.content_selected = True
                            media_ui_recovery.verified_by = "gsmtc_content_metadata"
                            media_recovery_verified = True
                            media_recovery_verified_by = media_ui_recovery.verified_by
                        elif content_matches:
                            # The exact material is selected but paused.  Only now may the
                            # engine recover through the main transport Play control.
                            media_ui_recovery.content_selected = True
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                        else:
                            media_ui_recovery.phase = "need_elements"
                            forced_recovery_call = {
                                "function": {
                                    "name": "window_elements",
                                    "arguments": {"handle": media_ui_recovery.handle},
                                }
                            }
                            messages.append({
                                "role": "user",
                                "content": (
                                    "MEDIA_CONTENT: live metadata ещё не подтверждает названный материал. "
                                    "Свежо перечитай сцену и продолжай выбор контента без transport-клика."
                                ),
                            })
                    elif media_ui_recovery and media_ui_recovery.phase == "need_click" and name == "window_click":
                        media_ui_recovery.fresh_stable_ids = ()
                        if tool_ok and media_ui_recovery.clicks == 0:
                            media_ui_recovery.clicks = 1
                            media_ui_click_budget.add(AdaptiveRecovery.signature(
                                media_ui_recovery.action,
                                media_ui_recovery.target,
                                media_ui_recovery.handle,
                            ))
                            if media_ui_recovery.exact_surface:
                                media_ui_recovery.phase = "verify_uia"
                                forced_recovery_call = {
                                    "function": {"name": "window_elements", "arguments": {"handle": media_ui_recovery.handle}}
                                }
                            elif "media_control" in available_tool_names:
                                media_ui_recovery.phase = "verify_session"
                                forced_recovery_call = {
                                    "function": {"name": "media_control", "arguments": {"action": "list"}}
                                }
                            else:
                                media_ui_recovery.phase = "verify_uia"
                                forced_recovery_call = {
                                    "function": {"name": "window_elements", "arguments": {"handle": media_ui_recovery.handle}}
                                }
                        else:
                            media_ui_recovery.phase = "failed"
                    elif media_ui_recovery and media_ui_recovery.phase == "verify_session" and name == "media_control" and media_control_reads:
                        if self._media_session_state_verifies(
                            media_ui_recovery.action,
                            result,
                            before_session_ids=media_ui_recovery.before_session_ids,
                        ):
                            media_ui_recovery.verified_by = "gsmtc_state"
                            if (
                                media_ui_recovery.content_required
                                and not self._media_result_content_matches(media_ui_recovery.target, result)
                            ):
                                media_ui_recovery.phase = "verify_content_uia"
                                media_transport_verified = True
                                forced_recovery_call = {
                                    "function": {"name": "window_elements", "arguments": {"handle": media_ui_recovery.handle}}
                                }
                            else:
                                media_ui_recovery.phase = "verified"
                                media_recovery_verified = True
                                media_recovery_verified_by = media_ui_recovery.verified_by
                        else:
                            media_ui_recovery.phase = "verify_uia"
                            forced_recovery_call = {
                                "function": {"name": "window_elements", "arguments": {"handle": media_ui_recovery.handle}}
                            }
                    elif media_ui_recovery and media_ui_recovery.phase == "verify_uia" and name == "window_elements":
                        rows = result.get("result") if tool_ok else None
                        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                        changed_control = self._media_ui_state_verifies(
                            rows, surface_task, media_ui_recovery.action,
                        ) or self._media_ui_candidate_state_changed(rows, media_ui_recovery)
                        try:
                            changed_handle = LocalAgent._coerce_id((changed_control or {}).get("surface_handle") or lease.handle)
                        except (TypeError, ValueError):
                            changed_handle = 0
                        if (
                            changed_control
                            and lease.handle == media_ui_recovery.handle
                            and changed_handle == lease.handle
                        ):
                            media_ui_recovery.phase = "verified"
                            media_ui_recovery.verified_by = "uia_transport_state_changed"
                            if media_ui_recovery.content_required and media_ui_recovery.candidate.get("page_grounded"):
                                media_recovery_verified = True
                                media_recovery_verified_by = "uia_transport_state_changed+page_content"
                            elif media_ui_recovery.content_required:
                                media_ui_recovery.phase = "transport_ready"
                                media_transport_verified = True
                                messages.append({
                                    "role": "user",
                                    "content": (
                                        "MEDIA_RECOVERY: воспроизведение подтверждено, но выбранный владельцем "
                                        "материал ещё не доказан. Используй свежую сцену для проверки/выбора "
                                        "контента и не выдавай transport state за всю цель."
                                    ),
                                })
                            else:
                                media_recovery_verified = True
                                media_recovery_verified_by = media_ui_recovery.verified_by
                        else:
                            media_ui_recovery.phase = "failed"
                            messages.append({
                                "role": "user",
                                "content": (
                                    "MEDIA_RECOVERY: после единственного клика ни GSMTC, ни смена главного "
                                    "Play/Pause control не подтвердили результат. Второй клик запрещён; "
                                    "не сообщай об успехе."
                                ),
                            })
                    elif media_ui_recovery and media_ui_recovery.phase == "verify_content_uia" and name == "window_elements":
                        media_ui_recovery.phase = "transport_ready"
                        messages.append({
                            "role": "user",
                            "content": (
                                "MEDIA_RECOVERY: GSMTC подтвердил playing, но metadata не доказала точный "
                                "материал владельца. Свежая UIA-сцена приложена; продолжай проверяемую "
                                "навигацию без повторного transport-клика."
                            ),
                        })
                    if media_ui_recovery and media_ui_recovery.phase == "failed":
                        # Failed recovery is terminal for this task. The policy model
                        # must not alternate observers forever or invent a second
                        # click/transport action after a stale or uncertain result.
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                            used_action=used_action, goal_effect_seen=goal_effect_seen,
                            commit_attempted=commit_attempted, recovery_exhausted=True,
                        )
                        return (
                            "Не удалось подтвердить воспроизведение на выбранной поверхности; "
                            "повторное действие не выполняю."
                        )
                    used_tool = True
                    embedded_postcondition = False
                    if tool_ok and is_observation_call:
                        last_successful_observation = True
                        evidence_row = {"tool": name, "arguments": dict(arguments), "result": result}
                        if media_transport_verified:
                            evidence_row["media_transport_postcondition"] = {
                                "action": media_ui_recovery.action if media_ui_recovery else "",
                                "verified_by": media_ui_recovery.verified_by if media_ui_recovery else "",
                                "target": media_ui_recovery.target if media_ui_recovery else "",
                                "content_verified": False,
                            }
                        if media_recovery_verified:
                            evidence_row["trusted_postcondition"] = True
                            evidence_row["media_ui_postcondition"] = {
                                "action": (
                                    media_ui_recovery.action if media_ui_recovery else ""
                                ),
                                "verified_by": media_recovery_verified_by,
                                "surface_handle": lease.handle,
                                "target": media_ui_recovery.target if media_ui_recovery else "",
                                "content_verified": True,
                            }
                        evidence_ledger.append(evidence_row)
                        evidence_ledger[:] = evidence_ledger[-10:]
                        last_goal_evidence = {"observations": list(evidence_ledger)}
                        must_observe_after_action = False
                        if used_side_effect:
                            verified_after_effect = bool(
                                verified_after_effect
                                or media_recovery_verified
                                or media_transport_verified
                                or self._observation_verifies(
                                    last_effect_name, last_effect_args, name, arguments, result
                                )
                            )
                    elif tool_ok and is_transition_call:
                        # Focusing a surface is grounding/setup, not progress toward
                        # the user's goal.  Do not let repeated successful window_focus
                        # calls defeat the model-only observer budget.
                        if name != "window_focus":
                            successful_transition_count += 1
                        used_action = True
                        last_successful_observation = False
                        embedded_postcondition = self._trusted_embedded_postcondition(name, result)
                        explicit_effect_verification = bool(embedded_postcondition)
                        must_observe_after_action = not embedded_postcondition
                        if is_side_effect_call:
                            used_side_effect = True
                            last_effect_name = name
                            last_effect_args = dict(arguments)
                            if media_recovery_click_action:
                                last_effect_args["_media_recovery_action"] = media_recovery_click_action
                                last_effect_args["_media_goal"] = surface_task
                            goal_effect_seen = goal_effect_seen or (
                                (name not in {"window_focus", "mouse_move", "scroll"})
                                if model_only_mode else
                                self._effect_matches_goal(surface_task, name, arguments)
                            )
                            verified_after_effect = explicit_effect_verification
                            if name in never_repeat_tools or name in scene_single_shot_tools:
                                executed_single_shot.add(dedupe_signature)
                            if embedded_postcondition:
                                evidence_ledger.append({
                                    "tool": name,
                                    "arguments": dict(arguments),
                                    "result": result,
                                    "trusted_postcondition": True,
                                })
                                evidence_ledger[:] = evidence_ledger[-10:]
                                last_goal_evidence = {"observations": list(evidence_ledger)}
                                last_successful_observation = True
                        if (risk is not None and risk.requires_confirmation) or name in irreversible_tools:
                            commit_attempted = True
                    elif is_transition_call and bool(
                        result.get("attempted") or result.get("executed") or result.get("completed")
                        or (isinstance(inner, dict) and (inner.get("attempted") or inner.get("executed") or inner.get("completed")))
                    ):
                        # Preserve uncertain execution immediately. A timeout/model crash
                        # after this point must never reset the journal and retry a send.
                        used_action = True
                        last_successful_observation = False
                        must_observe_after_action = True
                        if is_side_effect_call:
                            used_side_effect = True
                            goal_effect_seen = goal_effect_seen or (
                                (name not in {"window_focus", "mouse_move", "scroll"})
                                if model_only_mode else
                                self._effect_matches_goal(surface_task, name, arguments)
                            )
                            last_effect_name = name
                            last_effect_args = dict(arguments)
                            if media_recovery_click_action:
                                last_effect_args["_media_recovery_action"] = media_recovery_click_action
                                last_effect_args["_media_goal"] = surface_task
                            verified_after_effect = False
                            if name in never_repeat_tools or name in scene_single_shot_tools:
                                executed_single_shot.add(dedupe_signature)
                        if (risk is not None and risk.requires_confirmation) or name in irreversible_tools:
                            commit_attempted = True
                    recovery_goal_verified = bool(media_recovery_verified)
                    self._set_run_outcome(
                        used_tool=used_tool,
                        used_side_effect=used_side_effect,
                        verified=recovery_goal_verified,
                        used_action=used_action,
                        goal_effect_seen=goal_effect_seen,
                        effect_verified=verified_after_effect,
                        goal_verified=recovery_goal_verified,
                        commit_attempted=commit_attempted,
                    )
                    if media_recovery_verified:
                        # Engine-owned recovery has a complete typed postcondition;
                        # do not hand control back to the policy model, which could
                        # otherwise keep observing the unchanged scene until timeout.
                        return "Воспроизведение подтверждено; цель подтверждена свежей UIA/GSMTC-проверкой."
                    last_tool_ok = tool_ok
                    if media_ui_recovery and media_ui_recovery.phase in {"verified", "transport_ready"}:
                        media_ui_recovery = None
                    no_progress_directive = None
                    stagnation_warning = False
                    if tool_ok:
                        if is_observation_call:
                            successful_observation_count += 1
                            observation_signature = AdaptiveRecovery.signature(name, result.get("result") or result)
                            if observation_signature == last_observation_signature:
                                no_progress_directive = recovery.record_failure(
                                    signature=AdaptiveRecovery.signature("no-progress", name, observation_signature),
                                    reason="fresh observation is identical to the previous scene",
                                )
                            else:
                                last_observation_signature = observation_signature
                            current_epoch = (lease.handle, lease.generation)
                            if current_epoch != observation_epoch:
                                observation_scene_ledger.clear()
                                observation_only_streak = 0
                                stagnation_warned = False
                                observation_epoch = current_epoch
                            semantic_signature = self._semantic_observation_signature(name, result)
                            repeat_key = (name, semantic_signature)
                            if repeat_key == last_semantic_observation_key:
                                repeated_observations += 1
                            else:
                                last_semantic_observation_key = repeat_key
                                repeated_observations = 1
                            repeat_count = repeated_observations
                            # Two identical reads are enough to force a source change;
                            # three are terminal.  This is intentionally capability-
                            # agnostic and prevents any observer (not just window_list)
                            # from monopolising the reactive budget.
                            if repeat_count >= 2:
                                messages.append({
                                    "role": "user",
                                    "content": (
                                        "Это наблюдение уже повторилось без смыслового изменения. "
                                        "Не вызывай тот же observer снова: выбери другой источник "
                                        "состояния, переход к действию или запроси только недостающую информацию."
                                    ),
                                })
                            if repeat_count >= 3:
                                stagnation_terminal = True
                            scene_key = (name, lease.handle, lease.generation)
                            previous_semantic = observation_scene_ledger.get(scene_key)
                            observation_scene_ledger[scene_key] = semantic_signature
                            if previous_semantic is not None and previous_semantic != semantic_signature:
                                observation_only_streak = 0
                                stagnation_warned = False
                                recovery.record_success()
                            else:
                                observation_only_streak += 1
                            stagnation_warning = observation_only_streak >= 4 and not stagnation_warned
                            if stagnation_warning:
                                stagnation_warned = True
                            # A reactive task must reach a transition or a truthful
                            # clarification quickly.  Eight alternating observers were
                            # technically bounded but still produced 40–60 second
                            # hangs on a cold local model.  Five reads without any
                            # action is a hard, capability-neutral stop; the existing
                            # semantic ledger remains responsible for longer recovery
                            # after a real transition.
                            stagnation_terminal = (
                                stagnation_terminal
                                or observation_only_streak >= 8
                                or (observation_only_streak >= 5 and successful_transition_count == 0)
                                or (successful_observation_count >= 7 and successful_transition_count == 0)
                            )
                        else:
                            observation_scene_ledger.clear()
                            observation_only_streak = 0
                            stagnation_warned = False
                            observation_epoch = (lease.handle, lease.generation)
                            recovery.record_success()
                    else:
                        directive = recovery.record_failure(
                            signature=tool_signature,
                            reason=str(result.get("error") or inner)[:500],
                            completed=bool(result.get("completed")),
                            verified=bool(result.get("verified")),
                        )
                    if auto_vision and name == "screenshot" and result.get("ok"):
                        path = str((result.get("result") or {}).get("path") or "")
                        if path:
                            visual_row = {"tool": name, "arguments": dict(arguments), "result": result}
                            evidence_ledger.append(visual_row)
                            evidence_ledger[:] = evidence_ledger[-10:]
                            last_goal_evidence = {"observations": list(evidence_ledger)}
                            if used_side_effect:
                                verified_after_effect = verified_after_effect or self._observation_verifies(
                                    last_effect_name,
                                    last_effect_args,
                                    "screenshot",
                                    {"handle": lease.handle},
                                    result,
                                )
                            if used_side_effect:
                                verified_after_effect = verified_after_effect or self._observation_verifies(
                                    last_effect_name, last_effect_args, name, arguments, result
                                )

                    # UI Automation may legitimately expose no accessible nodes (canvas,
                    # custom Chromium controls, games). Escalate that same observation
                    # through a bounded cropped screenshot and VLM before asking for the
                    # next action. This is an observation pipeline, not a second action.
                    raw_uia = result.get("result") if name == "window_elements" else None
                    if (
                        auto_vision and name == "window_elements" and result.get("ok")
                        and not raw_uia and lease.bound
                        and not (media_ui_recovery and media_ui_recovery.phase == "need_vision")
                    ):
                        visual = self.tools.execute("screenshot", {"handle": lease.handle})
                        if visual.get("ok"):
                            shot = visual.get("result")
                            shot = shot if isinstance(shot, dict) else {}
                            path = str(shot.get("path") or "")
                            if path:
                                context_rows = self._fresh_vision_uia_rows(last_uia_context, lease)
                                shot["vision_analysis"] = self._describe_screenshot(
                                    path, surface_task, shot, context_rows,
                                )
                            result["visual_fallback"] = visual
                            visual_row = {
                                "tool": "screenshot", "arguments": {"handle": lease.handle}, "result": visual,
                            }
                            evidence_ledger.append(visual_row)
                            evidence_ledger[:] = evidence_ledger[-10:]
                            last_goal_evidence = {"observations": list(evidence_ledger)}
                            messages.append({
                                "role": "user",
                                "content": (
                                    "UI Automation не вернула элементов. Используй приложенный vision_analysis "
                                    "из visual_fallback для следующего единственного действия; координаты относятся "
                                    "только к текущему SurfaceLease."
                                ),
                            })
                    prompt = self._needs_user(name, result)
                    if prompt:
                        raise TaskNeedsUser(prompt)
                    transcript.append(f"{name}: {self._compact_result(result, 1800, surface_task)}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_name": name,
                            "content": self._compact_result(result, 5000, surface_task),
                        }
                    )
                    if tool_ok and is_observation_call and not require_side_effect:
                        authoritative_answer = self._authoritative_observation_answer(
                            surface_task, name, result,
                        )
                        if authoritative_answer:
                            self._set_run_outcome(
                                used_tool=True, used_side_effect=False, verified=True,
                                used_action=used_action, goal_effect_seen=False,
                                effect_verified=False, goal_verified=True,
                                commit_attempted=commit_attempted,
                            )
                            return authoritative_answer
                    if (
                        not tool_ok
                        and name == "launch_application"
                        and isinstance(model_first_action, dict)
                        and str(model_first_action.get("action") or "") == "launch_application"
                    ):
                        # A spoken/localized app name may not equal its Start-menu
                        # display name.  Ask the live catalog once, then let the model
                        # select the exact installed entry; never translate it with a
                        # hardcoded alias table in the engine.
                        forced_model_call = {
                            "function": {
                                "name": "application_list",
                                "arguments": {
                                    "query": str(model_first_action.get("target") or "").strip(),
                                    "limit": 200,
                                    "refresh": True,
                                },
                            }
                        }
                    if (
                        tool_ok
                        and name == "application_list"
                        and isinstance(model_first_action, dict)
                        and str(model_first_action.get("action") or "") == "launch_application"
                    ):
                            catalog = inner if isinstance(inner, dict) else {}
                            candidates = catalog.get("applications") if isinstance(catalog, dict) else None
                            best_score = float(catalog.get("best_score") or 0.0) if isinstance(catalog, dict) else 0.0
                            if isinstance(candidates, list) and candidates:
                                target = str(model_first_action.get("target") or "").strip()
                                first = candidates[0] if isinstance(candidates[0], dict) else {}
                                # ``application_list`` is fuzzy and can always return a
                                # best row.  A low score means the requested app is not
                                # grounded in the live Start-menu catalog; never let a
                                # second model call turn that unrelated row into a real
                                # launch (e.g. Notion → Microsoft 365 Copilot).  Hand the
                                # same free-form name to the browser resolver instead.
                                if best_score < 0.55:
                                    browser_fallback_in_flight = True
                                    forced_model_call = {
                                        "function": {
                                            "name": "open_service",
                                            "arguments": {"service": target},
                                        }
                                    }
                                else:
                                    exact_name = str(first.get("name") or "").strip() if best_score >= 0.8 else ""
                                    if not exact_name:
                                        exact_name = self._catalog_model_choice(target, candidates)
                                    if exact_name:
                                        forced_model_call = {
                                            "function": {
                                                "name": "launch_application",
                                                "arguments": {"application": exact_name},
                                        }
                                    }

                    if opening_only and name in {"open_service", "launch_application", "open_default_url"}:
                        opened_verified = bool(tool_ok and self._explicitly_verified(result))
                        self._set_run_outcome(
                            used_tool=True, used_side_effect=used_side_effect, verified=opened_verified,
                            used_action=used_action, goal_effect_seen=opened_verified,
                            effect_verified=opened_verified, goal_verified=opened_verified,
                            commit_attempted=commit_attempted,
                        )
                        if opened_verified:
                            return "Открыла запрошенный сайт или приложение."
                        return "Не удалось подтвердить открытие нужной страницы."

                    if (
                        browser_fallback_in_flight
                        and name == "open_service"
                        and tool_ok
                        and self._explicitly_verified(result)
                    ):
                        self._set_run_outcome(
                            used_tool=used_tool,
                            used_side_effect=used_side_effect,
                            verified=True,
                            used_action=used_action,
                            goal_effect_seen=True,
                            effect_verified=True,
                            goal_verified=True,
                            commit_attempted=commit_attempted,
                        )
                        browser_fallback_in_flight = False
                        return "Официальная веб‑версия открыта и подтверждена в браузере."

                    # ``media_control`` can carry its own authoritative before/after
                    # GSMTC receipt.  Bind that receipt to the typed requested action
                    # before treating it as terminal: a known state alone is not proof
                    # that Play/Pause/Stop reached the requested state.
                    atomic_media_verified = False
                    atomic_media_reason = ""
                    if name == "media_control" and embedded_postcondition:
                        atomic_media_verified, atomic_media_reason = (
                            self._deterministic_evidence_verdict(
                                surface_task,
                                last_goal_evidence,
                            )
                        )
                    atomic_typed_completion = bool(
                        tool_ok
                        and embedded_postcondition
                        and goal_effect_seen
                        and (
                            name in {
                                "launch_application", "system_open_named",
                                "system_open_path", "system_brightness",
                            }
                            or (name == "media_control" and atomic_media_verified)
                        )
                        and (
                            model_single_step
                            or self._embedded_action_completes_goal(surface_task, name, result)
                        )
                    )
                    if name in {
                        "launch_application", "system_brightness", "system_open_named",
                        "system_open_path", "media_control",
                    }:
                        try:
                            log_event(
                                self.settings.root_dir,
                                "ATOMIC_TYPED_CHECK",
                                tool=name,
                                tool_ok=tool_ok,
                                embedded_postcondition=embedded_postcondition,
                                goal_effect_seen=goal_effect_seen,
                                model_single_step=model_single_step,
                                atomic_typed_completion=atomic_typed_completion,
                            )
                        except Exception:
                            pass
                    if atomic_typed_completion:
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=used_side_effect, verified=True,
                            used_action=used_action, goal_effect_seen=goal_effect_seen,
                            effect_verified=True, goal_verified=True,
                            commit_attempted=commit_attempted,
                        )
                        if name == "media_control":
                            return atomic_media_reason or "Состояние медиаплеера подтверждено системным чтением."
                        return (
                            "Яркость экрана установлена и подтверждена системным чтением."
                            if name == "system_brightness"
                            else "Приложение открыто и подтверждено свежим состоянием окна."
                        )
                    if stagnation_terminal:
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                            used_action=used_action, goal_effect_seen=goal_effect_seen,
                            commit_attempted=commit_attempted, recovery_exhausted=True,
                        )
                        return "Независимые наблюдения исчерпаны: сцена не меняется, а подтверждённого действия нет."
                    if stagnation_warning:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Четыре read-only наблюдения не дали семантического прогресса. "
                                "Больше не чередуй observer-инструменты: используй grounded transition, "
                                "структурный vision/connector либо запроси только недостающую информацию."
                            ),
                        })
                    if no_progress_directive is not None:
                        if no_progress_directive.action == "switch_strategy":
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Повторные наблюдения не меняют сцену. Смени источник: UIA → cropped screenshot/VLM, "
                                    "foreground/window_list → структурный connector/API, либо запроси только реально "
                                    "недостающую информацию у владельца. Не повторяй то же наблюдение. "
                                    + recovery.prompt_context()
                                ),
                            })
                        elif no_progress_directive.action == "continue":
                            messages.append({
                                "role": "user",
                                "content": "Сцена не изменилась; проверь другую гипотезу или другой источник наблюдения.",
                            })
                        else:
                            self._set_run_outcome(
                                used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                                used_action=used_action, goal_effect_seen=goal_effect_seen,
                                commit_attempted=commit_attempted, recovery_exhausted=True,
                            )
                            return "Перепробовала доступные независимые наблюдения, но сцена не меняется и цель не подтверждена."
                    if not tool_ok and directive.action == "switch_strategy":
                        messages.append({
                            "role": "user",
                            "content": (
                                "Четыре инструментальных подхода не дали результата. Смени план: используй другой источник наблюдения, "
                                "другой инструмент или другой маршрут к цели. Не повторяй прежнюю сигнатуру. "
                                + recovery.prompt_context()
                            ),
                        })
                    elif not tool_ok and directive.action in {"stop", "stop_uncertain_commit"}:
                        self._set_run_outcome(
                            used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
                            used_action=used_action, goal_effect_seen=goal_effect_seen,
                            commit_attempted=commit_attempted,
                            uncertain_commit=directive.action == "stop_uncertain_commit",
                            recovery_exhausted=True,
                        )
                        return "Перепробовала доступные безопасные стратегии; дальше потребовалось бы повторить рискованное или уже выполненное действие."

        self._set_run_outcome(
            used_tool=used_tool, used_side_effect=used_side_effect, verified=False,
            used_action=used_action, goal_effect_seen=goal_effect_seen,
            commit_attempted=commit_attempted, step_limit=True,
        )
        return "Достигнут лимит шагов. Последние действия:\n" + "\n".join(transcript[-6:])
