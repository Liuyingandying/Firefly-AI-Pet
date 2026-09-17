"""Deterministic detector for candidate long-term memories.

Scans a single user message for three signals — long-term goals, stable
preferences, and important events — and emits ``MemorySuggestion`` objects.
It never writes a ``MemoryRecord``, never calls an LLM, and never touches
Character or Bond state.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..records import MemoryCategory


@dataclass(frozen=True)
class MemorySuggestion:
    """A candidate memory awaiting user confirmation.

    v1 boundary repair: ``source`` records where the candidate came from —
    ``"explicit"`` (user typed a remember-style request), ``"companion_auto"``
    (LLM extraction from ordinary chat), ``"conversation_summary"`` (session
    wrap-up).  Automatic sources are CANDIDATES ONLY: they can never write a
    MemoryRecord by themselves, and they must never carry an explicit marker.
    ``status`` tracks the user decision: pending / accepted / rejected.
    """

    content: str
    category: MemoryCategory
    reason: str
    evidence: tuple[str, ...]
    confidence: float
    id: str = ""
    source: str = "explicit"
    status: str = "pending"
    created_at: int = 0

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", uuid.uuid4().hex)
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time_ns() // 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category.value,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class _Rule:
    reason: str
    category: MemoryCategory
    pattern: re.Pattern[str]
    confidence: float


_RULES = (
    _Rule(
        "long_term_goal",
        MemoryCategory.PROJECT,
        re.compile(
            r"我(?:要|想|准备|计划|打算)"
            r"(?P<content>(?:申请|考|读|准备|成为|做|开|去)[^。！!？?，,]{1,30})"
        ),
        0.7,
    ),
    _Rule(
        "stable_preference",
        MemoryCategory.PREFERENCE,
        re.compile(r"(?:我喜欢|我偏好|以后希望)(?P<content>[^。！!？?，,]{1,30})"),
        0.6,
    ),
    _Rule(
        "important_event",
        MemoryCategory.SHARED_EXPERIENCE,
        re.compile(
            r"我(?:已经|刚刚|正式)?"
            r"(?P<content>(?:毕业|搬家|离职|入职|结婚|分手|考上|被录取|买房|生子|出国|回国|退休|创业|换工作|升职|转行)了)"
        ),
        0.8,
    ),
)


class MemoryCandidateDetector:
    """Detect candidate long-term memories from a user message."""

    def detect(self, user_message: str) -> list[MemorySuggestion]:
        if not isinstance(user_message, str) or not user_message.strip():
            return []
        text = user_message.strip()

        suggestions: list[MemorySuggestion] = []
        for rule in _RULES:
            match = rule.pattern.search(text)
            if match is None:
                continue
            content = match.group("content").strip()
            if not content:
                continue
            suggestions.append(
                MemorySuggestion(
                    content=content,
                    category=rule.category,
                    reason=rule.reason,
                    evidence=(text,),
                    confidence=rule.confidence,
                )
            )
        return suggestions


__all__ = ["MemoryCandidateDetector", "MemorySuggestion"]
