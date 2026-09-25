# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterable


SCHEMA_VERSION = "eirven.action-plan/v1"


class ProtocolError(ValueError):
    """A planner response is unsafe or structurally incomplete."""


class TaskState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_UI = "waiting_ui"
    RETRYING = "retrying"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class SuccessCondition:
    kind: str
    description: str
    args: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.kind or len(self.kind) > 80:
            raise ProtocolError("success_condition.kind is required")
        if not self.description or len(self.description) > 700:
            raise ProtocolError("success_condition.description is required")
        if not isinstance(self.args, dict):
            raise ProtocolError("success_condition.args must be an object")


@dataclass(slots=True)
class ActionStep:
    id: str
    tool: str
    args: dict[str, Any]
    success_condition: SuccessCondition
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    retries: int = 0
    failure_strategy: str = "replan"
    requires_confirmation: bool = False

    def validate(self) -> None:
        if not self.id or len(self.id) > 80:
            raise ProtocolError("step.id is required")
        if not self.tool or len(self.tool) > 80:
            raise ProtocolError(f"{self.id}: tool is required")
        if not isinstance(self.args, dict):
            raise ProtocolError(f"{self.id}: args must be an object")
        if not isinstance(self.depends_on, list) or any(not isinstance(item, str) for item in self.depends_on):
            raise ProtocolError(f"{self.id}: depends_on must contain step ids")
        if not 0.2 <= float(self.timeout_seconds) <= 3600:
            raise ProtocolError(f"{self.id}: timeout_seconds is out of bounds")
        if not 0 <= int(self.retries) <= 3:
            raise ProtocolError(f"{self.id}: retries is out of bounds")
        if self.failure_strategy not in {"fail", "replan", "ask_user"}:
            raise ProtocolError(f"{self.id}: unsupported failure_strategy")
        self.success_condition.validate()

    def to_executor_spec(self) -> dict[str, Any]:
        goal = str(self.args.get("goal") or "").strip()
        text = str(self.args.get("text") or "")
        mode = str(self.args.get("mode") or "auto")
        return {
            "plan_step_id": self.id,
            "goal": goal,
            "mode": mode,
            "text": text,
            "success": self.success_condition.description,
            "success_condition": asdict(self.success_condition),
            "tool": self.tool,
            "args": dict(self.args),
            "dependencies": list(self.depends_on),
            "timeout_seconds": float(self.timeout_seconds),
            "retries": int(self.retries),
            "failure_strategy": self.failure_strategy,
            "requires_confirmation": bool(self.requires_confirmation),
        }


@dataclass(slots=True)
class ActionPlan:
    goal: str
    steps: list[ActionStep]
    plan_id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex}")
    schema_version: str = SCHEMA_VERSION
    state: TaskState = TaskState.PLANNED
    created_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(f"unsupported schema_version: {self.schema_version!r}")
        if not self.plan_id or len(self.plan_id) > 96:
            raise ProtocolError("plan_id is required")
        if not self.goal or len(self.goal) > 12000:
            raise ProtocolError("goal is required")
        if not 1 <= len(self.steps) <= 24:
            raise ProtocolError("plan must contain 1..24 steps")
        ids: set[str] = set()
        completed_ids: set[str] = set()
        for step in self.steps:
            step.validate()
            if step.id in ids:
                raise ProtocolError(f"duplicate step id: {step.id}")
            ids.add(step.id)
            missing = [item for item in step.depends_on if item not in completed_ids]
            if missing:
                raise ProtocolError(f"{step.id}: dependencies must point to earlier steps: {missing}")
            completed_ids.add(step.id)

    def executor_specs(self) -> list[dict[str, Any]]:
        self.validate()
        values = [step.to_executor_spec() for step in self.steps]
        for value in values:
            value["schema_version"] = self.schema_version
            value["plan_id"] = self.plan_id
        return values

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(slots=True)
class Observation:
    step_id: str
    source: str
    data: dict[str, Any]
    captured_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class StepOutcome:
    step_id: str
    state: TaskState
    verified: bool
    observations: list[Observation] = field(default_factory=list)
    error: str = ""
    output_files: list[dict[str, Any]] = field(default_factory=list)
    remaining_backlog: list[str] = field(default_factory=list)


def plan_from_model(goal: str, raw_steps: Iterable[dict[str, Any]], *, plan_id: str = "") -> ActionPlan:
    """Validate an LLM response and convert it to the only executor input shape."""
    steps: list[ActionStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ProtocolError(f"step {index} must be an object")
        args = raw.get("args")
        condition = raw.get("success_condition")
        if not isinstance(args, dict) or not isinstance(condition, dict):
            raise ProtocolError(f"step {index} is missing args/success_condition")
        steps.append(ActionStep(
            id=str(raw.get("id") or f"step_{index}"),
            tool=str(raw.get("tool") or "desktop_agent"),
            args=dict(args),
            success_condition=SuccessCondition(
                kind=str(condition.get("kind") or "observable_state"),
                description=str(condition.get("description") or ""),
                args=dict(condition.get("args") or {}),
            ),
            depends_on=list(raw.get("depends_on") or []),
            timeout_seconds=float(raw.get("timeout_seconds") or 30.0),
            retries=int(raw.get("retries") or 0),
            failure_strategy=str(raw.get("failure_strategy") or "replan"),
            requires_confirmation=bool(raw.get("requires_confirmation", False)),
        ))
    plan = ActionPlan(goal=str(goal or "").strip(), steps=steps, plan_id=plan_id or f"plan_{uuid.uuid4().hex}")
    plan.validate()
    return plan


def deterministic_plan(goal: str, specs: Iterable[dict[str, Any]]) -> ActionPlan:
    raw: list[dict[str, Any]] = []
    previous = ""
    for index, spec in enumerate(specs, start=1):
        step_id = f"step_{index}"
        raw.append({
            "id": step_id,
            "tool": str(spec.get("tool") or "deterministic"),
            "args": {
                "goal": str(spec.get("goal") or goal),
                "mode": str(spec.get("mode") or "auto"),
                "text": str(spec.get("text") or ""),
            },
            "depends_on": [previous] if previous else [],
            "timeout_seconds": float(spec.get("timeout_seconds") or 30),
            "retries": int(spec.get("retries") or 0),
            "failure_strategy": str(spec.get("failure_strategy") or "replan"),
            "requires_confirmation": bool(spec.get("requires_confirmation", False)),
            "success_condition": {
                "kind": str(spec.get("success_kind") or "observable_state"),
                "description": str(spec.get("success") or "Цель реально достигнута и подтверждена"),
                "args": dict(spec.get("success_args") or {}),
            },
        })
        previous = step_id
    return plan_from_model(goal, raw)
