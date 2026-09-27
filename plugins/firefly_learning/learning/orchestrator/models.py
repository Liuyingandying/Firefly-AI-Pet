"""LearningLoopResult (Phase 6 Learning Loop Orchestrator).

The outcome of one orchestrated learning turn. It reports what the loop DID
(detect / record / read / decide / execute) and carries the prompt-facing text —
never a mastery value and never a decision of its own.

    status  : learning_command | learning_loop | ordinary | degraded
    action  : the decided next action (from the Phase 4 policy), or None
    response: the user/prompt-facing text of the turn
    source  : which signal drove the turn (provenance, not a second decision)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LoopStatus(str, Enum):
    """What kind of turn this was."""

    LEARNING_COMMAND = "learning_command"   # a deterministic learning command answered
    LEARNING_LOOP = "learning_loop"         # mode on: context/decision/action prepared
    ORDINARY = "ordinary"                   # mode off: no learning involvement
    DEGRADED = "degraded"                   # a step failed; the turn survived


class LoopSource(str, Enum):
    """Provenance of the turn (mirrors the decision source when decided)."""

    LEARNING_COMMAND = "learning_command"
    FREE_CHAT = "free_chat"
    NO_FOCUS = "no_focus"
    REVIEW_DUE = "review_due"
    MASTERY = "mastery"
    CHAPTER_COMPLETE = "chapter_complete"
    NO_CONCEPT = "no_concept"


@dataclass(frozen=True, slots=True)
class LearningLoopResult:
    """One orchestrated learning turn (read-only report)."""

    status: str = LoopStatus.ORDINARY.value
    action: str | None = None
    response: str = ""
    source: str = LoopSource.FREE_CHAT.value
    context_block: str | None = None
    # Phase 9B-1: the loop carries the four read-only contexts it computed so
    # the runner can build the response contract WITHOUT recomputing anything.
    learning_context: Any | None = None
    teaching_context: Any | None = None
    decision: Any | None = None
    action_result: Any | None = None
    # Phase 9C: the initial bootstrap of a fresh course (None otherwise)
    bootstrap: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _status_value(self.status))
        object.__setattr__(self, "source", _source_value(self.source))
        object.__setattr__(self, "response", str(self.response or ""))
        action = getattr(self, "action", None)
        object.__setattr__(self, "action", str(action).strip() if action else None)

    @property
    def status_kind(self) -> LoopStatus:
        return LoopStatus(self.status)

    @property
    def is_learning_turn(self) -> bool:
        return self.status in (
            LoopStatus.LEARNING_COMMAND.value,
            LoopStatus.LEARNING_LOOP.value,
        )

    def lines(self) -> tuple[str, ...]:
        lines = [f"学习回合：{self.status}"]
        if self.action:
            lines.append(f"动作：{self.action.upper()}")
        if self.response:
            lines.append(f"内容：{self.response}")
        return tuple(lines)


def _status_value(value) -> str:
    if isinstance(value, LoopStatus):
        return value.value
    text = str(value or "").strip().lower()
    for member in LoopStatus:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid loop status: {value!r}")


def _source_value(value) -> str:
    if isinstance(value, LoopSource):
        return value.value
    text = str(value or "").strip().lower()
    for member in LoopSource:
        if text in (member.value, member.name.lower()):
            return member.value
    # A decision source (review_due / mastery / ...) is accepted as-is so the
    # result can mirror the Phase 4 provenance without a second mapping.
    return str(value or "").strip().lower()


__all__ = ["LearningLoopResult", "LoopSource", "LoopStatus"]
