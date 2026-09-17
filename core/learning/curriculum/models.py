"""Curriculum domain model (Phase 2-CF1).

Pure, immutable, persistence-free value layer implementing the FROZEN
Curriculum Foundation Design (``Firefly_Learning_Agent_Phase2_Curriculum_Design.md``).

Aggregate shape (design §4)::

    Course                                  (long-lived project identity — untouched)
      ├─ CurriculumDraft 0..n               (editable, NEVER consumable)
      └─ Curriculum 0..n                    (versioned, at most one active)
           ├─ Chapter 1..n ─ ChapterConcept 0..n ──> Concept
           ├─ ConceptPrerequisite 0..n      (concept -> concept, version-scoped)
           ├─ LearningPath 1..n ─ LearningPathStep 1..n
           └─ CurriculumSource 1..n

Boundaries this module deliberately respects:

- No SQLite / LearningStore / Qt / providers / PageLens / UI imports. The model
  is pure so it can be unit-tested in memory and persisted later (CF2) without
  changing a single field.
- Concept identity keeps using the existing course-scoped ``normalize_name``
  from :mod:`core.learning.models`; a Curriculum never invents a second notion
  of "the same concept".
- ``Draft != Curriculum``: a Draft is a different type and has no way to be
  consumed as an active curriculum (see :func:`ensure_consumable`).
- Mastery / retention / review / assessment are NOT part of this layer. The
  structure describes how concepts are organised, never how well they are known.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.learning.models import SourceRef, normalize_name, utc_now_iso


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class CurriculumError(RuntimeError):
    """Base error for the Curriculum domain layer."""


class CurriculumValidationError(CurriculumError):
    """Raised when a validation report carrying errors is escalated."""

    def __init__(self, message: str, issues: Sequence[Any] = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(str(getattr(issue, "code", "")) for issue in self.issues)


class ConfirmationRequiredError(CurriculumError):
    """A Draft can only become Active through an explicit USER confirmation.

    Raised when a confirmation event is missing, unnamed, or originates from a
    non-user actor (AI / PageLens). This is the AI-pollution guard: AI output is
    always Draft/Proposal, never Active.
    """


class ActiveCurriculumRequiredError(CurriculumError):
    """Raised when a non-Active subject reaches an Active-only consumer.

    Drafts and superseded/archived versions must never enter the learning
    context, an assessment flow or any other consumer of active structure.
    """


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class CurriculumStatus(str, Enum):
    """Lifecycle of a published curriculum version (design §5).

    A Draft is not a Curriculum status — it is a different type
    (:class:`CurriculumDraft`), so ``DRAFT`` is intentionally absent here.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class DraftStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DraftOrigin(str, Enum):
    """Who produced a draft (design §10.2). None of these may activate."""

    USER = "user"
    PAGELENS = "pagelens"
    AI = "ai"
    INSTITUTIONAL_IMPORT = "institutional_import"


class SourceKind(str, Enum):
    MANUAL = "manual"
    PDF = "pdf"
    AI_GENERATED = "ai_generated"
    INSTITUTIONAL = "institutional"


class ChapterConceptRole(str, Enum):
    """A concept's role inside one chapter (design §7.3)."""

    PRIMARY = "primary"
    SUPPORTING = "supporting"
    REFERENCE = "reference"


class PrerequisiteKind(str, Enum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"


class PathKind(str, Enum):
    CANONICAL = "canonical"
    ALTERNATE = "alternate"


class PathTargetType(str, Enum):
    CHAPTER = "chapter"
    CONCEPT = "concept"


class DifficultyHint(str, Enum):
    """Curriculum-level authoring hint only.

    It must never influence ``AssessmentRecord.difficulty`` or any mastery
    rule (design §7.3).
    """

    INTRODUCTORY = "introductory"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


#: Actors that may never confirm/activate a curriculum.
NON_ACTIVATING_ACTORS: frozenset[str] = frozenset(
    {
        DraftOrigin.AI.value,
        DraftOrigin.PAGELENS.value,
        "assistant",
        "model",
        "llm",
        "bot",
        "auto",
        "system",
        "gpt",
    }
)


def _enum_value(enum_cls: type[Enum], value: Any, field: str) -> str:
    """Coerce ``value`` to ``enum_cls``'s value (accepting the enum, its value
    or a matching name) — strict enough to reject typos, lenient enough that
    callers may pass plain strings."""
    if isinstance(value, enum_cls):
        return value.value
    text = str(value or "").strip()
    for member in enum_cls:
        if text == member.value or text.upper() == member.name:
            return member.value
    raise CurriculumError(f"invalid {field}: {value!r}")


def _new_id() -> str:
    return uuid.uuid4().hex


def _text(value: Any) -> str:
    return str(value or "").strip()


def _tuple_of_text(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(_text(value) for value in (values or ()) if _text(value))


# ---------------------------------------------------------------------------
# published structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurriculumSource:
    """One provenance entry for a curriculum version (design §10.1)."""

    kind: str = SourceKind.MANUAL.value
    title: str = ""
    locator: str | None = None
    source_version: str | None = None
    fingerprint: str | None = None
    institution: str | None = None
    generated_by: str | None = None
    captured_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum_value(SourceKind, self.kind, "source kind"))
        object.__setattr__(self, "title", _text(self.title))
        if not self.captured_at:
            object.__setattr__(self, "captured_at", utc_now_iso())

    @property
    def is_traceable(self) -> bool:
        """A PDF / institutional source is only auditable with a locator or a
        content fingerprint (design §10.1)."""
        return bool(_text(self.locator) or _text(self.fingerprint))


@dataclass(frozen=True, slots=True)
class Chapter:
    """One chapter of a specific curriculum version (design §6)."""

    id: str
    curriculum_id: str
    title: str
    position: int
    description: str = ""
    goals: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise CurriculumError("Chapter.id is required")
        if not self.curriculum_id:
            raise CurriculumError("Chapter.curriculum_id is required")
        object.__setattr__(self, "goals", _tuple_of_text(self.goals))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))

    @property
    def has_title(self) -> bool:
        return bool(_text(self.title))


