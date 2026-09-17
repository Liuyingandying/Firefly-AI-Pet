"""PageLens / PDF -> CurriculumDraft adapter (Phase 2-CF3).

Pure input model, pure draft builder and a review gate. The adapter never
writes to the LearningStore and never creates Concepts — it only produces
``CurriculumDraft`` objects for a user to review and confirm.

    from core.learning.curriculum.adapter import (
        build_draft, DocumentStructure, CurriculumDraftReviewService,
    )
"""

from .documents import (
    DocumentStructure,
    DocumentStructureError,
    Section,
    SectionConceptHint,
)
from .draft import (
    DEFAULT_PATH_TITLE,
    DraftBuildError,
    PAGE_CONCEPTS_CHAPTER_TITLE,
    PAGE_CONCEPTS_SOURCE_SECTION,
    build_draft,
)
from .review import CurriculumDraftReviewService, ReviewSummary

__all__ = [
    # input model
    "DocumentStructure",
    "DocumentStructureError",
    "Section",
    "SectionConceptHint",
    # draft builder
    "build_draft",
    "DraftBuildError",
    "PAGE_CONCEPTS_CHAPTER_TITLE",
    "PAGE_CONCEPTS_SOURCE_SECTION",
    "DEFAULT_PATH_TITLE",
    # review gate
    "CurriculumDraftReviewService",
    "ReviewSummary",
]