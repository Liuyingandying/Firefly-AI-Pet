"""Concept selection UX tests (Phase 8B-1).

Covers the frozen contract:

    多概念出现选择卡 / 选择后启动 assessment / 单概念无需选择 /
    普通聊天不触发 / 退出学习模式清除 pending / mastery 不提前变化 /
    Rule Engine 不修改

Pure Qt-offscreen + tmp_path SQLite; no provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.controller import LearningModeController
from core.learning.assessment import AssessmentService, AssessmentState
from core.learning.curriculum.store import CurriculumStore
from core.learning.interactions import InteractionStore
from core.learning.quiz_adapter import QuizGrading
from core.learning.store import LearningStore
from ui.v2.course_picker import ConceptChoiceCard


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


class _FakeRunner:
    def __init__(self) -> None:
        self.agent_event = None
        self.session_video = None
        self.video_study = None
        self._screen_vision_settings = None
        self.learning_controller = None
        self.asked: list[str] = []

    def ask(self, text: str, learning_result=None) -> bool:
        self.asked.append(text)
        return True


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _visible_cards(console) -> list[ConceptChoiceCard]:
    cards = [
        console.chat._column.itemAt(i).widget()
        for i in range(console.chat._column.count())
        if isinstance(console.chat._column.itemAt(i).widget(), ConceptChoiceCard)
    ]
    return [c for c in cards if c.isVisibleTo(console)]


def _make_console(qapp, tmp_path, *, concepts=("传递函数", "状态空间")):
    from ui.v2.console import CompanionConsole

    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(
            q, a, "正确的部分：回答了核心定义", score=0.9
        ),
    )
    controller = LearningModeController(
        store=store, settings=_Settings(),
        curriculum_store=curricula, assessment_service=service,
    )
    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning = controller
    runner.learning_controller = controller
    console.enter_mode = controller.enter_mode  # noqa: F841 (kept for clarity)
    controller.enter_mode()
    controller.create_course("自动控制原理")
    for name in concepts:
        store.add_concept(controller.state.active_course_id, name)
    return console, runner, controller, store, service


def _send(console, text: str) -> None:
    console.input.setText(text)
    console._send()


# ---------------------------------------------------------------------------
# 1. 多概念出现选择卡
# ---------------------------------------------------------------------------


def test_multi_concept_quiz_shows_the_choice_card(qapp, tmp_path) -> None:
    console, runner, controller, store, _service = _make_console(qapp, tmp_path)
    _send(console, "检查一下我理解了吗")

    cards = _visible_cards(console)
    assert len(cards) == 1
    assert [name for _cid, name in cards[0].concepts] == ["传递函数", "状态空间"]
    assert "请选择知识点" in "".join(
        label.text() for label in cards[0].findChildren(__import__(
            "PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)
    )
    # nothing started, nothing recorded before the choice
    assert controller.assessment_service.state == AssessmentState.IDLE
    assert store.list_assessments() == []
    console.close()


def test_pending_assessment_accessor(qapp, tmp_path) -> None:
    console, runner, controller, store, _service = _make_console(qapp, tmp_path)
    assert controller.pending_assessment() is None

    _send(console, "检查一下我理解了吗")
    pending = controller.pending_assessment()
    assert pending is not None
    assert pending["course_name"] == "自动控制原理"
    assert [name for _cid, name in pending["concepts"]] == ["传递函数", "状态空间"]
    console.close()


# ---------------------------------------------------------------------------
# 2. 选择后启动 assessment
# ---------------------------------------------------------------------------


def test_choice_starts_the_pending_assessment(qapp, tmp_path) -> None:
    console, runner, controller, store, service = _make_console(qapp, tmp_path)
    _send(console, "检查一下我理解了吗")
    _visible_cards(console)[0].concept_selected.emit("传递函数")

    assert service.state == AssessmentState.WAITING_ANSWER
    assert service.pending_choice is None          # cleared on success
    assert service.active.concept_name == "传递函数"
    assert service.active.question == "传递函数的定义是什么？"
    # the assessment_start fact is recorded for the chosen concept
    concept = store.find_concept(controller.state.active_course_id, "传递函数")
    events = InteractionStore(store).list_interactions(controller.state.active_course_id)
    assert events[-1].event_type == "assessment_start"
    assert events[-1].concept_id == concept.id
    # the question is visible in the chat
    assert "传递函数的定义是什么？" in _chat_text(console)
    console.close()


def _chat_text(console) -> None:
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
# 3. 单概念无需选择
# ---------------------------------------------------------------------------


def test_single_concept_starts_directly_without_a_card(qapp, tmp_path) -> None:
    console, runner, controller, store, service = _make_console(
        qapp, tmp_path, concepts=("传递函数",)
    )
    _send(console, "检查一下我理解了吗")

    assert _visible_cards(console) == []
    assert service.state == AssessmentState.WAITING_ANSWER
    assert service.active.concept_name == "传递函数"
    console.close()


# ---------------------------------------------------------------------------
# 4. 普通聊天不触发
# ---------------------------------------------------------------------------


def test_ordinary_chat_shows_no_choice_card(qapp, tmp_path) -> None:
    console, runner, controller, store, _service = _make_console(qapp, tmp_path)
    for text in ("今天天气不错", "帮我写段代码", "你好呀"):
        _send(console, text)
        assert _visible_cards(console) == []
    assert controller.assessment_service.pending_choice is None
    assert runner.asked == ["今天天气不错", "帮我写段代码", "你好呀"]
    console.close()


def test_ignoring_the_card_does_not_re_show_it(qapp, tmp_path) -> None:
    """The card appears once at the transition; later turns don't re-show it."""
    console, runner, controller, store, _service = _make_console(qapp, tmp_path)
    _send(console, "检查一下我理解了吗")
    assert len(_visible_cards(console)) == 1

    _send(console, "今天天气不错")  # user ignores the card
    assert len(_visible_cards(console)) == 1  # no second card
    # ...but the text path still resumes the quiz (Phase 8A)
    assert controller.handle_text("传递函数") == "传递函数的定义是什么？"
    console.close()


