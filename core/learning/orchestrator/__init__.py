"""Learning loop orchestration (Phase 6 MVP).

Composes the existing learning layers into one deterministic turn:

    from core.learning.orchestrator import (
        LearningLoopOrchestrator, LearningLoopResult,
    )

Pure flow composition: no decisions of its own, no LLM, no mastery writes, no
rule-engine or curriculum-writer imports. The controller is injected and used
through its existing read-only interface.
"""

from .contract import (
    LearningResponseContract,
    REQUIRED_ELEMENTS_BY_ACTION,
    build_response_contract,
)
from .models import LearningLoopResult, LoopSource, LoopStatus
from .orchestrator import LearningLoopOrchestrator

__all__ = [
    "LearningLoopOrchestrator",
    "LearningLoopResult",
    "LearningResponseContract",
    "REQUIRED_ELEMENTS_BY_ACTION",
    "LoopSource",
    "LoopStatus",
    "build_response_contract",
]