@dataclass(frozen=True, slots=True)
class ChapterConcept:
    """Many-to-many link between a chapter and a course-scoped Concept.

    Logical key is ``(chapter_id, concept_id)``. A concept may appear in several
    chapters; ``role``/``position`` describe this placement only and never touch
    the Concept itself (design §7.3).
    """

    chapter_id: str
    concept_id: str
    position: int
    role: str = ChapterConceptRole.PRIMARY.value
    required: bool = True
    difficulty_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.chapter_id or not self.concept_id:
            raise CurriculumError("ChapterConcept requires chapter_id and concept_id")
        object.__setattr__(self, "role", _enum_value(ChapterConceptRole, self.role, "role"))
        if self.difficulty_hint is not None:
            object.__setattr__(
                self,
                "difficulty_hint",
                _enum_value(DifficultyHint, self.difficulty_hint, "difficulty_hint"),
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.chapter_id, self.concept_id)

    @property
    def is_primary(self) -> bool:
        return self.role == ChapterConceptRole.PRIMARY.value


#: Task-spec name for the same relation (Phase 2-CF1 §七).
ChapterConceptRef = ChapterConcept


@dataclass(frozen=True, slots=True)
class ConceptPrerequisite:
    """Version-scoped structural dependency between two concepts (design §8).

    It expresses structure only: it never implies mastery, never blocks a
    learner from jumping ahead and never generates a recommendation.
    """

    curriculum_id: str
    concept_id: str
    prerequisite_concept_id: str
    kind: str = PrerequisiteKind.REQUIRED.value

    def __post_init__(self) -> None:
        if not self.concept_id or not self.prerequisite_concept_id:
            raise CurriculumError(
                "ConceptPrerequisite requires concept_id and prerequisite_concept_id"
            )
        object.__setattr__(self, "kind", _enum_value(PrerequisiteKind, self.kind, "kind"))

    @property
    def prerequisite_id(self) -> str:
        """Task-spec alias (Phase 2-CF1 §八)."""
        return self.prerequisite_concept_id

    @property
    def is_required(self) -> bool:
        return self.kind == PrerequisiteKind.REQUIRED.value

    @property
    def edge(self) -> tuple[str, str]:
        """``(from, to)`` edge: ``concept_id`` depends on its prerequisite."""
        return (self.concept_id, self.prerequisite_concept_id)


@dataclass(frozen=True, slots=True)
class LearningPathStep:
    """One step on a learning path (design §9.2)."""

    id: str
    path_id: str
    position: int
    target_type: str
    target_id: str
    stage: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not self.id or not self.path_id or not self.target_id:
            raise CurriculumError("LearningPathStep requires id, path_id and target_id")
        object.__setattr__(
            self, "target_type", _enum_value(PathTargetType, self.target_type, "target_type")
        )


