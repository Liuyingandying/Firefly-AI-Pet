"""Learning action layer (Phase 5 MVP).

Executes the action Phase 4 already decided — it never picks the action itself:

    from core.learning.action import LearningActionExecutor, LearningActionResult

Read-only by default (``run_assessment=False``); only an explicit invocation
starts the existing assessment flow. No mastery writes, no rule-engine or
curriculum changes, no new provider.
"""

from .executor import CONSTRAINT_BY_ACTION, LearningActionExecutor
from .models import ActionStatus, LearningActionResult

__all__ = [
    "ActionStatus",
    "LearningActionResult",
    "LearningActionExecutor",
    "CONSTRAINT_BY_ACTION",
]
