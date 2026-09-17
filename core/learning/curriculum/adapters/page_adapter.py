"""PageLensCurriculumAdapter (Phase 7A).

Bridges the app's existing PDF reading capability
(``core.pdf_processor.build_pdf_lazy_index`` → ``LazyPdfIndex``) into the
Curriculum Draft flow:

    PDF bytes ──build_pdf_lazy_index──▶ LazyPdfIndex (bookmarks = 章节)
                                           │
                              PageLensCurriculumAdapter.build_draft
                                           ▼
                              CurriculumDraft (status=DRAFT)
                                           │  用户审核（review service）
                                           ▼
                              confirm_draft() → Active Curriculum

Purity:

- the adapter itself performs NO I/O and NO store writes: the caller parses the
  PDF (``build_pdf_lazy_index``) and passes the resulting index in, so this
  module never touches PyMuPDF, the filesystem, a provider or the database;
- concepts are NOT invented: they come from the injected ``concepts`` (produced
  by the existing ``PdfQa.extract_concepts`` with the user's explicit consent,
  or left empty), and enter the draft as proposals only;
- nothing is activated here — confirmation is the only door to an ACTIVE
  curriculum.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from core.learning.curriculum.adapter.documents import (
    DocumentStructure,
    DocumentStructureError,
    Section,
    SectionConceptHint,
)
from core.learning.curriculum.models import (
    CurriculumDraft,
    DraftOrigin,
    SourceKind,
)

#: Source label used for concepts that have no per-section evidence.
CONCEPT_SOURCE_SECTION = "（概念提取）"

#: Fallback course/draft title when nothing better is available.
DEFAULT_TEXTBOOK_TITLE = "未命名教材"


def _text(value: Any) -> str:
    return str(value or "").strip()


class TextbookParseError(DocumentStructureError):
    """The PDF/index could not be turned into a draft (broken, encrypted...)."""


class PageLensCurriculumAdapter:
    """LazyPdfIndex / PageLens result → ``CurriculumDraft`` (pure)."""

    def build_document(
        self,
        document: Any,
        *,
        document_id: str | None = None,
        title: str | None = None,
        author: str | None = None,
    ) -> DocumentStructure:
        """Normalize a PDF result into a CF3 :class:`DocumentStructure`.

        ``document`` is duck-typed: a ``LazyPdfIndex`` (``outline`` /
        ``page_count`` / ``display_name``) or any object exposing the same
        fields. Bookmarks become level-1 sections in bookmark order with a page
        range that ends where the next bookmark starts.
        """
        outline = list(getattr(document, "outline", None) or ())
        page_count = getattr(document, "page_count", None)
        display_name = _text(
            getattr(document, "display_name", None) or document_id or title
        )
        doc_id = _text(document_id) or display_name or "textbook:unknown"
        doc_title = _text(title) or display_name or DEFAULT_TEXTBOOK_TITLE

        entries: list[tuple[str, int]] = []
        for item in outline:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                heading, page = _text(item[0]), item[1]
            elif isinstance(item, dict):
                heading, page = _text(item.get("title")), item.get("page", 1)
            else:
                heading, page = _text(item), 1
            try:
                page_number = max(1, int(page))
            except (TypeError, ValueError):
                page_number = 1
            if heading:
                entries.append((heading, page_number))
        if page_count is None:
            page_count = max((page for _title, page in entries), default=1)

        total_pages = None
        try:
            total_pages = max(1, int(page_count))
        except (TypeError, ValueError):
            total_pages = None

        sections = []
        for position, (heading, page) in enumerate(entries, start=1):
            next_page = entries[position][1] if position < len(entries) else None
            # the last chapter extends to the end of the document
            end_page = (
                (next_page - 1) if next_page is not None
                else (total_pages or page)
            )
            sections.append(
                Section(
                    id=f"{doc_id}:s{position}",
                    title=heading,
                    level=1,
                    position=position,
                    source_page_range=(page, max(page, end_page)),
                )
            )
        return DocumentStructure(
            document_id=doc_id,
            title=doc_title,
            author=_text(author),
            sections=tuple(sections),
            page_concepts=(),
            fingerprint=None,
        )

    def build_draft(
        self,
        document: Any,
        *,
        course_id: str,
        course_name: str | None = None,
        source: str = SourceKind.PDF.value,
        concepts: Sequence[str] | Sequence[Any] = (),
        draft_id: str | None = None,
        created_by: str = DraftOrigin.PAGELENS.value,
        make_path: bool = True,
    ) -> CurriculumDraft:
        """Build a ``CurriculumDraft`` from a PDF result.

        ``course_id`` must reference an EXISTING course shell (created by the
        caller as part of the explicit import action) — CF2 anchors drafts to
        courses. ``concepts`` are optional suggestion strings (from the existing
        ``PdfQa.extract_concepts`` with user consent); they become
        ``ConceptProposal``s in the auto-review chapter, never Concepts.
        """
        structure = self.build_document(document)
        if course_name:
            structure = _rename(structure, course_name)
        if concepts:
            structure = _with_concepts(structure, concepts)

        from core.learning.curriculum.adapter.draft import build_draft

        return build_draft(
            structure,
            course_id=course_id,
            draft_id=draft_id,
            created_by=created_by,
            make_path=make_path,
        )


def _rename(structure: DocumentStructure, course_name: str) -> DocumentStructure:
    """Re-title the structure so the draft/project carries the user's name."""
    from dataclasses import replace

    name = _text(course_name)
    if not name or name == structure.title:
        return structure
    return replace(structure, title=name)


def _with_concepts(
    structure: DocumentStructure, concepts: Iterable[Any]
) -> DocumentStructure:
    """Attach concept suggestions to the structure as page-level hints.

    Accepts plain strings or ``{term/name}`` mappings; each hint keeps the
    document as its provenance (no per-section evidence exists at import time,
    and none is invented).
    """
    from dataclasses import replace

    hints = []
    for item in concepts:
        if isinstance(item, SectionConceptHint):
            hints.append(item)
        elif isinstance(item, str) and _text(item):
            hints.append(SectionConceptHint(name=item))
        elif isinstance(item, dict):
            hints.append(SectionConceptHint.from_payload(item))
    if not hints:
        return structure
    merged = tuple(structure.page_concepts) + tuple(hints)
    return replace(structure, page_concepts=merged)


__all__ = [
    "PageLensCurriculumAdapter",
    "TextbookParseError",
    "CONCEPT_SOURCE_SECTION",
    "DEFAULT_TEXTBOOK_TITLE",
]
