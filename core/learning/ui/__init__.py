"""Learning UX presentation layer (Phase 2-UX).

Widget-free view models for the learning entry experience. Rendering happens
in ``ui/v2``; nothing here imports Qt, a provider, PageLens or the quiz layer.
"""

from .entry import (
    ACTION_CHOOSE_OTHER,
    ACTION_CONTINUE,
    ACTION_CREATE,
    ACTION_ENTER,
    ACTION_IMPORT_TEXTBOOK,
    LearningEntryCard,
    LearningProjectCard,
    LearningStatusCard,
    build_entry_card,
    build_project_cards,
    build_status_card,
)

__all__ = [
    "ACTION_CHOOSE_OTHER",
    "ACTION_CONTINUE",
    "ACTION_CREATE",
    "ACTION_ENTER",
    "ACTION_IMPORT_TEXTBOOK",
    "LearningEntryCard",
    "LearningProjectCard",
    "LearningStatusCard",
    "build_entry_card",
    "build_project_cards",
    "build_status_card",
]
