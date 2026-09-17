"""LearningInteractionEvent (Phase 3.5).

An append-only FACT: "this concept was actually discussed / requested / tested
in this course at this time". It is NOT a judgement about how well the learner
knows the concept — no mastery, no score, no inference.

Only facts produced by an explicit learning action are recorded:

- the user explicitly asks about a course concept ("解释一下传递函数");
- learning mode explicitly discusses a course concept;
- the check-understanding flow starts for a concept.

Ambient sources (PageLens highlights, OCR, ordinary chat, auto extraction) never
produce an event, and the recorder cannot invent a concept it cannot resolve
inside the course.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class InteractionEventType(str, Enum):
    """What happened (factual, not evaluative)."""

    CONCEPT_DISCUSSION = "concept_discussion"
    CONCEPT_EXPLANATION = "concept_explanation"
    USER_REQUEST = "user_request"
    ASSESSMENT_START = "assessment_start"


class InteractionSource(str, Enum):
    """Who produced the fact — the audit trail for every row."""

    USER_EXPLICIT = "user_explicit"
    SYSTEM_GENERATED = "system_generated"


def _enum_value(enum_cls: type[Enum], value, field: str) -> str:
    if isinstance(value, enum_cls):
        return value.value
    text = str(value or "").strip()
    for member in enum_cls:
        if text == member.value or text.upper() == member.name:
            return member.value
    raise ValueError(f"invalid {field}: {value!r}")


@dataclass(frozen=True, slots=True)
class LearningInteractionEvent:
    """One recorded learning interaction (immutable)."""

    id: str
    course_id: str
    concept_id: str
    event_type: str
    source: str
    created_at: str

    def __post_init__(self) -> None:
        for field in ("id", "course_id", "concept_id", "created_at"):
            value = str(getattr(self, field) or "").strip()
            if not value:
                raise ValueError(f"LearningInteractionEvent.{field} is required")
            object.__setattr__(self, field, value)
        object.__setattr__(
            self, "event_type", _enum_value(InteractionEventType, self.event_type, "event_type")
        )
        object.__setattr__(
            self, "source", _enum_value(InteractionSource, self.source, "source")
        )

    @property
    def type(self) -> InteractionEventType:
        return InteractionEventType(self.event_type)

    @property
    def is_user_explicit(self) -> bool:
        return self.source == InteractionSource.USER_EXPLICIT.value


__all__ = [
    "InteractionEventType",
    "InteractionSource",
    "LearningInteractionEvent",
]
