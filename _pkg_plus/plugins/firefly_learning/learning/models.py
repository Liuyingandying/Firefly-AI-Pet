"""Learning Store data models (Phase 1A).

Frozen semantics (Phase 0.5 Learning Loop Design):
- Mastery 0-5 is *proven capability*; the store only validates the range.
  Deciding how to change mastery belongs to the future Rule Engine.
- Mastery and Retention are separated: mastery never decays with time;
  retention (high/medium/low) + review due dates carry recency.
- CANDIDATE concepts are never persisted; only user-confirmed concepts
  enter the store (DISCOVERED or later states).
- Concept identity is course-scoped: uniqueness key (course_id,
  normalized_name). Normalization is deterministic, never LLM-based.
- AI-generated content is marked ``ai_generated=True`` and kept apart from
  source evidence (SourceRef quotes).

Time format: UTC ISO 8601 (``datetime.now(timezone.utc).isoformat()``) for
everything this module persists.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 (this store's single time format)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Enums (frozen in Phase 0.5)
# ---------------------------------------------------------------------------


class CourseStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ConceptState(str, Enum):
    """Persisted concept states. CHECKING is transient and never stored;
    CANDIDATE is never persisted at all."""

    DISCOVERED = "discovered"
    LEARNING = "learning"
    ASSESSED = "assessed"
    MASTERED = "mastered"
    NEEDS_REVIEW = "needs_review"


class Retention(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceType(str, Enum):
    PDF = "pdf"
    PAGELENS = "pagelens"
    VIDEO = "video"
    MANUAL = "manual"
    QUIZ = "quiz"
    WEB = "web"


class AssessmentSource(str, Enum):
    QUIZ = "quiz"
    REVIEW = "review"
    SELF_ASSESSMENT = "self_assessment"
    APPLIED = "applied"
    USER_OVERRIDE = "user_override"


class Difficulty(str, Enum):
    RECALL = "recall"
    BASIC = "basic"
    VARIANT = "variant"
    APPLIED = "applied"


# ---------------------------------------------------------------------------
# Concept identity: deterministic normalization (course-scoped)
# ---------------------------------------------------------------------------

_NON_WORD_RE = re.compile(r"[^A-Za-z0-9\u4e00-\u9fff]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Deterministic canonical form for course-scoped concept identity.

    Rules (no LLM, no translation):
    - Unicode NFKC (full-width → half-width, compatibility chars)
    - lowercase
    - every run of non-word characters (incl. ``-``/``.``/``_``) becomes a
      single space; CJK is preserved as word characters
    - whitespace collapsed and trimmed

    ``"Scaled Dot-Product Attention"`` and ``"scaled dot product attention"``
    both normalize to ``"scaled dot product attention"``.
    """
    text = unicodedata.normalize("NFKC", name or "")
    text = text.lower()
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass
class SourceRef:
    """Minimal provenance for one concept / assessment."""

    source_type: str = SourceType.MANUAL.value
    document_id: str | None = None
    page: int | None = None
    section: str | None = None
    timestamp: str | None = None
    url: str | None = None
    quote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "document_id": self.document_id,
            "page": self.page,
            "section": self.section,
            "timestamp": self.timestamp,
            "url": self.url,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SourceRef":
        data = data or {}
        return cls(
            source_type=str(data.get("source_type") or SourceType.MANUAL.value),
            document_id=data.get("document_id"),
            page=data.get("page"),
            section=data.get("section"),
            timestamp=data.get("timestamp"),
            url=data.get("url"),
            quote=data.get("quote"),
        )


@dataclass
class Course:
    id: str
    name: str
    description: str = ""
    status: str = CourseStatus.ACTIVE.value
    source_title: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Concept:
    id: str
    course_id: str
    canonical_name: str
    normalized_name: str
    aliases: list[str] = field(default_factory=list)
    mastery_level: int = 0
    retention: str = Retention.LOW.value
    state: str = ConceptState.DISCOVERED.value
    source_refs: list[SourceRef] = field(default_factory=list)
    last_studied_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class StudySession:
    id: str
    course_id: str
    started_at: str = ""
    ended_at: str | None = None
    status: str = "active"
    summary: str = ""
    concept_ids: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class AssessmentRecord:
    id: str
    course_id: str
    concept_id: str
    session_id: str | None = None
    source: str = AssessmentSource.QUIZ.value
    score: float = 0.0
    confidence: float = 1.0
    difficulty: str = Difficulty.RECALL.value
    ai_generated: bool = False
    source_refs: list[SourceRef] = field(default_factory=list)
    created_at: str = ""


