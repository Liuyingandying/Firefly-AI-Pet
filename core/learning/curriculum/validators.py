"""Curriculum structural validation (Phase 2-CF1).

Deterministic, pure-Python validation of the Curriculum domain model. The
validator never reads a database: anything it needs from outside the aggregate
(course membership of a concept, the set of existing courses) is injected, which
is also what keeps this layer importable without the Learning Store's SQLite
dependency.

Six frozen rule families (Phase 2-CF1 §九):

1. ``COURSE_ISOLATION``        chapters of course A never reference concepts of course B
2. ``POSITION_*``              positions are unique per scope (and contiguous where frozen)
3. ``ACTIVE_NOT_UNIQUE``       at most one ACTIVE curriculum per course
4. ``DRAFT_NOT_CONSUMABLE``    a Draft can never reach an Active-only consumer
5. ``PREREQUISITE_*``          no self-loop; ``required`` edges are acyclic
6. ``VERSION_*``               versions per course are unique and monotonic

Every check returns a :class:`ValidationReport`; ``report.raise_if_invalid()``
turns a failing report into a :class:`CurriculumValidationError`. Errors block
activation/publication; warnings flag authoring smells that do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.learning.curriculum.models import (
    ActivateResult,
    Chapter,
    ChapterConcept,
    ConceptPrerequisite,
    Curriculum,
    CurriculumDraft,
    CurriculumValidationError,
    CurriculumView,
    DraftOrigin,
    DraftStatus,
    PathTargetType,
    PrerequisiteKind,
    SourceKind,
    build_curriculum_from_draft,
    ensure_consumable,
    normalize_name,
    next_version,
)


# ---------------------------------------------------------------------------
# report primitives
# ---------------------------------------------------------------------------


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationCode(str, Enum):
    # rule 1 — course isolation
    COURSE_ISOLATION = "course_isolation"
    COURSE_MEMBER_UNKNOWN = "course_member_unknown"
    CURRICULUM_SCOPE_MISMATCH = "curriculum_scope_mismatch"
    COURSE_UNKNOWN = "course_unknown"
    # rule 2 — ordering
    POSITION_DUPLICATE = "position_duplicate"
    POSITION_NOT_CONTIGUOUS = "position_not_contiguous"
    POSITION_MISSING = "position_missing"
    # rule 3 — active uniqueness
    ACTIVE_NOT_UNIQUE = "active_not_unique"
    # rule 4 — draft protection
    DRAFT_NOT_CONSUMABLE = "draft_not_consumable"
    DRAFT_NOT_CONFIRMED = "draft_not_confirmed"
    # rule 5 — prerequisites
    PREREQUISITE_SELF_LOOP = "prerequisite_self_loop"
    PREREQUISITE_CYCLE = "prerequisite_cycle"
    PREREQUISITE_UNKNOWN = "prerequisite_unknown"
    PREREQUISITE_DUPLICATE = "prerequisite_duplicate"
    # rule 6 — versioning
    VERSION_DUPLICATE = "version_duplicate"
    VERSION_BASE_UNKNOWN = "version_base_unknown"
    VERSION_BASE_COURSE_MISMATCH = "version_base_course_mismatch"
    VERSION_NOT_MONOTONIC = "version_not_monotonic"
    VERSION_ACTIVE_NOT_LATEST = "version_active_not_latest"
    # structure
    TITLE_EMPTY = "title_empty"
    CHAPTER_EMPTY = "chapter_empty"
    PATH_REFERENCE_INVALID = "path_reference_invalid"
    PATH_CANONICAL_NOT_UNIQUE = "path_canonical_not_unique"
    DEFAULT_PATH_INVALID = "default_path_invalid"
    CHAPTER_CONCEPT_DUPLICATE = "chapter_concept_duplicate"
    CHAPTER_CONCEPT_PRIMARY_NOT_UNIQUE = "chapter_concept_primary_not_unique"
    CONCEPT_PROPOSAL_DUPLICATE = "concept_proposal_duplicate"
    CONCEPT_PROPOSAL_MISSING = "concept_proposal_missing"
    SOURCE_INCOMPLETE = "source_incomplete"
    SOURCE_GENERATOR_MISSING = "source_generator_missing"
    DRAFT_STATUS_INVALID = "draft_status_invalid"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One finding. ``subject`` names the offending id so reports are actionable."""

    code: str
    severity: str
    message: str
    subject: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "severity", _severity_value(self.severity)
        )

    @property
    def is_error(self) -> bool:
        return self.severity == ValidationSeverity.ERROR.value

    def __str__(self) -> str:
        where = f" [{self.subject}]" if self.subject else ""
        return f"{self.severity}:{self.code}{where} {self.message}"


def _severity_value(value: Any) -> str:
    if isinstance(value, ValidationSeverity):
        return value.value
    for member in ValidationSeverity:
        if str(value).lower() in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid severity: {value!r}")


def _code_value(value: Any) -> str:
    if isinstance(value, ValidationCode):
        return value.value
    return str(value)


