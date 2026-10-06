"""Assessment flow tests (Phase 1D).

Locks the first full learning loop:

    check-understanding request -> question -> answer
      -> AssessmentEvidence -> RuleEngine -> Store (record/mastery/review)

plus the isolation guarantees: ordinary chat and PageLens explanations
never create assessments, repeated submits never double-record, and
grading failures degrade gracefully.
"""

from __future__ import annotations

import pytest

from core.learning import LearningStore
from core.learning.assessment import (
    AssessmentService,
    AssessmentState,
    is_check_understanding_request,
)
from core.learning.controller import LearningModeController
from core.learning.quiz_adapter import QuizGrading


T0 = "2026-09-01T00:00:00+00:00"


def _day(n: int) -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return (base + timedelta(days=n)).isoformat()


class _FakeSettings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


@pytest.fixture()
def store(tmp_path) -> LearningStore:
    s = LearningStore(tmp_path / "learning.sqlite3")
    s.initialize()
    return s


def _make_course_and_concept(store: LearningStore) -> tuple[str, str]:
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "传递函数", created_at=T0)
    return course.id, concept.id


def _pass_grading(concept: str) -> QuizGrading:
    return QuizGrading(
        question="传递函数的定义是什么？", answer="零极点比值",
        feedback=f"正确的部分：说清了{concept}的定义\n缺失的部分：无\n错误理解：无",
    )


def _fail_grading(concept: str) -> QuizGrading:
    return QuizGrading(
        question="传递函数的定义是什么？", answer="不知道",
        feedback=f"正确的部分：无\n缺失的部分：无\n错误理解：混淆了{concept}的对象",
    )


# 2. basic quiz: 0->1 -> 2 -> 3 ------------------------------------------------


