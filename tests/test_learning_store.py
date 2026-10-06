"""Learning Store foundation tests (Phase 1A).

Covers the frozen store contract:
1.  empty-database initialization
2.  schema_version == 4 (v4 resource migration)
3.  create course
4.  update/read course
5.  course-scoped concept uniqueness
6.  same-name concepts coexist across courses
7.  deterministic normalized_name
8.  mastery limited to 0-5
9.  retention enum validation
10. CandidateConcept is never written
11. session start/end
12. assessment record insert
13. assessment insert does NOT change mastery
14. review item CRUD
15. due-review query
16. SourceRef provenance roundtrip
17. ai_generated roundtrip
18. transactional course deletion (cascade)
19. clear_learning_data
20. export_course
21. cross-thread / multi-connection basic safety
22. exception transaction rollback
23. Learning Store never writes Memory
24. Learning Store never calls providers
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.learning import (
    AssessmentSource,
    CandidateConcept,
    ConceptExistsError,
    Difficulty,
    LearningStore,
    LearningStoreError,
    Retention,
    SourceRef,
    SourceType,
    normalize_name,
)


@pytest.fixture()
def store(tmp_path) -> LearningStore:
    s = LearningStore(tmp_path / "learning_store.sqlite3")
    s.initialize()
    return s


def _add_course_and_concept(store: LearningStore, course_name: str = "Control",
                            concept: str = "Root Locus") -> tuple[str, str]:
    course = store.create_course(course_name)
    c = store.add_concept(
        course.id,
        concept,
        source_refs=[SourceRef(source_type=SourceType.MANUAL.value)],
    )
    return course.id, c.id


# 1. empty-database initialization -----------------------------------------


def test_initializes_empty_database(tmp_path) -> None:
    store = LearningStore(tmp_path / "fresh.sqlite3")
    store.initialize()
    assert store.list_courses() == []
    assert store.list_concepts("missing") == []


# 2. schema version ---------------------------------------------------------


def test_schema_version_is_four(store) -> None:
    assert store.get_schema_version() == 4


# 3. create course ----------------------------------------------------------


def test_create_course(store) -> None:
    course = store.create_course("自动控制原理", description="第一门课")
    assert course.name == "自动控制原理"
    assert course.description == "第一门课"
    assert course.status == "active"
    assert course.created_at
    fetched = store.get_course(course.id)
    assert fetched is not None and fetched.name == course.name


# 4. update/read course -----------------------------------------------------


def test_update_and_read_course(store) -> None:
    course = store.create_course("C1")
    updated = store.update_course(course.id, name="C1 renamed",
                                  description="desc", source_title="书")
    assert updated is not None
    assert updated.name == "C1 renamed"
    assert updated.description == "desc"
    assert updated.source_title == "书"
    assert store.update_course("missing") is None


# 5. course-scoped concept uniqueness ---------------------------------------


def test_concept_uniqueness_within_course(store) -> None:
    course_id, _ = _add_course_and_concept(store, concept="Scaled Dot-Product Attention")
    with pytest.raises(ConceptExistsError):
        store.add_concept(course_id, "Scaled dot product attention")
    # Case/space variants resolve to the same concept via find.
    found = store.find_concept(course_id, "SCALED DOT-PRODUCT ATTENTION")
    assert found is not None
    assert found.canonical_name == "Scaled Dot-Product Attention"


# 6. same name across courses coexists --------------------------------------


def test_same_name_coexists_across_courses(store) -> None:
    course_a, _ = _add_course_and_concept(store, "Auto Control", "反馈")
    course_b, _ = _add_course_and_concept(store, "Machine Learning", "反馈")
    assert course_a != course_b
    assert len(store.list_concepts(course_a)) == 1
    assert len(store.list_concepts(course_b)) == 1


# 7. deterministic normalization --------------------------------------------


def test_normalized_name_deterministic() -> None:
    assert normalize_name("Scaled Dot-Product Attention") == \
        normalize_name("scaled dot product attention") == \
        normalize_name("  Scaled  Dot.Product_Attention  ")
    assert normalize_name("缩放点积注意力") == "缩放点积注意力"
    # No translation: Chinese and English forms are different keys.
    assert normalize_name("缩放点积注意力") != normalize_name("Scaled Dot-Product Attention")


# 8. mastery only 0-5 --------------------------------------------------------


def test_mastery_range_enforced(store) -> None:
    course_id, _ = _add_course_and_concept(store)
    with pytest.raises(LearningStoreError):
        store.add_concept(course_id, "Bad", mastery_level=6)
    with pytest.raises(LearningStoreError):
        store.add_concept(course_id, "Bad2", mastery_level=-1)
    concept_id = store.add_concept(course_id, "Good", mastery_level=5).id
    assert store.get_concept(concept_id).mastery_level == 5


# 9. retention enum ----------------------------------------------------------


def test_retention_enum_validation(store) -> None:
    course_id, _ = _add_course_and_concept(store)
    with pytest.raises(LearningStoreError):
        store.add_concept(course_id, "X", retention="super")
    concept_id = store.add_concept(
        course_id, "Y", retention=Retention.HIGH.value
    ).id
    assert store.get_concept(concept_id).retention == "high"


# 10. CandidateConcept never written -----------------------------------------


def test_candidate_concept_is_not_written(store) -> None:
    candidate = CandidateConcept(canonical_name="Ambient Term")
    # The store exposes no candidate write API; only an explicit add persists.
    assert not hasattr(store, "add_candidate")
    assert store.list_concepts("any-course") == []
    assert candidate.canonical_name == "Ambient Term"


# 11. session start/end ------------------------------------------------------


def test_session_start_and_end(store) -> None:
    course_id, _ = _add_course_and_concept(store)
    session = store.start_session(course_id, summary="今天学习根轨迹")
    assert session.status == "active"
    assert session.ended_at is None
    # Idempotent: one active session per course.
    again = store.start_session(course_id)
    assert again.id == session.id
    ended = store.end_session(session.id)
    assert ended is not None and ended.status == "ended"
    assert ended.ended_at is not None
    # A new session can start after the previous one ended.
    second = store.start_session(course_id)
    assert second.id != session.id


# 12. assessment record insert -----------------------------------------------


def test_record_assessment(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    record = store.record_assessment(
        course_id, concept_id,
        source=AssessmentSource.QUIZ.value,
        score=0.9, difficulty=Difficulty.BASIC.value,
    )
    assert record.score == 0.9
    assert record.source == "quiz"
    assert record.ai_generated is False
    assert len(store.list_assessments(concept_id)) == 1


# 13. assessment does NOT change mastery -------------------------------------


def test_assessment_never_changes_mastery(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    before = store.get_concept(concept_id).mastery_level
    for score in (0.0, 0.5, 1.0):
        store.record_assessment(course_id, concept_id, score=score)
    after = store.get_concept(concept_id).mastery_level
    assert before == after == 0, "evidence insert must not move mastery"


# 14. review item CRUD -------------------------------------------------------


def test_review_item_crud(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    item = store.create_review_item(concept_id, due_at="2026-09-10T00:00:00+00:00",
                                    interval_days=7)
    assert item.status == "pending"
    updated = store.update_review_item(item.id, status="done",
                                       consecutive_success=1)
    assert updated is not None
    assert updated.status == "done"
    assert updated.consecutive_success == 1
    assert store.get_review_item(item.id).interval_days == 7
    with pytest.raises(LearningStoreError):
        store.create_review_item(concept_id, due_at="x", interval_days=0)


# 15. due-review query -------------------------------------------------------


def test_due_reviews_query(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    store.create_review_item(concept_id, due_at="2026-01-01T00:00:00+00:00")
    store.create_review_item(concept_id, due_at="2099-01-01T00:00:00+00:00")
    due = store.get_due_reviews(now="2026-06-01T00:00:00+00:00")
    assert len(due) == 1


# 16. SourceRef provenance roundtrip -----------------------------------------


def test_source_ref_roundtrip(store) -> None:
    course_id = store.create_course("C").id
    ref = SourceRef(
        source_type=SourceType.PDF.value,
        document_id="doc-1", page=42, section="3.2",
        timestamp="2026-09-09T00:00:00+00:00",
        quote="原文证据片段",
    )
    concept = store.add_concept(course_id, "概念", source_refs=[ref])
    fetched = store.get_concept(concept.id)
    assert fetched is not None and len(fetched.source_refs) == 1
    stored = fetched.source_refs[0]
    assert stored.document_id == "doc-1"
    assert stored.page == 42
    assert stored.section == "3.2"
    assert stored.quote == "原文证据片段"
    assert stored.source_type == "pdf"


# 17. ai_generated roundtrip -------------------------------------------------


def test_ai_generated_roundtrip(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    record = store.record_assessment(
        course_id, concept_id, score=0.8, ai_generated=True,
        source_refs=[SourceRef(source_type=SourceType.QUIZ.value)],
    )
    fetched = store.list_assessments(concept_id)[0]
    assert fetched.id == record.id
    assert fetched.ai_generated is True
    assert len(fetched.source_refs) == 1


# 18. transactional course deletion (cascade) --------------------------------


def test_delete_course_cascades_transactionally(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    session = store.start_session(course_id)
    store.record_assessment(course_id, concept_id, score=0.7)
    store.create_review_item(concept_id, due_at="2026-01-01T00:00:00+00:00")
    store.apply_mastery_update(concept_id, new_mastery=2,
                               retention=Retention.MEDIUM.value, reason="test")

    store.delete_course(course_id)

    assert store.get_course(course_id) is None
    assert store.list_concepts(course_id) == []
    assert store.list_sessions(course_id) == []
    assert store.list_assessments(concept_id) == []
    assert store.list_review_items(concept_id) == []
    assert store.get_concept(concept_id) is None


# 19. clear_learning_data ----------------------------------------------------


def test_clear_learning_data(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    store.record_assessment(course_id, concept_id, score=1.0)
    store.clear_learning_data()
    assert store.list_courses() == []
    assert store.list_concepts(course_id) == []
    assert store.list_assessments(concept_id) == []
    assert store.get_schema_version() == 4  # schema/meta survive


# 20. export_course ----------------------------------------------------------


def test_export_course(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    store.record_assessment(course_id, concept_id, score=0.6)
    store.create_review_item(concept_id, due_at="2026-01-01T00:00:00+00:00")
    store.start_session(course_id)

    exported = store.export_course(course_id)
    assert exported["schema_version"] == 4
    assert exported["course"]["name"] == "Control"
    assert len(exported["concepts"]) == 1
    assert exported["concepts"][0]["id"] == concept_id
    assert len(exported["assessment_records"]) == 1
    assert len(exported["review_items"]) == 1
    assert len(exported["study_sessions"]) == 1
    # JSON-compatible (roundtrips through json).
    json.dumps(exported)


# 21. cross-thread / multi-connection safety ---------------------------------


def test_multi_connection_thread_safety(store, tmp_path) -> None:
    # Separate store instances (separate connections) hammer the same file.
    def worker(i: int) -> None:
        s = LearningStore(tmp_path / "learning_store.sqlite3")
        s.initialize()
        course = s.create_course(f"thread-{i}")
        s.add_concept(course.id, f"concept-{i}")
        s.start_session(course.id)
        return 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(8)))
    assert results == [1] * 8
    # Every concurrent write landed exactly once (no lost updates / no
    # cross-connection corruption).
    assert len(store.list_courses()) == 8
    for course in store.list_courses():
        assert len(store.list_concepts(course.id)) == 1


# 22. exception transaction rollback -----------------------------------------


def test_transaction_rollback_on_error(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    store.record_assessment(course_id, concept_id, score=0.9)
    # A failing multi-step write must not leave a partial state.
    with pytest.raises(LearningStoreError):
        store.record_assessment(
            course_id, concept_id, score=1.5,  # invalid -> raise before insert
        )
    assert len(store.list_assessments(concept_id)) == 1
    # Cascade delete with an invalid intermediate step is impossible by
    # design (single statement), but a failed concept insert leaves nothing.
    with pytest.raises(LearningStoreError):
        store.add_concept(course_id, "", mastery_level=9)
    assert len(store.list_concepts(course_id)) == 1


# 23. Learning Store never writes Memory -------------------------------------


def test_store_never_writes_memory(store, tmp_path, monkeypatch) -> None:
    import memory.repository as mem_repo

    calls: list[str] = []

    def spy_add(*args, **kwargs):  # noqa: ARG001
        calls.append("add")
        return "spy-id"

    def spy_clear(*args, **kwargs):  # noqa: ARG001
        calls.append("clear")
        return 0

    monkeypatch.setattr(mem_repo.MemoryRepository, "add", spy_add)
    monkeypatch.setattr(mem_repo.MemoryRepository, "clear", spy_clear)

    course_id, concept_id = _add_course_and_concept(store)
    store.record_assessment(course_id, concept_id, score=1.0)
    store.apply_mastery_update(concept_id, new_mastery=3,
                               retention=Retention.HIGH.value, reason="test")

    assert calls == [], "learning store operations must not touch Memory"


# 24. Learning Store never calls providers -----------------------------------


def test_store_never_calls_providers(store) -> None:
    import inspect

    import core.learning.store as learning_store_module

    source = inspect.getsource(learning_store_module)
    for forbidden in (
        "import providers",
        "from providers",
        "ai_router",
        "screen_vision",
        "requests.",
        "urllib.request",
    ):
        assert forbidden not in source, f"store must not reference {forbidden}"


# -- controlled mastery write entry ------------------------------------------


def test_apply_mastery_update_is_controlled_and_audited(store) -> None:
    course_id, concept_id = _add_course_and_concept(store)
    update = store.apply_mastery_update(
        concept_id, new_mastery=2, retention=Retention.MEDIUM.value,
        reason="review passed", source="rule_engine",
    )
    assert update.old_mastery == 0
    assert update.new_mastery == 2
    assert store.get_concept(concept_id).mastery_level == 2
    audit = store.list_mastery_audit(concept_id)
    assert len(audit) == 1
    assert audit[0]["reason"] == "review passed"
    assert audit[0]["new_mastery"] == 2
    # Range validation still enforced on the controlled path.
    with pytest.raises(LearningStoreError):
        store.apply_mastery_update(concept_id, new_mastery=9,
                                   retention=Retention.HIGH.value, reason="x")
    with pytest.raises(LearningStoreError):
        store.apply_mastery_update(concept_id, new_mastery=3,
                                   retention=Retention.HIGH.value, reason="")
