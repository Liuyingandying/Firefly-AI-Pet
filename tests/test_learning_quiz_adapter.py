"""Quiz adapter tests (Phase 1D).

Locks the frozen mapping QuizGrading -> AssessmentEvidence, the
deterministic feedback score parser, and the course-scoped concept binding
(no LLM binding, no auto-create).
"""

from __future__ import annotations

import pytest

from core.learning import LearningStore
from core.learning.quiz_adapter import (
    ConceptBinding,
    QuizGrading,
    bind_quiz_to_concept,
    derive_score_from_feedback,
    quiz_grading_to_evidence,
)


@pytest.fixture()
def store(tmp_path) -> LearningStore:
    s = LearningStore(tmp_path / "learning.sqlite3")
    s.initialize()
    return s


@pytest.fixture()
def course_with_concepts(store):
    course = store.create_course("自动控制原理")
    c1 = store.add_concept(course.id, "传递函数", created_at="2026-09-01T00:00:00+00:00")
    c2 = store.add_concept(course.id, "根轨迹", created_at="2026-09-01T00:01:00+00:00")
    return store, course.id, c1, c2


def _grading(**kwargs) -> QuizGrading:
    defaults = dict(question="传递函数的定义是什么？", answer="零极点形式的比值",
                    feedback="正确的部分：说对了定义\n缺失的部分：无\n错误理解：无")
    defaults.update(kwargs)
    return QuizGrading(**defaults)


# 1. Quiz Result -> AssessmentEvidence ----------------------------------------


def test_grading_maps_to_evidence() -> None:
    grading = _grading(score=0.9, quiz_id="q-1", question_type="definition",
                       created_at="2026-09-02T00:00:00+00:00")
    evidence = quiz_grading_to_evidence(grading, "concept-1", session_id="s-1")
    assert evidence.concept_id == "concept-1"
    assert evidence.source == "quiz"
    assert evidence.score == 0.9
    assert evidence.confidence == 0.8  # frozen default
    assert evidence.difficulty == "basic"  # frozen default
    assert evidence.session_id == "s-1"
    assert evidence.question_type == "definition"
    assert evidence.created_at == "2026-09-02T00:00:00+00:00"
    assert evidence.metadata["quiz_id"] == "q-1"
    assert evidence.metadata["question"] == grading.question
    assert evidence.metadata["difficulty"] == "basic"


def test_evidence_score_defaults_from_feedback_sections() -> None:
    correct = quiz_grading_to_evidence(_grading(), "c1")
    assert correct.score == 0.95
    assert correct.metadata["score_source"] == "feedback_sections"
    missing = quiz_grading_to_evidence(
        _grading(feedback="正确的部分：定义\n缺失的部分：零点\n错误理解：无"), "c1")
    assert missing.score == 0.7
    wrong = quiz_grading_to_evidence(
        _grading(feedback="正确的部分：无\n缺失的部分：无\n错误理解：概念混淆"), "c1")
    assert wrong.score == 0.3


def test_feedback_parser_sections() -> None:
    assert derive_score_from_feedback(
        "正确的部分：说对了\n缺失的部分：无\n错误理解：无") == 0.95
    assert derive_score_from_feedback(
        "正确的部分：部分\n缺失的部分：零点\n错误理解：无") == 0.7
    assert derive_score_from_feedback(
        "正确的部分：无\n缺失的部分：无\n错误理解：闭环开环混淆") == 0.3
    assert derive_score_from_feedback("随便聊几句") is None


def test_explicit_score_overrides_feedback_parse() -> None:
    evidence = quiz_grading_to_evidence(_grading(score=0.55), "c1")
    assert evidence.score == 0.55
    assert evidence.metadata["score_source"] == "explicit"


# 8. concept binding: no auto-binding, no auto-create --------------------------


def test_bind_unique_concept_auto_binds(store, course_with_concepts) -> None:
    store, course_id, c1, _ = course_with_concepts
    # Single-concept course: auto-bind.
    solo_course = store.create_course("物理光学")
    store.add_concept(solo_course.id, "干涉")
    binding = bind_quiz_to_concept(store, solo_course.id)
    assert binding.status == "unique"
    assert binding.concept_name == "干涉"


def test_bind_exact_match_by_name(course_with_concepts) -> None:
    store, course_id, c1, _ = course_with_concepts
    binding = bind_quiz_to_concept(store, course_id, "传递函数")
    assert binding.status == "exact"
    assert binding.concept_id == c1.id
    # Case/space variants resolve identically.
    assert bind_quiz_to_concept(store, course_id, " 传递函数 ").concept_id == c1.id


def test_bind_multiple_requires_user_choice(course_with_concepts) -> None:
    store, course_id, _, _ = course_with_concepts
    binding = bind_quiz_to_concept(store, course_id)
    assert binding.status == "needs_choice"
    assert len(binding.choices) == 2
    assert binding.concept_id is None  # never auto-picked


def test_bind_without_concept_returns_candidate_never_creates(
        store, course_with_concepts) -> None:
    store, course_id, _, _ = course_with_concepts
    binding = bind_quiz_to_concept(store, course_id, "不存在的概念")
    assert binding.status == "candidate"
    assert binding.concept_id is None
    # No concept was created by the binding attempt.
    assert len(store.list_concepts(course_id)) == 2
    # Unrelated course with no concepts: candidate too.
    empty = store.create_course("新课")
    assert bind_quiz_to_concept(store, empty.id).status == "candidate"


def test_bind_matches_alias(course_with_concepts) -> None:
    store, course_id, c1, _ = course_with_concepts
    # Alias matching goes through the course-scoped merge API (public).
    store.get_or_add_concept(course_id, "传递函数", aliases=["transfer function"])
    binding = bind_quiz_to_concept(store, course_id, "Transfer Function")
    assert binding.status == "exact"
    assert binding.concept_id == c1.id