def test_basic_quiz_promotes_step_by_step(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    assert service.start_assessment(course_id, "自动控制原理") == \
        "传递函数的定义是什么？"
    assert service.state == AssessmentState.WAITING_ANSWER

    outcome = service.submit_answer("零极点比值")
    assert service.state == AssessmentState.RECORDED
    assert outcome.decision.new_mastery == 1  # 0 -> 1
    assert len(store.list_assessments(concept_id)) == 1
    # ReviewItem scheduled by the rule engine path (1 day for mastery 1).
    assert len(store.list_review_items(concept_id)) == 1

    for expected in (2, 3):
        service.start_assessment(course_id, "自动控制原理")
        outcome = service.submit_answer("回答")
        assert outcome.decision.new_mastery == expected
    assert store.get_concept(concept_id).mastery_level == 3
    assert len(store.list_assessments(concept_id)) == 3


# 3. basic: 3->4 forbidden ------------------------------------------------------


def test_basic_quiz_cannot_reach_4(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    for _ in range(4):  # 0->1->2->3, then a 4th basic pass hits the cap
        service.start_assessment(course_id, "自动控制原理")
        outcome = service.submit_answer("回答")
    assert outcome.decision.new_mastery == 3
    assert outcome.decision.reason == "difficulty_cap"


# 4. variant promotion (needs 2 passes across >=24h) ---------------------------


def test_variant_quiz_promotes_after_second_pass(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    # Climb to 3 with backdated daily basic passes (rule engine directly).
    from core.learning.models import AssessmentEvidence
    from core.learning.rule_engine import LearningRuleEngine

    rule_engine = LearningRuleEngine(store)
    for level in range(3):
        rule_engine.apply_assessment(
            AssessmentEvidence(concept_id=concept_id, source="quiz",
                               score=1.0, difficulty="basic",
                               created_at=_day(level)),
            now=_day(level))

    service = AssessmentService(
        store, rule_engine=rule_engine,
        generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _variant_gradings.pop(0),
        )
    # Two variant passes spanning >=24h (explicit timestamps on the grading).
    _variant_gradings = [
        QuizGrading(question="变式1", answer="a",
                    feedback="正确的部分：很好\n缺失的部分：无\n错误理解：无",
                    difficulty="variant", created_at=_day(5)),
        QuizGrading(question="变式2", answer="a",
                    feedback="正确的部分：很好\n缺失的部分：无\n错误理解：无",
                    difficulty="variant", created_at=_day(6)),
    ]
    service.start_assessment(course_id, "自动控制原理")
    outcome = service.submit_answer("变式回答")
    assert outcome.decision.new_mastery == 3  # only one variant pass so far
    service.start_assessment(course_id, "自动控制原理")
    outcome = service.submit_answer("变式回答2")
    assert (outcome.decision.old_mastery, outcome.decision.new_mastery) == (3, 4)


# 5. low score: recorded but no promotion ---------------------------------------


def test_low_score_recorded_without_promotion(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _fail_grading(c))
    service.start_assessment(course_id, "自动控制原理")
    outcome = service.submit_answer("不知道")
    assert outcome.decision.new_mastery == 0  # fail never promotes
    assert len(store.list_assessments(concept_id)) == 1
    assert store.get_concept(concept_id).retention == "low"
    assert store.get_concept(concept_id).state == "needs_review"


# 6. ordinary chat: no assessment ------------------------------------------------


def test_ordinary_chat_never_creates_assessment(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    controller = LearningModeController(store=store, settings=_FakeSettings())
    controller.create_course("自动控制原理")
    assert len(store.list_assessments(concept_id)) == 0
    # Ordinary phrases pass through untouched.
    assert controller.handle_text("你好") is None
    assert controller.handle_text("什么是传递函数") is None
    assert len(store.list_assessments(concept_id)) == 0


def test_pagelens_explanation_never_touches_mastery(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    before = store.get_concept(concept_id).mastery_level
    # A PageLens-style explanation is not assessment evidence; nothing in
    # the learning domain consumes it.
    from core.learning.models import CandidateConcept

    candidate = CandidateConcept(canonical_name="传递函数")
    assert candidate.canonical_name  # ambient only, never stored
    assert store.get_concept(concept_id).mastery_level == before
    assert store.list_assessments(concept_id) == []


# 7. check-understanding markers route to the assessment flow --------------------


def test_check_understanding_request_starts_assessment(store) -> None:
    from core.learning.assessment import is_check_understanding_request

    assert is_check_understanding_request("检查一下我的理解")
    assert is_check_understanding_request("考考我")
    assert not is_check_understanding_request("你好")


def test_controller_routes_explicit_check_request(store) -> None:
    controller = LearningModeController(store=store, settings=_FakeSettings())
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    concept_id = store.add_concept(course_id, "传递函数", created_at=T0).id
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    controller.assessment_service = service

    reply = controller.handle_text("检查一下我理解了吗")
    assert reply == "传递函数的定义是什么？"
    assert service.state == AssessmentState.WAITING_ANSWER

    reply = controller.handle_text("零极点比值")
    assert "掌握度" in reply
    assert store.get_concept(concept_id).mastery_level == 1


# 8. no concept / multiple concepts: no blind binding ---------------------------


def test_start_assessment_without_concept_does_not_bind(store) -> None:
    course = store.create_course("新课")
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    reply = service.start_assessment(course.id, "新课")
    assert "知识点" in reply
    assert service.state == AssessmentState.IDLE
    assert service.active is None


def test_start_assessment_multiple_concepts_asks_user(store) -> None:
    course = store.create_course("自动控制原理")
    store.add_concept(course.id, "传递函数", created_at=T0)
    store.add_concept(course.id, "根轨迹", created_at=T0)
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    reply = service.start_assessment(course.id, "自动控制原理")
    assert "传递函数" in reply and "根轨迹" in reply
    assert service.active is None
    # Explicit concept name binds exactly.
    reply = service.start_assessment(course.id, "自动控制原理", requested_concept="根轨迹")
    assert reply == "根轨迹？"
    assert service.active.concept_name == "根轨迹"


# 9. repeated submit: exactly one record ------------------------------------------


def test_repeated_submit_records_once(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    service.start_assessment(course_id, "自动控制原理")
    first = service.submit_answer("回答")
    second = service.submit_answer("再答一次")
    assert second.state == AssessmentState.IDLE
    assert second.decision is None
    assert len(store.list_assessments(concept_id)) == 1


# 10. failure recovery -------------------------------------------------------------


def test_grading_failure_degrades_without_record(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    def broken_grader(concept, course, question, answer):
        raise RuntimeError("provider down")

    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=broken_grader)
    service.start_assessment(course_id, "自动控制原理")
    outcome = service.submit_answer("回答")
    assert outcome.state == AssessmentState.FAILED
    assert store.list_assessments(concept_id) == []
    assert store.get_concept(concept_id).mastery_level == 0
    # The failed flow is closed; a new assessment can start immediately.
    assert service.active is None
    assert service.start_assessment(course_id, "自动控制原理")


def test_question_generation_failure_sets_failed(store) -> None:
    course_id, concept_id = _make_course_and_concept(store)
    service = AssessmentService(
        store, generate_question=lambda c, k: "",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    reply = service.start_assessment(course_id, "自动控制原理")
    assert "再让我试一次" in reply
    assert service.state == AssessmentState.FAILED


# -- real-chain consistency: record + mastery + review -----------------------------


def test_full_chain_consistency(store) -> None:
    controller = LearningModeController(store=store, settings=_FakeSettings())
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    concept_id = store.add_concept(course_id, "传递函数", created_at=T0).id
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    controller.assessment_service = service

    controller.handle_text("检查一下我理解了吗")
    controller.handle_text("零极点比值")

    # The three stores agree: one record, mastery 1, one review item.
    records = store.list_assessments(concept_id)
    assert len(records) == 1
    assert records[0].session_id == controller.state.active_session_id
    concept = store.get_concept(concept_id)
    assert concept.mastery_level == 1
    assert concept.state == "learning"
    reviews = store.list_review_items(concept_id)
    assert len(reviews) == 1
    assert reviews[0].interval_days == 1


# ---------------------------------------------------------------------------
# Phase 8A: needs_choice resume wiring
# ---------------------------------------------------------------------------


def test_needs_choice_can_be_resumed_with_a_concept_name(store) -> None:
    """要检查哪个知识点 → 用户报名概念 → 测验对该概念启动（Phase 8A 接线）。"""
    course = store.create_course("自动控制原理")
    store.add_concept(course.id, "传递函数")
    store.add_concept(course.id, "方框图")
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))

    reply = service.start_assessment(course.id, "自动控制原理")
    assert "要检查哪个知识点" in reply
    assert service.pending_choice is not None
    assert service.active is None                      # nothing started yet

    resumed = service.resume_pending_choice("传递函数")
    assert resumed == "传递函数的定义是什么？"
    assert service.state == AssessmentState.WAITING_ANSWER
    assert service.pending_choice is None              # cleared on success
    assert service.active.concept_name == "传递函数"


def test_resume_without_a_pending_choice_is_none(store) -> None:
    store = store  # noqa: F841 — clarity
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    assert service.resume_pending_choice("传递函数") is None


def test_cancel_clears_the_pending_choice(store) -> None:
    course = store.create_course("自动控制原理")
    store.add_concept(course.id, "传递函数")
    store.add_concept(course.id, "方框图")
    service = AssessmentService(
        store, generate_question=lambda c, k: f"{c}？",
        grade_answer=lambda c, k, q, a: _pass_grading(c))
    service.start_assessment(course.id, "自动控制原理")
    assert service.pending_choice is not None

    service.cancel()
    assert service.pending_choice is None
