from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(slots=True)
class PlannedStep:
    kind: str
    target: str
    verify: str = ""
    fallback: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ActionPlanner:
    """Zero-LLM planner for routine owner commands.

    The language layer may normalize intent, but these plans are deterministic and bounded.
    LLM reasoning is reserved for unknown UI layouts or ambiguous multi-step goals.
    """

    def plan(self, action: str, target: str, *, camera: bool = False) -> list[PlannedStep]:
        a=(action or "").casefold(); t=(target or "").strip()
        if a == "open":
            return [PlannedStep("open",t,"window_or_page_visible","system_search_or_web")]
        if a == "close":
            return [PlannedStep("close",t,"window_gone","alt_f4_or_process_close")]
        if a in {"enable","disable"}:
            return [PlannedStep(a,t,"state_changed","visible_screen_operator")]
        if a == "send":
            return [PlannedStep("open_messenger",t,"messenger_visible"),PlannedStep("find_chat",t,"chat_visible"),PlannedStep("send",t,"message_visible")]
        if a == "answer":
            return [PlannedStep("focus_app",t,"app_visible"),PlannedStep("accept",t,"call_connected","visible_screen_operator")]
        if a == "show" and camera:
            return [PlannedStep("spatial_render",t,"widget_visible")]
        if a == "analyze":
            return [PlannedStep("capture",t,"frame_ready"),PlannedStep("analyze",t,"nonempty_answer")]
        return [PlannedStep(a or "act",t)]

    def describe(self, action: str, target: str, *, camera: bool = False) -> list[dict[str, str]]:
        return [x.to_dict() for x in self.plan(action,target,camera=camera)]
