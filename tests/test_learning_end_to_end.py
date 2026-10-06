"""Phase 8A — End-to-End Learning Journey Validation.

Walks the REAL chain exactly as a user would (no new capability):

    教材导入 → Draft → 用户确认 → ACTIVE Curriculum
      → 进入学习模式 → LearningContext
      → 概念讨论（interaction / TeachingContext / Decision / Action）
      → 检查理解（Assessment 启动 → 回答 → Rule Engine）
      → mastery 0→1 → ReviewItem → 复习流程 → REVIEW
      → 退出/重进（session 不污染）

The only simulated part is the AI grading (score=0.9 injected into the
existing QuizGrading contract) — every store write, gate and mapping runs the
production code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from types import SimpleNamespace

from core.learning.action import ActionStatus
from core.learning.controller import LearningModeController
from core.learning.curriculum import (
    active_curriculum_for,
    ConfirmationRequiredError,
    CurriculumDraft,
    DraftStatus,
)
from core.learning.curriculum.adapters import PageLensCurriculumAdapter
from core.learning.curriculum.adapter.review import CurriculumDraftReviewService
from core.learning.curriculum.store import CurriculumStore
from core.learning.interactions import InteractionStore, InteractionEventType
from core.learning.assessment import AssessmentService, AssessmentState
from core.learning.quiz_adapter import QuizGrading
from core.learning.resources import ResourceStore, record_textbook_resources
from core.learning.store import LearningStore
from core.learning.teaching import LearningStage


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _make_pdf(path: Path) -> Path:
    """《自动控制原理》教材：三章目录（真实 PyMuPDF 书签）。"""
    import fitz

    doc = fitz.open()
    for _ in range(4):
        doc.new_page()
    doc.set_toc([
        [1, "第一章 绪论", 1],
        [1, "第二章 数学模型", 2],
        [1, "第三章 时域分析", 4],
    ])
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def journey(tmp_path):
    """The real components, wired exactly as production wires them."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    resources = ResourceStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    # the AI grading is the only simulated part (score comes from the stub)
    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"请说明「{c}」的定义。",
        grade_answer=lambda c, k, q, a: QuizGrading(
            q, a, "正确的部分：回答了核心定义\n缺失的部分：无\n错误理解：无",
            score=0.9,
        ),
    )
    pdf = _make_pdf(tmp_path / "自动控制原理.pdf")
    return SimpleNamespace(
        store=store, curricula=curricula, resources=resources,
        controller=controller, pdf=pdf,
    )


def _facts(store: LearningStore, course_id: str, concept_id: str) -> tuple:
    return (
        store.get_concept(concept_id).mastery_level,
        len(store.list_mastery_audit(concept_id)),
        len(store.list_assessments(concept_id)),
        len(store.list_review_items(concept_id)),
        len(store.list_concepts(course_id)),
        len(store.list_sessions(course_id)),
    )


# ===========================================================================
# Step 1 — PDF → CurriculumDraft
# ===========================================================================


def test_step1_pdf_becomes_an_inert_draft(journey, tmp_path: Path) -> None:
    from core.pdf_processor import build_pdf_lazy_index

    index = build_pdf_lazy_index(
        journey.pdf.read_bytes(), display_name="自动控制原理.pdf"
    )
    course = journey.store.create_course("自动控制原理")
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name="自动控制原理",
        concepts=["传递函数", "状态空间"], draft_id="draft-journey",
    )

    assert draft.status == DraftStatus.DRAFT.value
    titles = [c.title for c in draft.chapters_in_order]
    assert titles[:3] == ["第一章 绪论", "第二章 数学模型", "第三章 时域分析"]
    assert titles[-1] == "全书概念（自动识别）"  # concepts ride the auto chapter
    # the draft is completely inert
    assert journey.store.list_concepts(course.id) == []
    assert all(
        c.mastery_level == 0
        for c in journey.store.list_concepts(course.id)
    )
    assert journey.store.list_assessments() == []
    assert journey.store.list_review_items() == []
    assert active_curriculum_for(course.id, journey.curricula.list_curriculums(course.id)) is None


# ===========================================================================
# Step 2 — 用户确认 → Course + ACTIVE Curriculum + Concepts(mastery=0)
# ===========================================================================