def _issue(
    code: ValidationCode | str,
    message: str,
    *,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
    subject: str = "",
    **detail: Any,
) -> ValidationIssue:
    return ValidationIssue(
        code=_code_value(code),
        severity=severity.value,
        message=message,
        subject=subject,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregate result of one or more checks."""

    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

    # -- queries -----------------------------------------------------------

    @property
    def ok(self) -> bool:
        """True when no ERROR was found (warnings are allowed)."""
        return not any(issue.is_error for issue in self.issues)

    @property
    def is_clean(self) -> bool:
        """True when nothing at all was found."""
        return not self.issues

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.is_error)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if not issue.is_error)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def has(self, code: ValidationCode | str) -> bool:
        wanted = _code_value(code)
        return any(issue.code == wanted for issue in self.issues)

    def count(self, code: ValidationCode | str) -> int:
        wanted = _code_value(code)
        return sum(1 for issue in self.issues if issue.code == wanted)

    def error_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.errors)

    def merge(self, *others: "ValidationReport") -> "ValidationReport":
        merged: list[ValidationIssue] = list(self.issues)
        for other in others:
            merged.extend(other.issues)
        return ValidationReport(tuple(merged))

    def raise_if_invalid(self, context: str = "curriculum") -> "ValidationReport":
        if not self.ok:
            summary = "; ".join(str(issue) for issue in self.errors)
            raise CurriculumValidationError(
                f"{context} validation failed ({len(self.errors)} error(s)): {summary}",
                self.errors,
            )
        return self

    def summary(self) -> str:
        if self.is_clean:
            return "ok"
        return (
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s): "
            + ", ".join(sorted(set(self.codes)))
        )


# ---------------------------------------------------------------------------
# the validator
# ---------------------------------------------------------------------------


class CurriculumValidator:
    """Validates curriculum structure, drafts, and version sets (design §14).

    Injected context (keeps the layer pure — no store import):

    ``course_of_concept``
        ``concept_id -> course_id``. Course isolation cannot be proven without
        it, so an unknown concept is an error rather than a pass.
    ``known_course_ids``
        Courses that exist. When given, a draft for an unknown course is
        rejected — this is what stops a Draft from being activated after its
        Course was deleted (design §14.1).
    ``normalizer``
        Concept-name normaliser; defaults to the Learning Store's
        course-scoped identity, because "the same concept" must not have two
        definitions.
    """

    def __init__(
        self,
        *,
        course_of_concept: Mapping[str, str] | None = None,
        known_course_ids: Iterable[str] | None = None,
        normalizer: Callable[[str], str] = normalize_name,
    ) -> None:
        self._course_of_concept = dict(course_of_concept or {})
        self._known_course_ids = (
            frozenset(known_course_ids) if known_course_ids is not None else None
        )
        self._normalize = normalizer

    # -- context helpers ---------------------------------------------------

    def course_of(self, concept_id: str, mapping: Mapping[str, str] | None = None) -> str | None:
        source = self._course_of_concept if mapping is None else mapping
        return source.get(concept_id)

    def with_context(
        self,
        *,
        course_of_concept: Mapping[str, str] | None = None,
        known_course_ids: Iterable[str] | None = None,
    ) -> "CurriculumValidator":
        """A validator with extra (merged) injected context."""
        merged = dict(self._course_of_concept)
        merged.update(course_of_concept or {})
        known = self._known_course_ids
        if known_course_ids is not None:
            known = frozenset(set(known or ()) | set(known_course_ids))
        return CurriculumValidator(
            course_of_concept=merged, known_course_ids=known, normalizer=self._normalize
        )

    # -----------------------------------------------------------------
    # rule 1 — course isolation
    # -----------------------------------------------------------------

    def check_course_isolation(
        self,
        view: CurriculumView,
        *,
        course_of_concept: Mapping[str, str] | None = None,
    ) -> ValidationReport:
        """Every concept referenced by this version must belong to its course."""
        curriculum = view.curriculum
        issues: list[ValidationIssue] = []
        for chapter in view.chapters:
            if chapter.curriculum_id != curriculum.id:
                issues.append(
                    _issue(
                        ValidationCode.CURRICULUM_SCOPE_MISMATCH,
                        f"chapter belongs to curriculum {chapter.curriculum_id}, "
                        f"not {curriculum.id}",
                        subject=chapter.id,
                        expected=curriculum.id,
                        actual=chapter.curriculum_id,
                    )
                )
        concept_ids = {link.concept_id for link in view.chapter_concepts}
        concept_ids.update(
            edge.concept_id for edge in view.prerequisites
        )
        concept_ids.update(
            edge.prerequisite_concept_id for edge in view.prerequisites
        )
        concept_ids.update(
            step.target_id
            for path in view.learning_paths
            for step in path.ordered_steps
            if step.target_type == PathTargetType.CONCEPT.value
        )
        for concept_id in sorted(concept_ids):
            owner = self.course_of(concept_id, course_of_concept)
            if owner is None:
                issues.append(
                    _issue(
                        ValidationCode.COURSE_MEMBER_UNKNOWN,
                        f"concept {concept_id} has no known course: course isolation "
                        "cannot be proven",
                        subject=concept_id,
                        course_id=curriculum.course_id,
                    )
                )
            elif owner != curriculum.course_id:
                issues.append(
                    _issue(
                        ValidationCode.COURSE_ISOLATION,
                        f"concept {concept_id} belongs to course {owner}, but "
                        f"curriculum {curriculum.id} is course {curriculum.course_id}",
                        subject=concept_id,
                        concept_course=owner,
                        curriculum_course=curriculum.course_id,
                    )
                )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # rule 2 — position uniqueness
    # -----------------------------------------------------------------

    def check_chapter_positions(self, chapters: Iterable[Chapter]) -> ValidationReport:
        """Chapter positions are unique per curriculum, positive and contiguous.

        Duplicates are an error (two "chapter 2"s are ambiguous). Gaps are a
        warning: order is still well defined, and
        :func:`normalize_chapter_positions` can renumber them.
        """
        issues: list[ValidationIssue] = []
        grouped: dict[str, list[Chapter]] = {}
        for chapter in chapters:
            grouped.setdefault(chapter.curriculum_id, []).append(chapter)
        for curriculum_id in sorted(grouped):
            members = grouped[curriculum_id]
            seen: dict[int, str] = {}
            for chapter in sorted(members, key=lambda c: (c.position, c.id)):
                if chapter.position < 1:
                    issues.append(
                        _issue(
                            ValidationCode.POSITION_MISSING,
                            f"chapter position must be >= 1 (got {chapter.position})",
                            subject=chapter.id,
                            position=chapter.position,
                        )
                    )
                previous = seen.get(chapter.position)
                if previous is not None:
                    issues.append(
                        _issue(
                            ValidationCode.POSITION_DUPLICATE,
                            f"chapters {previous} and {chapter.id} share position "
                            f"{chapter.position}",
                            subject=chapter.id,
                            position=chapter.position,
                            conflicts_with=previous,
                        )
                    )
                else:
                    seen[chapter.position] = chapter.id
            self._check_contiguity(
                seen,
                scope=f"curriculum {curriculum_id} chapters",
                subject=curriculum_id,
                contiguous_is_error=False,
                issues=issues,
            )
        return ValidationReport(tuple(issues))

    def check_path_step_positions(self, path) -> ValidationReport:
        """Path step positions are unique AND contiguous (design §9.2).

        A gap here is an error, unlike chapter gaps: a path is a sequence, so
        "step 1, step 3" means a step was lost.
        """
        issues: list[ValidationIssue] = []
        seen: dict[int, str] = {}
        for step in path.ordered_steps:
            previous = seen.get(step.position)
            if previous is not None:
                issues.append(
                    _issue(
                        ValidationCode.POSITION_DUPLICATE,
                        f"path steps {previous} and {step.id} share position "
                        f"{step.position}",
                        subject=path.id,
                        position=step.position,
                        conflicts_with=previous,
                    )
                )
            else:
                seen[step.position] = step.id
        self._check_contiguity(
            seen,
            scope=f"path {path.id} steps",
            subject=path.id,
            contiguous_is_error=True,
            issues=issues,
        )
        return ValidationReport(tuple(issues))

    def check_chapter_concept_positions(
        self, chapter_id: str, links: Sequence[ChapterConcept]
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        seen: dict[int, str] = {}
        for link in links:
            other = seen.get(link.position)
            if other is not None:
                issues.append(
                    _issue(
                        ValidationCode.POSITION_DUPLICATE,
                        f"concepts {other} and {link.concept_id} share position "
                        f"{link.position} in chapter {chapter_id}",
                        subject=chapter_id,
                        position=link.position,
                        conflicts_with=other,
                    )
                )
            else:
                seen[link.position] = link.concept_id
        return ValidationReport(tuple(issues))

    @staticmethod
    def _check_contiguity(
        seen: Mapping[int, str],
        *,
        scope: str,
        subject: str,
        contiguous_is_error: bool,
        issues: list[ValidationIssue],
    ) -> None:
        positions = sorted(seen)
        if not positions:
            return
        expected = list(range(1, len(positions) + 1))
        if positions != expected:
            issues.append(
                _issue(
                    ValidationCode.POSITION_NOT_CONTIGUOUS,
                    f"{scope} positions {positions} are not contiguous 1..{len(positions)}",
                    severity=(
                        ValidationSeverity.ERROR
                        if contiguous_is_error
                        else ValidationSeverity.WARNING
                    ),
                    subject=subject,
                    positions=tuple(positions),
                )
            )

    # -----------------------------------------------------------------
    # rule 3 — active uniqueness
    # -----------------------------------------------------------------

    def check_active_uniqueness(self, curricula: Iterable[Curriculum]) -> ValidationReport:
        """At most one ACTIVE curriculum per course (design §5)."""
        issues: list[ValidationIssue] = []
        grouped: dict[str, list[Curriculum]] = {}
        for curriculum in curricula:
            if curriculum.is_active:
                grouped.setdefault(curriculum.course_id, []).append(curriculum)
        for course_id in sorted(grouped):
            actives = sorted(grouped[course_id], key=lambda c: (c.version, c.id))
            if len(actives) > 1:
                issues.append(
                    _issue(
                        ValidationCode.ACTIVE_NOT_UNIQUE,
                        f"course {course_id} has {len(actives)} active curricula "
                        f"({', '.join(c.id for c in actives)}); at most one may be active",
                        subject=course_id,
                        curriculum_ids=tuple(c.id for c in actives),
                        versions=tuple(c.version for c in actives),
                    )
                )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # rule 4 — draft protection
    # -----------------------------------------------------------------

    def check_draft_protection(self, subject: Any) -> ValidationReport:
        """A Draft (or a non-active version) may never reach an Active consumer."""
        try:
            ensure_consumable(subject)
        except Exception as exc:  # noqa: BLE001 - surfaced as a validation issue
            return ValidationReport(
                (
                    _issue(
                        ValidationCode.DRAFT_NOT_CONSUMABLE,
                        str(exc),
                        subject=getattr(subject, "id", ""),
                        subject_type=type(subject).__name__,
                    ),
                )
            )
        return ValidationReport()

    def check_draft_confirmable(self, draft: CurriculumDraft) -> ValidationReport:
        """Whether the draft is in a state that may be confirmed."""
        issues: list[ValidationIssue] = []
        if draft.status != DraftStatus.DRAFT.value:
            issues.append(
                _issue(
                    ValidationCode.DRAFT_STATUS_INVALID,
                    f"draft is {draft.status}; only a {DraftStatus.DRAFT.value} draft "
                    "can be confirmed",
                    subject=draft.id,
                    status=draft.status,
                )
            )
        if draft.created_by in {DraftOrigin.AI.value, DraftOrigin.PAGELENS.value}:
            issues.append(
                _issue(
                    ValidationCode.DRAFT_NOT_CONFIRMED,
                    f"draft originated from {draft.created_by}: it may be proposed, "
                    "edited and confirmed, but the confirmation event must come from "
                    "an explicit user action",
                    severity=ValidationSeverity.WARNING,
                    subject=draft.id,
                    created_by=draft.created_by,
                )
            )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # rule 5 — prerequisite cycles
    # -----------------------------------------------------------------

    def check_prerequisite_cycles(
        self, prerequisites: Iterable[ConceptPrerequisite]
    ) -> ValidationReport:
        """Self-loops are always errors; a cycle containing a ``required`` edge
        is an error; a cycle made only of ``recommended`` edges is a warning
        (mutual recommendations can be legitimate)."""
        edges = tuple(prerequisites)
        issues: list[ValidationIssue] = []
        all_edges: dict[str, set[str]] = {}
        required_pairs: set[tuple[str, str]] = set()
        for edge in edges:
            head, tail = edge.edge
            if head == tail:
                issues.append(
                    _issue(
                        ValidationCode.PREREQUISITE_SELF_LOOP,
                        f"concept {head} is its own prerequisite",
                        subject=head,
                        kind=edge.kind,
                    )
                )
                continue
            all_edges.setdefault(head, set()).add(tail)
            if edge.is_required:
                required_pairs.add((head, tail))

        cycle = _find_cycle(all_edges)
        if cycle:
            pairs = list(zip(cycle, cycle[1:]))
            blocking = any(pair in required_pairs for pair in pairs)
            issues.append(
                _issue(
                    ValidationCode.PREREQUISITE_CYCLE,
                    ("required " if blocking else "recommended ")
                    + "prerequisite cycle: "
                    + " -> ".join(cycle),
                    severity=(
                        ValidationSeverity.ERROR
                        if blocking
                        else ValidationSeverity.WARNING
                    ),
                    subject=cycle[0],
                    cycle=tuple(cycle),
                    kind=(
                        PrerequisiteKind.REQUIRED.value
                        if blocking
                        else PrerequisiteKind.RECOMMENDED.value
                    ),
                )
            )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # rule 6 — version consistency
    # -----------------------------------------------------------------

    def check_version_consistency(self, curricula: Iterable[Curriculum]) -> ValidationReport:
        """(course, version) is unique; ``based_on`` is same-course and older."""
        issues: list[ValidationIssue] = []
        items = tuple(curricula)
        by_id = {curriculum.id: curriculum for curriculum in items}
        seen: dict[tuple[str, int], str] = {}
        for curriculum in sorted(items, key=lambda c: (c.course_id, c.version, c.id)):
            key = (curriculum.course_id, curriculum.version)
            previous = seen.get(key)
            if previous is not None:
                issues.append(
                    _issue(
                        ValidationCode.VERSION_DUPLICATE,
                        f"course {curriculum.course_id} has two curricula at version "
                        f"{curriculum.version} ({previous} / {curriculum.id})",
                        subject=curriculum.id,
                        version=curriculum.version,
                        conflicts_with=previous,
                    )
                )
            else:
                seen[key] = curriculum.id

            base_id = curriculum.based_on_curriculum_id
            if base_id:
                base = by_id.get(base_id)
                if base is None:
                    issues.append(
                        _issue(
                            ValidationCode.VERSION_BASE_UNKNOWN,
                            f"based_on curriculum {base_id} does not exist",
                            subject=curriculum.id,
                            based_on=base_id,
                        )
                    )
                else:
                    if base.course_id != curriculum.course_id:
                        issues.append(
                            _issue(
                                ValidationCode.VERSION_BASE_COURSE_MISMATCH,
                                f"based_on {base_id} belongs to course {base.course_id}, "
                                f"not {curriculum.course_id}",
                                subject=curriculum.id,
                                based_on=base_id,
                            )
                        )
                    if base.version >= curriculum.version:
                        issues.append(
                            _issue(
                                ValidationCode.VERSION_NOT_MONOTONIC,
                                f"version {curriculum.version} must be greater than its "
                                f"base version {base.version}",
                                subject=curriculum.id,
                                version=curriculum.version,
                                base_version=base.version,
                            )
                        )

        grouped: dict[str, list[Curriculum]] = {}
        for curriculum in items:
            grouped.setdefault(curriculum.course_id, []).append(curriculum)
        for course_id in sorted(grouped):
            members = grouped[course_id]
            latest = max(member.version for member in members)
            for member in members:
                if member.is_active and member.version != latest:
                    issues.append(
                        _issue(
                            ValidationCode.VERSION_ACTIVE_NOT_LATEST,
                            f"active curriculum {member.id} is version "
                            f"{member.version}, but version {latest} exists for this course",
                            severity=ValidationSeverity.WARNING,
                            subject=member.id,
                            version=member.version,
                            latest_version=latest,
                        )
                    )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # aggregated: published structure
    # -----------------------------------------------------------------

    def validate_curriculum(
        self,
        view: CurriculumView,
        *,
        course_of_concept: Mapping[str, str] | None = None,
    ) -> ValidationReport:
        """Structural validation of one published version (no lifecycle gate)."""
        curriculum = view.curriculum
        issues: list[ValidationIssue] = []
        if not curriculum.title:
            issues.append(
                _issue(ValidationCode.TITLE_EMPTY, "curriculum title is empty",
                       subject=curriculum.id)
            )
        if not view.chapters:
            issues.append(
                _issue(
                    ValidationCode.CHAPTER_EMPTY,
                    "curriculum has no chapters",
                    severity=ValidationSeverity.WARNING,
                    subject=curriculum.id,
                )
            )

        report = ValidationReport(tuple(issues))
        report = report.merge(
            self.check_course_isolation(view, course_of_concept=course_of_concept),
            self.check_chapter_positions(view.chapters),
            self.check_prerequisite_cycles(view.prerequisites),
        )

        # chapter titles + chapter concept placement
        for chapter in view.chapters_in_order:
            if not chapter.has_title:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.TITLE_EMPTY,
                                "chapter title is empty",
                                subject=chapter.id,
                            ),
                        )
                    )
                )
            links = view.concepts_of_chapter(chapter.id)
            report = report.merge(
                self.check_chapter_concept_positions(chapter.id, links)
            )
            keys: dict[tuple[str, str], ChapterConcept] = {}
            for link in links:
                if link.key in keys:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.CHAPTER_CONCEPT_DUPLICATE,
                                    f"concept {link.concept_id} appears twice in "
                                    f"chapter {chapter.id}",
                                    subject=chapter.id,
                                    concept_id=link.concept_id,
                                ),
                            )
                        )
                    )
                else:
                    keys[link.key] = link

        # at most one PRIMARY placement per concept within the version
        primaries: dict[str, list[str]] = {}
        for link in view.chapter_concepts:
            if link.is_primary:
                primaries.setdefault(link.concept_id, []).append(link.chapter_id)
        for concept_id in sorted(primaries):
            chapters = primaries[concept_id]
            if len(chapters) > 1:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.CHAPTER_CONCEPT_PRIMARY_NOT_UNIQUE,
                                f"concept {concept_id} is primary in {len(chapters)} "
                                f"chapters ({', '.join(chapters)}); keep one primary and "
                                "mark the rest supporting/reference",
                                subject=concept_id,
                                chapters=tuple(chapters),
                            ),
                        )
                    )
                )

        # paths
        canonical = [path for path in view.learning_paths if path.is_canonical]
        if len(canonical) > 1:
            report = report.merge(
                ValidationReport(
                    (
                        _issue(
                            ValidationCode.PATH_CANONICAL_NOT_UNIQUE,
                            f"{len(canonical)} canonical paths exist; a curriculum has at "
                            "most one default route",
                            subject=curriculum.id,
                            path_ids=tuple(path.id for path in canonical),
                        ),
                    )
                )
            )
        chapter_ids = {chapter.id for chapter in view.chapters}
        concept_ids = {link.concept_id for link in view.chapter_concepts}
        for path in view.learning_paths:
            if path.curriculum_id != curriculum.id:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.CURRICULUM_SCOPE_MISMATCH,
                                f"path belongs to curriculum {path.curriculum_id}, "
                                f"not {curriculum.id}",
                                subject=path.id,
                            ),
                        )
                    )
                )
            for step in path.ordered_steps:
                if step.target_type == PathTargetType.CHAPTER.value:
                    valid = step.target_id in chapter_ids
                else:
                    valid = step.target_id in concept_ids
                if not valid:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.PATH_REFERENCE_INVALID,
                                    f"path step {step.id} targets {step.target_type} "
                                    f"{step.target_id} which is not part of this version",
                                    subject=path.id,
                                    target_id=step.target_id,
                                ),
                            )
                        )
                    )
            report = report.merge(self.check_path_step_positions(path))

        if curriculum.default_path_id:
            if not any(
                path.id == curriculum.default_path_id for path in view.learning_paths
            ):
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.DEFAULT_PATH_INVALID,
                                f"default_path_id {curriculum.default_path_id} is not a "
                                "path of this curriculum",
                                subject=curriculum.id,
                            ),
                        )
                    )
                )

        # prerequisite endpoints must be part of this version
        for edge in view.prerequisites:
            if edge.curriculum_id != curriculum.id:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.CURRICULUM_SCOPE_MISMATCH,
                                f"prerequisite belongs to curriculum {edge.curriculum_id}, "
                                f"not {curriculum.id}",
                                subject=edge.concept_id,
                            ),
                        )
                    )
                )
            for endpoint in (edge.concept_id, edge.prerequisite_concept_id):
                if endpoint not in concept_ids:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.PREREQUISITE_UNKNOWN,
                                    f"prerequisite endpoint {endpoint} is not taught by "
                                    "this version",
                                    subject=edge.concept_id,
                                    endpoint=endpoint,
                                ),
                            )
                        )
                    )
        seen_edges: dict[tuple[str, str], str] = {}
        for edge in view.prerequisites:
            if edge.edge in seen_edges:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.PREREQUISITE_DUPLICATE,
                                f"duplicate prerequisite edge {edge.edge[0]} -> "
                                f"{edge.edge[1]}",
                                subject=edge.concept_id,
                            ),
                        )
                    )
                )
            else:
                seen_edges[edge.edge] = edge.kind
        return report

    # -----------------------------------------------------------------
    # aggregated: draft / confirm gate
    # -----------------------------------------------------------------

    def validate_draft(
        self,
        draft: CurriculumDraft,
        *,
        course_of_concept: Mapping[str, str] | None = None,
    ) -> ValidationReport:
        """The confirm gate (design §11.2).

        A passing report means the draft may be confirmed and published; it does
        NOT confirm it. Confirmation itself requires the explicit user event
        (:meth:`CurriculumDraft.confirm`).
        """
        issues: list[ValidationIssue] = []
        if not draft.title:
            issues.append(
                _issue(ValidationCode.TITLE_EMPTY, "draft title is empty", subject=draft.id)
            )
        if self._known_course_ids is not None and draft.course_id not in self._known_course_ids:
            issues.append(
                _issue(
                    ValidationCode.COURSE_UNKNOWN,
                    f"course {draft.course_id} does not exist; an orphan draft must never "
                    "be activated",
                    subject=draft.id,
                    course_id=draft.course_id,
                )
            )
        if not draft.chapters:
            issues.append(
                _issue(
                    ValidationCode.CHAPTER_EMPTY,
                    "draft has no chapters",
                    subject=draft.id,
                )
            )
        if not draft.concept_candidates:
            issues.append(
                _issue(
                    ValidationCode.CONCEPT_PROPOSAL_MISSING,
                    "draft proposes no concepts",
                    subject=draft.id,
                )
            )

        report = ValidationReport(tuple(issues)).merge(
            self.check_draft_confirmable(draft)
        )

        # chapters: titles, order, placements, per-chapter duplicate proposals
        seen_positions: dict[int, str] = {}
        for chapter in draft.chapters_in_order:
            if not str(chapter.title or "").strip():
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.TITLE_EMPTY,
                                "draft chapter title is empty",
                                subject=chapter.id,
                            ),
                        )
                    )
                )
            previous = seen_positions.get(chapter.position)
            if previous is not None:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.POSITION_DUPLICATE,
                                f"draft chapters {previous} and {chapter.id} share "
                                f"position {chapter.position}",
                                subject=chapter.id,
                                position=chapter.position,
                            ),
                        )
                    )
                )
            else:
                seen_positions[chapter.position] = chapter.id

            by_name: dict[str, str] = {}
            for link in chapter.ordered_concepts:
                name = self._normalize(link.name)
                if not name:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.CONCEPT_PROPOSAL_MISSING,
                                    "concept proposal has an empty name",
                                    subject=chapter.id,
                                    proposal_id=link.proposal_id,
                                ),
                            )
                        )
                    )
                    continue
                if name in by_name:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.CONCEPT_PROPOSAL_DUPLICATE,
                                    f"chapter {chapter.id} proposes {link.name!r} twice "
                                    "(course-scoped normalised duplicate)",
                                    subject=chapter.id,
                                    normalized_name=name,
                                ),
                            )
                        )
                    )
                else:
                    by_name[name] = link.proposal_id

        contiguous: list[ValidationIssue] = list(report.issues)
        self._check_contiguity(
            seen_positions,
            scope=f"draft {draft.id} chapters",
            subject=draft.id,
            contiguous_is_error=False,
            issues=contiguous,
        )
        report = ValidationReport(tuple(contiguous))

        # at most one PRIMARY placement per proposed concept
        primaries: dict[str, list[str]] = {}
        for link in draft.concept_links:
            if link.is_primary:
                primaries.setdefault(self._normalize(link.name), []).append(
                    link.proposal_id
                )
        for name in sorted(primaries):
            if len(primaries[name]) > 1:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.CHAPTER_CONCEPT_PRIMARY_NOT_UNIQUE,
                                f"proposed concept {name!r} is primary in "
                                f"{len(primaries[name])} chapters; keep one primary",
                                subject=draft.id,
                                normalized_name=name,
                            ),
                        )
                    )
                )

        # paths: canonical uniqueness, references, positions
        canonical = [path for path in draft.paths if path.is_canonical]
        if len(canonical) > 1:
            report = report.merge(
                ValidationReport(
                    (
                        _issue(
                            ValidationCode.PATH_CANONICAL_NOT_UNIQUE,
                            f"draft has {len(canonical)} canonical paths",
                            subject=draft.id,
                        ),
                    )
                )
            )
        chapter_draft_ids = {chapter.id for chapter in draft.chapters}
        proposal_ids = {link.proposal_id for link in draft.concept_links}
        for path in draft.paths:
            for step in path.steps:
                valid = (
                    step.target_id in chapter_draft_ids
                    if step.target_type == PathTargetType.CHAPTER.value
                    else step.target_id in proposal_ids
                )
                if not valid:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.PATH_REFERENCE_INVALID,
                                    f"draft path step {step.id} targets {step.target_type} "
                                    f"{step.target_id} which is not in the draft",
                                    subject=path.id,
                                    target_id=step.target_id,
                                ),
                            )
                        )
                    )
            positions = sorted(step.position for step in path.steps)
            if positions and positions != list(range(1, len(positions) + 1)):
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.POSITION_NOT_CONTIGUOUS,
                                f"draft path {path.id} step positions {positions} are not "
                                f"contiguous 1..{len(positions)}",
                                subject=path.id,
                                positions=tuple(positions),
                            ),
                        )
                    )
                )
            duplicates = _duplicates(positions)
            for position in duplicates:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.POSITION_DUPLICATE,
                                f"draft path {path.id} has two steps at position {position}",
                                subject=path.id,
                                position=position,
                            ),
                        )
                    )
                )

        # prerequisites: endpoints exist, self loop, cycles
        draft_edges: list[ConceptPrerequisite] = []
        for edge in draft.prerequisites:
            if (
                edge.concept_proposal_id not in proposal_ids
                or edge.prerequisite_proposal_id not in proposal_ids
            ):
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.PREREQUISITE_UNKNOWN,
                                "prerequisite references a proposal that is not in the draft",
                                subject=edge.concept_proposal_id,
                                prerequisite=edge.prerequisite_proposal_id,
                            ),
                        )
                    )
                )
                continue
            if edge.concept_proposal_id == edge.prerequisite_proposal_id:
                report = report.merge(
                    ValidationReport(
                        (
                            _issue(
                                ValidationCode.PREREQUISITE_SELF_LOOP,
                                f"proposal {edge.concept_proposal_id} is its own prerequisite",
                                subject=edge.concept_proposal_id,
                                kind=edge.kind,
                            ),
                        )
                    )
                )
                continue
            draft_edges.append(
                ConceptPrerequisite(
                    curriculum_id=draft.id,
                    concept_id=edge.concept_proposal_id,
                    prerequisite_concept_id=edge.prerequisite_proposal_id,
                    kind=edge.kind,
                )
            )
        report = report.merge(self.check_prerequisite_cycles(draft_edges))

        # provenance completeness
        report = report.merge(self.check_sources(draft))

        # concept isolation for proposals that reuse an existing Concept
        effective_course_of_concept = (
            course_of_concept if course_of_concept is not None else self._course_of_concept
        )
        if effective_course_of_concept:
            for link in draft.concept_links:
                matched = link.proposal.matched_concept_id
                if not matched:
                    continue
                owner = self.course_of(matched, effective_course_of_concept)
                if owner is not None and owner != draft.course_id:
                    report = report.merge(
                        ValidationReport(
                            (
                                _issue(
                                    ValidationCode.COURSE_ISOLATION,
                                    f"proposal {link.name!r} reuses concept {matched} of "
                                    f"course {owner}, but the draft is course "
                                    f"{draft.course_id}",
                                    subject=matched,
                                    concept_course=owner,
                                    draft_course=draft.course_id,
                                ),
                            )
                        )
                    )
        return report

    def check_sources(self, draft: CurriculumDraft) -> ValidationReport:
        """Provenance completeness by source kind (design §10.2)."""
        issues: list[ValidationIssue] = []
        for source in draft.sources:
            if not source.title:
                issues.append(
                    _issue(
                        ValidationCode.SOURCE_INCOMPLETE,
                        "curriculum source has no title",
                        subject=draft.id,
                        kind=source.kind,
                    )
                )
            if source.kind == SourceKind.AI_GENERATED.value and not _text(
                source.generated_by
            ):
                issues.append(
                    _issue(
                        ValidationCode.SOURCE_GENERATOR_MISSING,
                        "AI-generated source must record which generator produced it",
                        subject=draft.id,
                    )
                )
            if source.kind in (SourceKind.PDF.value, SourceKind.INSTITUTIONAL.value):
                if not source.is_traceable:
                    issues.append(
                        _issue(
                            ValidationCode.SOURCE_INCOMPLETE,
                            f"{source.kind} source needs a locator or a content "
                            "fingerprint to stay auditable",
                            severity=ValidationSeverity.WARNING,
                            subject=draft.id,
                            kind=source.kind,
                        )
                    )
        if draft.created_by in (DraftOrigin.AI.value, DraftOrigin.PAGELENS.value) and not draft.sources:
            issues.append(
                _issue(
                    ValidationCode.SOURCE_INCOMPLETE,
                    f"a {draft.created_by}-originated draft must carry provenance",
                    subject=draft.id,
                )
            )
        return ValidationReport(tuple(issues))

    # -----------------------------------------------------------------
    # aggregated: cross-version set
    # -----------------------------------------------------------------

    def validate_set(self, curricula: Iterable[Curriculum]) -> ValidationReport:
        """Cross-version rules for one or more courses (rules 3 + 6)."""
        items = tuple(curricula)
        return self.check_active_uniqueness(items).merge(
            self.check_version_consistency(items)
        )

    # -----------------------------------------------------------------
    # aggregated: consumption gate
    # -----------------------------------------------------------------

    def validate_consumption(self, subject: Any) -> ValidationReport:
        """Report whether ``subject`` may be consumed as active structure."""
        return self.check_draft_protection(subject)

    # -----------------------------------------------------------------
    # publishing helper (validate -> confirm -> build)
    # -----------------------------------------------------------------

    def confirm_and_build(
        self,
        draft: CurriculumDraft,
        *,
        curriculum_id: str,
        concept_ids: Mapping[str, str],
        existing: Iterable[Curriculum] = (),
        previous_active: Curriculum | None = None,
        confirmed_by: str = "user",
        chapter_ids: Mapping[str, str] | None = None,
        path_ids: Mapping[str, str] | None = None,
        activated_at: str | None = None,
        course_of_concept: Mapping[str, str] | None = None,
    ) -> ActivateResult:
        """Validate, confirm and build the next ACTIVE version, all in memory.

        Order matters and is enforced: validation runs against the DRAFT, the
        version is allocated by the system, the previous active version comes
        back as ``superseded``, and the draft must be confirmed by a user actor.
        The caller (CF2) persists the result inside one transaction.
        """
        items = tuple(existing)
        self.validate_draft(draft, course_of_concept=course_of_concept).raise_if_invalid(
            f"draft {draft.id}"
        )
        if previous_active is None:
            previous_active = _active_of(draft.course_id, items)
        elif not previous_active.is_active:
            previous_active = None
        version = next_version(items, draft.course_id)
        confirmed = draft.confirm(confirmed_by=confirmed_by)
        result = build_curriculum_from_draft(
            confirmed,
            curriculum_id=curriculum_id,
            version=version,
            concept_ids=concept_ids,
            chapter_ids=chapter_ids,
            path_ids=path_ids,
            previous_active=previous_active,
            activated_at=activated_at,
        )
        self.validate_curriculum(
            result.view(), course_of_concept=course_of_concept
        ).raise_if_invalid(f"curriculum {curriculum_id}")
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value or "").strip()


def _duplicates(values: Sequence[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    repeated: list[int] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return tuple(sorted(repeated))


def _find_cycle(edges: Mapping[str, set[str]]) -> list[str]:
    """Deterministic cycle search over ``edges`` (``a -> b``).

    Returns the cycle as a path whose first node repeats at the end, or an empty
    list. Iterative DFS so deep curricula cannot blow the Python stack.
    """
    nodes = sorted(set(edges) | {tail for tails in edges.values() for tail in tails})
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in nodes}
    for root in nodes:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, list[str]]] = [(root, sorted(edges.get(root, ())))]
        path: list[str] = [root]
        color[root] = GREY
        while stack:
            node, remaining = stack[-1]
            if not remaining:
                stack.pop()
                path.pop()
                color[node] = BLACK
                continue
            nxt = remaining.pop(0)
            if color.get(nxt, WHITE) == GREY:
                start = path.index(nxt)
                return path[start:] + [nxt]
            if color.get(nxt, WHITE) == BLACK:
                continue
            color[nxt] = GREY
            path.append(nxt)
            stack.append((nxt, sorted(edges.get(nxt, ()))))
    return []


def _active_of(course_id: str, curricula: Iterable[Curriculum]) -> Curriculum | None:
    for curriculum in curricula:
        if curriculum.course_id == course_id and curriculum.is_active:
            return curriculum
    return None


# ---------------------------------------------------------------------------
# module-level convenience
# ---------------------------------------------------------------------------


def validate_curriculum(view: CurriculumView, **kwargs: Any) -> ValidationReport:
    return CurriculumValidator(**kwargs).validate_curriculum(view)


def validate_draft(draft: CurriculumDraft, **kwargs: Any) -> ValidationReport:
    return CurriculumValidator(**kwargs).validate_draft(draft)


def validate_set(curricula: Iterable[Curriculum], **kwargs: Any) -> ValidationReport:
    return CurriculumValidator(**kwargs).validate_set(curricula)


__all__ = [
    "ValidationSeverity",
    "ValidationCode",
    "ValidationIssue",
    "ValidationReport",
    "CurriculumValidator",
    "validate_curriculum",
    "validate_draft",
    "validate_set",
]
