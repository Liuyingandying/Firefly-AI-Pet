"""TeachingPolicy (Phase 3 Teaching Strategy MVP).

Deterministic mapping from learning facts to a teaching posture:

    review_due?  -> REVIEW                     (priority over mastery)
    mastery 0    -> BEGINNER   explain + example
    mastery 1    -> INTRODUCED recall + simple question
    mastery 2/3  -> PRACTICE   exercise
    mastery >= 4 -> TRANSFER   application

Decision inputs are ONLY ``(review_due, mastery)``. There is no LLM call, no
scoring heuristic and no inference of mastery here: mastery is read from the
Rule Engine's stored value and is never written back or second-guessed. The
class deliberately does not import the rule engine / assessment service / quiz
layer — the dependency direction is learning facts -> teaching posture, never
the reverse.

Assessment history is accepted as an input (per the phase spec) and used only
for provenance (``source``): it never changes the stage or the strategy.
"""

from __future__ import annotations

from typing import Any, Iterable

from core.learning.teaching.models import (
    STAGE_BY_MASTERY,
    STRATEGY_BY_STAGE,
    LearningStage,
    TeachingContext,
    TeachingSource,
)
from core.learning.models import normalize_name

#: Strategy used when there is no concrete concept yet (safe degradation).
NO_CONCEPT_STRATEGY = "先澄清要学的概念，再解释，最后检查是否理解"


class TeachingPolicy:
    """Read-only policy: learning facts -> :class:`TeachingContext`."""

    def __init__(self, store: Any, *, now: str | None = None) -> None:
        self._store = store
        self._now = now

    # ------------------------------------------------------------------
    # the pure rule (no store, no side effects)
    # ------------------------------------------------------------------

    @staticmethod
    def decide(mastery: int | None, *, review_due: bool) -> tuple[LearningStage, str]:
        """The frozen deterministic mapping.

        ``review_due`` wins over the mastery band; an unknown mastery (None) is
        treated as 0 for the *posture* only (it never claims a mastery value).
        """
        if review_due:
            return LearningStage.REVIEW, STRATEGY_BY_STAGE[LearningStage.REVIEW]
        band = int(mastery or 0)
        if band < 0:
            band = 0
        if band > 5:
            band = 5
        stage = STAGE_BY_MASTERY[band]
        return stage, STRATEGY_BY_STAGE[stage]

    # ------------------------------------------------------------------
    # store-backed build
    # ------------------------------------------------------------------

    def build(
        self,
        learning_context: Any | None,
        *,
        resources: Any | None = None,
    ) -> TeachingContext | None:
        """Build the teaching posture for the current focus of a course.

        Returns None when there is no running course (nothing to teach).
        With a course but no resolvable concept the posture degrades safely:
        ``mastery=None`` (no mastery claim) and a clarifying strategy.

        ``resources`` (Phase 7B) is an optional read-only sequence of
        ``LearningResource`` references for the current concept — display data
        only; it never changes the stage or the strategy.
        """
        if learning_context is None:
            return None
        course_id = getattr(learning_context, "course_id", None)
        focus_name = getattr(learning_context, "current_focus", None)
        if not course_id:
            return None

        concept = self._resolve_concept(course_id, focus_name)
        if concept is None:
            return TeachingContext(
                concept_id=None,
                concept_name=None,
                mastery=None,
                learning_stage=LearningStage.BEGINNER.value,
                recommended_strategy=NO_CONCEPT_STRATEGY,
                source=TeachingSource.NO_CONCEPT.value,
                resources=tuple(resources or ()),
            )

        review_due = self._is_review_due(concept.id)
        stage, strategy = self.decide(concept.mastery_level, review_due=review_due)
        return TeachingContext(
            concept_id=concept.id,
            concept_name=concept.canonical_name,
            mastery=concept.mastery_level,
            learning_stage=stage.value,
            recommended_strategy=strategy,
            source=self._source(review_due, concept.id),
            resources=tuple(resources or ()),
        )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def _resolve_concept(self, course_id: str, focus_name: str | None):
        """Focus name -> Concept (deterministic, course-scoped).

        No guessing: an unknown or missing focus resolves to None.
        """
        if not focus_name:
            return None
        try:
            concept = self._store.find_concept(course_id, focus_name)
        except Exception:  # noqa: BLE001 - teaching posture is optional
            return None
        if concept is not None:
            return concept
        # Fall back to the most recently studied concept of the course.
        try:
            studied = [
                c for c in self._store.list_concepts(course_id) if c.last_studied_at
            ]
        except Exception:  # noqa: BLE001
            return None
        if not studied:
            return None
        return max(studied, key=lambda c: c.last_studied_at or "")

    def _is_review_due(self, concept_id: str) -> bool:
        try:
            due = self._store.get_due_reviews(self._now) if self._now else self._store.get_due_reviews()
        except Exception:  # noqa: BLE001 - no review info means "not due"
            return False
        return any(item.concept_id == concept_id for item in due)

    def _source(self, review_due: bool, concept_id: str) -> str:
        """Provenance only — never part of the stage/strategy decision."""
        if review_due:
            return TeachingSource.REVIEW_DUE.value
        if self._assessment_count(concept_id) > 0:
            return TeachingSource.MASTERY_WITH_EVIDENCE.value
        return TeachingSource.MASTERY.value

    def _assessment_count(self, concept_id: str) -> int:
        try:
            return len(self._store.list_assessments(concept_id))
        except Exception:  # noqa: BLE001
            return 0


def concept_matches(concept: Any, name: str) -> bool:
    """Deterministic course-scoped name match (shared identity primitive)."""
    return getattr(concept, "normalized_name", "") == normalize_name(name or "")


__all__ = ["TeachingPolicy", "NO_CONCEPT_STRATEGY", "concept_matches"]