@dataclass
class ReviewItem:
    id: str
    concept_id: str
    due_at: str = ""
    status: str = "pending"
    interval_days: int = 1
    consecutive_success: int = 0
    retention: str = Retention.LOW.value
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CandidateConcept:
    """Ambient concept (PageLens/OCR recognition) — never persisted.

    Candidates live only in the current session context; a user action
    (study / ask / quiz / add-to-course) is required before a candidate
    becomes a stored Concept. The Learning Store deliberately has no
    ``add_candidate`` API.
    """

    canonical_name: str
    source: SourceRef | None = None
    recognized_at: str = ""


@dataclass(frozen=True)
class MasteryUpdate:
    """Result of the controlled mastery write entry.

    The store only validates legality and commits; the *decision* (new
    mastery / retention) is supplied by the caller (future Rule Engine or a
    user override through the same channel).
    """

    concept_id: str
    old_mastery: int
    new_mastery: int
    retention: str
    reason: str
    source: str
    updated_at: str
    details: str = ""


@dataclass(frozen=True)
class AssessmentEvidence:
    """The only data contract between an assessment producer (future LLM
    grading / quiz tools) and the deterministic Learning Rule Engine.

    Ordinary chat / PageLens ambient / OCR / reading / explanations are NOT
    assessment evidence — producers must never fabricate this from them.
    ``score`` is normalized 0.0-1.0; ``confidence`` may lower the weight of
    AI grading but never replaces score. ``difficulty`` must be supplied by
    the upstream explicitly (the rule engine never guesses it).
    """

    concept_id: str
    source: str = AssessmentSource.QUIZ.value   # quiz | review | applied | self_assessment | user_override
    score: float = 0.0                          # normalized 0.0-1.0
    confidence: float = 1.0                     # 0.0-1.0
    difficulty: str = Difficulty.BASIC.value    # recall | basic | variant | applied
    correct: bool | None = None                 # objective result when known
    created_at: str = ""                        # UTC ISO; empty = now
    session_id: str | None = None
    question_type: str | None = None            # for "different question types" evidence rules
    metadata: dict = field(default_factory=dict)

    def normalized(self) -> "AssessmentEvidence":
        """Deterministic normalization: clamp score/confidence to 0-1,
        map legacy difficulty/source aliases onto the frozen enums."""
        score = min(1.0, max(0.0, float(self.score)))
        confidence = min(1.0, max(0.0, float(self.confidence)))
        difficulty = _DIFFICULTY_ALIASES.get(self.difficulty, self.difficulty)
        source = _SOURCE_ALIASES.get(self.source, self.source)
        return replace(
            self, score=score, confidence=confidence,
            difficulty=difficulty, source=source,
        )


# Legacy / variant spellings -> frozen enum values (minimal compatibility).
_DIFFICULTY_ALIASES: dict[str, str] = {
    Difficulty.RECALL.value: Difficulty.RECALL.value,
    Difficulty.BASIC.value: Difficulty.BASIC.value,
    Difficulty.VARIANT.value: Difficulty.VARIANT.value,
    Difficulty.APPLIED.value: Difficulty.APPLIED.value,
    "application": Difficulty.APPLIED.value,
    "transfer": Difficulty.APPLIED.value,
}
_SOURCE_ALIASES: dict[str, str] = {
    AssessmentSource.QUIZ.value: AssessmentSource.QUIZ.value,
    AssessmentSource.REVIEW.value: AssessmentSource.REVIEW.value,
    AssessmentSource.SELF_ASSESSMENT.value: AssessmentSource.SELF_ASSESSMENT.value,
    AssessmentSource.APPLIED.value: AssessmentSource.APPLIED.value,
    AssessmentSource.USER_OVERRIDE.value: AssessmentSource.USER_OVERRIDE.value,
    "applied_problem": AssessmentSource.APPLIED.value,
    "learning_activity": AssessmentSource.QUIZ.value,  # minimal compat
}