# ---------------------------------------------------------------------------
# 5. 退出学习模式清除 pending
# ---------------------------------------------------------------------------


def test_exit_clears_the_pending_choice(qapp, tmp_path) -> None:
    console, runner, controller, store, service = _make_console(qapp, tmp_path)
    _send(console, "检查一下我理解了吗")
    assert controller.pending_assessment() is not None

    controller.exit_mode()
    assert service.pending_choice is None
    assert controller.pending_assessment() is None
    # after exit, a quiz request starts a fresh pending (mode re-entered first)
    console.close()


def test_exit_then_reenter_start_clean(qapp, tmp_path) -> None:
    console, runner, controller, store, service = _make_console(qapp, tmp_path)
    _send(console, "检查一下我理解了吗")
    controller.exit_mode()
    controller.enter_mode()
    controller.continue_last_course()
    assert controller.pending_assessment() is None
    console.close()


# ---------------------------------------------------------------------------
# 6. mastery 不提前变化
# ---------------------------------------------------------------------------


def test_mastery_unchanged_until_the_quiz_is_answered(qapp, tmp_path) -> None:
    console, runner, controller, store, service = _make_console(qapp, tmp_path)
    course_id = controller.state.active_course_id
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 2, "medium", "existing", source="quiz")

    _send(console, "检查一下我理解了吗")            # card appears
    _visible_cards(console)[0].concept_selected.emit("传递函数")
    assert service.state == AssessmentState.WAITING_ANSWER
    assert store.get_concept(concept.id).mastery_level == 2   # question only
    assert store.list_assessments(concept.id) == []           # nothing recorded
    assert len(store.list_mastery_audit(concept.id)) == 1     # only "existing"

    _send(console, "传递函数是输出与输入拉氏变换之比")  # the actual answer
    assert service.state == AssessmentState.RECORDED
    assert store.get_concept(concept.id).mastery_level == 3   # via Rule Engine
    console.close()


# ---------------------------------------------------------------------------
# 7. Rule Engine 不修改
# ---------------------------------------------------------------------------


def test_rule_engine_is_untouched_by_the_choice_flow(qapp, tmp_path) -> None:
    """The engine module carries no choice UI wiring (AST) and its decisions
    are identical before/after the selection flow."""
    import ast

    engine_path = Path(__file__).resolve().parents[1] / "core" / "learning" / "rule_engine.py"
    tree = ast.parse(engine_path.read_text(encoding="utf-8"), filename=str(engine_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "concept_choice" not in node.module
            assert "console" not in node.module
        if isinstance(node, ast.Import):
            assert all(
                "concept_choice" not in alias.name and "console" not in alias.name
                for alias in node.names
            )

    console, runner, controller, store, _service = _make_console(qapp, tmp_path)
    concept = store.find_concept(controller.state.active_course_id, "传递函数")
    from core.learning.models import AssessmentEvidence
    from core.learning.rule_engine import LearningRuleEngine

    engine = LearningRuleEngine(store)
    evidence = AssessmentEvidence(
        concept_id=concept.id, source="quiz", score=0.9, confidence=0.9,
        difficulty="basic", created_at="2026-06-01T00:00:00+00:00",
    )
    first = engine.evaluate(concept.id, evidence)

    _send(console, "检查一下我理解了吗")
    _visible_cards(console)[0].concept_selected.emit("传递函数")
    second = engine.evaluate(concept.id, evidence)
    assert (first.new_mastery, first.changed) == (second.new_mastery, second.changed)
    console.close()


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
