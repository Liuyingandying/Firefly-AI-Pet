"""Curriculum domain layer (Phase 2-CF1).

Public surface of the Curriculum Foundation: pure immutable models plus the
deterministic validator. No persistence, no UI, no provider, no PageLens — CF2
adds the store migration on top of exactly these types, and CF3 feeds
``CurriculumDraft`` from textbook/PageLens extraction.

    from core.learning.curriculum import (
        Curriculum, CurriculumDraft, Chapter, LearningPath,
        ChapterConcept, ConceptPrerequisite, CurriculumValidator,
    )

Design reference: ``Firefly_Learning_Agent_Phase2_Curriculum_Design.md``.
"""

from .models import (
    NON_ACTIVATING_ACTORS,
    ActivateResult,
    ActiveCurriculumRequiredError,
    Chapter,
    ChapterConcept,
    ChapterConceptDraft,
    ChapterConceptRef,
    ChapterConceptRole,
    ChapterDraft,
    ConceptPrerequisite,
    ConceptProposal,
    ConfirmationRequiredError,
    Curriculum,
    CurriculumDraft,
    CurriculumError,
    CurriculumItemRef,
    CurriculumSource,
    CurriculumStatus,
    CurriculumValidationError,
    CurriculumView,
    DifficultyHint,
    DraftOrigin,
    DraftStatus,
    LearningPath,
    LearningPathDraft,
    LearningPathStep,
    PathKind,
    PathStepDraft,
    PathTargetType,
    PrerequisiteDraft,
    PrerequisiteKind,
    SourceKind,
    active_curriculum_for,
    build_curriculum_from_draft,
    derive_learning_order,
    draft_from_curriculum,
    ensure_consumable,
    is_consumable,
    next_version,
    normalize_name,
)
from .validators import (
    CurriculumValidator,
    ValidationCode,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
    validate_curriculum,
    validate_draft,
    validate_set,
)

__all__ = [
    # errors
    "CurriculumError",
    "CurriculumValidationError",
    "ConfirmationRequiredError",
    "ActiveCurriculumRequiredError",
    # enums
    "CurriculumStatus",
    "DraftStatus",
    "DraftOrigin",
    "SourceKind",
    "ChapterConceptRole",
    "PrerequisiteKind",
    "PathKind",
    "PathTargetType",
    "DifficultyHint",
    "NON_ACTIVATING_ACTORS",
    # published structure
    "CurriculumSource",
    "Chapter",
    "ChapterConcept",
    "ChapterConceptRef",
    "ConceptPrerequisite",
    "LearningPathStep",
    "LearningPath",
    "Curriculum",
    "CurriculumItemRef",
    "CurriculumView",
    "derive_learning_order",
    # Active-only gate
    "is_consumable",
    "ensure_consumable",
    "active_curriculum_for",
    # drafts
    "ConceptProposal",
    "ChapterConceptDraft",
    "ChapterDraft",
    "PathStepDraft",
    "LearningPathDraft",
    "PrerequisiteDraft",
    "CurriculumDraft",
    # lifecycle
    "next_version",
    "ActivateResult",
    "build_curriculum_from_draft",
    "draft_from_curriculum",
    # validation
    "CurriculumValidator",
    "ValidationSeverity",
    "ValidationCode",
    "ValidationIssue",
    "ValidationReport",
    "validate_curriculum",
    "validate_draft",
    "validate_set",
    # shared identity primitive
    "normalize_name",
]
