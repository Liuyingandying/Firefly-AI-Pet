"""Curriculum Store tests (Phase 2-CF2).

Covers the frozen store contract:

    migration v1→v2 / idempotency / old-data preservation /
    create draft / confirm draft / validator failure rejection /
    active uniqueness / version conflict / chapter ordering /
    course isolation at the store / transaction rollback /
    archive / active read / mastery zero-change

Pure SQLite tests against ``tmp_path`` databases — no Qt, no provider, no UI.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.learning.curriculum import (
    ChapterConceptDraft,
    ChapterDraft,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    DraftStatus,
    LearningPathDraft,
    PathStepDraft,
    PrerequisiteDraft,
    SourceKind,
    build_curriculum_from_draft,
    draft_from_curriculum,
)
from core.learning.curriculum.store import (
    CurriculumStore,
    CurriculumStoreIntegrityError,
    CurriculumStoreError,
    DraftNotFoundError,
)
from core.learning.store import LearningStore, _SCHEMA, _SCHEMA_V2


# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def proposal(pid: str, name: str, *, matched: str | None = None) -> ConceptProposal:
    return ConceptProposal(proposal_id=pid, name=name, matched_concept_id=matched)


def placement(pid, position, *, name=None, matched=None):
    return ChapterConceptDraft(
        proposal=proposal(pid, name or pid, matched=matched), position=position
    )


def make_draft(
    *,
    draft_id: str = "draft-1",
    course_id: str = "c1",
    title: str = "自动控制原理",
    chapters=None,
    paths=None,
    prerequisites=(),
    sources=(),
) -> CurriculumDraft:
    if chapters is None:
        chapters = (
            ChapterDraft(
                id="ch1", title="绪论", position=1,
                concepts=(placement("p-intro", 1, name="自动控制概述"),),
            ),
            ChapterDraft(
                id="ch2", title="数学模型", position=2,
                concepts=(
                    placement("p-laplace", 1, name="拉普拉斯变换"),
                    placement("p-transfer", 2, name="传递函数"),
                ),
            ),
        )
    if paths is None:
        paths = (
            LearningPathDraft(
                id="path-main", title="默认路线",
                steps=(
                    PathStepDraft("s1", 1, "chapter", "ch1"),
                    PathStepDraft("s2", 2, "chapter", "ch2"),
                ),
            ),
        )
    if not sources:
        sources = (CurriculumSource(kind=SourceKind.MANUAL.value, title="手工录入"),)
    return CurriculumDraft(
        id=draft_id,
        course_id=course_id,
        title=title,
        chapters=tuple(chapters),
        paths=tuple(paths),
        prerequisites=tuple(prerequisites),
        sources=tuple(sources),
    )


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO courses (id, name, status, created_at, updated_at)"
            " VALUES (?, '自动控制原理', 'active', ?, ?)",
            ("c1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    curriculum_store = CurriculumStore(store)
    return store, curriculum_store


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# 1-3. migration
# ---------------------------------------------------------------------------


def _make_v1_database(db_path, *, rows: bool = True) -> None:
    """Build a genuine v1 database: v1 DDL only + schema_version=1."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO learning_schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        if rows:
            connection.execute(
                "INSERT INTO courses (id, name, status, created_at, updated_at)"
                " VALUES ('v1-course', 'Control', 'active', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO concepts (id, course_id, canonical_name, normalized_name,"
                " mastery_level, retention, state, aliases, source_refs, created_at,"
                " updated_at) VALUES ('v1-concept', 'v1-course', '传递函数',"
                " '传递函数', 3, 'medium', 'assessed', '[]', '[]',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO assessment_records (id, course_id, concept_id, source,"
                " score, created_at) VALUES ('v1-asm', 'v1-course', 'v1-concept',"
                " 'quiz', 0.8, '2026-01-02T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO review_items (id, concept_id, due_at, status, created_at,"
                " updated_at) VALUES ('v1-review', 'v1-concept', '2026-02-01T00:00:00+00:00',"
                " 'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
        connection.commit()
    finally:
        connection.close()


def test_migration_v1_to_v2_preserves_everything(tmp_path) -> None:
    db_path = tmp_path / "v1.sqlite3"
    _make_v1_database(db_path)

    store = LearningStore(db_path)
    assert store.get_schema_version() == 4  # migrating on first touch

    with store.connect() as connection:
        tables = _tables(connection)
        for table in (
            "curriculums",
            "curriculum_chapters",
            "learning_paths",
            "learning_path_items",
            "chapter_concepts",
            "concept_prerequisites",
            "curriculum_drafts",
            "draft_chapters",
            "draft_paths",
            "draft_prerequisites",
        ):
            assert table in tables, table
        # old tables keep their rows and values
        assert _count(connection, "courses") == 1
        assert _count(connection, "concepts") == 1
        assert _count(connection, "assessment_records") == 1
        assert _count(connection, "review_items") == 1
        assert _count(connection, "study_sessions") == 0
        row = connection.execute(
            "SELECT mastery_level, retention, state FROM concepts WHERE id='v1-concept'"
        ).fetchone()
        assert (row["mastery_level"], row["retention"], row["state"]) == (3, "medium", "assessed")
        assessment = connection.execute(
            "SELECT score FROM assessment_records WHERE id='v1-asm'"
        ).fetchone()
        assert assessment["score"] == 0.8


def test_migration_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "v1.sqlite3"
    _make_v1_database(db_path, rows=False)
    store = LearningStore(db_path)
    store.initialize()
    store.initialize()
    store.initialize()
    assert store.get_schema_version() == 4
    with store.connect() as connection:
        tables = _tables(connection)
        assert "curriculums" in tables
        assert _count(connection, "curriculums") == 0
        assert _count(connection, "courses") == 0


def test_fresh_database_is_created_at_v2_directly(tmp_path) -> None:
    store = LearningStore(tmp_path / "fresh.sqlite3")
    store.initialize()
    assert store.get_schema_version() == 4
    with store.connect() as connection:
        assert "curriculums" in _tables(connection)
        assert _count(connection, "courses") == 0


# ---------------------------------------------------------------------------
# 4-5. drafts
# ---------------------------------------------------------------------------


def test_create_and_read_draft(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    loaded = cs.get_draft("draft-1")
    assert loaded is not None
    assert loaded.title == "自动控制原理"
    assert loaded.course_id == "c1"
    assert loaded.status == DraftStatus.DRAFT.value
    assert [c.id for c in loaded.chapters_in_order] == ["ch1", "ch2"]
    assert [p.name for p in loaded.chapters_in_order[1].proposals] == [
        "拉普拉斯变换",
        "传递函数",
    ]
    assert len(loaded.paths) == 1
    assert loaded.sources[0].kind == SourceKind.MANUAL.value
    assert cs.get_draft("missing") is None


def test_draft_round_trip_keeps_prerequisites_and_paths(env) -> None:
    store, cs = env
    draft = make_draft(prerequisites=(PrerequisiteDraft("p-transfer", "p-laplace"),))
    cs.create_draft(draft)
    loaded = cs.get_draft("draft-1")
    assert [(e.concept_proposal_id, e.prerequisite_proposal_id) for e in loaded.prerequisites] == [
        ("p-transfer", "p-laplace")
    ]
    assert [step.target_id for step in loaded.paths[0].steps] == ["ch1", "ch2"]


def test_duplicate_draft_id_is_rejected(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    with pytest.raises(CurriculumStoreIntegrityError):
        cs.create_draft(make_draft())
    with pytest.raises(DraftNotFoundError):
        cs.discard_draft("missing")


def test_discard_draft_marks_rejected(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    cs.discard_draft("draft-1")
    assert cs.get_draft("draft-1").status == DraftStatus.REJECTED.value
    # a discarded draft is still not consumable anywhere
    from core.learning.curriculum import is_consumable

    assert is_consumable(cs.get_draft("draft-1")) is False


def test_update_draft_replaces_in_place(env) -> None:
    store, cs = env
    cs.create_draft(make_draft(title="旧标题"))
    edited = make_draft(title="新标题")
    cs.update_draft(edited)
    loaded = cs.get_draft("draft-1")
    assert loaded.title == "新标题"
    # no duplicate rows from the update
    assert len(cs.list_drafts("c1")) == 1


# ---------------------------------------------------------------------------
# 6. confirm draft
# ---------------------------------------------------------------------------


def test_confirm_draft_creates_concepts_and_active_curriculum(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    curriculum = cs.confirm_draft("draft-1", confirmed_by="user")

    assert curriculum.course_id == "c1"
    assert curriculum.version == 1
    assert curriculum.is_active
    assert cs.get_draft("draft-1").status == DraftStatus.CONFIRMED.value
    # concepts were created with the frozen defaults (mastery untouched)
    concepts = store.list_concepts("c1")
    assert {c.canonical_name for c in concepts} == {
        "自动控制概述",
        "拉普拉斯变换",
        "传递函数",
    }
    assert all(c.mastery_level == 0 for c in concepts)
    assert all(c.retention == "low" and c.state == "discovered" for c in concepts)
    # no assessment/review/session side effects
    assert store.list_assessments() == []
    assert store.list_review_items() == []
    assert store.list_sessions("c1") == []


def test_confirm_draft_reuses_existing_concepts(env) -> None:
    store, cs = env
    store.add_concept("c1", "传递函数")
    cs.create_draft(make_draft())
    curriculum = cs.confirm_draft("draft-1", confirmed_by="user")
    transfer = store.find_concept("c1", "传递函数")
    assert transfer is not None
    view = cs.get_active_curriculum("c1")
    linked = {link.concept_id for link in view.chapter_concepts}
    assert transfer.id in linked


# ---------------------------------------------------------------------------
# 7. validator rejection
# ---------------------------------------------------------------------------


def test_invalid_draft_is_rejected_and_nothing_written(env) -> None:
    store, cs = env
    bad = make_draft(
        chapters=(
            ChapterDraft(
                id="ch1", title="A", position=1,
                concepts=(placement("p-a", 1, name="自动控制概述"),),
            ),
            ChapterDraft(
                id="ch2", title="B", position=1,  # duplicate position
                concepts=(placement("p-b", 1, name="拉普拉斯变换"),),
            ),
        ),
        paths=(),
    )
    cs.create_draft(bad)
    with pytest.raises(Exception) as excinfo:
        cs.confirm_draft("draft-1", confirmed_by="user")
    assert "validation failed" in str(excinfo.value)
    # nothing was activated, no concepts created, draft still draft
    assert store.list_concepts("c1") == []
    assert cs.get_active_curriculum("c1") is None
    assert cs.get_draft("draft-1").status == DraftStatus.DRAFT.value


def test_unconfirmed_draft_is_not_publishable(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    # AI never confirms; only the explicit user event works
    cs.get_draft("draft-1").confirm(confirmed_by="user")  # not persisted yet
    assert cs.get_draft("draft-1").status == DraftStatus.DRAFT.value


def test_confirm_requires_user_actor(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    with pytest.raises(Exception) as excinfo:
        cs.confirm_draft("draft-1", confirmed_by="ai")
    assert "cannot confirm" in str(excinfo.value)
    assert cs.get_active_curriculum("c1") is None


# ---------------------------------------------------------------------------
# 8. active uniqueness / 9. version
# ---------------------------------------------------------------------------


def test_activate_switches_active_and_supersedes(env) -> None:
    store, cs = env
    cs.create_draft(make_draft(draft_id="draft-1"))
    v1 = cs.confirm_draft("draft-1", confirmed_by="user")

    cs.create_draft(make_draft(draft_id="draft-2"))
    v2 = cs.confirm_draft("draft-2", confirmed_by="user")

    assert v2.version == 2
    assert cs.get_active_curriculum("c1").curriculum.id == v2.id
    versions = cs.list_curriculums("c1")
    assert [(c.version, c.status) for c in versions] == [
        (1, "superseded"),
        (2, "active"),
    ]
    # the superseded version still reads back structurally
    old_view = cs.get_curriculum(v1.id)
    assert old_view is not None
    assert old_view.curriculum.status == "superseded"


def test_version_is_assigned_monotonically_per_course(env) -> None:
    store, cs = env
    for draft_id in ("draft-1", "draft-2", "draft-3"):
        cs.create_draft(make_draft(draft_id=draft_id))
        curriculum = cs.confirm_draft(draft_id, confirmed_by="user")
        assert curriculum.version == cs.list_curriculums("c1")[-1].version
    assert [c.version for c in cs.list_curriculums("c1")] == [1, 2, 3]


def test_version_conflict_rolls_back_everything(env) -> None:
    store, cs = env
    cs.create_draft(make_draft(draft_id="draft-1"))
    v1 = cs.confirm_draft("draft-1", confirmed_by="user")

    # Build a second result with the SAME (course, version) and persist it.
    from core.learning.curriculum import CurriculumDraft

    clone = make_draft(draft_id="draft-clone")
    confirmed = clone.confirm(confirmed_by="user")
    result = build_curriculum_from_draft(
        confirmed,
        curriculum_id="cur-clone",
        version=1,  # collides with v1
        concept_ids={"p-intro": "K-intro", "p-laplace": "K-laplace",
                     "p-transfer": "K-transfer"},
    )
    with pytest.raises(CurriculumStoreIntegrityError):
        cs.activate_curriculum(result)

    # rollback: only v1 remains; no clone row; no half-written concepts
    versions = cs.list_curriculums("c1")
    assert [c.id for c in versions] == [v1.id]
    assert cs.get_curriculum("cur-clone") is None
    with store.connect() as connection:
        assert _count(connection, "chapter_concepts") == 3
        assert _count(connection, "learning_path_items") == 2


def test_activate_requires_existing_concepts(env) -> None:
    store, cs = env
    confirmed = make_draft().confirm(confirmed_by="user")
    result = build_curriculum_from_draft(
        confirmed,
        curriculum_id="cur-fake",
        version=1,
        concept_ids={"p-intro": "ghost-a", "p-laplace": "ghost-b",
                     "p-transfer": "ghost-c"},
    )
    with pytest.raises(CurriculumStoreIntegrityError):
        cs.activate_curriculum(result)
    assert cs.get_curriculum("cur-fake") is None


# ---------------------------------------------------------------------------
# 10. chapter ordering at the store
# ---------------------------------------------------------------------------


def test_chapter_ordering_round_trips_by_position(env) -> None:
    store, cs = env
    # Chapters stored out of order: the read projection orders by position.
    draft = make_draft(
        chapters=(
            ChapterDraft(id="ch-b", title="数学模型", position=2,
                         concepts=(placement("p-b", 1, name="拉普拉斯变换"),)),
            ChapterDraft(id="ch-a", title="绪论", position=1,
                         concepts=(placement("p-a", 1, name="自动控制概述"),)),
        ),
        paths=(),
    )
    cs.create_draft(draft)
    cs.confirm_draft("draft-1", confirmed_by="user")
    view = cs.get_active_curriculum("c1")
    assert [c.title for c in view.chapters] == ["绪论", "数学模型"]


# ---------------------------------------------------------------------------
# 11. course isolation at the store
# ---------------------------------------------------------------------------


def test_course_isolation_is_enforced_at_confirm(env) -> None:
    store, cs = env
    # A proposal that reuses a concept from ANOTHER course.
    foreign = store.add_concept("c1", "他课概念", created_at="2026-01-01T00:00:00+00:00")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO courses (id, name, status, created_at, updated_at)"
            " VALUES ('c2', '物理光学', 'active', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    moved = store.add_concept("c2", "传递函数-光学版", created_at="2026-01-01T00:00:00+00:00")

    draft = make_draft(
        chapters=(
            ChapterDraft(
                id="ch1", title="绪论", position=1,
                concepts=(placement("p-x", 1, name="外来概念", matched=moved.id),),
            ),
        ),
        paths=(),
    )
    cs.create_draft(draft)
    with pytest.raises(Exception) as excinfo:
        cs.confirm_draft("draft-1", confirmed_by="user")
    assert "course_isolation" in str(excinfo.value)
    assert cs.get_active_curriculum("c1") is None


# ---------------------------------------------------------------------------
# 12. archive
# ---------------------------------------------------------------------------


def test_archive_curriculum(env) -> None:
    store, cs = env
    cs.create_draft(make_draft())
    curriculum = cs.confirm_draft("draft-1", confirmed_by="user")
    cs.archive_curriculum(curriculum.id)
    assert cs.get_curriculum(curriculum.id).curriculum.status == "archived"
    assert cs.get_active_curriculum("c1") is None
    with pytest.raises(CurriculumStoreError):
        cs.archive_curriculum("missing")


# ---------------------------------------------------------------------------
# 13. transaction rollback
# ---------------------------------------------------------------------------


def test_transaction_rollback_on_failure_has_no_partial_state(env) -> None:
    """A mid-transaction failure (FK violation on a ghost concept) leaves ZERO
    partial rows — including the concepts created during resolution."""
    store, cs = env
    cs.create_draft(make_draft(draft_id="draft-1"))
    v1 = cs.confirm_draft("draft-1", confirmed_by="user")
    concepts_before = len(store.list_concepts("c1"))

    # Resolution will INSERT a brand-new concept, then the persist step hits a
    # foreign-key violation on the ghost matched concept -> the whole
    # transaction must roll back, concept creation included.
    bad = make_draft(
        draft_id="draft-2",
        chapters=(
            ChapterDraft(
                id="x1", title="新章节", position=1,
                concepts=(
                    placement("p-new", 1, name="全新概念甲"),
                    placement("p-ghost", 2, name="幽灵引用", matched="ghost-does-not-exist"),
                ),
            ),
        ),
        paths=(),
    )
    cs.create_draft(bad)
    with pytest.raises(CurriculumStoreIntegrityError):
        cs.confirm_draft("draft-2", confirmed_by="user")

    with store.connect() as connection:
        assert _count(connection, "curriculums") == 1  # only v1
        assert _count(connection, "curriculum_chapters") == 2  # v1's two chapters
        assert _count(connection, "chapter_concepts") == 3  # v1's
        assert _count(connection, "concepts") == concepts_before  # 全新概念甲 rolled back
    assert cs.get_draft("draft-2").status == DraftStatus.DRAFT.value
    assert cs.get_active_curriculum("c1").curriculum.id == v1.id


# ---------------------------------------------------------------------------
# 14. mastery zero-change
# ---------------------------------------------------------------------------


def test_activation_never_touches_mastery_or_learning_facts(env) -> None:
    store, cs = env
    # a concept with real learning history
    store.add_concept("c1", "传递函数", created_at="2026-01-01T00:00:00+00:00")
    transfer = store.find_concept("c1", "传递函数")
    store.apply_mastery_update(transfer.id, 4, "high", "existing-history", source="quiz")
    store.record_assessment("c1", transfer.id, score=0.9)
    store.create_review_item(transfer.id, due_at="2026-02-01T00:00:00+00:00")
    session = store.start_session("c1")

    before = (
        store.get_concept(transfer.id).mastery_level,
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(transfer.id)),
        len(store.list_sessions("c1")),
    )
    cs.create_draft(make_draft())
    cs.confirm_draft("draft-1", confirmed_by="user")
    after = (
        store.get_concept(transfer.id).mastery_level,
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(transfer.id)),
        len(store.list_sessions("c1")),
    )
    assert before == after
    assert store.get_concept(transfer.id).mastery_level == 4


# ---------------------------------------------------------------------------
# derived drafts / second version through the store
# ---------------------------------------------------------------------------


def test_full_version_chain_through_the_store(env) -> None:
    store, cs = env
    cs.create_draft(make_draft(draft_id="draft-1"))
    v1 = cs.confirm_draft("draft-1", confirmed_by="user")

    view = cs.get_active_curriculum("c1")
    assert view is not None
    d2 = draft_from_curriculum(
        view,
        draft_id="draft-2",
        concept_names={
            c.id: c.canonical_name for c in store.list_concepts("c1")
        },
    )
    cs.create_draft(d2)
    v2 = cs.confirm_draft("draft-2", confirmed_by="user")
    assert v2.version == 2
    assert v2.based_on_curriculum_id == v1.id
    # same concepts reused — mastery/history stays attached
    new_view = cs.get_active_curriculum("c1")
    assert {l.concept_id for l in new_view.chapter_concepts} == {
        l.concept_id for l in view.chapter_concepts
    }
    # both drafts survive
    assert {d.id for d in cs.list_drafts("c1")} == {"draft-1", "draft-2"}
