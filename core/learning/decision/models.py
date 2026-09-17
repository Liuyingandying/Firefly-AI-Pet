"""LearningRecommendation (Phase 4 Learning Decision Layer).

A deterministic "what should happen next" derived from existing read-only
signals. It is NOT mastery, NOT an assessment and NOT a plan: it selects one
next action from a frozen priority list, and it never writes anything.

    review_due                      -> REVIEW
    no current focus                -> FREE_CHAT
    mastery == 0 (or unknown)       -> EXPLAIN
    mastery == 1                    -> EXPLAIN_RECALL
    mastery in (2, 3)               -> PRACTICE
    mastery >= 4, chapter complete
        and a next node exists      -> MOVE_NEXT
    mastery >= 4 otherwise          -> TRANSFER
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LearningAction(str, Enum):
    """The next action the runtime may offer.

    ``EXPLAIN_RECALL`` is part of the frozen priority list (mastery == 1) even
    though it was missing from the enum list in the phase spec; without it the
    mastery==1 rule is not expressible. The other six match the spec verbatim.
    """

    EXPLAIN = "explain"
    EXPLAIN_RECALL = "explain_recall"
    PRACTICE = "practice"
    REVIEW = "review"
    TRANSFER = "transfer"
    MOVE_NEXT = "move_next"
    FREE_CHAT = "free_chat"


class DecisionSource(str, Enum):
    """Which signal decided the action (provenance, not a second decision)."""

    REVIEW_DUE = "review_due"
    NO_FOCUS = "no_focus"
    MASTERY = "mastery"
    CHAPTER_COMPLETE = "chapter_complete"


#: Deterministic reason text per action (single source, shared with the prompt).
REASON_BY_ACTION: dict[LearningAction, str] = {
    LearningAction.REVIEW: "该知识点已到复习时间，先复习再继续",
    LearningAction.FREE_CHAT: "还没有明确的学习重点，先自由对话或确定一个概念",
    LearningAction.EXPLAIN: "尚未开始学习这个概念，先解释概念并给一个例子",
    LearningAction.EXPLAIN_RECALL: "已经接触过这个概念，先让学习者回忆再问一个简单问题",
    LearningAction.PRACTICE: "已有基础，进入练习巩固",
    LearningAction.TRANSFER: "掌握较好，进入应用/迁移场景",
    LearningAction.MOVE_NEXT: "本章已全部掌握，进入下一结构节点",
}


@dataclass(frozen=True, slots=True)
class LearningRecommendation:
    """One deterministic next-action recommendation (read-only)."""

    action: str
    reason: str = ""
    concept_id: str | None = None
    concept_name: str | None = None
    source: str = DecisionSource.NO_FOCUS.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _action_value(self.action))
        object.__setattr__(self, "source", _source_value(self.source))
        if not self.reason:
            object.__setattr__(
                self, "reason", REASON_BY_ACTION[LearningAction(self.action)]
            )
        for field in ("concept_id", "concept_name"):
            value = getattr(self, field)
            object.__setattr__(self, field, str(value).strip() if value else None)

    @property
    def kind(self) -> LearningAction:
        return LearningAction(self.action)

    def lines(self) -> tuple[str, ...]:
        lines = [f"下一步动作：{self.action.upper()}"]
        if self.reason:
            lines.append(f"理由：{self.reason}")
        if self.concept_name:
            lines.append(f"对应概念：{self.concept_name}")
        return tuple(lines)

    def context_block(self) -> str | None:
        """Prompt block for the chat turn; None for FREE_CHAT (nothing to say)."""
        if self.kind is LearningAction.FREE_CHAT and not self.concept_name:
            return None
        return "\n".join(
            ["下一步学习动作（确定性规则给出，不要自行更改）：", *[f"- {l}" for l in self.lines()]]
        )


def _action_value(value) -> str:
    if isinstance(value, LearningAction):
        return value.value
    text = str(value or "").strip().lower()
    for member in LearningAction:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid learning action: {value!r}")


def _source_value(value) -> str:
    if isinstance(value, DecisionSource):
        return value.value
    text = str(value or "").strip().lower()
    for member in DecisionSource:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid decision source: {value!r}")


__all__ = [
    "LearningAction",
    "DecisionSource",
    "REASON_BY_ACTION",
    "LearningRecommendation",
]
