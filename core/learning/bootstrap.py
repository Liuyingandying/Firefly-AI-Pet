"""Initial Learning Bootstrap (Phase 9C).

Answers ONE question for a brand-new course: 「从哪里开始学？」— derived
deterministically from the ACTIVE curriculum's default path:

    LearningBootstrapContext(
        course_id=…, course_name=…, curriculum_id=…,
        first_chapter="第一章 绪论",
        first_focus="自动控制概述",      # first concept of the first chapter
        source="active_curriculum_first_item",
    )

Generated ONLY when all hold:

    - an ACTIVE curriculum exists for the course;
    - LearningContext.current_focus is None (no interactions, no session
      touches, no studied concepts — already implied by the focus chain);
    - the course has NO learning history (no assessment records either —
      belt and braces over the focus check);
    - the first chapter actually exists in the published structure.

It is NOT a decision (FREE_CHAT stays FREE_CHAT) and NOT a recommendation —
the first chapter and the first task come from the user-confirmed curriculum
structure, verbatim. Nothing here writes: no interactions, no sessions, no
mastery, and ``current_focus`` is left exactly as it was (None).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BOOTSTRAP_SOURCE = "active_curriculum_first_item"


@dataclass(frozen=True, slots=True)
class LearningBootstrapContext:
    """Read-only starting point of a fresh course's ACTIVE curriculum."""

    course_id: str
    course_name: str
    curriculum_id: str
    first_chapter: str
    first_focus: str | None = None
    source: str = BOOTSTRAP_SOURCE

    def __post_init__(self) -> None:
        for field_name in ("course_id", "course_name", "curriculum_id",
                           "first_chapter"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"LearningBootstrapContext.{field_name} is required")
            object.__setattr__(self, field_name, value)
        focus = getattr(self, "first_focus", None)
        object.__setattr__(self, "first_focus", str(focus).strip() if focus else None)

    def lines(self) -> tuple[str, ...]:
        """课程定位 / 开始章节 / 第一学习任务 lines for prompt and UI."""
        task = self.first_focus or f"学习「{self.first_chapter}」"
        return (
            f"课程定位：{self.course_name}",
            f"开始章节：{self.first_chapter}",
            f"第一学习任务：{task}",
        )


def build_learning_bootstrap(
    store: Any,
    view: Any | None,
    *,
    course_id: str,
    course_name: str | None = None,
    learning_context: Any | None = None,
) -> LearningBootstrapContext | None:
    """Build the bootstrap for a FRESH course with an ACTIVE curriculum.

    Returns None when the course has no published structure, already has a
    focus, or carries learning history — every degradation keeps the caller in
    normal chat. Read-only: no writes of any kind.
    """
    if view is None:
        return None
    curriculum = getattr(view, "curriculum", None)
    if curriculum is None:
        return None
    chapters = tuple(getattr(view, "chapters_in_order", ()) or ())
    if not chapters:
        return None

    if learning_context is not None:
        if getattr(learning_context, "current_focus", None):
            return None  # the learner already has a focus — no bootstrap
        if getattr(learning_context, "course_id", None) not in (None, course_id):
            return None

    # 没有学习历史: no studied concepts AND no assessment records — a course
    # with quiz history is not "fresh" even if the focus is somehow lost.
    concepts = _list(store.list_concepts, course_id)
    if concepts is None:
        return None
    if any(getattr(c, "last_studied_at", None) for c in concepts):
        return None
    concept_ids = {getattr(c, "id", None) for c in concepts}
    try:
        assessments = _list(store.list_assessments, None)
    except Exception:  # noqa: BLE001 - history is optional
        assessments = None
    if assessments is None:
        return None
    if any(a.concept_id in concept_ids for a in assessments):
        return None

    first = chapters[0]
    first_focus = _first_concept(store, view, chapters)
    return LearningBootstrapContext(
        course_id=course_id,
        course_name=_clean_name(course_name) or _course_name(store, course_id) or "",
        curriculum_id=curriculum.id,
        first_chapter=first.title,
        first_focus=first_focus,
    )


def _first_concept(store: Any, view: Any, chapters: tuple) -> str | None:
    """The first concept of the curriculum, in chapter order.

    The PDF adapter places generated concepts in an auto-review chapter
    ("全书概念（自动识别）") rather than inside chapter 1. When the first
    chapter carries no concept of its own, fall back to the first concept
    anywhere in the published structure — that is still the correct
    「第一个知识点」 for a fresh course. Read-only.
    """
    for chapter in chapters:
        for link in view.concepts_of_chapter(chapter.id):
            concept = _get(store.get_concept, link.concept_id)
            if concept is not None:
                return concept.canonical_name
    return None


def _list(reader, course_id):
    try:
        return reader(course_id) if course_id is not None else reader(None)
    except Exception:  # noqa: BLE001 - history is optional
        return None


def _get(reader, concept_id):
    try:
        return reader(concept_id)
    except Exception:  # noqa: BLE001
        return None


def _course_name(store: Any, course_id: str) -> str | None:
    try:
        course = store.get_course(course_id)
    except Exception:  # noqa: BLE001
        return None
    return getattr(course, "name", None) if course is not None else None


def _clean_name(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["LearningBootstrapContext", "build_learning_bootstrap", "BOOTSTRAP_SOURCE"]
