"""LearningContext layer (Phase 2-LC).

Read-only consumption of the ACTIVE curriculum: where the learner is, what
they last worked on, and the deterministic next structural node.

    from core.learning.context import LearningContext, LearningContextBuilder

`next_in_order` is curriculum order, never a personalised recommendation, and
nothing in this package writes to the learning store.
"""

from .builder import LearningContextBuilder
from .models import ContextSource, LearningContext

__all__ = ["ContextSource", "LearningContext", "LearningContextBuilder"]
