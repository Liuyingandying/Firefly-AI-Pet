"""Learning action executor tests (Phase 5).

Covers the frozen contract:

    decision -> action mapping / PRACTICE drives the existing assessment /
    REVIEW reads the review item / MOVE_NEXT reads the curriculum /
    ordinary chat unaffected / failure-safe degradation /
    no mastery writes / works with no provider

plus the purity of the action layer and the read-only runner injection.
Pure in-memory + tmp_path SQLite; no Qt, no real provider.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.action import (
    CONSTRAINT_BY_ACTION,
    ActionStatus,
    LearningActionExecutor,
    LearningActionResult,
)
from core.learning.controller import LearningModeController
from core.learning.curriculum import (
    ChapterConceptDraft,
    ChapterDraft,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    LearningPathDraft,
    PathStepDraft,
)
from core.learning.curriculum.store import CurriculumStore
from core.learning.decision import (
    LearningAction,
    LearningDecisionPolicy,
    LearningRecommendation,
)
from core.learning.store import LearningStore


ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "core" / "learning" / "action"


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _seed(tmp_path):
    """Course with two chapters: ch2 (传递函数) and ch3 (稳定性)."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id

    def placement(pid, name, position):
        return ChapterConceptDraft(
            proposal=ConceptProposal(proposal_id=pid, name=name), position=position
        )

    draft = CurriculumDraft(
        id="d1", course_id=course_id, title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(id="ch2", title="第二章 数学模型", position=1,
                         concepts=(placement("p2", "传递函数", 1),)),
            ChapterDraft(id="ch3", title="第三章 时域分析", position=2,
                         concepts=(placement("p4", "稳定性", 1),)),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch2"),
            PathStepDraft("s2", 2, "chapter", "ch3"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d1", confirmed_by="user")
    session = store.start_session(course_id)
    store.touch_concept(
        session.id, store.find_concept(course_id, "传递函数").id, "study"
    )
    return store, curricula, controller, course_id


def _facts(store: LearningStore, course_id: str, concept_id: str) -> tuple:
    return (
        store.get_concept(concept_id).mastery_level,
        len(store.list_mastery_audit(concept_id)),
        len(store.list_assessments(concept_id)),
        len(store.list_review_items(concept_id)),
        len(store.list_concepts(course_id)),
        len(store.list_sessions(course_id)),
    )


class _StubAssessmentService:
    """Records calls; mimics the real service's ``active`` contract."""

    def __init__(self, *, starts: bool = True) -> None:
        self.calls: list[tuple] = []
        self.active = None
        self._starts = starts

    def start_assessment(self, course_id, course_name, requested_concept=None, session_id=None):
        self.calls.append((course_id, course_name, requested_concept, session_id))
        if self._starts:
            self.active = SimpleNamespace(
                concept_id="k", concept_name=requested_concept or ""
            )
            return f"{requested_concept}的定义是什么？"
        return "要检查哪个知识点？当前项目里有：A、B"


# ---------------------------------------------------------------------------
# 1. decision -> action mapping
# ---------------------------------------------------------------------------


def test_every_action_maps_to_a_constraint() -> None:
    expectations = {
        0: LearningAction.EXPLAIN,
        1: LearningAction.EXPLAIN_RECALL,
        2: LearningAction.PRACTICE,
        3: LearningAction.PRACTICE,
        4: LearningAction.TRANSFER,
        5: LearningAction.TRANSFER,
    }
    executor = LearningActionExecutor()
    for mastery, expected in expectations.items():
        recommendation = LearningDecisionPolicy.decide(
            focus=("k", "传递函数"), mastery=mastery
        )
        result = executor.execute(
            recommendation, course_id="c1", run_assessment=False
        )
        assert result.action == expected.value
        assert result.status == ActionStatus.READY.value
        assert result.concept_name == "传递函数"
        assert result.message == CONSTRAINT_BY_ACTION[expected].format(concept="传递函数")


def test_free_chat_is_skipped_and_injects_nothing() -> None:
    executor = LearningActionExecutor()
    result = executor.execute(LearningDecisionPolicy.decide(focus=None))
    assert result.action == LearningAction.FREE_CHAT.value
    assert result.status == ActionStatus.SKIPPED.value
    assert result.context_block() is None
    assert result.ok is False


def test_none_recommendation_is_skipped() -> None:
    result = LearningActionExecutor().execute(None)
    assert result.status == ActionStatus.SKIPPED.value


def test_result_carries_the_recommendation_source() -> None:
    recommendation = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=3, review_due=True
    )
    result = LearningActionExecutor().execute(recommendation)
    assert result.source == recommendation.source == "review_due"
    assert result.action == LearningAction.REVIEW.value


def test_explain_recall_and_transfer_texts_are_prepared() -> None:
    executor = LearningActionExecutor()
    recall = executor.execute(
        LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=1)
    )
    assert "回忆" in recall.message
    transfer = executor.execute(
        LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=4)
    )
    assert "应用" in transfer.message or "迁移" in transfer.message


