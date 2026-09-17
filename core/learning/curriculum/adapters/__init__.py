"""Textbook / PageLens curriculum adapters (Phase 7A).

Bridges the app's real PDF reading capability into the Curriculum Draft flow:

    from core.learning.curriculum.adapters import PageLensCurriculumAdapter

    index = build_pdf_lazy_index(pdf_bytes, display_name)   # existing, local
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name="自动控制原理",
        concepts=extractor(path),
    )

The adapter is pure (no I/O, no provider, no store writes); the caller parses
the PDF and owns every persistence step, and the draft only becomes an ACTIVE
curriculum through the user's explicit confirmation.
"""

from .page_adapter import (
    CONCEPT_SOURCE_SECTION,
    DEFAULT_TEXTBOOK_TITLE,
    PageLensCurriculumAdapter,
    TextbookParseError,
)

__all__ = [
    "PageLensCurriculumAdapter",
    "TextbookParseError",
    "CONCEPT_SOURCE_SECTION",
    "DEFAULT_TEXTBOOK_TITLE",
]
