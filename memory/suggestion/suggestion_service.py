"""Pending-suggestion service with user-confirmed accept/reject.

Holds candidate memories until the user confirms or discards them. Accepting
writes through the existing ``MemoryService.remember()`` interface (never a
direct repository write); rejecting simply drops the candidate.
"""

from __future__ import annotations

from typing import Any

from .memory_candidate_detector import MemoryCandidateDetector, MemorySuggestion


class SuggestionService:
    """Accumulate and resolve candidate memories for one conversation."""

    def __init__(
        self,
        memory_service: Any,
        detector: MemoryCandidateDetector | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.memory_service = memory_service
        self.detector = detector or MemoryCandidateDetector()
        self.enabled = enabled
        self._pending: list[MemorySuggestion] = []

    def detect(self, user_message: str) -> list[MemorySuggestion]:
        """Detect suggestions from a message and queue them as pending."""
        if not self.enabled:
            return []
        suggestions = self.detector.detect(user_message)
        self._pending.extend(suggestions)
        return suggestions

    def list_pending(self) -> list[MemorySuggestion]:
        return list(self._pending)

    def accept(self, suggestion: MemorySuggestion) -> Any:
        """Write one suggestion via ``MemoryService.remember`` and drop it."""
        try:
            record = self.memory_service.remember(
                suggestion.content,
                category=suggestion.category.value,
                trigger=f"suggestion:{suggestion.reason}",
                asserted_explicit=True,
            )
        except Exception:
            return None
        if record is not None and suggestion in self._pending:
            self._pending.remove(suggestion)
        return record

    def reject(self, suggestion: MemorySuggestion) -> None:
        if suggestion in self._pending:
            self._pending.remove(suggestion)


__all__ = ["SuggestionService"]