# ---------------------------------------------------------------------------
# 2. PRACTICE drives the existing assessment service
# ---------------------------------------------------------------------------


def test_practice_starts_the_existing_assessment() -> None:
    service = _StubAssessmentService()
    executor = LearningActionExecutor(assessment_service=service)
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)

    result = executor.execute(
        recommendation, course_id="c1", course_name="自动控制原理",
        session_id="s1", run_assessment=True,
    )
    assert result.status == ActionStatus.STARTED.value
    assert result.ok is True
    assert result.message == "传递函数的定义是什么？"
    assert service.calls == [("c1", "自动控制原理", "传递函数", "s1")]


def test_read_only_practice_never_starts_an_assessment() -> None:
    service = _StubAssessmentService()
    executor = LearningActionExecutor(assessment_service=service)
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)

    result = executor.execute(recommendation, course_id="c1", run_assessment=False)
    assert result.status == ActionStatus.READY.value
    assert service.calls == []
    assert service.active is None
    # the prepared text tells the runtime to use the existing quiz flow
    assert "考考我" in result.message


def test_practice_without_a_service_degrades() -> None:
    executor = LearningActionExecutor()
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)
    result = executor.execute(recommendation, course_id="c1", run_assessment=True)
    assert result.status == ActionStatus.DEGRADED.value


def test_practice_that_cannot_issue_a_question_degrades() -> None:
    service = _StubAssessmentService(starts=False)
    executor = LearningActionExecutor(assessment_service=service)
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)
    result = executor.execute(recommendation, course_id="c1", run_assessment=True)
    assert result.status == ActionStatus.DEGRADED.value
    assert "知识点" in result.message  # the service's guidance is surfaced
    assert service.calls  # it did try


# ---------------------------------------------------------------------------
# 3. REVIEW reads the review item
# ---------------------------------------------------------------------------


def test_review_reads_the_due_review_item(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00",
                             interval_days=7)

    review = LearningRecommendation(
        action=LearningAction.REVIEW.value, source="review_due",
        concept_id=concept.id, concept_name="传递函数",
    )
    result = LearningActionExecutor(store=store).execute(review)
    assert result.status == ActionStatus.READY.value
    assert "复习" in result.message
    assert "间隔 7 天" in result.message