@dataclass(frozen=True, slots=True)
class LearningPath:
    """The default (canonical) or an alternate structural route (design §9)."""

    id: str
    curriculum_id: str
    title: str
    description: str = ""
    kind: str = PathKind.CANONICAL.value
    steps: tuple[LearningPathStep, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.curriculum_id:
            raise CurriculumError("LearningPath requires id and curriculum_id")
        object.__setattr__(self, "kind", _enum_value(PathKind, self.kind, "path kind"))
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def is_canonical(self) -> bool:
        return self.kind == PathKind.CANONICAL.value

    @property
    def items(self) -> tuple[LearningPathStep, ...]:
        """Task-spec alias (Phase 2-CF1 §六)."""
        return self.steps

    @property
    def ordered_steps(self) -> tuple[LearningPathStep, ...]:
        """Order comes from the normalised ``position``, never list order."""
        return tuple(sorted(self.steps, key=lambda step: (step.position, step.id)))

    def step_for(self, target_id: str) -> LearningPathStep | None:
        for step in self.ordered_steps:
            if step.target_id == target_id:
                return step
        return None


@dataclass(frozen=True, slots=True)
class Curriculum:
    """One published, versioned curriculum of a course (design §5).

    Immutability is the mechanism behind "an Active curriculum is never
    overwritten in place": editing always derives a new Draft, which is
    published as a NEW version (see :func:`draft_from_curriculum`).
    """

    id: str
    course_id: str
    title: str
    version: int
    status: str = CurriculumStatus.ACTIVE.value
    description: str = ""
    goals: tuple[str, ...] = ()
    based_on_curriculum_id: str | None = None
    default_path_id: str | None = None
    sources: tuple[CurriculumSource, ...] = ()
    created_at: str = ""
    activated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise CurriculumError("Curriculum.id is required")
        if not self.course_id:
            raise CurriculumError("Curriculum.course_id is required")
        object.__setattr__(
            self, "status", _enum_value(CurriculumStatus, self.status, "status")
        )
        object.__setattr__(self, "goals", _tuple_of_text(self.goals))
        object.__setattr__(self, "sources", tuple(self.sources))
        if self.version < 1:
            raise CurriculumError(f"Curriculum.version must be >= 1 (got {self.version})")

    @property
    def is_active(self) -> bool:
        return self.status == CurriculumStatus.ACTIVE.value

    def superseded(self) -> "Curriculum":
        """The transition applied to the previous Active on re-activation."""
        return replace(self, status=CurriculumStatus.SUPERSEDED.value)

    def archived(self) -> "Curriculum":
        return replace(self, status=CurriculumStatus.ARCHIVED.value)


@dataclass(frozen=True, slots=True)
class CurriculumItemRef:
    """One element of the derived ``learning_order`` (design §5).

    It is DERIVED from the default path — never stored next to the path/step
    positions, so there is exactly one source of ordering truth.
    """

    position: int
    target_type: str
    target_id: str
    stage: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_type", _enum_value(PathTargetType, self.target_type, "target_type")
        )


@dataclass(frozen=True, slots=True)
class CurriculumView:
    """Read projection: the header plus its version-scoped members (design §5)."""

    curriculum: Curriculum
    chapters: tuple[Chapter, ...] = ()
    learning_paths: tuple[LearningPath, ...] = ()
    chapter_concepts: tuple[ChapterConcept, ...] = ()
    prerequisites: tuple[ConceptPrerequisite, ...] = ()
    learning_order: tuple[CurriculumItemRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "chapters", tuple(self.chapters))
        object.__setattr__(self, "learning_paths", tuple(self.learning_paths))
        object.__setattr__(self, "chapter_concepts", tuple(self.chapter_concepts))
        object.__setattr__(self, "prerequisites", tuple(self.prerequisites))
        object.__setattr__(self, "learning_order", tuple(self.learning_order))

    @classmethod
    def of(
        cls,
        curriculum: Curriculum,
        *,
        chapters: Iterable[Chapter] = (),
        learning_paths: Iterable[LearningPath] = (),
        chapter_concepts: Iterable[ChapterConcept] = (),
        prerequisites: Iterable[ConceptPrerequisite] = (),
    ) -> "CurriculumView":
        """Build the projection; ``learning_order`` is derived from the default
        path. Any lifecycle status is accepted — a view is a read projection
        (auditing a superseded version must stay possible), and the Active-only
        rule is enforced where structure is *consumed*."""
        paths = tuple(learning_paths)
        order = derive_learning_order(
            curriculum, chapters=chapters, paths=paths, require_active=False
        )
        return cls(
            curriculum=curriculum,
            chapters=tuple(chapters),
            learning_paths=paths,
            chapter_concepts=tuple(chapter_concepts),
            prerequisites=tuple(prerequisites),
            learning_order=order,
        )

    @property
    def course_id(self) -> str:
        return self.curriculum.course_id

    @property
    def default_path(self) -> LearningPath | None:
        if self.curriculum.default_path_id:
            for path in self.learning_paths:
                if path.id == self.curriculum.default_path_id:
                    return path
        for path in self.learning_paths:
            if path.is_canonical:
                return path
        return None

    @property
    def chapters_in_order(self) -> tuple[Chapter, ...]:
        return tuple(sorted(self.chapters, key=lambda c: (c.position, c.id)))

    def chapter_by_id(self, chapter_id: str) -> Chapter | None:
        for chapter in self.chapters:
            if chapter.id == chapter_id:
                return chapter
        return None

    def concepts_of_chapter(self, chapter_id: str) -> tuple[ChapterConcept, ...]:
        links = (link for link in self.chapter_concepts if link.chapter_id == chapter_id)
        return tuple(sorted(links, key=lambda link: (link.position, link.concept_id)))

    def next_in_order(self, target_id: str) -> CurriculumItemRef | None:
        """Deterministic successor on the DEFAULT path (design §9.2).

        This is structural order, not a personal recommendation.
        """
        ensure_consumable(self.curriculum)
        order = self.learning_order
        for index, item in enumerate(order):
            if item.target_id == target_id:
                return order[index + 1] if index + 1 < len(order) else None
        return None


