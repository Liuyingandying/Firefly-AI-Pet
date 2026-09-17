"""Interaction persistence (Phase 3.5).

Append-only access to the ``learning_interactions`` table (schema v3). There is
deliberately NO update/delete API — the table is the audit trail. Writes run in
one ``with connection:`` transaction, so a failed record leaves nothing behind.

Reads are the only thing the context layer consumes.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.learning.interactions.models import (
    InteractionEventType,
    InteractionSource,
    LearningInteractionEvent,
)
from core.learning.models import utc_now_iso
from core.learning.store import LearningStore


class InteractionStoreError(RuntimeError):
    """Interaction persistence failed (the caller degrades, never crashes)."""


def _new_id() -> str:
    return uuid.uuid4().hex


class InteractionStore:
    """Append-only store for :class:`LearningInteractionEvent` rows."""

    def __init__(self, store: LearningStore | None = None) -> None:
        self._store = store if store is not None else LearningStore()

    # ------------------------------------------------------------------
    # writes (append only)
    # ------------------------------------------------------------------

    def record(
        self,
        course_id: str,
        concept_id: str,
        event_type: str | InteractionEventType,
        source: str | InteractionSource,
        *,
        created_at: str | None = None,
        event_id: str | None = None,
    ) -> LearningInteractionEvent:
        """Append one event. Raises on invalid input (the recorder catches)."""
        event = LearningInteractionEvent(
            id=event_id or _new_id(),
            course_id=course_id,
            concept_id=concept_id,
            event_type=event_type.value if isinstance(event_type, InteractionEventType) else event_type,
            source=source.value if isinstance(source, InteractionSource) else source,
            created_at=created_at or utc_now_iso(),
        )
        try:
            with self._store.connect() as connection:
                connection.execute(
                    "INSERT INTO learning_interactions (id, course_id, concept_id,"
                    " event_type, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.course_id,
                        event.concept_id,
                        event.event_type,
                        event.source,
                        event.created_at,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - surface as a typed store error
            raise InteractionStoreError(
                f"could not record interaction for concept {concept_id}: {exc}"
            ) from exc
        return event

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def list_interactions(
        self, course_id: str | None = None, *, limit: int | None = None
    ) -> list[LearningInteractionEvent]:
        """Events, oldest first (course-scoped when ``course_id`` is given).

        ``limit`` keeps the N most RECENT events (returned oldest-first).
        """
        if limit is not None:
            sql = "SELECT * FROM learning_interactions"
            params: tuple = ()
            if course_id is not None:
                sql += " WHERE course_id=?"
                params = (course_id,)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            with self._store.connect() as connection:
                rows = connection.execute(sql, params + (int(limit),)).fetchall()
            return [self._from_row(row) for row in reversed(rows)]

        sql = "SELECT * FROM learning_interactions"
        params = ()
        if course_id is not None:
            sql += " WHERE course_id=?"
            params = (course_id,)
        sql += " ORDER BY created_at, id"
        with self._store.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def latest_for_course(self, course_id: str) -> LearningInteractionEvent | None:
        """Most recent event of a course (read-only; None when there is none)."""
        if not course_id:
            return None
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_interactions WHERE course_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (course_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def count(self, course_id: str | None = None) -> int:
        with self._store.connect() as connection:
            if course_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM learning_interactions"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM learning_interactions WHERE course_id=?",
                    (course_id,),
                ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _from_row(row: Any) -> LearningInteractionEvent:
        return LearningInteractionEvent(
            id=row["id"],
            course_id=row["course_id"],
            concept_id=row["concept_id"],
            event_type=row["event_type"],
            source=row["source"],
            created_at=row["created_at"],
        )


__all__ = ["InteractionStore", "InteractionStoreError"]
