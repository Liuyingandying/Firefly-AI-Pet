"""ResourceStore (Phase 7B) — persistence for learning resources.

Append/update-light CRUD over the ``learning_resources`` table (schema v4).
Deleting a resource removes ONLY the reference row: Concept / mastery /
Assessment / Review rows live on other tables and are untouched by design
(foreign keys cascade from concept/curriculum deletions, never from a resource
deletion). No provider, no LLM, no downloads — sources in, sources out.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.learning.models import utc_now_iso
from core.learning.resources.models import (
    LearningResource,
    ResourceOrigin,
    ResourceType,
)
from core.learning.store import LearningStore


class ResourceStoreError(RuntimeError):
    """Resource persistence failed (the caller degrades, never crashes)."""


def _new_id() -> str:
    return uuid.uuid4().hex


class ResourceStore:
    """CRUD facade for :class:`LearningResource` rows."""

    def __init__(self, store: LearningStore | None = None) -> None:
        self._store = store if store is not None else LearningStore()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def add_resource(
        self,
        course_id: str,
        resource_type: str | ResourceType,
        title: str,
        *,
        source: str = "",
        locator: str = "",
        concept_id: str | None = None,
        chapter_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        origin: str | ResourceOrigin = ResourceOrigin.USER_EXPLICIT,
        created_at: str | None = None,
        resource_id: str | None = None,
    ) -> LearningResource:
        """Insert one resource reference. Raises on invalid input."""
        payload = dict(metadata or {})
        payload.setdefault("origin", ResourceOrigin(origin).value)
        resource = LearningResource(
            id=resource_id or _new_id(),
            course_id=course_id,
            resource_type=(
                resource_type.value
                if isinstance(resource_type, ResourceType) else resource_type
            ),
            title=title,
            source=source,
            locator=locator,
            concept_id=concept_id,
            chapter_id=chapter_id,
            metadata=payload,
            created_at=created_at or utc_now_iso(),
        )
        try:
            with self._store.connect() as connection:
                connection.execute(
                    "INSERT INTO learning_resources (id, course_id, concept_id,"
                    " chapter_id, resource_type, title, source, locator, metadata,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource.id,
                        resource.course_id,
                        resource.concept_id,
                        resource.chapter_id,
                        resource.resource_type,
                        resource.title,
                        resource.source,
                        resource.locator,
                        json.dumps(payload, ensure_ascii=False),
                        resource.created_at,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise ResourceStoreError(
                f"could not add resource {resource.title!r}: {exc}"
            ) from exc
        return resource

    def delete_resource(self, resource_id: str) -> bool:
        """Delete ONE reference row. Returns True when a row was removed.

        Only the resource row is deleted — Concept / mastery / Assessment /
        Review rows are structurally untouched (no cascade path from here).
        """
        try:
            with self._store.connect() as connection:
                deleted = connection.execute(
                    "DELETE FROM learning_resources WHERE id=?", (resource_id,)
                )
                return bool(deleted.rowcount)
        except Exception as exc:  # noqa: BLE001
            raise ResourceStoreError(
                f"could not delete resource {resource_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_resources_for_concept(
        self, concept_id: str | None
    ) -> list[LearningResource]:
        """All resources bound to a concept, oldest first; [] when none."""
        if not concept_id:
            return []
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_resources WHERE concept_id=?"
                " ORDER BY created_at, id",
                (concept_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_resources_for_chapter(
        self, chapter_id: str | None
    ) -> list[LearningResource]:
        """All resources bound to a chapter, oldest first; [] when none."""
        if not chapter_id:
            return []
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_resources WHERE chapter_id=?"
                " ORDER BY created_at, id",
                (chapter_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_for_course(self, course_id: str | None) -> list[LearningResource]:
        if not course_id:
            return []
        with self._store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_resources WHERE course_id=?"
                " ORDER BY created_at, id",
                (course_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_resource(self, resource_id: str) -> LearningResource | None:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_resources WHERE id=?", (resource_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def count(self, course_id: str | None = None) -> int:
        with self._store.connect() as connection:
            if course_id is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM learning_resources"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM learning_resources WHERE course_id=?",
                    (course_id,),
                ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _from_row(row: Any) -> LearningResource:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return LearningResource(
            id=row["id"],
            course_id=row["course_id"],
            resource_type=row["resource_type"],
            title=row["title"],
            source=row["source"],
            locator=row["locator"],
            concept_id=row["concept_id"],
            chapter_id=row["chapter_id"],
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=row["created_at"],
        )


__all__ = ["ResourceStore", "ResourceStoreError"]