def derive_learning_order(
    curriculum: Curriculum,
    *,
    chapters: Iterable[Chapter],
    paths: Iterable[LearningPath],
    require_active: bool = True,
) -> tuple[CurriculumItemRef, ...]:
    """Derive ``learning_order`` from the default path (design §5/§9.2)."""
    if require_active:
        ensure_consumable(curriculum)
    path_list = tuple(paths)
    default = None
    if curriculum.default_path_id:
        default = next(
            (path for path in path_list if path.id == curriculum.default_path_id), None
        )
    if default is None:
        default = next((path for path in path_list if path.is_canonical), None)
    if default is None:
        return ()
    chapter_ids = {chapter.id for chapter in chapters}
    order: list[CurriculumItemRef] = []
    for index, step in enumerate(default.ordered_steps, start=1):
        if step.target_type == PathTargetType.CHAPTER.value and step.target_id not in chapter_ids:
            # A dangling step cannot be part of the derived order; the
            # validator reports it so authoring can fix the path.
            continue
        order.append(
            CurriculumItemRef(
                position=index,
                target_type=step.target_type,
                target_id=step.target_id,
                stage=step.stage,
            )
        )
    return tuple(order)


# ---------------------------------------------------------------------------
# Active-only consumption gate (design §11.2 / §14.4)
# ---------------------------------------------------------------------------


_ACTIVE_CONSUMERS = "LearningContext / assessment / any active-structure consumer"


def is_consumable(subject: Any) -> bool:
    """True only for an ACTIVE published curriculum."""
    curriculum = getattr(subject, "curriculum", subject)
    return isinstance(curriculum, Curriculum) and curriculum.is_active


def ensure_consumable(subject: Any) -> Curriculum:
    """The single Active-only gate (Phase 2-CF1 rule 4).

    Accepts an ACTIVE :class:`Curriculum` or a :class:`CurriculumView` whose
    curriculum is ACTIVE, and refuses everything else — including every
    :class:`CurriculumDraft` and any superseded/archived version. Drafts are a
    different type and have no route into an Active consumer.
    """
    if isinstance(subject, CurriculumDraft):
        raise ActiveCurriculumRequiredError(
            "a CurriculumDraft can never be consumed as an active curriculum; "
            "confirm and publish it first"
        )
    curriculum = getattr(subject, "curriculum", subject)
    if not isinstance(curriculum, Curriculum):
        raise ActiveCurriculumRequiredError(
            f"{type(subject).__name__} is not a curriculum"
        )
    if not curriculum.is_active:
        raise ActiveCurriculumRequiredError(
            f"curriculum {curriculum.id} is {curriculum.status}, not "
            f"{CurriculumStatus.ACTIVE.value}; only an active version may be "
            f"consumed by {_ACTIVE_CONSUMERS}"
        )
    return curriculum


def active_curriculum_for(
    course_id: str, curricula: Iterable[Curriculum]
) -> Curriculum | None:
    """The ACTIVE version of a course, or None when the course has none.

    Having no active curriculum is a legal state (design §5): the course still
    works, it just has no chapter structure yet.
    """
    for curriculum in curricula:
        if curriculum.course_id == course_id and curriculum.is_active:
            return curriculum
    return None


# ---------------------------------------------------------------------------
# drafts (never consumable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConceptProposal:
    """A proposed concept inside a draft.

    NOT a Concept: it carries no id in the store, no mastery, no state, no
    review and no assessment (design §11.1).

    ``source_section`` names the outline/extraction section that produced the
    proposal and ``confidence`` rates the extraction (Phase 2-CF3) — both are
    authoring metadata that never influence mastery or assessment rules.
    """

    proposal_id: str
    name: str
    aliases: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    matched_concept_id: str | None = None
    source_section: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise CurriculumError("ConceptProposal.proposal_id is required")
        object.__setattr__(self, "name", _text(self.name))
        object.__setattr__(self, "aliases", _tuple_of_text(self.aliases))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "source_section", _text(self.source_section) or None)
        if self.confidence is not None:
            confidence = float(self.confidence)
            if not (0.0 <= confidence <= 1.0):
                raise CurriculumError(
                    f"proposal {self.proposal_id!r} confidence must be in [0, 1]"
                )
            object.__setattr__(self, "confidence", confidence)

    @property
    def normalized_name(self) -> str:
        """Course-scoped identity key (shared with the Learning Store)."""
        return normalize_name(self.name)

    @property
    def is_matched(self) -> bool:
        """True when this proposal reuses an existing course Concept, which is
        how mastery/history is preserved across curriculum versions."""
        return bool(self.matched_concept_id)


