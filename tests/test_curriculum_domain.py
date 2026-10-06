"""Curriculum domain model + validator tests (Phase 2-CF1).

Covers the frozen contract (design §5-§11, CF1 §九):

    创建 Curriculum / Draft→Active 转换 / Chapter 排序 / ChapterConcept 关联 /
    Course isolation / Active 唯一 / version 冲突 / prerequisite 环检测 /
    Draft 禁止进入学习上下文

plus the six validator rule families and the purity constraints of the layer
(no store, no sqlite, no Qt, no provider, no UI).

Everything here is in memory: no Qt, no provider, no UI, no database, no
filesystem beyond reading this package's own source for the purity assertions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.learning.curriculum import (
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
    CurriculumSource,
    CurriculumStatus,
    CurriculumValidationError,
    CurriculumValidator,
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
    validate_draft,
    validate_set,
)


CURRICULUM_PKG = Path(__file__).resolve().parents[1] / "core" / "learning" / "curriculum"

# The course every test fixture belongs to.
COURSE = "course-ctrl"
OTHER_COURSE = "course-optics"

# concept_id -> course_id, injected so isolation can be proven.
COURSE_OF_CONCEPT = {
    "K-intro": COURSE,
    "K-laplace": COURSE,
    "K-transfer": COURSE,
    "K-freq": COURSE,
    "K-foreign": OTHER_COURSE,
}


def proposal(pid: str, name: str, *, matched: str | None = None) -> ConceptProposal:
    return ConceptProposal(proposal_id=pid, name=name, matched_concept_id=matched)


def manual_source(**kwargs) -> CurriculumSource:
    payload = {"kind": SourceKind.MANUAL.value, "title": "手工录入"}
    payload.update(kwargs)
    return CurriculumSource(**payload)


def chapter_draft(
    cid: str,
    title: str,
    position: int,
    *concepts: ChapterConceptDraft,
) -> ChapterDraft:
    return ChapterDraft(id=cid, title=title, position=position, concepts=tuple(concepts))


def placement(
    pid: str,
    position: int,
    *,
    name: str | None = None,
    matched: str | None = None,
    role: str = ChapterConceptRole.PRIMARY.value,
) -> ChapterConceptDraft:
    return ChapterConceptDraft(
        proposal=proposal(pid, name or pid, matched=matched), position=position, role=role
    )


def canonical_path(*chapter_ids: str, path_id: str = "path-main") -> LearningPathDraft:
    return LearningPathDraft(
        id=path_id,
        title="默认路线",
        steps=tuple(
            PathStepDraft(
                id=f"{path_id}-s{index}",
                position=index,
                target_type=PathTargetType.CHAPTER.value,
                target_id=chapter_id,
            )
            for index, chapter_id in enumerate(chapter_ids, start=1)
        ),
    )


def make_draft(
    *,
    draft_id: str = "draft-1",
    course_id: str = COURSE,
    title: str = "自动控制原理",
    chapters=None,
    paths=None,
    prerequisites=(),
    sources=(),
    created_by: str = DraftOrigin.USER.value,
) -> CurriculumDraft:
    if chapters is None:
        chapters = (
            chapter_draft(
                "d-ch1", "绪论", 1,
                placement("p-intro", 1, name="自动控制概述", matched="K-intro"),
            ),
            chapter_draft(
                "d-ch2", "数学模型", 2,
                placement("p-laplace", 1, name="拉普拉斯变换", matched="K-laplace"),
                placement("p-transfer", 2, name="传递函数", matched="K-transfer"),
            ),
        )
    if paths is None:
        paths = (canonical_path("d-ch1", "d-ch2"),)
    if not sources:
        sources = (manual_source(),)
    return CurriculumDraft(
        id=draft_id,
        course_id=course_id,
        title=title,
        chapters=tuple(chapters),
        paths=tuple(paths),
        prerequisites=tuple(prerequisites),
        sources=tuple(sources),
        created_by=created_by,
    )


def make_validator(**kwargs) -> CurriculumValidator:
    payload = {"course_of_concept": COURSE_OF_CONCEPT, "known_course_ids": {COURSE, OTHER_COURSE}}
    payload.update(kwargs)
    return CurriculumValidator(**payload)


# ---------------------------------------------------------------------------
# 1. creating a Curriculum
# ---------------------------------------------------------------------------


def test_curriculum_requires_id_course_and_version() -> None:
    curriculum = Curriculum(id="cur1", course_id=COURSE, title="自动控制原理", version=1)
    assert curriculum.is_active
    assert curriculum.status == CurriculumStatus.ACTIVE.value
    assert curriculum.version == 1
    with pytest.raises(Exception):
        Curriculum(id="", course_id=COURSE, title="x", version=1)
    with pytest.raises(Exception):
        Curriculum(id="cur2", course_id="", title="x", version=1)
    with pytest.raises(Exception):
        Curriculum(id="cur3", course_id=COURSE, title="x", version=0)


def test_curriculum_is_immutable() -> None:
    curriculum = Curriculum(id="cur1", course_id=COURSE, title="自动控制原理", version=1)
    with pytest.raises(Exception):
        curriculum.status = CurriculumStatus.ARCHIVED.value  # type: ignore[misc]
    # Lifecycle transitions return copies; the original is untouched.
    superseded = curriculum.superseded()
    assert superseded.status == CurriculumStatus.SUPERSEDED.value
    assert curriculum.is_active
    assert superseded.id == curriculum.id


def test_invalid_status_and_source_kind_are_rejected() -> None:
    with pytest.raises(Exception):
        Curriculum(id="c", course_id=COURSE, title="x", version=1, status="draft")
    with pytest.raises(Exception):
        CurriculumSource(kind="not-a-kind", title="x")


def test_curriculum_source_provenance_traceability() -> None:
    pdf = CurriculumSource(kind=SourceKind.PDF.value, title="教材", locator="doc-1#p12")
    assert pdf.is_traceable
    ai = CurriculumSource(
        kind=SourceKind.AI_GENERATED.value, title="AI 草稿", generated_by="qwen"
    )
    assert ai.generated_by == "qwen"
    assert not CurriculumSource(kind=SourceKind.PDF.value, title="无出处").is_traceable


def test_one_curriculum_belongs_to_one_course() -> None:
    curriculum = Curriculum(id="cur1", course_id=COURSE, title="t", version=1)
    assert curriculum.course_id == COURSE
    view = CurriculumView.of(curriculum)
    assert view.course_id == COURSE


# ---------------------------------------------------------------------------
# 2. Draft -> Active transition
# ---------------------------------------------------------------------------


def test_draft_is_not_a_curriculum() -> None:
    draft = make_draft()
    assert not isinstance(draft, Curriculum)
    assert draft.is_consumable is False
    assert not hasattr(draft, "version")
    assert not hasattr(draft, "status") or draft.status == DraftStatus.DRAFT.value
    for member in CurriculumStatus:
        assert draft.status != member.value


def test_confirmed_draft_publishes_active_curriculum() -> None:
    draft = make_draft()
    validator = make_validator()
    result = validator.confirm_and_build(
        draft,
        curriculum_id="cur1",
        concept_ids={"p-intro": "K-intro", "p-laplace": "K-laplace", "p-transfer": "K-transfer"},
    )
    curriculum = result.curriculum
    assert curriculum.id == "cur1"
    assert curriculum.course_id == COURSE
    assert curriculum.version == 1
    assert curriculum.is_active
    assert curriculum.based_on_curriculum_id is None
    assert curriculum.activated_at
    # The active version keeps the default route and the prerequisite edges.
    assert curriculum.default_path_id == result.paths[0].id
    assert [(e.concept_id, e.prerequisite_concept_id) for e in result.prerequisites] == []
    assert len(result.chapters) == 2
    assert len(result.chapter_concepts) == 3


def test_publishing_requires_a_confirmed_draft() -> None:
    draft = make_draft()
    with pytest.raises(ConfirmationRequiredError):
        build_curriculum_from_draft(
            draft, curriculum_id="cur1", version=1, concept_ids={"p-intro": "K-intro"}
        )
    confirmed = draft.confirm(confirmed_by="user")
    assert confirmed.status == DraftStatus.CONFIRMED.value
    result = build_curriculum_from_draft(
        confirmed,
        curriculum_id="cur1",
        version=1,
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    assert result.curriculum.is_active


def test_ai_and_pagelens_can_never_confirm() -> None:
    for actor in ("ai", "AI", "pagelens", "model", "assistant", "system"):
        with pytest.raises(ConfirmationRequiredError):
            make_draft().confirm(confirmed_by=actor)
    with pytest.raises(ConfirmationRequiredError):
        make_draft().confirm(confirmed_by="  ")


def test_ai_draft_can_be_proposed_but_needs_the_user_to_confirm() -> None:
    ai_draft = make_draft(
        created_by=DraftOrigin.AI.value,
        sources=(
            CurriculumSource(
                kind=SourceKind.AI_GENERATED.value, title="AI 生成", generated_by="qwen"
            ),
        ),
    )
    # Validating an AI proposal is fine (it is only a proposal)...
    report = make_validator().validate_draft(ai_draft)
    assert report.ok
    # ...confirming it is not, and the warning says who must act.
    assert any(
        issue.severity == "warning" for issue in report.issues
    )
    with pytest.raises(ConfirmationRequiredError):
        ai_draft.confirm(confirmed_by="ai")
    # Only the explicit user event publishes it.
    published = build_curriculum_from_draft(
        ai_draft.confirm(confirmed_by="user"),
        curriculum_id="cur-ai",
        version=1,
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    assert published.curriculum.is_active
    assert published.curriculum.sources[0].generated_by == "qwen"


def test_draft_reject_transition() -> None:
    draft = make_draft()
    rejected = draft.reject()
    assert rejected.status == DraftStatus.REJECTED.value
    with pytest.raises(ConfirmationRequiredError):
        rejected.confirm(confirmed_by="user")


def test_second_publish_supersedes_the_first() -> None:
    validator = make_validator()
    draft = make_draft()
    v1 = validator.confirm_and_build(
        draft,
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    book = v1.apply_to([])
    assert [c.version for c in book] == [1]
    assert validator.validate_set(book).ok

    derived = draft_from_curriculum(v1.view(), draft_id="draft-2")
    v2 = validator.confirm_and_build(
        derived, curriculum_id="cur2", concept_ids={}, existing=book
    )
    book2 = v2.apply_to(book)
    # Exactly one active version, the previous one flipped to superseded.
    assert [(c.id, c.version, c.status) for c in book2] == [
        ("cur1", 1, CurriculumStatus.SUPERSEDED.value),
        ("cur2", 2, CurriculumStatus.ACTIVE.value),
    ]
    assert validator.validate_set(book2).ok
    assert active_curriculum_for(COURSE, book2).id == "cur2"
    assert next_version(book2, COURSE) == 3


def test_republish_reuses_concepts_to_preserve_history() -> None:
    validator = make_validator()
    v1 = validator.confirm_and_build(
        make_draft(),
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    derived = draft_from_curriculum(v1.view(), draft_id="draft-2")
    # Proposals carry matched_concept_id, so no id mapping is needed at all.
    assert all(p.matched_concept_id for p in derived.concept_candidates)
    v2 = validator.confirm_and_build(
        derived, curriculum_id="cur2", concept_ids={}, existing=v1.apply_to([])
    )
    assert {link.concept_id for link in v2.chapter_concepts} == {
        "K-intro",
        "K-laplace",
        "K-transfer",
    }


def test_draft_from_curriculum_does_not_mutate_the_source() -> None:
    validator = make_validator()
    v1 = validator.confirm_and_build(
        make_draft(),
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    before = v1.view()
    draft_from_curriculum(before, draft_id="draft-2")
    assert before.curriculum.is_active
    assert before.curriculum.version == 1
    assert [c.id for c in before.chapters] == [c.id for c in v1.chapters]


def test_activation_assigns_the_version_and_refuses_a_bad_one() -> None:
    confirmed = make_draft().confirm(confirmed_by="user")
    ids = {"p-intro": "K-intro", "p-laplace": "K-laplace", "p-transfer": "K-transfer"}
    with pytest.raises(Exception):
        build_curriculum_from_draft(
            confirmed, curriculum_id="cur1", version=0, concept_ids=ids
        )
    existing = [Curriculum(id="old", course_id=COURSE, title="t", version=7)]
    assert next_version(existing, COURSE) == 8
    assert next_version(existing, "another-course") == 1
    assert next_version([], COURSE) == 1


def test_publish_refuses_a_previous_active_of_another_course() -> None:
    confirmed = make_draft().confirm(confirmed_by="user")
    stranger = Curriculum(id="cur-x", course_id=OTHER_COURSE, title="t", version=1)
    with pytest.raises(Exception):
        build_curriculum_from_draft(
            confirmed,
            curriculum_id="cur1",
            version=1,
            concept_ids={
                "p-intro": "K-intro",
                "p-laplace": "K-laplace",
                "p-transfer": "K-transfer",
            },
            previous_active=stranger,
        )


# ---------------------------------------------------------------------------
# 3. Chapter ordering
# ---------------------------------------------------------------------------


def test_chapter_order_comes_from_position_not_list_order() -> None:
    chapters = (
        Chapter(id="ch-b", curriculum_id="cur1", title="数学模型", position=2),
        Chapter(id="ch-a", curriculum_id="cur1", title="绪论", position=1),
    )
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1), chapters=chapters
    )
    assert [chapter.id for chapter in view.chapters_in_order] == ["ch-a", "ch-b"]
    # The input tuple keeps its own order; only the projection is ordered.
    assert [chapter.id for chapter in view.chapters] == ["ch-b", "ch-a"]


def test_duplicate_chapter_position_is_an_error() -> None:
    chapters = (
        Chapter(id="ch-a", curriculum_id="cur1", title="绪论", position=1),
        Chapter(id="ch-b", curriculum_id="cur1", title="数学模型", position=1),
    )
    report = make_validator().check_chapter_positions(chapters)
    assert not report.ok
    assert report.has("position_duplicate")
    assert report.count("position_duplicate") == 1


def test_chapter_gap_is_a_warning_not_an_error() -> None:
    chapters = (
        Chapter(id="ch-a", curriculum_id="cur1", title="绪论", position=1),
        Chapter(id="ch-c", curriculum_id="cur1", title="时域分析", position=3),
    )
    report = make_validator().check_chapter_positions(chapters)
    assert report.ok  # order is still unambiguous
    assert report.has("position_not_contiguous")
    assert not report.errors


def test_chapter_requires_a_curriculum_scope() -> None:
    with pytest.raises(Exception):
        Chapter(id="ch-a", curriculum_id="", title="绪论", position=1)
    with pytest.raises(Exception):
        Chapter(id="", curriculum_id="cur1", title="绪论", position=1)


def test_path_step_positions_must_be_unique_and_contiguous() -> None:
    path = LearningPath(
        id="path-1",
        curriculum_id="cur1",
        title="默认路线",
        steps=(
            LearningPathStep(
                id="s1", path_id="path-1", position=1,
                target_type=PathTargetType.CHAPTER.value, target_id="ch-a",
            ),
            LearningPathStep(
                id="s3", path_id="path-1", position=3,
                target_type=PathTargetType.CHAPTER.value, target_id="ch-b",
            ),
        ),
    )
    report = make_validator().check_path_step_positions(path)
    assert not report.ok  # a gap in a route means a lost step
    assert report.has("position_not_contiguous")
    assert path.ordered_steps[0].id == "s1"


def test_chapter_output_is_deterministic() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(
            Chapter(id="ch-z", curriculum_id="cur1", title="Z", position=1),
            Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),
        ),
    )
    assert [c.id for c in view.chapters_in_order] == ["ch-a", "ch-z"]


# ---------------------------------------------------------------------------
# 4. ChapterConcept association
# ---------------------------------------------------------------------------


def test_chapter_concept_link_carries_position_and_role() -> None:
    link = ChapterConcept(
        chapter_id="ch-a",
        concept_id="K-transfer",
        position=2,
        role=ChapterConceptRole.SUPPORTING.value,
        difficulty_hint=DifficultyHint.INTERMEDIATE.value,
    )
    assert link.key == ("ch-a", "K-transfer")
    assert not link.is_primary
    assert link.difficulty_hint == "intermediate"
    assert ChapterConceptRef is ChapterConcept


def test_concept_can_appear_in_several_chapters() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(
            Chapter(id="ch-a", curriculum_id="cur1", title="数学模型", position=1),
            Chapter(id="ch-b", curriculum_id="cur1", title="时域分析", position=2),
        ),
        chapter_concepts=(
            ChapterConcept("ch-a", "K-transfer", 1),
            ChapterConcept(
                "ch-b", "K-transfer", 1, role=ChapterConceptRole.SUPPORTING.value
            ),
        ),
    )
    report = make_validator().validate_curriculum(view)
    assert report.ok, report.summary()
    assert len(view.concepts_of_chapter("ch-a")) == 1
    assert len(view.concepts_of_chapter("ch-b")) == 1


def test_two_primary_placements_of_one_concept_are_rejected() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(
            Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),
            Chapter(id="ch-b", curriculum_id="cur1", title="B", position=2),
        ),
        chapter_concepts=(
            ChapterConcept("ch-a", "K-transfer", 1),
            ChapterConcept("ch-b", "K-transfer", 1),
        ),
    )
    report = make_validator().validate_curriculum(view)
    assert not report.ok
    assert report.has("chapter_concept_primary_not_unique")


def test_duplicate_chapter_concept_key_is_rejected() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(
            ChapterConcept("ch-a", "K-transfer", 1),
            ChapterConcept("ch-a", "K-transfer", 2),
        ),
    )
    report = make_validator().validate_curriculum(view)
    assert report.has("chapter_concept_duplicate")


def test_chapter_concept_positions_are_unique_within_a_chapter() -> None:
    links = (
        ChapterConcept("ch-a", "K-laplace", 1),
        ChapterConcept("ch-a", "K-transfer", 1),
    )
    report = make_validator().check_chapter_concept_positions("ch-a", links)
    assert not report.ok
    assert report.has("position_duplicate")


def test_difficulty_hint_is_kept_out_of_concept_identity() -> None:
    """A curriculum hint must not leak into the learning facts."""
    link = ChapterConcept(
        "ch-a", "K-transfer", 1, difficulty_hint=DifficultyHint.ADVANCED.value
    )
    # The hint is placement metadata; the link has no mastery/difficulty field.
    assert not hasattr(link, "mastery_level")
    assert not hasattr(link, "difficulty")
    assert link.difficulty_hint == "advanced"


# ---------------------------------------------------------------------------
# 5. Course isolation
# ---------------------------------------------------------------------------


def test_course_isolation_rejects_a_foreign_concept() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(ChapterConcept("ch-a", "K-foreign", 1),),
    )
    report = make_validator().check_course_isolation(view)
    assert not report.ok
    assert report.has("course_isolation")
    issue = report.errors[0]
    assert issue.subject == "K-foreign"
    assert issue.detail["concept_course"] == OTHER_COURSE


def test_course_isolation_surfaces_in_full_validation() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(ChapterConcept("ch-a", "K-foreign", 1),),
    )
    report = make_validator().validate_curriculum(view)
    assert not report.ok
    assert report.error_codes() == ("course_isolation",)
    with pytest.raises(CurriculumValidationError):
        report.raise_if_invalid("curriculum")


def test_unknown_concept_cannot_prove_isolation() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(ChapterConcept("ch-a", "K-unknown", 1),),
    )
    report = make_validator().check_course_isolation(view)
    assert not report.ok
    assert report.has("course_member_unknown")
    assert "cannot be proven" in report.errors[0].message


def test_chapter_from_another_curriculum_is_rejected() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur-other", title="A", position=1),),
    )
    report = make_validator().validate_curriculum(view)
    assert report.has("curriculum_scope_mismatch")


def test_foreign_prerequisite_endpoint_is_rejected() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(
            ChapterConcept("ch-a", "K-transfer", 1),
            ChapterConcept("ch-a", "K-laplace", 2),
        ),
        prerequisites=(
            ConceptPrerequisite("cur1", "K-transfer", "K-foreign"),
        ),
    )
    report = make_validator().validate_curriculum(view)
    assert not report.ok
    assert report.has("course_isolation")


def test_draft_isolation_checks_pre_matched_concepts() -> None:
    draft = make_draft(
        chapters=(
            chapter_draft(
                "d-ch1", "绪论", 1,
                placement("p-intro", 1, name="光学基础", matched="K-foreign"),
            ),
        ),
        paths=(canonical_path("d-ch1"),),
    )
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("course_isolation")


# ---------------------------------------------------------------------------
# 6. Active uniqueness
# ---------------------------------------------------------------------------


def test_two_active_curricula_for_one_course_are_rejected() -> None:
    actives = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(id="cur2", course_id=COURSE, title="t", version=2),
    ]
    report = make_validator().check_active_uniqueness(actives)
    assert not report.ok
    assert report.has("active_not_unique")
    assert report.errors[0].subject == COURSE
    assert set(report.errors[0].detail["curriculum_ids"]) == {"cur1", "cur2"}


def test_one_active_per_course_is_fine_across_courses() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(id="cur2", course_id=OTHER_COURSE, title="t", version=1),
    ]
    assert make_validator().check_active_uniqueness(curricula).ok


def test_a_course_may_have_no_active_curriculum() -> None:
    curricula = [
        Curriculum(
            id="cur1", course_id=COURSE, title="t", version=1,
            status=CurriculumStatus.ARCHIVED.value,
        )
    ]
    assert make_validator().validate_set(curricula).ok
    assert active_curriculum_for(COURSE, curricula) is None


def test_active_plus_superseded_is_valid() -> None:
    curricula = [
        Curriculum(
            id="cur1", course_id=COURSE, title="t", version=1,
            status=CurriculumStatus.SUPERSEDED.value,
        ),
        Curriculum(
            id="cur2", course_id=COURSE, title="t", version=2,
            based_on_curriculum_id="cur1",
        ),
    ]
    assert make_validator().validate_set(curricula).ok


# ---------------------------------------------------------------------------
# 7. Draft protection
# ---------------------------------------------------------------------------


def test_draft_can_never_be_consumed_as_active_structure() -> None:
    draft = make_draft()
    assert is_consumable(draft) is False
    with pytest.raises(ActiveCurriculumRequiredError):
        ensure_consumable(draft)
    report = make_validator().check_draft_protection(draft)
    assert not report.ok
    assert report.has("draft_not_consumable")


def test_confirmed_draft_is_still_not_consumable() -> None:
    confirmed = make_draft().confirm(confirmed_by="user")
    assert confirmed.is_confirmed
    assert is_consumable(confirmed) is False
    with pytest.raises(ActiveCurriculumRequiredError):
        ensure_consumable(confirmed)


def test_superseded_and_archived_versions_are_not_consumable() -> None:
    for status in (CurriculumStatus.SUPERSEDED.value, CurriculumStatus.ARCHIVED.value):
        curriculum = Curriculum(id="cur1", course_id=COURSE, title="t", version=1, status=status)
        assert is_consumable(curriculum) is False
        with pytest.raises(ActiveCurriculumRequiredError):
            ensure_consumable(curriculum)


def test_active_curriculum_and_view_may_be_consumed() -> None:
    curriculum = Curriculum(id="cur1", course_id=COURSE, title="t", version=1)
    assert is_consumable(curriculum)
    assert ensure_consumable(curriculum) is curriculum
    view = CurriculumView.of(curriculum)
    assert ensure_consumable(view) is curriculum


def test_consumption_gate_blocks_a_superseded_projection() -> None:
    """A read projection of a non-active version is fine; consuming it is not."""
    validator = make_validator()
    v1 = validator.confirm_and_build(
        make_draft(),
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    superseded = v1.curriculum.superseded()
    view = CurriculumView.of(
        superseded,
        chapters=v1.chapters,
        learning_paths=v1.paths,
        chapter_concepts=v1.chapter_concepts,
    )
    assert view.curriculum.status == CurriculumStatus.SUPERSEDED.value
    assert is_consumable(view) is False
    with pytest.raises(ActiveCurriculumRequiredError):
        view.next_in_order(v1.chapters[0].id)


def test_next_in_order_is_a_deterministic_successor() -> None:
    validator = make_validator()
    v1 = validator.confirm_and_build(
        make_draft(),
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    view = v1.view()
    first, second = view.chapters_in_order[0], view.chapters_in_order[1]
    successor = view.next_in_order(first.id)
    assert successor is not None and successor.target_id == second.id
    assert view.next_in_order(second.id) is None
    assert view.next_in_order("nope") is None


# ---------------------------------------------------------------------------
# 8. Version consistency
# ---------------------------------------------------------------------------


def test_duplicate_version_within_a_course_is_rejected() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(id="cur2", course_id=COURSE, title="t", version=1),
    ]
    report = make_validator().check_version_consistency(curricula)
    assert not report.ok
    assert report.has("version_duplicate")


def test_same_version_on_different_courses_is_fine() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(id="cur2", course_id=OTHER_COURSE, title="t", version=1),
    ]
    report = make_validator().check_version_consistency(curricula)
    assert report.ok, report.summary()


def test_based_on_must_exist_and_share_the_course() -> None:
    unknown = [
        Curriculum(
            id="cur2", course_id=COURSE, title="t", version=2,
            based_on_curriculum_id="missing",
        )
    ]
    assert make_validator().check_version_consistency(unknown).has("version_base_unknown")

    crossed = [
        Curriculum(id="cur1", course_id=OTHER_COURSE, title="t", version=1),
        Curriculum(
            id="cur2", course_id=COURSE, title="t", version=2,
            based_on_curriculum_id="cur1",
        ),
    ]
    report = make_validator().check_version_consistency(crossed)
    assert report.has("version_base_course_mismatch")


def test_based_on_must_be_an_older_version() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=5,
                   status=CurriculumStatus.SUPERSEDED.value),
        Curriculum(id="cur2", course_id=COURSE, title="t", version=5,
                   based_on_curriculum_id="cur1"),
    ]
    report = make_validator().check_version_consistency(curricula)
    assert report.has("version_not_monotonic")


def test_active_version_behind_a_newer_version_warns() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(
            id="cur2", course_id=COURSE, title="t", version=2,
            status=CurriculumStatus.ARCHIVED.value,
        ),
    ]
    report = make_validator().check_version_consistency(curricula)
    assert report.ok  # a warning, not an error
    assert report.has("version_active_not_latest")


def test_version_is_positive_by_construction() -> None:
    with pytest.raises(Exception):
        Curriculum(id="cur1", course_id=COURSE, title="t", version=-1)


# ---------------------------------------------------------------------------
# 9. Prerequisite graph (cycle detection)
# ---------------------------------------------------------------------------


def edge(head: str, tail: str, kind: str = PrerequisiteKind.REQUIRED.value):
    return ConceptPrerequisite("cur1", head, tail, kind=kind)


def test_prerequisite_self_loop_is_rejected() -> None:
    report = make_validator().check_prerequisite_cycles([edge("K-transfer", "K-transfer")])
    assert not report.ok
    assert report.has("prerequisite_self_loop")


def test_required_cycle_is_detected() -> None:
    # A -> B -> C -> A  (A depends on B, B on C, C on A)
    report = make_validator().check_prerequisite_cycles(
        [edge("K-a", "K-b"), edge("K-b", "K-c"), edge("K-c", "K-a")]
    )
    assert not report.ok
    assert report.has("prerequisite_cycle")
    issue = report.errors[0]
    assert issue.detail["kind"] == PrerequisiteKind.REQUIRED.value
    assert len(issue.detail["cycle"]) >= 4  # path + repeated first node


def test_two_node_cycle_is_detected() -> None:
    report = make_validator().check_prerequisite_cycles(
        [edge("K-a", "K-b"), edge("K-b", "K-a")]
    )
    assert report.has("prerequisite_cycle")
    assert not report.ok


def test_acyclic_chain_is_accepted() -> None:
    # 拉普拉斯变换 -> 传递函数 -> 频率响应
    report = make_validator().check_prerequisite_cycles(
        [edge("K-transfer", "K-laplace"), edge("K-freq", "K-transfer")]
    )
    assert report.ok, report.summary()
    assert report.is_clean


def test_recommended_only_cycle_is_a_warning() -> None:
    recommended = PrerequisiteKind.RECOMMENDED.value
    report = make_validator().check_prerequisite_cycles(
        [edge("K-a", "K-b", recommended), edge("K-b", "K-a", recommended)]
    )
    assert report.ok
    assert report.has("prerequisite_cycle")
    assert report.warnings[0].detail["kind"] == recommended


def test_mixed_cycle_with_a_required_edge_is_an_error() -> None:
    recommended = PrerequisiteKind.RECOMMENDED.value
    report = make_validator().check_prerequisite_cycles(
        [
            edge("K-a", "K-b"),                      # required
            edge("K-b", "K-c", recommended),         # recommended
            edge("K-c", "K-a"),                      # required
        ]
    )
    assert not report.ok
    assert report.errors[0].detail["kind"] == PrerequisiteKind.REQUIRED.value


def test_deep_chain_does_not_recurse_into_a_stack_overflow() -> None:
    chain = [edge(f"K-{i}", f"K-{i + 1}") for i in range(3000)]
    report = make_validator().check_prerequisite_cycles(chain)
    assert report.ok
    # ...and a deep cycle is still found.
    closed = chain + [edge("K-3000", "K-0")]
    assert make_validator().check_prerequisite_cycles(closed).has("prerequisite_cycle")


def test_prerequisite_endpoint_must_be_taught_by_the_version() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(ChapterConcept("ch-a", "K-transfer", 1),),
        prerequisites=(ConceptPrerequisite("cur1", "K-transfer", "K-laplace"),),
    )
    report = make_validator().validate_curriculum(view)
    assert not report.ok
    assert report.has("prerequisite_unknown")


def test_duplicate_prerequisite_edge_is_reported() -> None:
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(
            ChapterConcept("ch-a", "K-transfer", 1),
            ChapterConcept("ch-a", "K-laplace", 2),
        ),
        prerequisites=(
            ConceptPrerequisite("cur1", "K-transfer", "K-laplace"),
            ConceptPrerequisite("cur1", "K-transfer", "K-laplace"),
        ),
    )
    assert make_validator().validate_curriculum(view).has("prerequisite_duplicate")


def test_draft_required_cycle_blocks_the_confirm_gate() -> None:
    draft = make_draft(
        chapters=(
            chapter_draft(
                "d-ch1", "数学模型", 1,
                placement("p-laplace", 1, name="拉普拉斯变换", matched="K-laplace"),
                placement("p-transfer", 2, name="传递函数", matched="K-transfer"),
                placement("p-freq", 3, name="频率响应", matched="K-freq"),
            ),
        ),
        paths=(canonical_path("d-ch1"),),
        prerequisites=(
            PrerequisiteDraft("p-transfer", "p-laplace"),
            PrerequisiteDraft("p-freq", "p-transfer"),
            PrerequisiteDraft("p-laplace", "p-freq"),  # closes the loop
        ),
    )
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("prerequisite_cycle")
    with pytest.raises(CurriculumValidationError):
        report.raise_if_invalid("draft")


def test_prerequisite_is_structural_only() -> None:
    """It must not imply mastery, block jumping ahead, or rank anything."""
    link = ConceptPrerequisite("cur1", "K-transfer", "K-laplace")
    for forbidden in ("mastery", "mastery_level", "recommend", "score", "weight"):
        assert not hasattr(link, forbidden)
    assert link.kind == PrerequisiteKind.REQUIRED.value
    assert link.prerequisite_id == "K-laplace"  # task-spec alias


# ---------------------------------------------------------------------------
# draft validation (confirm gate) — remaining rules
# ---------------------------------------------------------------------------


def test_draft_without_chapters_or_concepts_is_rejected() -> None:
    empty = make_draft(chapters=(), paths=())
    report = make_validator().validate_draft(empty)
    assert not report.ok
    assert report.has("chapter_empty")
    assert report.has("concept_proposal_missing")


def test_draft_for_an_unknown_course_is_rejected() -> None:
    draft = make_draft(course_id="course-deleted")
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("course_unknown")


def test_draft_duplicate_chapter_position_is_rejected() -> None:
    draft = make_draft(
        chapters=(
            chapter_draft("d-ch1", "A", 1, placement("p-a", 1, name="A", matched="K-intro")),
            chapter_draft("d-ch2", "B", 1, placement("p-b", 1, name="B", matched="K-laplace")),
        ),
        paths=(canonical_path("d-ch1", "d-ch2"),),
    )
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("position_duplicate")


def test_draft_normalised_duplicate_within_a_chapter_is_rejected() -> None:
    """Two spellings of one concept in the same chapter collapse to one key."""
    draft = make_draft(
        chapters=(
            chapter_draft(
                "d-ch1", "A", 1,
                placement("p-a", 1, name="Transformed Function", matched="K-transfer"),
                placement("p-b", 2, name="transformed-function!", matched="K-transfer"),
            ),
        ),
        paths=(canonical_path("d-ch1"),),
    )
    first, second = draft.chapters[0].ordered_concepts
    assert first.proposal.normalized_name == second.proposal.normalized_name
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("concept_proposal_duplicate")


def test_cjk_concept_name_duplicate_is_detected() -> None:
    draft = make_draft(
        chapters=(
            chapter_draft(
                "d-ch1", "A", 1,
                placement("p-a", 1, name="传递函数", matched="K-transfer"),
                placement("p-b", 2, name="传递函数。", matched="K-transfer"),
            ),
        ),
        paths=(canonical_path("d-ch1"),),
    )
    assert make_validator().validate_draft(draft).has("concept_proposal_duplicate")


def test_draft_path_must_reference_draft_members() -> None:
    draft = make_draft(
        chapters=(chapter_draft("d-ch1", "A", 1, placement("p-a", 1, name="A", matched="K-intro")),),
        paths=(canonical_path("d-ch1", "d-missing"),),
    )
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("path_reference_invalid")


def test_draft_two_canonical_paths_are_rejected() -> None:
    draft = make_draft(
        paths=(
            canonical_path("d-ch1", "d-ch2", path_id="path-main"),
            canonical_path("d-ch2", "d-ch1", path_id="path-other"),
        )
    )
    assert make_validator().validate_draft(draft).has("path_canonical_not_unique")


def test_ai_source_must_record_its_generator() -> None:
    draft = make_draft(
        sources=(CurriculumSource(kind=SourceKind.AI_GENERATED.value, title="AI 草稿"),)
    )
    report = make_validator().validate_draft(draft)
    assert not report.ok
    assert report.has("source_generator_missing")


def test_pdf_source_without_locator_only_warns() -> None:
    draft = make_draft(
        sources=(CurriculumSource(kind=SourceKind.PDF.value, title="自动控制原理.pdf"),)
    )
    report = make_validator().validate_draft(draft)
    assert report.ok
    assert report.has("source_incomplete")
    assert report.warnings


def test_valid_draft_and_curriculum_reports_are_clean() -> None:
    draft = make_draft()
    draft_report = make_validator().validate_draft(draft)
    assert draft_report.ok and draft_report.is_clean, draft_report.summary()
    result = make_validator().confirm_and_build(
        draft,
        curriculum_id="cur1",
        concept_ids={
            "p-intro": "K-intro",
            "p-laplace": "K-laplace",
            "p-transfer": "K-transfer",
        },
    )
    report = make_validator().validate_curriculum(result.view())
    assert report.ok and report.is_clean, report.summary()


def test_report_helpers_and_module_functions() -> None:
    curricula = [
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        Curriculum(id="cur2", course_id=COURSE, title="t", version=1),
    ]
    report = validate_set(curricula, course_of_concept=COURSE_OF_CONCEPT)
    assert not report.ok
    assert "version_duplicate" in report.codes
    assert "version_duplicate" in report.error_codes()
    assert report.count("version_duplicate") == 1
    assert "error" in report.summary() or "error(s)" in report.summary()
    with pytest.raises(CurriculumValidationError) as excinfo:
        report.raise_if_invalid("set")
    assert "version_duplicate" in excinfo.value.codes

    empty = validate_draft(make_draft(), course_of_concept=COURSE_OF_CONCEPT,
                           known_course_ids={COURSE})
    assert empty.ok


def test_normalizer_is_shared_with_the_learning_store() -> None:
    """Concept identity must not have a second definition in this layer."""
    assert normalize_name("传递函数") == normalize_name(" 传递函数 ")
    assert normalize_name("Scaled Dot-Product Attention") == normalize_name(
        "scaled dot product attention"
    )
    left = ConceptProposal(proposal_id="p", name="Scaled Dot-Product Attention")
    right = ConceptProposal(proposal_id="q", name="scaled dot product attention")
    assert left.normalized_name == right.normalized_name


def test_draft_projection_helpers() -> None:
    draft = make_draft(
        prerequisites=(PrerequisiteDraft("p-transfer", "p-laplace"),)
    )
    assert draft.source_type == SourceKind.MANUAL.value
    assert draft.provenance == draft.sources
    names = [p.name for p in draft.concept_candidates]
    assert names == ["自动控制概述", "拉普拉斯变换", "传递函数"]
    assert set(draft.proposals_by_id()) == {"p-intro", "p-laplace", "p-transfer"}
    assert draft.provenance[0].title == "手工录入"


def test_draft_reusing_one_proposal_id_for_two_names_is_refused() -> None:
    shared = proposal("p-dup", "传递函数")
    other = proposal("p-dup", "频率响应")
    draft = make_draft(
        chapters=(
            chapter_draft("d-ch1", "A", 1, ChapterConceptDraft(shared, 1)),
            chapter_draft("d-ch2", "B", 2, ChapterConceptDraft(other, 1)),
        ),
        paths=(canonical_path("d-ch1", "d-ch2"),),
    )
    with pytest.raises(Exception):
        draft.proposals_by_id()


def test_derive_learning_order_follows_the_default_path() -> None:
    curriculum = Curriculum(
        id="cur1", course_id=COURSE, title="t", version=1, default_path_id="path-b"
    )
    chapters = (
        Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),
        Chapter(id="ch-b", curriculum_id="cur1", title="B", position=2),
    )
    paths = (
        LearningPath(
            id="path-a", curriculum_id="cur1", title="canonical",
            kind=PathKind.CANONICAL.value,
            steps=(
                LearningPathStep(
                    id="s1", path_id="path-a", position=1,
                    target_type=PathTargetType.CHAPTER.value, target_id="ch-b",
                ),
            ),
        ),
        LearningPath(
            id="path-b", curriculum_id="cur1", title="explicit default",
            kind=PathKind.ALTERNATE.value,
            steps=(
                LearningPathStep(
                    id="s2", path_id="path-b", position=1,
                    target_type=PathTargetType.CHAPTER.value, target_id="ch-a",
                ),
                LearningPathStep(
                    id="s3", path_id="path-b", position=2,
                    target_type=PathTargetType.CHAPTER.value, target_id="ch-b",
                ),
            ),
        ),
    )
    order = derive_learning_order(curriculum, chapters=chapters, paths=paths)
    assert [item.target_id for item in order] == ["ch-a", "ch-b"]


def test_learning_path_items_alias() -> None:
    path = LearningPathDraft(
        id="path-1", title="默认路线", steps=(PathStepDraft("s1", 1, "chapter", "d-ch1"),)
    )
    assert path.is_canonical
    assert LearningPath(
        id="p", curriculum_id="cur1", title="t", steps=()
    ).items == ()
    assert len(path.steps) == 1


# ---------------------------------------------------------------------------
# architecture constraints: the domain layer stays pure
# ---------------------------------------------------------------------------

FORBIDDEN_ROOTS = {
    "sqlite3",
    "PySide6",
    "PyQt5",
    "PyQt6",
    "ui",
    "requests",
    "httpx",
    "openai",
    "numpy",
    "pandas",
}
FORBIDDEN_DOTTED = {
    "core.ai_router",
    "core.providers",
    "core.pagelens_bridge",
    "core.learning.store",
    "core.learning.controller",
    "core.learning.assessment",
    "core.learning.rule_engine",
    "core.learning.review_scheduler",
    "core.learning.quiz_adapter",
    "core.memory",
    "core.screen_vision",
    "core.video_study",
    "core.document_router",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module)
    return found


def _curriculum_modules() -> list[Path]:
    return sorted(CURRICULUM_PKG.glob("*.py"))


def test_curriculum_package_has_the_expected_modules() -> None:
    names = {path.name for path in _curriculum_modules()}
    assert {
        "__init__.py",
        "models.py",
        "validators.py",
        "store.py",  # Phase 2-CF2: persistence adapter
    } <= names


def test_pure_domain_modules_import_only_stdlib_and_learning_models() -> None:
    """models.py / validators.py / __init__.py stay persistence-free.

    ``store.py`` is deliberately excluded here — it is the persistence
    adapter and may import the LearningStore (see the dedicated test below).
    """
    pure_names = {"__init__.py", "models.py", "validators.py"}
    offenders: dict[str, set[str]] = {}
    for path in _curriculum_modules():
        if path.name not in pure_names:
            continue
        imports = _imported_modules(path)
        bad = {
            module
            for module in imports
            if module.split(".")[0] in FORBIDDEN_ROOTS
            or any(
                module == dotted or module.startswith(dotted + ".")
                for dotted in FORBIDDEN_DOTTED
            )
        }
        # Allowed core imports: the pure learning models module + siblings.
        core_imports = {m for m in imports if m.startswith("core.")}
        unexpected_core = {
            m
            for m in core_imports
            if m != "core.learning.models"
            and not m.startswith("core.learning.curriculum")
        }
        if bad or unexpected_core:
            offenders[path.name] = bad | unexpected_core
    assert not offenders, f"curriculum domain modules import forbidden modules: {offenders}"


def test_curriculum_store_imports_stay_out_of_provider_ui_ai() -> None:
    """Phase 2-CF2 §九: the store may touch the LearningStore (hence sqlite),
    but never providers, Qt/UI or anything AI/network."""
    store = CURRICULUM_PKG / "store.py"
    imports = _imported_modules(store)
    forbidden = {
        "core.ai_router",
        "core.providers",
        "core.pagelens_bridge",
        "core.screen_vision",
        "core.memory",
        "ui",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "openai",
        "anthropic",
        "requests",
        "httpx",
        "core.video_study",
    }
    bad = {
        module
        for module in imports
        if module.split(".")[0] in forbidden
        or any(module == f or module.startswith(f + ".") for f in forbidden)
    }
    assert not bad, f"curriculum store imports forbidden modules: {bad}"
    # It must reach the learning store through one door only.
    assert "core.learning.store" in imports


def test_learning_models_stays_sqlite_free() -> None:
    """The re-used identity primitives must not drag the store in."""
    models = Path(__file__).resolve().parents[1] / "core" / "learning" / "models.py"
    imports = _imported_modules(models)
    assert "sqlite3" not in imports
    assert not any(module.startswith("core.learning.store") for module in imports)


def test_curriculum_domain_does_not_mention_qt_or_sql_in_source() -> None:
    """Belt-and-braces for the PURE modules only: no QWidget/SQL text.

    The store adapter is exempt: it is the persistence layer and legitimately
    contains SQL strings and the sqlite import (covered by its own test).
    """
    banned = ("PySide6", "QWidget", "QObject", "sqlite3", "CREATE TABLE", "SELECT ")
    for path in _curriculum_modules():
        if path.name == "store.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} mentions {token!r}"


def test_validator_works_on_in_memory_values_only() -> None:
    """No injected context, no store: the layer still validates and reports."""
    validator = CurriculumValidator()
    # The draft is structurally sound on its own...
    assert validator.validate_draft(make_draft()).ok
    # ...but course isolation needs evidence, so an unknown concept is an ERROR
    # rather than a silent pass.
    view = CurriculumView.of(
        Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
        chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
        chapter_concepts=(ChapterConcept("ch-a", "K-any", 1),),
    )
    report = validator.check_course_isolation(view)
    assert report.has("course_member_unknown")
    assert not report.ok
    # With the evidence injected, the same structure is clean.
    assert make_validator().check_course_isolation(
        CurriculumView.of(
            Curriculum(id="cur1", course_id=COURSE, title="t", version=1),
            chapters=(Chapter(id="ch-a", curriculum_id="cur1", title="A", position=1),),
            chapter_concepts=(ChapterConcept("ch-a", "K-transfer", 1),),
        )
    ).is_clean
