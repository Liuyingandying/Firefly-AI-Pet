"""Learning loop orchestrator tests (Phase 6).

Covers the frozen contract:

    full learning turn / explanation flow / check-understanding flow /
    review flow / ordinary chat isolation / failure safety /
    no mastery writes / works with no provider

plus the purity of the layer (no provider / LLM / rule engine / curriculum
writer imports) and the read-only runner injection. Pure in-memory +
tmp_path SQLite.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from core.learning.interactions import (
    InteractionEventType,
    InteractionSource,
    InteractionStore,
)
from core.learning.orchestrator import (
    LearningLoopOrchestrator,
    LearningLoopResult,
    LoopStatus,
)
from core.learning.store import LearningStore


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_DIR = ROOT / "core" / "learning" / "orchestrator"


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _seed(tmp_path, *, concepts=("传递函数",), touch: bool = False):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    placements = tuple(
        ChapterConceptDraft(
            proposal=ConceptProposal(proposal_id=f"p{index}", name=name), position=index
        )
        for index, name in enumerate(concepts, start=1)
    )
    draft = CurriculumDraft(
        id="d1", course_id=course_id, title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(id="ch2", title="第二章 数学模型", position=1, concepts=placements),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d1", confirmed_by="user")
    if touch:
        session = store.start_session(course_id)
        store.touch_concept(session.id, store.find_concept(course_id, concepts[0]).id, "study")
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


# ---------------------------------------------------------------------------
# 1. full learning turn
# ---------------------------------------------------------------------------


def test_full_learning_turn(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("解释一下传递函数")

    assert result.is_learning_turn
    assert result.status == LoopStatus.LEARNING_LOOP.value
    assert result.action == "explain"
    assert result.source == "mastery"
    assert result.context_block is not None
    # the loop recorded the interaction fact and moved the focus
    assert InteractionStore(store).count() == 1
    assert controller.learning_context().current_focus == "传递函数"


def test_loop_composes_all_four_layers(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, touch=True)
    block = controller.learning_loop_block()
    assert block is not None
    assert "当前模式：学习模式" in block          # context layer
    assert "教学阶段：" in block                  # teaching layer
    assert "下一步动作：" in block                # decision layer
    assert "已准备的学习动作" in block            # action layer


def test_run_is_deterministic_per_state(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    first = orchestrator.run("解释一下传递函数")
    second = orchestrator.run("传递函数")
    # the state moved (interaction), but the turn shape is stable
    assert first.status == second.status == LoopStatus.LEARNING_LOOP.value
    assert first.action == second.action == "explain"


# ---------------------------------------------------------------------------
# 2. explanation flow
# ---------------------------------------------------------------------------


def test_explanation_flow_records_and_updates_focus(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("解释一下传递函数")

    events = InteractionStore(store).list_interactions(course_id)
    assert [e.event_type for e in events] == [InteractionEventType.CONCEPT_EXPLANATION.value]
    assert events[0].source == InteractionSource.USER_EXPLICIT.value
    assert result.context_block is not None
    assert "传递函数" in result.context_block


# ---------------------------------------------------------------------------
# 3. check-understanding flow
# ---------------------------------------------------------------------------


def test_check_understanding_flow_answers_and_records(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    from core.learning.assessment import AssessmentService
    from core.learning.quiz_adapter import QuizGrading

    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(q, a, "正确的部分：好"),
    )
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("考考我")

    assert result.status == LoopStatus.LEARNING_COMMAND.value
    assert result.response == "传递函数的定义是什么？"
    events = InteractionStore(store).list_interactions(course_id)
    assert events[-1].event_type == InteractionEventType.ASSESSMENT_START.value
    assert events[-1].source == InteractionSource.SYSTEM_GENERATED.value
    # the answer turn is also a learning-command turn (graded by the service)
    answer = orchestrator.run("零极点比值")
    assert answer.status == LoopStatus.LEARNING_COMMAND.value


# ---------------------------------------------------------------------------
# 4. review flow
# ---------------------------------------------------------------------------


def test_review_flow_surfaces_the_due_review(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("解释一下传递函数")

    assert result.action == "review"
    assert result.source == "review_due"
    assert result.context_block is not None
    assert "复习" in result.context_block


# ---------------------------------------------------------------------------
# 5. ordinary chat isolation
# ---------------------------------------------------------------------------


def test_mode_off_is_ordinary_and_writes_nothing(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.exit_mode()
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("解释一下传递函数")

    assert result.status == LoopStatus.ORDINARY.value
    assert result.action is None
    assert result.context_block is None
    assert InteractionStore(store).count() == 0
    assert controller.handle_text("解释一下传递函数") is None


def test_ordinary_message_in_mode_still_prepares_the_loop(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    # an ordinary message while learning mode is on: the loop context is still
    # prepared for the prompt (the message itself flows to normal chat)
    result = orchestrator.run("今天天气不错")
    assert result.status == LoopStatus.LEARNING_LOOP.value
    assert result.context_block is not None
    assert controller.learning_context().current_focus is None


def test_loop_block_is_none_outside_mode(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.exit_mode()
    assert controller.learning_loop_block() is None


# ---------------------------------------------------------------------------
# 6. failure safety
# ---------------------------------------------------------------------------


def test_broken_controller_degrades(tmp_path) -> None:
    class _Exploding:
        state = SimpleNamespace(enabled=True)

        def handle_text(self, text):
            raise RuntimeError("boom")

    result = LearningLoopOrchestrator(_Exploding()).run("解释一下传递函数")
    assert result.status == LoopStatus.DEGRADED.value


def test_broken_context_read_degrades(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.state.enabled = True
    controller.state.active_course_id = "c1"  # course lookup will still work

    class _BrokenContext:
        state = SimpleNamespace(enabled=True)

        def handle_text(self, text):
            return None

        def learning_context(self):
            raise RuntimeError("boom")

    result = LearningLoopOrchestrator(_BrokenContext()).run("你好")
    # the turn survived with an empty loop block (every layer read failed)
    assert result.status == LoopStatus.LEARNING_LOOP.value
    assert result.context_block is None


def test_loop_survives_a_failing_recorder(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)

    class _ExplodingRecorder:
        def record_from_message(self, *args, **kwargs):
            raise RuntimeError("boom")

    controller._interaction_recorder = _ExplodingRecorder()
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("解释一下传递函数")
    assert result.is_learning_turn  # the turn survived the tracking failure


def test_loop_block_never_raises(tmp_path) -> None:
    class _Exploding:
        state = SimpleNamespace(enabled=True)

        def context_block(self):
            raise RuntimeError("boom")

        def teaching_block(self):
            raise RuntimeError("boom")

        def learning_decision(self):
            raise RuntimeError("boom")

        def learning_action(self, run_assessment=True):
            raise RuntimeError("boom")

    assert LearningLoopOrchestrator(_Exploding()).loop_block() is None


def test_result_is_immutable_and_validated() -> None:
    result = LearningLoopResult(status="learning_loop", response="x")
    with pytest.raises(Exception):
        result.status = "ordinary"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LearningLoopResult(status="not-a-status")


# ---------------------------------------------------------------------------
# 7. no mastery writes
# ---------------------------------------------------------------------------


def test_loop_never_writes_mastery_or_learning_facts(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")
    before = _facts(store, course_id, concept.id)

    orchestrator = LearningLoopOrchestrator(controller)
    for text in ("解释一下传递函数", "传递函数这里没懂", "今天天气不错"):
        orchestrator.run(text)

    assert _facts(store, course_id, concept.id) == before
    assert store.get_concept(concept.id).mastery_level == 3


def test_orchestrator_has_no_write_api() -> None:
    for name in ("apply_mastery_update", "record_assessment", "add_concept",
                 "start_session", "confirm_draft", "activate_curriculum"):
        assert not hasattr(LearningLoopOrchestrator, name)


# ---------------------------------------------------------------------------
# 8. works with no provider
# ---------------------------------------------------------------------------


def test_loop_runs_without_a_provider(tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the orchestrator must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, controller, course_id = _seed(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("解释一下传递函数")
    assert result.is_learning_turn
    assert result.context_block is not None


def test_purity_of_the_orchestrator_package() -> None:
    forbidden = (
        "core.ai_router", "core.providers", "core.pagelens_bridge",
        "core.screen_vision", "core.memory", "PySide6", "PyQt5", "PyQt6", "ui",
        "openai", "anthropic", "requests", "httpx", "core.video_study",
        "core.learning.rule_engine", "core.learning.assessment",
        "core.learning.quiz_adapter", "core.learning.review_scheduler",
        "core.learning.store", "core.learning.curriculum",
        "core.learning.controller", "sqlite3",
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(ORCHESTRATOR_DIR.glob("*.py")):
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
    assert not offenders, f"orchestrator imports forbidden modules: {offenders}"


# ---------------------------------------------------------------------------
# runner injection (read-only)
# ---------------------------------------------------------------------------


def test_runner_uses_the_loop_block(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, touch=True)
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
    assert "当前模式：学习模式" in block
    assert "教学阶段：" in block
    assert "下一步动作：" in block
    assert "已准备的学习动作" in block


def test_runner_loop_block_absent_outside_mode(tmp_path) -> None:
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
