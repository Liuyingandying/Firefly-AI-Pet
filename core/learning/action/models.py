"""LearningActionResult (Phase 5 Learning Action Executor).

The outcome of executing one already-decided action. It carries TEXT (a
constraint, a hint, a question, review info) and a status — never a mastery
value, never a score, never a store side effect beyond starting the existing
assessment flow.

    action : which action was executed (mirrors LearningAction)
    status : ready | started | degraded | skipped | failed
    message: the user/prompt-facing text produced by the execution
    source : the recommendation source that led here (provenance, not a decision)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.learning.decision.models import LearningAction


class ActionStatus(str, Enum):
    """How the execution went."""

    READY = "ready"          # constraint/hint/request prepared (no side effect)
    STARTED = "started"      # the existing assessment flow actually started
    DEGRADED = "degraded"    # an input was unavailable; safe fallback text
    SKIPPED = "skipped"      # nothing to execute (free chat)
    FAILED = "failed"        # unexpected error, caught and reported


@dataclass(frozen=True, slots=True)
class LearningActionResult:
    """One executed (or safely degraded) learning action."""

    action: str
    status: str = ActionStatus.READY.value
    message: str = ""
    source: str = ""
    concept_id: str | None = None
    concept_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _value(LearningAction, self.action, "action"))
        object.__setattr__(self, "status", _value(ActionStatus, self.status, "status"))
        object.__setattr__(self, "message", str(self.message or "").strip())
        object.__setattr__(self, "source", str(self.source or "").strip())
        for field in ("concept_id", "concept_name"):
            value = getattr(self, field)
            object.__setattr__(self, field, str(value).strip() if value else None)

    @property
    def kind(self) -> LearningAction:
        return LearningAction(self.action)

    @property
    def status_kind(self) -> ActionStatus:
        return ActionStatus(self.status)

    @property
    def ok(self) -> bool:
        return self.status in (ActionStatus.READY.value, ActionStatus.STARTED.value)

    def lines(self) -> tuple[str, ...]:
        lines = [f"动作：{self.action.upper()}（{self.status}）"]
        if self.concept_name:
            lines.append(f"概念：{self.concept_name}")
        if self.message:
            lines.append(f"执行结果：{self.message}")
        return tuple(lines)

    def context_block(self) -> str | None:
        """Prompt block; None when there is nothing useful to inject."""
        if self.status == ActionStatus.SKIPPED.value or not self.message:
            return None
        return "\n".join(
            ["已准备的学习动作（只读引导，不要自行改变动作）：", *[f"- {l}" for l in self.lines()]]
        )


def _value(enum_cls, value, field: str) -> str:
    if isinstance(value, enum_cls):
        return value.value
    text = str(value or "").strip().lower()
    for member in enum_cls:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid {field}: {value!r}")


__all__ = ["ActionStatus", "LearningActionResult"]