@dataclass(frozen=True, slots=True)
class ChapterConceptDraft:
    """Placement of one proposal inside a draft chapter.

    The proposal is embedded (not referenced by a loose id) so a draft can
    never point at a proposal that does not exist. One proposal object may be
    placed in several chapters — that is the intended way to express "the same
    concept is taught in two chapters".
    """

    proposal: ConceptProposal
    position: int
    role: str = ChapterConceptRole.PRIMARY.value
    required: bool = True
    difficulty_hint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ConceptProposal):
            raise CurriculumError("ChapterConceptDraft.proposal must be a ConceptProposal")
        object.__setattr__(self, "role", _enum_value(ChapterConceptRole, self.role, "role"))
        if self.difficulty_hint is not None:
            object.__setattr__(
                self,
                "difficulty_hint",
                _enum_value(DifficultyHint, self.difficulty_hint, "difficulty_hint"),
            )

    @property
    def proposal_id(self) -> str:
        return self.proposal.proposal_id

    @property
    def name(self) -> str:
        return self.proposal.name

    @property
    def is_primary(self) -> bool:
        return self.role == ChapterConceptRole.PRIMARY.value


@dataclass(frozen=True, slots=True)
class ChapterDraft:
    """A proposed chapter (design §11.1)."""

    id: str
    title: str
    position: int
    description: str = ""
    goals: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    concepts: tuple[ChapterConceptDraft, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise CurriculumError("ChapterDraft.id is required")
        object.__setattr__(self, "goals", _tuple_of_text(self.goals))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "concepts", tuple(self.concepts))

    @property
    def ordered_concepts(self) -> tuple[ChapterConceptDraft, ...]:
        return tuple(
            sorted(self.concepts, key=lambda link: (link.position, link.proposal_id))
        )

    @property
    def proposals(self) -> tuple[ConceptProposal, ...]:
        return tuple(link.proposal for link in self.ordered_concepts)


@dataclass(frozen=True, slots=True)
class PathStepDraft:
    """A proposed path step; targets are draft-local ids."""

    id: str
    position: int
    target_type: str
    target_id: str
    stage: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not self.id or not self.target_id:
            raise CurriculumError("PathStepDraft requires id and target_id")
        object.__setattr__(
            self, "target_type", _enum_value(PathTargetType, self.target_type, "target_type")
        )


