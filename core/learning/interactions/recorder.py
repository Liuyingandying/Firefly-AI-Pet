"""InteractionRecorder (Phase 3.5) — the ONLY writer of learning interactions.

The gate is intentionally narrow. An event is recorded only when ALL hold:

1. learning mode is ON with an active course (the controller guarantees this by
   only calling the recorder from that state);
2. the message deterministically matches a concept that ALREADY EXISTS in that
   course (looked up by the stored course-scoped name; nothing is invented and
   no new concept is created);
3. the message carries an explicit learning intent:
   - explanation/request markers ("解释一下X", "什么是X", "我想学X") -> user_request
     or concept_explanation;
   - discussion markers, or an explicit mention of the current focus concept
     while in learning mode -> concept_discussion;
   - the check-understanding flow starting -> assessment_start (system_generated).

Everything else is a no-op: ordinary chat, ambient PageLens highlights, OCR and
auto-extraction have no path into this class (they never call it, and the
message-matching never extracts a concept it cannot resolve).

The recorder never writes mastery, never grades and never calls a provider. A
failing store degrades to None so a chat turn can never break on tracking.
"""

from __future__ import annotations

from typing import Any, Iterable

from core.learning.interactions.models import (
    InteractionEventType,
    InteractionSource,
    LearningInteractionEvent,
)
from core.learning.interactions.store import (
    InteractionStore,
    InteractionStoreError,
)
from core.learning.models import normalize_name

#: "Explain this concept to me" style markers -> concept_explanation.
EXPLANATION_MARKERS: tuple[str, ...] = (
    "解释", "什么是", "是什么", "讲讲", "说一下", "介绍一下", "什么意思",
)

#: "I want to learn this" style markers -> user_request.
REQUEST_MARKERS: tuple[str, ...] = (
    "我想学", "想学", "学一下", "开始学", "学习一下", "教我",
)

#: "I'm struggling with this" style markers -> concept_discussion.
DISCUSSION_MARKERS: tuple[str, ...] = (
    "没懂", "不懂", "不明白", "不太懂", "搞不懂", "这里", "这一段", "有点难", "学不会",
)


def match_course_concept(
    text: str, concepts: Iterable[Any]
) -> Any | None:
    """Deterministic course-scoped concept mention lookup.

    Returns the course Concept whose canonical name (or an alias) appears in the
    text — LONGEST name first, ties broken by concept id, so the result never
    depends on iteration order. Returns None when nothing matches: an unknown
    term is never extracted into a concept.
    """
    compact = normalize_name(text or "")
    if not compact:
        return None
    best: Any | None = None
    best_length = 0
    for concept in concepts:
        names = [getattr(concept, "canonical_name", "") or ""]
        names.extend(getattr(concept, "aliases", ()) or ())
        for name in names:
            normalized = normalize_name(name)
            if not normalized or normalized not in compact:
                continue
            if len(normalized) > best_length or (
                len(normalized) == best_length
                and best is not None
                and str(getattr(concept, "id", "")) < str(getattr(best, "id", ""))
            ):
                best = concept
                best_length = len(normalized)
    return best


class InteractionRecorder:
    """Deterministic, conservative writer for learning interaction facts."""

    def __init__(
        self,
        store: Any,
        *,
        interaction_store: InteractionStore | None = None,
    ) -> None:
        self._store = store
        self._interactions = (
            interaction_store if interaction_store is not None else InteractionStore(store)
        )

    # ------------------------------------------------------------------
    # public entry points (one per allowed case)
    # ------------------------------------------------------------------

    def record_from_message(
        self,
        course_id: str,
        text: str,
        *,
        current_focus: str | None = None,
    ) -> LearningInteractionEvent | None:
        """Record an explicit user concept action found in ``text``.

        Returns None (no write) when the text carries no explicit learning
        intent or mentions no concept of the course.
        """
        return self._safe(
            self._record_from_message, course_id, text, current_focus
        )

    def record_assessment_start(
        self, course_id: str, concept_id: str
    ) -> LearningInteractionEvent | None:
        """Record the check-understanding flow starting (system-generated)."""
        return self._safe(
            self._record_assessment_start, course_id, concept_id
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _record_from_message(
        self, course_id: str, text: str, current_focus: str | None
    ) -> LearningInteractionEvent | None:
        course_id = str(course_id or "").strip()
        text = (text or "").strip()
        if not course_id or not text:
            return None
        concepts = self._list_concepts(course_id)
        if not concepts:
            return None
        concept = match_course_concept(text, concepts)
        if concept is None:
            return None  # no resolved course concept -> never write

        event_type = self._classify(text, concept, current_focus)
        if event_type is None:
            return None  # a passing mention is not a learning interaction
        return self._interactions.record(
            course_id,
            concept.id,
            event_type,
            InteractionSource.USER_EXPLICIT,
        )

    @staticmethod
    def _classify(text: str, concept: Any, current_focus: str | None):
        """Deterministic marker classification (no LLM, no scoring)."""
        compact = "".join((text or "").split())
        if any(marker in compact for marker in EXPLANATION_MARKERS):
            return InteractionEventType.CONCEPT_EXPLANATION
        if any(marker in compact for marker in REQUEST_MARKERS):
            return InteractionEventType.USER_REQUEST
        if any(marker in compact for marker in DISCUSSION_MARKERS):
            return InteractionEventType.CONCEPT_DISCUSSION
        # An explicit mention of the CURRENT focus while in learning mode is a
        # discussion of that concept (case 2 of the write rules).
        if current_focus and normalize_name(current_focus) == normalize_name(
            getattr(concept, "canonical_name", "")
        ):
            return InteractionEventType.CONCEPT_DISCUSSION
        return None

    def _record_assessment_start(self, course_id: str, concept_id: str):
        course_id = str(course_id or "").strip()
        concept_id = str(concept_id or "").strip()
        if not course_id or not concept_id:
            return None
        concept = self._get_concept(concept_id)
        if concept is None or str(getattr(concept, "course_id", "")) != course_id:
            return None  # never record across courses
        return self._interactions.record(
            course_id,
            concept_id,
            InteractionEventType.ASSESSMENT_START,
            InteractionSource.SYSTEM_GENERATED,
        )

    # -- store reads (degrade to "nothing") --------------------------------

    def _list_concepts(self, course_id: str) -> list[Any]:
        try:
            return list(self._store.list_concepts(course_id))
        except Exception:  # noqa: BLE001 - tracking is optional
            return []

    def _get_concept(self, concept_id: str) -> Any | None:
        try:
            return self._store.get_concept(concept_id)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe(fn, *args) -> LearningInteractionEvent | None:
        """Tracking must never break the chat turn."""
        try:
            return fn(*args)
        except (InteractionStoreError, ValueError, TypeError, KeyError):
            return None
        except Exception:  # noqa: BLE001 - belt and braces
            return None


__all__ = [
    "InteractionRecorder",
    "match_course_concept",
    "EXPLANATION_MARKERS",
    "REQUEST_MARKERS",
    "DISCUSSION_MARKERS",
]
