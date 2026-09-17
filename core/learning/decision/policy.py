"""LearningDecisionPolicy (Phase 4).

A PURE function: the same inputs always produce the same recommendation. It
imports no store, no provider, no LLM, no quiz layer and no rule engine, and it
writes nothing — so it can be unit-tested with plain values and it keeps
working when every model/provider is unavailable.

The caller injects the facts it already has:

- ``focus``           : the current concept (id, name) or None, from LearningContext
- ``mastery``         : the concept's stored mastery (Rule-Engine-owned) or None
- ``review_due``      : the review-due flag (computed once by TeachingPolicy)
- ``chapter_complete``: every concept of the current chapter is at TRANSFER
                        level or above (injected by the caller, read-only)
- ``next_label``      : the next structural node, for the MOVE_NEXT reason

See :mod:`core.learning.decision.models` for the frozen priority table.
"""

from __future__ import annotations

from typing import Any

from core.learning.decision.models import (
    REASON_BY_ACTION,
    DecisionSource,
    LearningAction,
    LearningRecommendation,
)

#: Mastery at which a concept counts as mastered for chapter completion.
MASTERY_MASTERED = 4

#: Band -> action for the current concept's mastery (frozen by the phase spec).
ACTION_BY_MASTERY: dict[int, LearningAction] = {
    0: LearningAction.EXPLAIN,
    1: LearningAction.EXPLAIN_RECALL,
    2: LearningAction.PRACTICE,
    3: LearningAction.PRACTICE,
    4: LearningAction.TRANSFER,
    5: LearningAction.TRANSFER,
}


class LearningDecisionPolicy:
    """Deterministic next-action policy (pure, stateless)."""

    # ------------------------------------------------------------------
    # the frozen rule
    # ------------------------------------------------------------------

    @staticmethod
    def decide(
        *,
        focus: tuple[str, str] | None = None,
        mastery: int | None = None,
        review_due: bool = False,
        chapter_complete: bool = False,
        next_label: str | None = None,
    ) -> LearningRecommendation:
        """Map the current facts to exactly one next action.

        Priority is the frozen list: review-due wins over everything; without a
        focus there is nothing to teach; otherwise the mastery band decides, and
        a fully mastered chapter moves on to the next structural node instead of
        deepening further.
        """
        concept_id, concept_name = focus if focus else (None, None)

        if review_due and focus is not None:
            return LearningRecommendation(
                action=LearningAction.REVIEW.value,
                concept_id=concept_id,
                concept_name=concept_name,
                source=DecisionSource.REVIEW_DUE.value,
            )
        if focus is None:
            return LearningRecommendation(
                action=LearningAction.FREE_CHAT.value,
                source=DecisionSource.NO_FOCUS.value,
            )

        band = _clamp_mastery(mastery)
        action = ACTION_BY_MASTERY[band]
        if (
            action is LearningAction.TRANSFER
            and chapter_complete
            and next_label
        ):
            # Rule 7 refines the high-mastery band: a finished chapter with a
            # next node available means "move on" rather than "go deeper".
            # This is the only reachable position for MOVE_NEXT, because a
            # completed chapter implies the current concept is at TRANSFER.
            return LearningRecommendation(
                action=LearningAction.MOVE_NEXT.value,
                reason=f"{REASON_BY_ACTION[LearningAction.MOVE_NEXT]}（{next_label}）",
                concept_id=concept_id,
                concept_name=concept_name,
                source=DecisionSource.CHAPTER_COMPLETE.value,
            )
        return LearningRecommendation(
            action=action.value,
            concept_id=concept_id,
            concept_name=concept_name,
            source=DecisionSource.MASTERY.value,
        )

    # ------------------------------------------------------------------
    # convenience: build from the existing contexts
    # ------------------------------------------------------------------

    @classmethod
    def from_contexts(
        cls,
        learning_context: Any | None,
        teaching_context: Any | None,
        *,
        chapter_complete: bool = False,
        next_label: str | None = None,
    ) -> LearningRecommendation:
        """Derive the facts from LearningContext + TeachingContext.

        ``review_due`` comes from TeachingContext (``is_review``) so the review
        lookup exists in exactly one place; ``mastery`` likewise. A missing
        context degrades to FREE_CHAT — never to a guess.
        """
        if learning_context is None:
            return cls.decide(focus=None)
        concept_id = getattr(teaching_context, "concept_id", None)
        concept_name = getattr(teaching_context, "concept_name", None)
        if not concept_id and not concept_name:
            return cls.decide(focus=None)
        focus = (concept_id or "", concept_name or "")
        review_due = bool(getattr(teaching_context, "is_review", False))
        mastery = getattr(teaching_context, "mastery", None)
        return cls.decide(
            focus=focus,
            mastery=mastery,
            review_due=review_due,
            chapter_complete=chapter_complete,
            next_label=next_label,
        )


def _clamp_mastery(mastery: int | None) -> int:
    """Unknown mastery is treated as 0 for the ACTION only (never claimed)."""
    if mastery is None:
        return 0
    try:
        band = int(mastery)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, band))


__all__ = [
    "LearningDecisionPolicy",
    "ACTION_BY_MASTERY",
    "MASTERY_MASTERED",
]
