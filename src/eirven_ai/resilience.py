from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


RecoveryAction = Literal["continue", "switch_strategy", "stop", "stop_uncertain_commit"]


@dataclass(slots=True)
class RecoveryDirective:
    action: RecoveryAction
    strategy_generation: int
    attempts_in_strategy: int
    reason: str = ""


@dataclass(slots=True)
class AdaptiveRecovery:
    """Bounded retry policy shared by desktop agents.

    A retry is useful only while it produces a new observation or tests a new hypothesis.
    Four failed local approaches trigger a strategy reset.  A side effect which may already
    have happened is never retried merely because the UI did not expose confirmation.
    """

    attempts_per_strategy: int = 4
    max_strategy_changes: int = 3
    strategy_generation: int = 0
    attempts_in_strategy: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.attempts_per_strategy = max(1, min(int(self.attempts_per_strategy), 8))
        self.max_strategy_changes = max(0, min(int(self.max_strategy_changes), 5))
        self.strategy_generation = max(0, int(self.strategy_generation))
        self.attempts_in_strategy = max(0, int(self.attempts_in_strategy))
        self.failures = [dict(row) for row in self.failures[-24:] if isinstance(row, dict)]

    @staticmethod
    def signature(*parts: Any) -> str:
        raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def failed_signatures(self) -> list[str]:
        return [str(row.get("signature") or "") for row in self.failures if row.get("signature")]

    def record_success(self) -> None:
        self.attempts_in_strategy = 0

    def record_failure(
        self,
        *,
        signature: str = "",
        reason: str = "",
        completed: bool = False,
        verified: bool = False,
    ) -> RecoveryDirective:
        if completed and not verified:
            return RecoveryDirective(
                "stop_uncertain_commit",
                self.strategy_generation,
                self.attempts_in_strategy,
                "side effect completed once without reliable verification",
            )

        self.attempts_in_strategy += 1
        self.failures.append(
            {
                "strategy": self.strategy_generation,
                "attempt": self.attempts_in_strategy,
                "signature": str(signature or "")[:120],
                "reason": str(reason or "")[:500],
            }
        )
        self.failures = self.failures[-24:]
        if self.attempts_in_strategy < self.attempts_per_strategy:
            return RecoveryDirective(
                "continue", self.strategy_generation, self.attempts_in_strategy, reason
            )
        if self.strategy_generation < self.max_strategy_changes:
            self.strategy_generation += 1
            self.attempts_in_strategy = 0
            return RecoveryDirective(
                "switch_strategy",
                self.strategy_generation,
                0,
                reason or "four approaches made no progress",
            )
        return RecoveryDirective(
            "stop",
            self.strategy_generation,
            self.attempts_in_strategy,
            reason or "all bounded strategies exhausted",
        )

    def prompt_context(self, limit: int = 8) -> str:
        recent = self.failures[-max(1, int(limit)) :]
        if not recent:
            return f"Стратегия {self.strategy_generation}: неудачных подходов пока нет."
        rows = []
        for item in recent:
            rows.append(
                f"s{item.get('strategy', 0)}/a{item.get('attempt', 0)} "
                f"{item.get('signature', '')}: {item.get('reason', '')}"
            )
        return (
            f"Текущее поколение стратегии: {self.strategy_generation}. "
            "Не повторяй перечисленные подходы без нового наблюдаемого основания:\n- "
            + "\n- ".join(rows)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "AdaptiveRecovery":
        raw = dict(value) if isinstance(value, dict) else {}
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: item for key, item in raw.items() if key in allowed})