@dataclass(frozen=True, slots=True)
class LearningPathDraft:
    id: str
    title: str
    description: str = ""
    kind: str = PathKind.CANONICAL.value
    steps: tuple[PathStepDraft, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise CurriculumError("LearningPathDraft.id is required")
        object.__setattr__(self, "kind", _enum_value(PathKind, self.kind, "path kind"))
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def is_canonical(self) -> bool:
        return self.kind == PathKind.CANONICAL.value


@dataclass(frozen=True, slots=True)
class PrerequisiteDraft:
    """A proposed prerequisite edge; endpoints are proposal ids."""

    concept_proposal_id: str
    prerequisite_proposal_id: str
    kind: str = PrerequisiteKind.REQUIRED.value

    def __post_init__(self) -> None:
        if not self.concept_proposal_id or not self.prerequisite_proposal_id:
            raise CurriculumError("PrerequisiteDraft requires both proposal ids")
        object.__setattr__(self, "kind", _enum_value(PrerequisiteKind, self.kind, "kind"))

    @property
    def edge(self) -> tuple[str, str]:
        return (self.concept_proposal_id, self.prerequisite_proposal_id)


@dataclass(frozen=True, slots=True)
class CurriculumDraft:
    """Editable proposal for a curriculum version (design §11.1).

    A draft is deliberately NOT a :class:`Curriculum`: it has no version, no
    lifecycle status and no path into an Active consumer. Confirmation is the
    only door, and it requires an explicit user actor.
    """

    id: str
    course_id: str
    title: str
    description: str = ""
    goals: tuple[str, ...] = ()
    based_on_curriculum_id: str | None = None
    chapters: tuple[ChapterDraft, ...] = ()
    paths: tuple[LearningPathDraft, ...] = ()
    prerequisites: tuple[PrerequisiteDraft, ...] = ()
    sources: tuple[CurriculumSource, ...] = ()
    status: str = DraftStatus.DRAFT.value
    created_by: str = DraftOrigin.USER.value
    created_at: str = ""
    updated_at: str = ""
    confirmed_by: str | None = None
    confirmed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise CurriculumError("CurriculumDraft.id is required")
        if not self.course_id:
            raise CurriculumError("CurriculumDraft.course_id is required")
        object.__setattr__(
            self, "status", _enum_value(DraftStatus, self.status, "draft status")
        )
        object.__setattr__(
            self, "created_by", _enum_value(DraftOrigin, self.created_by, "draft origin")
        )
        object.__setattr__(self, "goals", _tuple_of_text(self.goals))
        object.__setattr__(self, "chapters", tuple(self.chapters))
        object.__setattr__(self, "paths", tuple(self.paths))
        object.__setattr__(self, "prerequisites", tuple(self.prerequisites))
        object.__setattr__(self, "sources", tuple(self.sources))

    # -- projections -------------------------------------------------------

    @property
    def source_type(self) -> str | None:
        """Primary provenance kind (task-spec convenience)."""
        return self.sources[0].kind if self.sources else None

    @property
    def provenance(self) -> tuple[CurriculumSource, ...]:
        """Task-spec alias for the draft's provenance set."""
        return self.sources

    @property
    def concept_candidates(self) -> tuple[ConceptProposal, ...]:
        """All proposals in chapter order (task-spec convenience)."""
        return tuple(
            proposal
            for chapter in self.chapters_in_order
            for proposal in chapter.proposals
        )

    @property
    def chapters_in_order(self) -> tuple[ChapterDraft, ...]:
        return tuple(sorted(self.chapters, key=lambda c: (c.position, c.id)))

    @property
    def is_confirmed(self) -> bool:
        return self.status == DraftStatus.CONFIRMED.value

    @property
    def is_consumable(self) -> bool:
        """Always False — the point of the type."""
        return False

    def proposals_by_id(self) -> dict[str, ConceptProposal]:
        """Index of every proposal in the draft.

        The same proposal object placed in several chapters keeps one identity
        (that is how "one concept, two chapters" is expressed); reusing an id
        for a *different* name is a data bug and is refused.
        """
        index: dict[str, ConceptProposal] = {}
        for link in self.concept_links:
            existing = index.get(link.proposal_id)
            if existing is not None and existing.normalized_name != link.proposal.normalized_name:
                raise CurriculumError(
                    f"proposal id {link.proposal_id} is reused for two different "
                    f"concepts ({existing.name!r} / {link.name!r})"
                )
            index[link.proposal_id] = link.proposal
        return index

    @property
    def concept_links(self) -> tuple[ChapterConceptDraft, ...]:
        """Every chapter placement, in chapter then position order."""
        return tuple(
            link for chapter in self.chapters_in_order for link in chapter.ordered_concepts
        )

    # -- state machine -----------------------------------------------------

    def confirm(
        self,
        *,
        confirmed_by: str,
        confirmed_at: str | None = None,
    ) -> "CurriculumDraft":
        """Record the confirmation event (Draft -> Confirmed).

        Requires an explicit actor and refuses AI/PageLens actors, which is how
        "AI has no Active write access" is enforced structurally rather than by
        convention.
        """
        actor = _text(confirmed_by).lower()
        if not actor:
            raise ConfirmationRequiredError(
                "confirming a curriculum draft requires an explicit actor "
                "(the user confirmation event)"
            )
        if actor in NON_ACTIVATING_ACTORS:
            raise ConfirmationRequiredError(
                f"{actor!r} cannot confirm a curriculum draft: only an explicit "
                "user action may publish a curriculum version"
            )
        if self.status != DraftStatus.DRAFT.value:
            raise ConfirmationRequiredError(
                f"draft {self.id} is {self.status}, not {DraftStatus.DRAFT.value}"
            )
        stamp = confirmed_at or utc_now_iso()
        return replace(
            self,
            status=DraftStatus.CONFIRMED.value,
            confirmed_by=actor,
            confirmed_at=stamp,
            updated_at=stamp,
        )

    def reject(self, *, at: str | None = None) -> "CurriculumDraft":
        stamp = at or utc_now_iso()
        return replace(self, status=DraftStatus.REJECTED.value, updated_at=stamp)


# ---------------------------------------------------------------------------
# lifecycle: versioning and Draft -> Active
# ---------------------------------------------------------------------------


def next_version(curricula: Iterable[Curriculum], course_id: str) -> int:
    """Version the system will assign to the next activation of ``course_id``.

    Versions are monotonic per course (design §5) and are allocated at
    activation, never by the draft.
    """
    versions = [
        curriculum.version for curriculum in curricula if curriculum.course_id == course_id
    ]
    return (max(versions) + 1) if versions else 1


@dataclass(frozen=True, slots=True)
class ActivateResult:
    """Pure result of publishing a confirmed draft.

    ``superseded`` holds the previously ACTIVE version that the publication
    replaces. Nothing here is persisted — CF2 does that atomically.
    """

    curriculum: Curriculum
    chapters: tuple[Chapter, ...] = ()
    chapter_concepts: tuple[ChapterConcept, ...] = ()
    prerequisites: tuple[ConceptPrerequisite, ...] = ()
    paths: tuple[LearningPath, ...] = ()
    superseded: Curriculum | None = None

    def view(self) -> CurriculumView:
        return CurriculumView.of(
            self.curriculum,
            chapters=self.chapters,
            learning_paths=self.paths,
            chapter_concepts=self.chapter_concepts,
            prerequisites=self.prerequisites,
        )

    def apply_to(self, curricula: Iterable[Curriculum]) -> tuple[Curriculum, ...]:
        """The full post-activation version set for one course.

        ``Curriculum`` is immutable, so publishing a new version cannot flip the
        previous one in place — it hands back a :attr:`superseded` copy instead.
        Persisting only ``curriculum`` would leave two ACTIVE rows (a rule-3
        violation), so CF2 must persist this set: the previous active is
        replaced by its superseded form and the new version is appended.
        """
        previous_id = self.superseded.id if self.superseded is not None else None
        updated: list[Curriculum] = []
        for curriculum in curricula:
            if curriculum.id == self.curriculum.id:
                continue  # re-activating the same id replaces it
            if previous_id is not None and curriculum.id == previous_id:
                updated.append(self.superseded)
            else:
                updated.append(curriculum)
        if previous_id is None:
            # No previous active was supplied: any active version of this course
            # still in the set must not remain active next to the new one.
            updated = [
                item.superseded()
                if item.course_id == self.curriculum.course_id and item.is_active
                else item
                for item in updated
            ]
        updated.append(self.curriculum)
        return tuple(updated)


def build_curriculum_from_draft(
    draft: CurriculumDraft,
    *,
    curriculum_id: str,
    version: int,
    concept_ids: Mapping[str, str],
    chapter_ids: Mapping[str, str] | None = None,
    path_ids: Mapping[str, str] | None = None,
    previous_active: Curriculum | None = None,
    activated_at: str | None = None,
    title: str | None = None,
    id_factory: Callable[[], str] = _new_id,
) -> ActivateResult:
    """Publish a CONFIRMED draft as an ACTIVE curriculum version.

    ``concept_ids`` maps ``proposal_id -> concept_id``: an existing course
    Concept id (reuse keeps all mastery/history) or a newly created one. CF1
    stays pure — the caller supplies the ids; CF2 performs the store work
    inside one transaction.

    Refuses anything that is not a confirmed draft, so an unconfirmed or
    AI-authored proposal can never reach Active.
    """
    if draft.status != DraftStatus.CONFIRMED.value:
        raise ConfirmationRequiredError(
            f"draft {draft.id} is {draft.status}: a curriculum version can only be "
            "published from a CONFIRMED draft (explicit user confirmation)"
        )
    if not curriculum_id:
        raise CurriculumError("curriculum_id is required")
    if version < 1:
        raise CurriculumError(f"version must be >= 1 (got {version})")
    if previous_active is not None and previous_active.course_id != draft.course_id:
        raise CurriculumError(
            "previous_active belongs to another course; a course's versions cannot mix"
        )

    chapter_ids = dict(chapter_ids or {})
    path_ids = dict(path_ids or {})
    stamp = activated_at or utc_now_iso()

    curriculum = Curriculum(
        id=curriculum_id,
        course_id=draft.course_id,
        title=_text(title) or _text(draft.title),
        version=version,
        status=CurriculumStatus.ACTIVE.value,
        description=draft.description,
        goals=draft.goals,
        based_on_curriculum_id=draft.based_on_curriculum_id,
        default_path_id=None,
        sources=draft.sources,
        created_at=stamp,
        activated_at=stamp,
    )

    proposals = draft.proposals_by_id()
    chapters: list[Chapter] = []
    links: list[ChapterConcept] = []
    for chapter_draft in draft.chapters_in_order:
        chapter_id = chapter_ids.get(chapter_draft.id) or f"{curriculum_id}:{chapter_draft.id}"
        chapters.append(
            Chapter(
                id=chapter_id,
                curriculum_id=curriculum_id,
                title=chapter_draft.title,
                position=chapter_draft.position,
                description=chapter_draft.description,
                goals=chapter_draft.goals,
                source_refs=chapter_draft.source_refs,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        for link in chapter_draft.ordered_concepts:
            proposal = link.proposal
            concept_id = concept_ids.get(link.proposal_id) or proposal.matched_concept_id
            if not concept_id:
                raise CurriculumError(
                    f"proposal {link.proposal_id} ({proposal.name!r}) has no concept id: "
                    "pass concept_ids[proposal_id] to reuse or create the Concept"
                )
            links.append(
                ChapterConcept(
                    chapter_id=chapter_id,
                    concept_id=concept_id,
                    position=link.position,
                    role=link.role,
                    required=link.required,
                    difficulty_hint=link.difficulty_hint,
                )
            )

    prerequisites: list[ConceptPrerequisite] = []
    for edge in draft.prerequisites:
        for proposal_id in (edge.concept_proposal_id, edge.prerequisite_proposal_id):
            if proposal_id not in proposals:
                raise CurriculumError(
                    f"prerequisite references unknown proposal {proposal_id}"
                )
        head = (
            concept_ids.get(edge.concept_proposal_id)
            or proposals[edge.concept_proposal_id].matched_concept_id
        )
        tail = (
            concept_ids.get(edge.prerequisite_proposal_id)
            or proposals[edge.prerequisite_proposal_id].matched_concept_id
        )
        if not head or not tail:
            raise CurriculumError(
                "prerequisite endpoints must map to concepts before activation"
            )
        prerequisites.append(
            ConceptPrerequisite(
                curriculum_id=curriculum_id,
                concept_id=head,
                prerequisite_concept_id=tail,
                kind=edge.kind,
            )
        )

    paths: list[LearningPath] = []
    for path_draft in draft.paths:
        path_id = path_ids.get(path_draft.id) or f"{curriculum_id}:{path_draft.id}"
        steps: list[LearningPathStep] = []
        for step in path_draft.steps:
            if step.target_type == PathTargetType.CHAPTER.value:
                target_id = (
                    chapter_ids.get(step.target_id)
                    or f"{curriculum_id}:{step.target_id}"
                )
            else:
                proposal = proposals.get(step.target_id)
                target_id = (
                    concept_ids.get(step.target_id)
                    or (proposal.matched_concept_id if proposal is not None else None)
                )
                if not target_id:
                    raise CurriculumError(
                        f"path step {step.id} targets proposal {step.target_id} with no "
                        "concept id: pass concept_ids[proposal_id] before activation"
                    )
            steps.append(
                LearningPathStep(
                    id=step.id or id_factory(),
                    path_id=path_id,
                    position=step.position,
                    target_type=step.target_type,
                    target_id=target_id,
                    stage=step.stage,
                    required=step.required,
                )
            )
        paths.append(
            LearningPath(
                id=path_id,
                curriculum_id=curriculum_id,
                title=path_draft.title,
                description=path_draft.description,
                kind=path_draft.kind,
                steps=tuple(steps),
            )
        )

    canonical = next((path for path in paths if path.is_canonical), None)
    if canonical is not None:
        curriculum = replace(curriculum, default_path_id=canonical.id)

    superseded = previous_active.superseded() if previous_active is not None else None
    return ActivateResult(
        curriculum=curriculum,
        chapters=tuple(chapters),
        chapter_concepts=tuple(links),
        prerequisites=tuple(prerequisites),
        paths=tuple(paths),
        superseded=superseded,
    )


def draft_from_curriculum(
    view: CurriculumView,
    *,
    draft_id: str,
    created_by: str = DraftOrigin.USER.value,
    created_at: str | None = None,
    title: str | None = None,
    concept_names: Mapping[str, str] | None = None,
) -> CurriculumDraft:
    """Derive the next Draft from an existing version (design §11.2).

    This is the only sanctioned way to edit an Active curriculum: derive a
    draft, edit it, confirm it, publish it as a new version. The source version
    is never mutated. Concept proposals carry ``matched_concept_id`` so that
    republishing reuses the same Concepts and keeps mastery/history intact.

    ``concept_names`` lets the caller (CF2, which may read the store) supply
    display names; without it a proposal's name falls back to its concept id —
    which is harmless for a re-publication, because the link is by
    ``matched_concept_id``, not by name.
    """
    curriculum = view.curriculum
    stamp = created_at or utc_now_iso()
    names = dict(concept_names or {})

    # One proposal per Concept, referenced by every chapter that teaches it:
    # proposal ids are derived from the concept id so prerequisite edges and
    # concept-targeted path steps resolve without an extra lookup table.
    def proposal_id_for(concept_id: str) -> str:
        return f"{draft_id}:c:{concept_id}"

    proposals: dict[str, ConceptProposal] = {}
    for link in view.chapter_concepts:
        proposals.setdefault(
            link.concept_id,
            ConceptProposal(
                proposal_id=proposal_id_for(link.concept_id),
                name=_text(names.get(link.concept_id)) or link.concept_id,
                matched_concept_id=link.concept_id,
            ),
        )

    chapter_drafts: list[ChapterDraft] = []
    for chapter in view.chapters_in_order:
        links = view.concepts_of_chapter(chapter.id)
        placements = tuple(
            ChapterConceptDraft(
                proposal=proposals[link.concept_id],
                position=link.position,
                role=link.role,
                required=link.required,
                difficulty_hint=link.difficulty_hint,
            )
            for link in links
        )
        chapter_drafts.append(
            ChapterDraft(
                id=f"{draft_id}:{chapter.id}",
                title=chapter.title,
                position=chapter.position,
                description=chapter.description,
                goals=chapter.goals,
                source_refs=chapter.source_refs,
                concepts=placements,
            )
        )

    path_drafts = tuple(
        LearningPathDraft(
            id=f"{draft_id}:{path.id}",
            title=path.title,
            description=path.description,
            kind=path.kind,
            steps=tuple(
                PathStepDraft(
                    id=f"{draft_id}:{step.id}",
                    position=step.position,
                    target_type=step.target_type,
                    target_id=(
                        f"{draft_id}:{step.target_id}"
                        if step.target_type == PathTargetType.CHAPTER.value
                        else proposal_id_for(step.target_id)
                    ),
                    stage=step.stage,
                    required=step.required,
                )
                for step in path.ordered_steps
            ),
        )
        for path in view.learning_paths
    )

    return CurriculumDraft(
        id=draft_id,
        course_id=curriculum.course_id,
        title=_text(title) or curriculum.title,
        description=curriculum.description,
        goals=curriculum.goals,
        based_on_curriculum_id=curriculum.id,
        chapters=tuple(chapter_drafts),
        paths=path_drafts,
        prerequisites=tuple(
            PrerequisiteDraft(
                concept_proposal_id=proposal_id_for(edge.concept_id),
                prerequisite_proposal_id=proposal_id_for(edge.prerequisite_concept_id),
                kind=edge.kind,
            )
            for edge in view.prerequisites
        ),
        sources=curriculum.sources,
        created_by=created_by,
        created_at=stamp,
        updated_at=stamp,
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
    # shared identity primitives (re-exported for convenience)
    "normalize_name",
]
