"""Learning interaction tracking (Phase 3.5).

Facts about what the learner actually discussed, used to keep
``LearningContext.current_focus`` current. Facts only — never mastery, never a
score, and never a record produced by ordinary chat or an ambient source.

    from core.learning.interactions import InteractionRecorder, InteractionStore

`current_focus` priority becomes: most recent interaction -> study session ->
None (see :class:`core.learning.context.LearningContextBuilder`).
"""

from .models import (
    InteractionEventType,
    InteractionSource,
    LearningInteractionEvent,
)
from .recorder import InteractionRecorder, match_course_concept
from .store import InteractionStore, InteractionStoreError

__all__ = [
    "InteractionEventType",
    "InteractionSource",
    "LearningInteractionEvent",
    "InteractionStore",
    "InteractionStoreError",
    "InteractionRecorder",
    "match_course_concept",
]
