"""LearningContextBuilder (Phase 2-LC).

Reads the LearningStore (learning facts) and the CurriculumStore (ACTIVE
structure) and produces a :class:`LearningContext`. READ-ONLY by construction:

- it calls only read APIs (``get_course``, ``list_sessions``, ``list_concepts``,
  ``get_active_curriculum``);
- it never activates a curriculum, never writes mastery/concepts and never
  opens a session;
- it never guesses: an undeterminable chapter is ``None``.

No provider / LLM / UI / PageLens / Quiz imports (source-checked by tests).
"""

from __future__ import annotations

from typing import Any

from core.learning.context.models import ContextSource, LearningContext
from core.learning.curriculum.models import ChapterConceptRole, PathTargetType
from core.learning.store import LearningStore, LearningStoreError

# Only an ACTIVE curriculum is consumed; a Draft can never reach this layer.
_ACTIVE_ONLY = True


class LearningContextBuilder:
    """Builds a read-only :class:`LearningContext` for one course."""

    def __init__(
        self,
        store: LearningStore,
        curriculum_store: Any | None = None,
        interaction_store: Any | None = None,
    ) -> None:
        self._store = store
        self._curriculum_store = curriculum_store
        self._interaction_store = interaction_store

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def build(self, course_id: str | None) -> LearningContext | None:
        """The context for ``course_id``; None only when the course is unknown.

        A course WITHOUT an active curriculum is a legal state: the context is
        returned with ``curriculum_id=None`` and ``source="no_curriculum"`` so
        every consumer degrades safely instead of guessing structure.
        """
        course_id = (course_id or "").strip()
        if not course_id:
            return None
        course = self._course(course_id)
        if course is None:
            return None

        view = self._active_view(course_id)
        if view is None:
            return LearningContext(
                course_id=course_id,
                course_name=course.name,
                source=ContextSource.NO_CURRICULUM.value,
            )

        concept_names = self._concept_names(course_id)
        current_focus, focus_concept_id = self._recent_focus(course_id)
        current_chapter = self._chapter_for_concept(view, focus_concept_id)
        nodes = self._structure_nodes(view, concept_names)
        next_in_order = self._next_in_order(nodes, focus_concept_id)

        return LearningContext(
            course_id=course_id,
            course_name=course.name,
            curriculum_id=view.curriculum.id,
            curriculum_title=view.curriculum.title,
            curriculum_version=view.curriculum.version,
            current_chapter=current_chapter,
            current_focus=current_focus,
            next_in_order=next_in_order,
            source=ContextSource.ACTIVE_CURRICULUM.value,
        )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def _course(self, course_id: str):
        try:
            return self._store.get_course(course_id)
        except LearningStoreError:
            return None

    def _active_view(self, course_id: str):
        if self._curriculum_store is None:
            return None
        try:
            return self._curriculum_store.get_active_curriculum(course_id)
        except Exception:  # noqa: BLE001 - structure is optional, never fatal
            return None

    def _recent_focus(self, course_id: str) -> tuple[str | None, str | None]:
        """(focus name, concept id) from real learning facts.

        Phase 3.5 priority:

            1. the most recent LearningInteractionEvent of the course — what the
               learner actually discussed/asked about;
            2. the last concept touched by the most recent study session;
            3. the most recently studied concept on record (a study FACT — this
               is what keeps the focus alive when a new session has just been
               opened for the same project and has no concepts yet);
            4. None — never guessed.
        """
        concept_id = self._latest_interaction_concept(course_id)
        if concept_id is None:
            concept_id = self._last_session_concept(course_id)
        if concept_id is None:
            concept_id = self._most_recently_studied_concept_id(course_id)
        if concept_id is None:
            return None, None
        concept = self._concept(concept_id)
        if concept is None:
            return None, None
        return concept.canonical_name, concept.id

    def _interactions(self):
        """Lazily build the interaction store (read-only use)."""
        if self._interaction_store is None:
            try:
                from core.learning.interactions import InteractionStore

                self._interaction_store = InteractionStore(self._store)
            except Exception:  # noqa: BLE001 - focus falls back to the session
                return None
        return self._interaction_store

    def _latest_interaction_concept(self, course_id: str) -> str | None:
        interactions = self._interactions()
        if interactions is None:
            return None
        try:
            event = interactions.latest_for_course(course_id)
        except Exception:  # noqa: BLE001 - tracking is optional
            return None
        return event.concept_id if event is not None else None

    def _last_session_concept(self, course_id: str) -> str | None:
        try:
            sessions = self._store.list_sessions(course_id)
        except LearningStoreError:
            return None
        if not sessions:
            return None
        latest = max(sessions, key=lambda s: (s.started_at or "", s.id))
        concept_ids = list(getattr(latest, "concept_ids", ()) or ())
        # touch_concept appends, so the last entry is the most recent.
        return concept_ids[-1] if concept_ids else None

    def _most_recently_studied_concept_id(self, course_id: str) -> str | None:
        """Last recorded study fact of the course (session-independent).

        Keeps the focus stable when a fresh session has just been opened for a
        project that was studied before — never a guess, always a stored fact.
        """
        try:
            concepts = [
                c for c in self._store.list_concepts(course_id) if c.last_studied_at
            ]
        except LearningStoreError:
            return None
        if not concepts:
            return None
        return max(concepts, key=lambda c: c.last_studied_at or "").id

    def _concept(self, concept_id: str):
        try:
            return self._store.get_concept(concept_id)
        except LearningStoreError:
            return None

    @staticmethod
    def _chapter_for_concept(view, concept_id: str | None) -> str | None:
        """Map a concept to its chapter via ChapterConcept links.

        A concept taught in several chapters prefers its PRIMARY placement; a
        concept with no link (or no concept at all) yields None — the builder
        never falls back to "the first chapter".
        """
        if not concept_id:
            return None
        links = [link for link in view.chapter_concepts if link.concept_id == concept_id]
        if not links:
            return None
        primary = [
            link for link in links
            if link.role == ChapterConceptRole.PRIMARY.value
        ]
        chosen = (primary or links)[0]
        for chapter in view.chapters:
            if chapter.id == chosen.chapter_id:
                return chapter.title
        return None

    def _concept_names(self, course_id: str) -> dict[str, str]:
        try:
            return {
                concept.id: concept.canonical_name
                for concept in self._store.list_concepts(course_id)
            }
        except LearningStoreError:
            return {}

    @staticmethod
    def _structure_nodes(view, concept_names: dict[str, str]) -> tuple[tuple[str, str | None], ...]:
        """Flatten the default path into ordered ``(label, concept_id)`` nodes.

        A chapter step expands to its concept placements in ``position`` order
        (falling back to the chapter title when it has none); a concept step is
        itself. This is pure curriculum order — no mastery, no recommendation.
        """
        chapters = {chapter.id: chapter for chapter in view.chapters}
        nodes: list[tuple[str, str | None]] = []
        for item in tuple(getattr(view, "learning_order", ()) or ()):
            if item.target_type == PathTargetType.CHAPTER.value:
                chapter = chapters.get(item.target_id)
                if chapter is None:
                    continue
                links = view.concepts_of_chapter(chapter.id)
                if links:
                    for link in links:
                        nodes.append(
                            (concept_names.get(link.concept_id) or link.concept_id,
                             link.concept_id)
                        )
                else:
                    nodes.append((chapter.title, None))
            else:
                nodes.append(
                    (concept_names.get(item.target_id) or item.target_id, item.target_id)
                )
        return tuple(nodes)

    @staticmethod
    def _next_in_order(
        nodes: tuple[tuple[str, str | None], ...], current_concept_id: str | None
    ) -> str | None:
        """The deterministic successor of the current concept.

        With a known current concept: the next node, or None at the end of the
        route. Without one: the route's first node (where the curriculum
        starts) — structural order, never a recommendation.
        """
        if not nodes:
            return None
        if not current_concept_id:
            return nodes[0][0]
        for index, (_label, concept_id) in enumerate(nodes):
            if concept_id == current_concept_id:
                if index + 1 >= len(nodes):
                    return None
                return nodes[index + 1][0]
        return nodes[0][0]


__all__ = ["LearningContextBuilder"]