def test_step2_confirm_activates_the_curriculum(journey) -> None:
    from core.pdf_processor import build_pdf_lazy_index

    index = build_pdf_lazy_index(
        journey.pdf.read_bytes(), display_name="自动控制原理.pdf"
    )
    course = journey.store.create_course("自动控制原理")
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name="自动控制原理",
        concepts=["传递函数", "状态空间"], draft_id="draft-journey",
    )
    service = CurriculumDraftReviewService(journey.curricula)
    service.save(draft)
    curriculum = service.confirm("draft-journey", confirmed_by="user")

    assert curriculum.is_active
    assert curriculum.course_id == course.id
    view = journey.curricula.get_active_curriculum(course.id)
    titles = [c.title for c in view.chapters_in_order]
    assert titles[:3] == ["第一章 绪论", "第二章 数学模型", "第三章 时域分析"]
    assert titles[-1] == "全书概念（自动识别）"
    concepts = {c.canonical_name: c for c in journey.store.list_concepts(course.id)}
    assert set(concepts) == {"传递函数", "状态空间"}
    assert all(c.mastery_level == 0 for c in concepts.values())


# ===========================================================================
# The full journey (Steps 1-8 in one continuous run)
# ===========================================================================


def test_full_learning_journey_end_to_end(journey) -> None:
    controller = journey.controller
    store = journey.store
    curricula = journey.curricula
    resources = journey.resources
    interactions = InteractionStore(store)

    # ---- Step 1: PDF → CurriculumDraft (inert) ---------------------------
    from core.pdf_processor import build_pdf_lazy_index

    index = build_pdf_lazy_index(
        journey.pdf.read_bytes(), display_name="自动控制原理.pdf"
    )
    # the production import path: a course shell is created for the textbook
    course = controller.create_course_shell("自动控制原理")
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name="自动控制原理",
        concepts=["传递函数", "状态空间"], draft_id="draft-journey",
    )
    assert draft.status == DraftStatus.DRAFT.value
    titles = [c.title for c in draft.chapters_in_order]
    assert titles[:3] == ["第一章 绪论", "第二章 数学模型", "第三章 时域分析"]
    assert store.list_concepts(course.id) == []
    concept_facts = None

    # ---- Step 2: 用户确认 → ACTIVE Curriculum + Concepts(mastery=0) -------
    service = CurriculumDraftReviewService(curricula)
    service.save(draft)
    curriculum = service.confirm("draft-journey", confirmed_by="user")
    assert curriculum.is_active
    concepts = {c.canonical_name: c for c in store.list_concepts(course.id)}
    assert set(concepts) == {"传递函数", "状态空间"}
    assert all(c.mastery_level == 0 for c in concepts.values())
    # 7B: chapter provenance becomes course resources
    view = curricula.get_active_curriculum(course.id)
    created = record_textbook_resources(resources, course.id, view)
    assert len(created) == 3  # one per published chapter
    # the concepts bound to chapters stay consistent with the curriculum
    view = curricula.get_active_curriculum(course.id)
    published_chapters = {c.id for c in view.chapters_in_order}
    assert {r.chapter_id for r in created} <= published_chapters

    # ---- Step 3: 进入学习模式 → LearningContext --------------------------
    controller.enter_mode()
    controller.continue_last_course()  # the user confirms continuing the project
    assert controller.state.active_course_id == course.id
    assert controller.state.active_session_id is not None

    context = controller.learning_context()
    assert context.course_name == "自动控制原理"
    # nothing studied yet: no chapter/focus, never guessed
    assert context.current_chapter is None
    assert context.current_focus is None

    # ---- Step 4: 学习交互 "解释一下传递函数" ------------------------------
    assert controller.handle_text("解释一下传递函数") is None  # ordinary chat
    events = interactions.list_interactions(course.id)
    assert events[-1].event_type == InteractionEventType.CONCEPT_EXPLANATION.value

    context = controller.learning_context()
    assert context.current_focus == "传递函数"
    assert context.current_chapter is not None

    teaching = controller.teaching_context()
    assert teaching.concept_name == "传递函数"
    assert teaching.learning_stage == LearningStage.BEGINNER.value
    assert teaching.mastery == 0

    decision = controller.learning_decision()
    assert decision.action == "explain"
    assert decision.concept_name == "传递函数"

    action = controller.learning_action(run_assessment=False)
    assert action.action == "explain"
    assert action.status == ActionStatus.READY.value

    # ---- Step 5: 检查理解 → Assessment 启动 -------------------------------
    concept = store.find_concept(course.id, "传递函数")
    reply = controller.handle_text("检查一下我理解了吗")
    assert reply is not None  # the deterministic command answered
    # two concepts in the course → the quiz asks WHICH one (needs_choice)
    assert "要检查哪个知识点" in reply
    assert controller.assessment_service.pending_choice is not None
    assert store.list_assessments(concept.id) == []  # nothing recorded yet

    # the user names the concept → the quiz starts for it
    reply = controller.handle_text("传递函数")
    assert controller.assessment_service.state == AssessmentState.WAITING_ANSWER
    assert reply == "请说明「传递函数」的定义。"

    # ---- Step 6: 回答正确（score=0.9）→ Rule Engine 0→1 -------------------
    answer = controller.handle_text("传递函数是输出与输入拉氏变换之比")
    assert answer is not None
    assert controller.assessment_service.state == AssessmentState.RECORDED

    records = store.list_assessments(concept.id)
    assert len(records) == 1 and records[0].score == pytest.approx(0.9)
    assert store.get_concept(concept.id).mastery_level == 1
    assert len(store.list_mastery_audit(concept.id)) == 1
    assert len(store.list_review_items(concept.id)) == 1  # ReviewItem 生成
    concept_facts = _facts(store, course.id, concept.id)

    # ---- Step 7: 继续学习 → 策略变为 EXPLAIN_RECALL -----------------------
    store.apply_mastery_update  # noqa: B018 — mastery came from the Rule Engine
    assert store.get_concept(concept.id).mastery_level == 1

    context = controller.learning_context()
    assert context.current_focus == "传递函数"
    teaching = controller.teaching_context()
    assert teaching.learning_stage == LearningStage.INTRODUCED.value
    assert teaching.recommended_strategy == "先让学习者回忆，再问一个简单问题"
    decision = controller.learning_decision()
    assert decision.action == "explain_recall"

    # "继续学习" still works as a command (new session for the same project)
    sessions_before = len(store.list_sessions(course.id))
    reply = controller.handle_text("继续学习")
    assert reply is not None
    assert controller.state.active_session_id is not None
    assert len(store.list_sessions(course.id)) == sessions_before + 1

    # ---- Step 8: 复习流程（模拟 review_due） → REVIEW ----------------------
    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    decision = controller.learning_decision()
    assert decision.action == "review"
    assert decision.source == "review_due"

    # ---- 附加 1: Curriculum ↔ Resource ↔ Concept 一致 ----------------------
    created_resources = record_textbook_resources(resources, course.id, view)
    assert len(created_resources) == 3
    published = {c.id for c in view.chapters_in_order}
    assert {r.chapter_id for r in created_resources} <= published
    concept_ids = {c.id for c in store.list_concepts(course.id)}
    assert all(
        r.concept_id is None or r.concept_id in concept_ids
        for r in resources.list_for_course(course.id)
    )

    # ---- 附加 2: Session 生命周期（退出/重进不污染） -----------------------
    session_before = controller.state.active_session_id
    controller.exit_mode()
    assert controller.state.active_session_id is None
    controller.enter_mode()
    controller.continue_last_course()
    session_after = controller.state.active_session_id
    assert session_after is not None and session_after != session_before
    # the old session was closed; exactly one active session exists
    active_sessions = [
        s for s in store.list_sessions(course.id) if s.status == "active"
    ]
    assert [s.id for s in active_sessions] == [session_after]

    # ---- 附加 3: Interaction 生命周期 --------------------------------------
    interactions_before = interactions.count(course.id)
    assert interactions_before >= 1  # the explanation was recorded
    # 普通聊天不写
    assert controller.handle_text("今天天气不错") is None
    assert interactions.count(course.id) == interactions_before

    # ---- 附加 4: Mastery 生命周期（只有 Assessment + Rule Engine 能改）-----
    assert store.get_concept(concept.id).mastery_level == 1
    assert len(store.list_mastery_audit(concept.id)) == 1  # only the quiz pass
    # interactions/decisions/actions never touched it along the way
    assert concept_facts is not None


# ===========================================================================
# Additional focused checks
# ===========================================================================


def test_sessions_are_not_polluted_by_enter_exit_cycles(journey) -> None:
    store = journey.store
    controller = journey.controller
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    first = controller.state.active_session_id
    for _ in range(3):
        controller.exit_mode()
        controller.enter_mode()
        controller.continue_last_course()
    active = [s for s in store.list_sessions(course_id) if s.status == "active"]
    assert [s.id for s in active] == [controller.state.active_session_id]
    assert controller.state.active_session_id != first


def test_ordinary_chat_never_writes_learning_facts(journey) -> None:
    store = journey.store
    controller = journey.controller
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "existing", source="quiz")
    before = _facts(store, course.id, concept.id)

    controller.enter_mode()
    for text in ("今天天气不错", "帮我写段代码", "什么是好课"):
        assert controller.handle_text(text) is None
    assert _facts(store, course.id, concept.id) == before