def test_review_without_a_due_item_degrades(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    review = LearningRecommendation(
        action=LearningAction.REVIEW.value, source="review_due",
        concept_id=concept.id, concept_name="传递函数",
    )
    result = LearningActionExecutor(store=store).execute(review)
    assert result.status == ActionStatus.DEGRADED.value
    assert "没有到期" in result.message


def test_review_without_a_store_degrades() -> None:
    review = LearningRecommendation(
        action=LearningAction.REVIEW.value, source="review_due",
        concept_id="k", concept_name="传递函数",
    )
    result = LearningActionExecutor().execute(review)
    assert result.status == ActionStatus.DEGRADED.value


# ---------------------------------------------------------------------------
# 4. MOVE_NEXT reads the curriculum
# ---------------------------------------------------------------------------


def test_move_next_reads_the_next_curriculum_node(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 5, "high", "test", source="quiz")

    decision = controller.learning_decision()
    assert decision.action == LearningAction.MOVE_NEXT.value

    result = controller.learning_action(run_assessment=False)
    assert result.action == LearningAction.MOVE_NEXT.value
    assert result.status == ActionStatus.READY.value
    assert "第三章 时域分析" in result.message


def test_move_next_without_a_label_degrades() -> None:
    recommendation = LearningRecommendation(
        action=LearningAction.MOVE_NEXT.value, source="chapter_complete",
        concept_name="传递函数",
    )
    result = LearningActionExecutor().execute(recommendation, next_label=None)
    assert result.status == ActionStatus.DEGRADED.value
    assert "读不到" in result.message


# ---------------------------------------------------------------------------
# 5. ordinary chat is unaffected
# ---------------------------------------------------------------------------


def test_ordinary_chat_still_returns_none(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    for text in ("解释一下传递函数", "今天天气不错", "帮我写一段代码"):
        assert controller.handle_text(text) is None


def test_action_execution_does_not_change_the_reply_flow(tmp_path) -> None:
    """Preparing actions must not turn ordinary messages into commands."""
    store, curricula, controller, course_id = _seed(tmp_path)
    for _ in range(3):
        controller.action_block()
    assert controller.handle_text("今天天气不错") is None


def test_runner_injects_the_action_block(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")

    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    ).perform("传递函数是什么？")

    block = captured["turn_context"]
    assert "已准备的学习动作" in block
    assert "PRACTICE" in block
    # the earlier blocks are still composed in
    assert "下一步动作：PRACTICE" in block
    assert "教学阶段：PRACTICE" in block


def test_runner_has_no_action_block_outside_mode(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    controller.exit_mode()
    CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    ).perform("随便聊聊")
    assert captured["turn_context"] is None


# ---------------------------------------------------------------------------
# 6. failure-safe degradation
# ---------------------------------------------------------------------------


def test_breaking_store_degrades_instead_of_raising() -> None:
    class _BrokenStore:
        def get_due_reviews(self):
            raise RuntimeError("boom")

    review = LearningRecommendation(
        action=LearningAction.REVIEW.value, source="review_due",
        concept_id="k", concept_name="传递函数",
    )
    result = LearningActionExecutor(store=_BrokenStore()).execute(review)
    assert result.status == ActionStatus.DEGRADED.value


def test_exploding_assessment_service_degrades_instead_of_raising() -> None:
    class _Exploding:
        active = None

        def start_assessment(self, *args, **kwargs):
            raise RuntimeError("provider down")

    executor = LearningActionExecutor(assessment_service=_Exploding())
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)
    result = executor.execute(recommendation, course_id="c1", run_assessment=True)
    assert result.status == ActionStatus.FAILED.value
    assert "没执行成功" in result.message


def test_executor_never_raises_for_unknown_actions() -> None:
    class _Weird:
        action = "practice"
        reason = ""
        source = ""
        concept_id = None
        concept_name = None
        @property
        def kind(self):
            raise RuntimeError("boom")

    result = LearningActionExecutor().execute(_Weird())  # type: ignore[arg-type]
    assert result.status == ActionStatus.FAILED.value


def test_controller_action_hooks_are_safe_without_services(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    controller = LearningModeController(store=store, settings=_Settings())
    assert controller.learning_action() is None
    assert controller.action_block() is None
    assert controller.execute_learning_action() is None


def test_result_is_immutable_and_validated() -> None:
    result = LearningActionResult(action="explain", message="约束")
    with pytest.raises(Exception):
        result.status = "failed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LearningActionResult(action="not-an-action")
    with pytest.raises(ValueError):
        LearningActionResult(action="explain", status="not-a-status")


# ---------------------------------------------------------------------------
# 7. no mastery writes
# ---------------------------------------------------------------------------


def test_executing_actions_never_writes_mastery(tmp_path) -> None:
    service = _StubAssessmentService()
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "existing", source="quiz")
    controller._executor = LearningActionExecutor(
        store=store, assessment_service=service
    )
    before = _facts(store, course_id, concept.id)

    for _ in range(3):
        controller.action_block()
        controller.learning_action(run_assessment=True)
    assert _facts(store, course_id, concept.id) == before
    # starting an assessment is not a mastery write and records no evidence
    assert store.list_assessments(concept.id) == []
    assert len(store.list_mastery_audit(concept.id)) == 1  # only the pre-existing one


def test_action_layer_has_no_mastery_or_store_write_api() -> None:
    for name in ("apply_mastery_update", "record_assessment", "add_concept", "start_session"):
        assert not hasattr(LearningActionExecutor, name)


# ---------------------------------------------------------------------------
# 8. works with no provider
# ---------------------------------------------------------------------------


def test_read_only_preparation_works_when_the_provider_explodes(tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the read-only action path must not call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")

    result = controller.learning_action(run_assessment=False)
    assert result is not None
    assert result.status == ActionStatus.READY.value
    assert controller.action_block() is not None


def test_explicit_practice_survives_a_broken_provider(tmp_path, monkeypatch) -> None:
    """A provider failure surfaces as a safe result, never as an exception."""
    import core.ai_router
    from core.learning.assessment import AssessmentService

    def explode(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    controller.assessment_service = AssessmentService(store)

    result = controller.execute_learning_action()
    assert result is not None
    assert result.action == LearningAction.PRACTICE.value
    assert result.status in (ActionStatus.FAILED.value, ActionStatus.DEGRADED.value)
    # nothing was recorded and mastery is untouched
    assert store.list_assessments(concept.id) == []
    assert store.get_concept(concept.id).mastery_level == 2


def test_action_package_has_no_dependency_on_provider_or_store() -> None:
    forbidden = (
        "core.learning.store", "sqlite3", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory",
        "PySide6", "PyQt5", "PyQt6", "ui", "openai", "anthropic", "requests",
        "httpx", "core.video_study", "core.learning.rule_engine",
        "core.learning.assessment", "core.learning.quiz_adapter",
        "core.learning.review_scheduler", "core.learning.curriculum",
        "core.learning.interactions", "core.learning.teaching",
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(ACTION_DIR.glob("*.py")):
        imports: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        bad = {
            module for module in imports
            if any(module == f or module.startswith(f + ".") for f in forbidden)
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"action imports forbidden modules: {offenders}"
    # the only allowed domain dependency is the decision layer it executes
    allowed_domain = ("core.learning.decision", "core.learning.action")
    for path in sorted(ACTION_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module):
                continue
            if not node.module.startswith("core.learning."):
                continue  # stdlib / third-party is covered by the block above
            assert node.module.startswith(allowed_domain), (
                f"{path.name} imports {node.module}"
            )
