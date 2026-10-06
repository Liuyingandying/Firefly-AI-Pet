"""Learning decision layer tests (Phase 4).

Covers the frozen contract:

    mastery 0 -> EXPLAIN / mastery 2 -> PRACTICE / review-due priority /
    mastery 5 -> TRANSFER / no course -> FREE_CHAT / no database writes /
    works without an LLM / Rule Engine isolation

plus the pure-policy properties (determinism, chapter-complete MOVE_NEXT,
immutability) and the runner injection. Pure in-memory + tmp_path SQLite.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.context import LearningContextBuilder
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
    ACTION_BY_MASTERY,
    MASTERY_MASTERED,
    DecisionSource,
    LearningAction,
    LearningDecisionPolicy,
    LearningRecommendation,
)
from core.learning.store import LearningStore


ROOT = Path(__file__).resolve().parents[1]
DECISION_DIR = ROOT / "core" / "learning" / "decision"


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _seed(tmp_path):
    """Course with two chapters: ch2 (传递函数, 方框图) and ch3 (稳定性)."""
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
                         concepts=(placement("p2", "传递函数", 1), placement("p3", "方框图", 2))),
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
        len(store.list_interactions(course_id))
        if hasattr(store, "list_interactions") else 0,
    )


# ---------------------------------------------------------------------------
# 1-4. the mastery bands
# ---------------------------------------------------------------------------


def test_mastery_zero_maps_to_explain() -> None:
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=0)
    assert recommendation.action == LearningAction.EXPLAIN.value
    assert recommendation.source == DecisionSource.MASTERY.value
    assert recommendation.concept_name == "传递函数"
    assert ACTION_BY_MASTERY[0] is LearningAction.EXPLAIN


def test_mastery_two_maps_to_practice() -> None:
    for mastery in (2, 3):
        recommendation = LearningDecisionPolicy.decide(
            focus=("k", "传递函数"), mastery=mastery
        )
        assert recommendation.action == LearningAction.PRACTICE.value
    assert ACTION_BY_MASTERY[2] is LearningAction.PRACTICE
    assert ACTION_BY_MASTERY[3] is LearningAction.PRACTICE


def test_mastery_one_maps_to_explain_recall() -> None:
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=1)
    assert recommendation.action == LearningAction.EXPLAIN_RECALL.value


def test_mastery_five_maps_to_transfer() -> None:
    for mastery in (4, 5):
        recommendation = LearningDecisionPolicy.decide(
            focus=("k", "传递函数"), mastery=mastery
        )
        assert recommendation.action == LearningAction.TRANSFER.value
    assert ACTION_BY_MASTERY[4] is LearningAction.TRANSFER
    assert ACTION_BY_MASTERY[5] is LearningAction.TRANSFER


def test_unknown_mastery_degrades_to_explain_without_claiming_mastery() -> None:
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=None)
    assert recommendation.action == LearningAction.EXPLAIN.value


def test_review_due_wins_over_every_band() -> None:
    for mastery in (0, 1, 2, 3, 4, 5):
        recommendation = LearningDecisionPolicy.decide(
            focus=("k", "传递函数"), mastery=mastery, review_due=True
        )
        assert recommendation.action == LearningAction.REVIEW.value
        assert recommendation.source == DecisionSource.REVIEW_DUE.value
    # review-due with no focus still cannot review a non-existent concept
    assert LearningDecisionPolicy.decide(focus=None, review_due=True).action == (
        LearningAction.FREE_CHAT.value
    )


def test_chapter_complete_moves_next_only_with_a_following_node() -> None:
    moved = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=5,
        chapter_complete=True, next_label="第三章 时域分析",
    )
    assert moved.action == LearningAction.MOVE_NEXT.value
    assert moved.source == DecisionSource.CHAPTER_COMPLETE.value
    assert "第三章 时域分析" in moved.reason
    # a completed chapter with nothing after it keeps deepening
    last = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=5, chapter_complete=True, next_label=None
    )
    assert last.action == LearningAction.TRANSFER.value
    # a low-mastery concept never moves on
    low = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=2,
        chapter_complete=True, next_label="第三章 时域分析",
    )
    assert low.action == LearningAction.PRACTICE.value


# ---------------------------------------------------------------------------
# 5. no course / no focus -> FREE_CHAT
# ---------------------------------------------------------------------------


def test_no_focus_maps_to_free_chat() -> None:
    recommendation = LearningDecisionPolicy.decide(focus=None)
    assert recommendation.action == LearningAction.FREE_CHAT.value
    assert recommendation.source == DecisionSource.NO_FOCUS.value
    # FREE_CHAT with no concept injects nothing into the prompt
    assert recommendation.context_block() is None


def test_no_learning_context_maps_to_free_chat() -> None:
    recommendation = LearningDecisionPolicy.from_contexts(None, None)
    assert recommendation.action == LearningAction.FREE_CHAT.value


def test_controller_without_a_course_injects_nothing(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    controller = LearningModeController(store=store, settings=_Settings())
    assert controller.learning_decision() is None
    assert controller.decision_block() is None


def test_controller_decision_reflects_the_stored_mastery(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    assert controller.learning_decision().action == LearningAction.EXPLAIN.value

    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    assert controller.learning_decision().action == LearningAction.PRACTICE.value

    store.apply_mastery_update(concept.id, 5, "high", "test", source="quiz")
    assert controller.learning_decision().action == LearningAction.TRANSFER.value


def test_controller_detects_a_completed_chapter(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    first = store.find_concept(course_id, "传递函数")
    second = store.find_concept(course_id, "方框图")
    store.apply_mastery_update(first.id, 5, "high", "test", source="quiz")
    store.apply_mastery_update(second.id, 4, "high", "test", source="quiz")

    complete, next_label = controller._chapter_progress(controller.learning_context())
    assert complete is True
    assert next_label == "第三章 时域分析"
    recommendation = controller.learning_decision()
    assert recommendation.action == LearningAction.MOVE_NEXT.value
    assert MASTERY_MASTERED == 4


def test_chapter_is_not_complete_with_one_unmastered_concept(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    first = store.find_concept(course_id, "传递函数")
    second = store.find_concept(course_id, "方框图")
    store.apply_mastery_update(first.id, 5, "high", "test", source="quiz")
    store.apply_mastery_update(second.id, 2, "medium", "test", source="quiz")
    complete, _next = controller._chapter_progress(controller.learning_context())
    assert complete is False
    assert controller.learning_decision().action == LearningAction.TRANSFER.value


# ---------------------------------------------------------------------------
# determinism / purity
# ---------------------------------------------------------------------------


def test_decision_is_deterministic() -> None:
    for mastery in range(6):
        outputs = {
            LearningDecisionPolicy.decide(
                focus=("k", "传递函数"), mastery=mastery, review_due=(mastery == 3)
            ).action
            for _ in range(20)
        }
        assert len(outputs) == 1


def test_decision_recommendation_is_immutable_and_validated() -> None:
    recommendation = LearningRecommendation(action="practice", concept_name="传递函数")
    with pytest.raises(Exception):
        recommendation.action = "explain"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LearningRecommendation(action="not-an-action")
    # the reason defaults to the frozen text for the action
    assert recommendation.reason


def test_context_block_lists_action_and_reason() -> None:
    recommendation = LearningDecisionPolicy.decide(focus=("k", "传递函数"), mastery=2)
    block = recommendation.context_block()
    assert "下一步动作：PRACTICE" in block
    assert "理由：" in block
    assert "不要自行更改" in block


# ---------------------------------------------------------------------------
# 6. no database writes
# ---------------------------------------------------------------------------


def test_decision_layer_writes_nothing(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "existing", source="quiz")
    before = _facts(store, course_id, concept.id)

    for _ in range(5):
        controller.learning_decision()
        controller.decision_block()
    assert _facts(store, course_id, concept.id) == before


def test_decision_module_has_no_store_or_provider_imports() -> None:
    """The policy is pure: no store, no sqlite, no provider, no UI, no quiz."""
    forbidden = (
        "core.learning.store", "sqlite3", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory",
        "PySide6", "PyQt5", "PyQt6", "ui", "openai", "anthropic", "requests",
        "httpx", "core.video_study", "core.learning.rule_engine",
        "core.learning.assessment", "core.learning.quiz_adapter",
        "core.learning.review_scheduler", "core.learning.curriculum",
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(DECISION_DIR.glob("*.py")):
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
    assert not offenders, f"decision imports forbidden modules: {offenders}"


# ---------------------------------------------------------------------------
# 7. works with no LLM / provider available
# ---------------------------------------------------------------------------


def test_decision_works_when_the_provider_explodes(tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the decision layer must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, controller, course_id = _seed(tmp_path)
    recommendation = controller.learning_decision()
    assert recommendation is not None
    assert recommendation.action == LearningAction.EXPLAIN.value
    assert controller.decision_block() is not None


def test_the_pure_policy_needs_no_store_at_all() -> None:
    """No fixture, no database: a plain function call is enough."""
    recommendation = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=3
    )
    assert recommendation.action == LearningAction.PRACTICE.value


# ---------------------------------------------------------------------------
# 8. Rule Engine isolation
# ---------------------------------------------------------------------------


def test_rule_engine_does_not_import_decisions() -> None:
    path = ROOT / "core" / "learning" / "rule_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "decision" not in node.module
        if isinstance(node, ast.Import):
            assert all("decision" not in alias.name for alias in node.names)


def test_decisions_do_not_change_rule_engine_results(tmp_path) -> None:
    from core.learning.models import AssessmentEvidence
    from core.learning.rule_engine import LearningRuleEngine

    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    engine = LearningRuleEngine(store)
    evidence = AssessmentEvidence(
        concept_id=concept.id, source="quiz", score=0.95, confidence=0.9,
        difficulty="basic", created_at="2026-06-01T00:00:00+00:00",
    )
    first = engine.evaluate(concept.id, evidence)

    for _ in range(3):
        controller.learning_decision()
        controller.decision_block()
    second = engine.evaluate(concept.id, evidence)

    assert (first.new_mastery, first.changed) == (second.new_mastery, second.changed)
    assert store.get_concept(concept.id).mastery_level == 0  # preview writes nothing


def test_decision_does_not_import_the_teaching_layer() -> None:
    """It consumes TeachingContext as plain data, not as a dependency."""
    for path in sorted(DECISION_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        assert not any("teaching" in module for module in imports), path.name


# ---------------------------------------------------------------------------
# runner injection (read-only)
# ---------------------------------------------------------------------------


def test_runner_receives_the_decision_block(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")

    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("传递函数是什么？")

    block = captured["turn_context"]
    assert "下一步动作：PRACTICE" in block
    # the earlier blocks are still composed in (nothing replaced)
    assert "教学阶段：PRACTICE" in block
    assert "当前章节：第二章 数学模型" in block


def test_runner_has_no_decision_block_outside_mode(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    controller.exit_mode()
    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("随便聊聊")
    assert captured["turn_context"] is None


def test_decision_does_not_change_the_reply_flow(tmp_path) -> None:
    """Injecting the decision must not turn an ordinary message into a command."""
    store, curricula, controller, course_id = _seed(tmp_path)
    for text in ("解释一下传递函数", "今天天气不错"):
        # still ordinary chat (None), with interactions tracked but no command
        assert controller.handle_text(text) is None
