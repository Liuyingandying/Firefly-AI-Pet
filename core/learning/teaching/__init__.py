"""Teaching strategy layer (Phase 3 MVP).

Deterministic teaching posture derived from learning facts:

    from core.learning.teaching import TeachingContext, TeachingPolicy

Stage comes from ``(review_due, mastery)`` only — the Rule Engine stays the
sole authority on mastery, and nothing in this package writes to the store.
"""

from .models import (
    STAGE_BY_MASTERY,
    STRATEGY_BY_STAGE,
    LearningStage,
    TeachingContext,
    TeachingSource,
)
from .policy import NO_CONCEPT_STRATEGY, TeachingPolicy

__all__ = [
    "LearningStage",
    "TeachingSource",
    "TeachingContext",
    "TeachingPolicy",
    "STAGE_BY_MASTERY",
    "STRATEGY_BY_STAGE",
    "NO_CONCEPT_STRATEGY",
]
