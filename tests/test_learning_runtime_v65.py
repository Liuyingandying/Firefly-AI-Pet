"""Runtime integration tests (Phase 6.5).

Covers the frozen contract:

    learning-mode messages enter the orchestrator / ordinary chat still uses the
    original flow / exit restores free chat / learning commands reach their
    actions / the UI status comes from the Context / no extra mastery writes /
    failure-safe degradation

Pure Qt-offscreen + tmp_path SQLite; no provider.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

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
from core.learning.interactions import InteractionStore
from core.learning.orchestrator import LoopStatus
from core.learning.store import LearningStore


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


class _FakeRunner:
    """Records ask() calls and the turn_context the runner would inject."""

    def __init__(self) -> None:
        self.agent_event = None
        self.session_video = None
        self.video_study = None
        self._screen_vision_settings = None
        self.learning_controller = None
        self.asked: list[str] = []
        self.turn_contexts: list = []

    def ask(self, text: str, learning_result=None) -> bool:
        self.asked.append(text)
        block = None
        controller = self.learning_controller
        loop_block = getattr(controller, "learning_loop_block", None)
        if callable(loop_block):
            block = loop_block()
        self.turn_contexts.append(block)
        return True


def _seed(tmp_path, *, concepts=("传递函数", "方框图")):
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
        id="d0", course_id=course_id, title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(id="ch2", title="第二章 数学模型", position=1, concepts=placements),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d0", confirmed_by="user")
    return store, curricula, controller, course_id


def _make_console(qapp, store, curricula, controller):
    from ui.v2.console import CompanionConsole

    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning = controller
    runner.learning_controller = controller
    return console, runner


def _chat_text(console) -> str:
    from PySide6.QtWidgets import QLabel, QTextBrowser

    parts: list[str] = []
    for index in range(console.chat._column.count()):
        widget = console.chat._column.itemAt(index).widget()
        if widget is None:
            continue
        browsers = widget.findChildren(QTextBrowser)
        if browsers:
            parts.extend(browser.toPlainText() for browser in browsers)
        else:
            parts.extend(label.text() for label in widget.findChildren(QLabel))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. learning-mode messages enter the orchestrator
# ---------------------------------------------------------------------------


def test_learning_mode_message_enters_the_orchestrator(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("解释一下传递函数")
    console._send()

    # the orchestrator recorded the interaction fact (only it can do that)
    assert InteractionStore(store).count() == 1
    event = InteractionStore(store).list_interactions(course_id)[0]
    assert event.event_type == "concept_explanation"
    # the focus moved — the loop ran, not the bare controller path
    assert controller.learning_context().current_focus == "传递函数"
    # the ordinary message itself still flows to the normal runner pipeline
    assert runner.asked == ["解释一下传递函数"]
    console.close()


# ---------------------------------------------------------------------------
# 2. ordinary chat still uses the original flow
# ---------------------------------------------------------------------------


def test_ordinary_chat_uses_the_original_flow(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("今天天气不错")
    console._send()

    assert runner.asked == ["今天天气不错"]
    assert InteractionStore(store).count() == 0
    console.close()


def test_free_chat_message_outside_learning_mode(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.exit_mode()

    console.input.setText("解释一下传递函数")
    console._send()

    assert runner.asked == ["解释一下传递函数"]
    assert InteractionStore(store).count() == 0
    assert controller.state.enabled is False
    console.close()


# ---------------------------------------------------------------------------
# 3. exit restores free chat
# ---------------------------------------------------------------------------


def test_exit_restores_the_original_flow(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)
    console.input.setText("解释一下传递函数")
    console._send()
    assert InteractionStore(store).count() == 1

    console.input.setText("退出学习模式")
    console._send()
    assert controller.state.enabled is False

    console.input.setText("什么是传递函数？")
    console._send()
    assert runner.asked[-1] == "什么是传递函数？"
    assert InteractionStore(store).count() == 1  # no new facts after exit
    console.close()


# ---------------------------------------------------------------------------
# 4. learning commands reach their actions
# ---------------------------------------------------------------------------


def test_continue_learning_command_activates_the_project(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.exit_mode()  # the project stays remembered
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("继续学习")
    console._send()

    assert controller.state.enabled is True
    assert controller.state.active_course_id == course_id
    assert runner.asked == []
    console.close()


def test_quiz_command_starts_the_assessment(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, concepts=("传递函数",))
    from core.learning.assessment import AssessmentService
    from core.learning.quiz_adapter import QuizGrading

    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(q, a, "正确的部分：好"),
    )
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("考考我")
    console._send()

    assert runner.asked == []  # answered by the learning command path
    assert "传递函数的定义是什么？" in _chat_text(console)
    events = InteractionStore(store).list_interactions(course_id)
    assert events[-1].event_type == "assessment_start"
    console.close()


def test_review_command_routes_to_the_review_flow(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, concepts=("传递函数",))
    from core.learning.assessment import AssessmentService
    from core.learning.quiz_adapter import QuizGrading

    concept = store.find_concept(course_id, "传递函数")
    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(q, a, "正确的部分：好"),
    )
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("复习一下")
    console._send()

    assert runner.asked == []
    events = InteractionStore(store).list_interactions(course_id)
    assert events[-1].event_type == "assessment_start"  # review drives the quiz flow
    console.close()


def test_next_node_command_answers_structurally(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("下一节")
    console._send()

    assert runner.asked == []
    assert "按照课程顺序" in _chat_text(console)
    console.close()


# ---------------------------------------------------------------------------
# 5. the UI status comes from the Context
# ---------------------------------------------------------------------------


def test_status_card_rows_come_from_the_context(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    session = store.start_session(course_id)
    concept = store.find_concept(course_id, "传递函数")
    store.touch_concept(session.id, concept.id, "study")
    console, runner = _make_console(qapp, store, curricula, controller)

    console._apply_learning_state()

    view = console.companion.context_status.view
    decision = controller.learning_decision()
    assert view.chapter == controller.learning_context().current_chapter
    assert view.focus == controller.learning_context().current_focus
    assert view.next_in_order == controller.learning_context().next_in_order
    assert view.decision_action == decision.action
    console.close()


def test_status_card_action_is_free_chat_omitted(qapp, tmp_path) -> None:
    """A FREE_CHAT decision shows no action row (there is nothing to execute)."""
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.state.enabled = True
    controller.state.active_course_id = course_id
    console._apply_learning_state()

    decision = controller.learning_decision()
    assert decision.action == "free_chat"
    assert console.companion.context_status.view.decision_action in ("", "free_chat")
    from PySide6.QtWidgets import QLabel
    labels = [label.text() for label in console.companion.context_status.findChildren(QLabel)]
    assert "下一步" not in labels
    console.close()


# ---------------------------------------------------------------------------
# 6. no extra mastery writes
# ---------------------------------------------------------------------------


def test_runtime_integration_never_writes_mastery(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")
    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
    )
    console, runner = _make_console(qapp, store, curricula, controller)

    for text in ("解释一下传递函数", "传递函数这里没懂", "下一节", "今天天气不错"):
        console.input.setText(text)
        console._send()

    after = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
    )
    assert before == after
    console.close()


# ---------------------------------------------------------------------------
# 7. failure-safe degradation
# ---------------------------------------------------------------------------


def test_broken_orchestrator_degrades_to_the_normal_flow(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)

    class _BrokenOrchestrator:
        def run(self, user_message, *, run_assessment=False):
            raise RuntimeError("boom")

        def loop_block(self):
            return None

    controller._orchestrator_instance = _BrokenOrchestrator()
    console, runner = _make_console(qapp, store, curricula, controller)
    controller.state.enabled = True
    controller.state.active_course_id = course_id
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = store.start_session(course_id).id

    # a learning command still works through the legacy interception fallback
    console.input.setText("下一节")
    console._send()
    assert runner.asked == []  # handled without the orchestrator
    assert "按照课程顺序" in _chat_text(console)

    # an ordinary message still flows to the normal chat pipeline
    console.input.setText("今天天气不错")
    console._send()
    assert runner.asked[-1] == "今天天气不错"
    console.close()


def test_none_result_from_the_loop_falls_back_legacypath(qapp, tmp_path) -> None:
    """A None loop result keeps the free-chat interception working."""
    store, curricula, controller, course_id = _seed(tmp_path)
    console, runner = _make_console(qapp, store, curricula, controller)
    controller._orchestrator_instance = None  # run_learning_loop returns None
    controller.exit_mode()

    console.input.setText("继续学习")
    console._send()

    assert controller.state.enabled is True
    assert controller.state.active_course_id == course_id
    assert runner.asked == []
    console.close()


def test_exploding_recorder_does_not_break_the_turn(qapp, tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)

    class _ExplodingRecorder:
        def record_from_message(self, *args, **kwargs):
            raise RuntimeError("boom")

    controller._interaction_recorder = _ExplodingRecorder()
    console, runner = _make_console(qapp, store, curricula, controller)

    console.input.setText("解释一下传递函数")
    console._send()

    assert runner.asked == ["解释一下传递函数"]
    assert InteractionStore(store).count() == 0
    console.close()
