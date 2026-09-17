"""Textbook resource recording (Phase 7B Step 4).

After a textbook draft is CONFIRMED, the published chapters carry PDF
provenance (``Chapter.source_refs``: document_id + page). This module turns
those refs into course resources — references only, never the document text:

    Resource(type=PDF, title=教材文件名, locator="page 35-48",
             chapter_id=<published chapter>)

IMPORTANT: bind to the PUBLISHED chapters (e.g. from
``curricula.get_active_curriculum(course_id)``), not the draft's chapters —
the published ids are the ones that exist in ``curriculum_chapters``.

Deterministic and side-effect-limited: one resource per published chapter that
has a source ref; a chapter without refs gets none (nothing invented). Nothing
here touches mastery / assessments / reviews, and nothing activates anything.
"""

from __future__ import annotations

from typing import Any, Iterable

from core.learning.resources.models import ResourceOrigin, ResourceType
from core.learning.resources.store import ResourceStore


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _locator_from_refs(refs: Iterable[Any]) -> str:
    """Compact locator from a chapter's source refs: "page 35-48" / "page 12"."""
    pages = sorted(
        int(ref.page) for ref in refs
        if getattr(ref, "page", None) is not None
    )
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    return f"page {pages[0]}-{pages[-1]}"


def record_textbook_resources(
    store: ResourceStore | Any,
    course_id: str,
    view: Any,
    *,
    document_title: str | None = None,
    document_path: str | None = None,
) -> list[Any]:
    """Register one PDF resource per published chapter that has provenance.

    ``view`` is the ACTIVE curriculum view (``CurriculumView``) or any object
    exposing ``chapters_in_order`` (with ``source_refs``) and ``curriculum``.
    The document title comes from the confirmed curriculum's first source (the
    imported file); ``document_title`` overrides it.
    """
    created: list[Any] = []
    if view is None:
        return created
    curriculum = getattr(view, "curriculum", None)
    sources = tuple(getattr(curriculum, "sources", ()) or ())
    title = (
        _clean(document_title)
        or (_clean(sources[0].title) if sources else "")
        or _clean(getattr(curriculum, "title", None))
        or "教材"
    )
    locator_source = _clean(sources[0].locator) if sources else ""

    for chapter in getattr(view, "chapters_in_order", ()) or ():
        refs = tuple(getattr(chapter, "source_refs", ()) or ())
        locator = _locator_from_refs(refs)
        if not refs or not locator:
            # a chapter without page provenance gets no invented resource
            # (e.g. the auto-collected concept container has no page evidence)
            continue
        created.append(
            store.add_resource(
                course_id,
                ResourceType.PDF.value,
                title,
                source="textbook",
                locator=locator,
                chapter_id=chapter.id,
                metadata={
                    "document_id": getattr(refs[0], "document_id", None),
                    "curriculum_id": getattr(curriculum, "id", None),
                    "curriculum_version": getattr(curriculum, "version", None),
                    "locator_source": locator_source,
                    # Phase 8B-2: the full file path, so the resource viewer can
                    # reopen the document (the model itself is unchanged).
                    "path": _clean(document_path) or None,
                },
                origin=ResourceOrigin.TEXTBOOK_IMPORT,
            )
        )
    return created


__all__ = ["record_textbook_resources"]
