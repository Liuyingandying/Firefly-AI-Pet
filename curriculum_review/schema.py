"""Validated data contract for a human-gated curriculum review."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


DECISIONS = frozenset({"KEEP_ORIGINAL", "MERGE", "MOVE"})


@dataclass(frozen=True)
class ReviewCase:
    course_id: str
    chapter_id: str  # draft unit ID, e.g. ch05; not a book_chXX assignment
    original_title: str
    bookmark_context: dict[str, Any]
    current_assignment: dict[str, Any]
    conflict_type: str
    concept_summary: list[dict[str, Any]]
    question_summary: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewRecommendation:
    decision: str
    confidence: float
    reason: str
    impact: str
    recommended_assignment: str = ""
    human_confirmation_points: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: object) -> "ReviewRecommendation":
        if not isinstance(data, dict):
            raise ValueError("recommendation must be a JSON object")
        decision = data.get("decision")
        confidence = data.get("confidence")
        reason = data.get("reason")
        impact = data.get("impact")
        assignment = data.get("recommended_assignment", "")
        points = data.get("human_confirmation_points", [])
        if decision not in DECISIONS:
            raise ValueError("decision must be KEEP_ORIGINAL, MERGE, or MOVE")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1):
            raise ValueError("confidence must be a number from 0 to 1")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty")
        if not isinstance(impact, str) or not impact.strip():
            raise ValueError("impact must be non-empty")
        if not isinstance(assignment, str):
            raise ValueError("recommended_assignment must be text")
        if not isinstance(points, list) or any(not isinstance(p, str) or not p.strip() for p in points):
            raise ValueError("human_confirmation_points must be a list of text")
        return cls(decision, float(confidence), reason.strip(), impact.strip(),
                   assignment.strip(), [p.strip() for p in points])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
