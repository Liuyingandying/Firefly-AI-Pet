"""Phase 1E: Learning loop wired into the daily conversation runtime.

Locks the runtime-integration contract only (no new learning capability):

1. ordinary chat never enters the learning flow
2. learning mode entry / course switch works through the console shell
3. assessment intents (考考我 / 测试一下 / 帮我复习) reach AssessmentService
4. explanation / ordinary question phrasings never trigger an assessment
5. exiting learning mode returns to free chat and hides the strip
6. startup restore brings back the project + unfinished session, never an exam
7. the injected LearningRuntimeContext is read-only (no store writes)
8. assessment still goes through the Rule Engine (the only mastery writer)
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.assessment import AssessmentService, AssessmentState
from core.learning.controller import LearningModeController
from core.learning.intents import LearningIntent, parse_learning_intent
from core.learning.quiz_adapter import QuizGrading
from core.learning.store import LearningStore


T0 = "2026-09-01T00:00:00+00:00"


class _FakeSettings:
    """In-memory stand-in for SettingsManager's learning keys."""

    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _pass_grading(concept: str) -> QuizGrading:
    return QuizGrading(
        question=f"{concept}的定义是什么？", answer="零极点比值",
        feedback=f"正确的部分：说清了{concept}的定义\n缺失的部分：无\n错误理解：无",
    )


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    settings = _FakeSettings()
    service = AssessmentService(
        store,
        generate_question=lambda concept, course: f"{concept}的定义是什么？",
        grade_answer=lambda concept, course, question, answer: _pass_grading(concept),
    )
    controller = LearningModeController(
        store=store, settings=settings, assessment_service=service)
    return store, settings, controller, service


def _enter_with_course(store, controller, name: str = "自动控制原理") -> str:
    controller.enter_mode()
    controller.create_course(name)
    course_id = controller.state.active_course_id
    assert course_id is not None
    return course_id


def _make_console(qapp, tmp_path, controller=None):
    from ui.v2.console import CompanionConsole

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

    runner = _FakeRunner()
    console = CompanionConsole(runner)
    if controller is None:
        store = LearningStore(tmp_path / "console_learning.sqlite3")
        store.initialize()
        controller = LearningModeController(store=store, settings=_FakeSettings())
    console.learning = controller
    runner.learning_controller = controller
    return console, runner, controller


# 1. ordinary chat never enters the learning flow -----------------------------


def test_ordinary_chat_does_not_enter_learning_flow(env) -> None:
    store, _, controller, service = env
    # Mode off: nothing is intercepted at all.
    assert controller.handle_text("什么是传递函数？") is None
    assert controller.handle_text("考考我") is None

    _enter_with_course(store, controller)
    # Mode on, but an ordinary question / explanation request still passes
    # straight through to the normal runner path.
    assert controller.handle_text("什么是传递函数？") is None
    assert controller.handle_text("帮我解释一下这个公式") is None
    assert controller.handle_text("今天天气不错") is None
    assert service.active is None
    assert store.list_assessments() == []


def test_ordinary_chat_reaches_runner_via_console(qapp, tmp_path) -> None:
    console, runner, _ = _make_console(qapp, tmp_path)
    console.input.setText("什么是传递函数？")
    console._send()
    assert runner.asked == ["什么是传递函数？"]
    console.close()


# 2. learning mode entry works normally --------------------------------------


def test_learning_mode_entry_shows_project(env) -> None:
    store, _, controller, _ = env
    _enter_with_course(store, controller)
    assert controller.state.enabled is True
    assert "自动控制原理" in controller.status_line()
    context = controller.build_runtime_context()
    assert context.enabled is True
    assert context.course_name == "自动控制原理"
    assert context.session_active is True


def test_console_strip_reflects_learning_mode(qapp, tmp_path) -> None:
    console, _, controller = _make_console(qapp, tmp_path)
    console.learning.enter_mode(); console._apply_learning_state()  # enters the mode + shows the picker
    console._on_picker_create()
    reply = console.learning.handle_text("自动控制原理")
    assert reply is not None
    console._apply_learning_state()
    assert console.learning.state.enabled is True
    assert console.mode_strip.isVisible() is True or console.mode_strip.text() != ""
    assert "自动控制原理" in console.mode_strip.text()
    console.close()


# 3. assessment intents trigger the assessment flow --------------------------


@pytest.mark.parametrize("text", [
    "考考我", "检查一下我的理解", "测试一下", "出题", "测测我",
])
def test_quiz_intents_start_assessment(env, text) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)
    reply = controller.handle_text(text)
    assert reply == "传递函数的定义是什么？"
    assert service.state == AssessmentState.WAITING_ANSWER
    assert service.active is not None


def test_review_intent_prefers_due_concept(env) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    # Two concepts; only the FIRST one has a due review item.
    store.add_concept(course_id, "传递函数", created_at=T0)
    due_concept = store.add_concept(course_id, "根轨迹", created_at=T0)
    store.create_review_item(due_concept.id, due_at=T0, interval_days=1)

    reply = controller.handle_text("帮我复习")
    assert reply == "根轨迹的定义是什么？"
    assert service.active is not None
    assert service.active.concept_id == due_concept.id


def test_review_intent_without_due_falls_back_to_binding(env) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)
    reply = controller.handle_text("复习一下")
    assert reply is not None
    assert service.active is not None
    assert service.active.concept_name == "传递函数"


def test_quiz_intent_without_course_asks_for_project(env) -> None:
    store, _, controller, service = env
    controller.enter_mode()  # no courses -> awaiting-name flow is armed
    reply = controller.handle_text("考考我")
    assert reply is not None and "学习项目" in reply
    assert service.active is None
    # The armed name flow must NOT swallow the command as a project name.
    assert store.list_courses() == []


def test_quiz_intent_while_awaiting_name_keeps_flow_armed(env) -> None:
    store, _, controller, _ = env
    controller.enter_mode()
    controller.handle_text("考考我")
    assert controller.handle_text("自动控制原理") is not None
    assert [course.name for course in store.list_courses()] == ["自动控制原理"]


def test_review_intent_without_course_asks_for_project(env) -> None:
    store, _, controller, service = env
    controller.create_course("自动控制原理")  # course exists, mode still off
    controller.enter_mode()
    controller.exit_mode()
    reply = controller.handle_text("帮我复习")
    assert reply is None  # mode off: nothing is intercepted at all
    assert service.active is None


# 4. explanation / chat phrasings never trigger an assessment ----------------


@pytest.mark.parametrize("text", [
    "解释一下传递函数",
    "帮我解释一下测试策略",
    "讲讲复习计划应该怎么安排",
    "复习和预习有什么区别",
    "测试驱动开发是什么",
    "考试前怎么安排时间",
    "今天有点累，先看看别的",
])
def test_non_assessment_phrasings_do_not_trigger(env, text) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)
    assert controller.handle_text(text) is None
    assert service.active is None
    assert store.list_assessments() == []


def test_rest_marker_exits_mode_without_assessment(env) -> None:
    """Pre-existing 1B exit phrasing ("休息一下") ends the mode; it must never
    be mistaken for an assessment trigger."""
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)
    assert controller.handle_text("今天想休息一下，先看看别的") is not None
    assert controller.state.enabled is False
    assert service.active is None
    assert store.list_assessments() == []


def test_intent_layer_separates_quiz_from_explanation() -> None:
    assert parse_learning_intent("考考我").intent is LearningIntent.QUIZ_REQUEST
    assert parse_learning_intent("测试一下").intent is LearningIntent.QUIZ_REQUEST
    assert parse_learning_intent("帮我复习").intent is LearningIntent.REVIEW_PRACTICE
    assert parse_learning_intent("复习一下").intent is LearningIntent.REVIEW_PRACTICE
    # Explanation phrasings must stay unresolved -> ordinary chat.
    assert parse_learning_intent("解释一下传递函数").intent is None
    assert parse_learning_intent("什么是传递函数？").intent is None
    assert parse_learning_intent("复习计划怎么安排").intent is None


# 5. exit restores free chat -------------------------------------------------


def test_exit_restores_free_chat(env) -> None:
    store, settings, controller, service = env
    _enter_with_course(store, controller)
    controller.exit_mode()
    assert controller.state.enabled is False
    assert controller.build_runtime_context().enabled is False
    assert controller.context_block() is None
    # Learning commands become inert; ordinary text passes through untouched.
    assert controller.handle_text("考考我") is None
    assert controller.handle_text("帮我复习") is None
    assert service.active is None
    # The project itself is remembered for the next entry.
    assert settings.learning_last_course_id != ""


def test_console_after_exit_hides_strip_and_forwards_chat(qapp, tmp_path) -> None:
    console, runner, controller = _make_console(qapp, tmp_path)
    controller.enter_mode()
    controller.create_course("自动控制原理")
    console._apply_learning_state()
    assert console.mode_strip.isVisibleTo(console) is True
    assert "自动控制原理" in console.mode_strip.text()

    console.learning.exit_mode(); console._apply_learning_state()  # explicit exit
    assert controller.state.enabled is False
    assert console.mode_strip.isVisibleTo(console) is False
    console.input.setText("什么是传递函数？")
    console._send()
    assert runner.asked == ["什么是传递函数？"]
    console.close()


# 6. course switch + startup restore ----------------------------------------


def test_course_switch_restores_new_project(env) -> None:
    store, settings, controller, service = env
    _enter_with_course(store, controller, "自动控制原理")
    first_session = controller.state.active_session_id
    reply = controller.create_course("物理光学")
    assert controller.state.active_course_name == "物理光学"
    assert settings.learning_last_course_id == controller.state.active_course_id
    assert controller.state.active_session_id != first_session
    assert store.get_session(first_session).status == "ended"
    # Switching projects cancels any in-flight assessment.
    assert service.active is None
    context = controller.build_runtime_context()
    assert context.course_name == "物理光学"


def test_restart_stays_in_free_chat(env) -> None:
    """Phase 1E.1: a restart never re-activates learning mode, even when the
    previous run ended inside learning mode."""
    store, settings, controller, _ = env
    course_id = _enter_with_course(store, controller)

    # "Restart Firefly": a brand-new controller over the same store/settings.
    fresh = LearningModeController(store=store, settings=settings)
    assert fresh.state.enabled is False
    # Validation only: it reports the remembered project, activates nothing.
    assert fresh.restore_runtime() is True
    assert fresh.state.enabled is False
    assert fresh.state.active_course_id is None
    assert fresh.state.active_session_id is None
    assert fresh.context_block() is None
    assert fresh.status_line() == ""
    # The remembered project survived for a later explicit 继续.
    assert fresh.last_course().id == course_id


def test_restart_never_auto_starts_assessment(env) -> None:
    store, settings, controller, _ = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)

    fresh_service = AssessmentService(
        store,
        generate_question=lambda concept, course: f"{concept}的定义是什么？",
        grade_answer=lambda concept, course, q, a: _pass_grading(concept),
    )
    fresh = LearningModeController(
        store=store, settings=settings, assessment_service=fresh_service)
    assert fresh.restore_runtime() is True
    assert fresh.state.enabled is False
    assert fresh_service.active is None
    assert fresh_service.state == AssessmentState.IDLE
    assert store.list_assessments() == []


def test_restart_validation_without_course_reports_nothing(env) -> None:
    store, settings, controller, _ = env
    assert controller.restore_runtime() is False
    assert controller.state.enabled is False
    assert controller.context_block() is None


def test_console_cold_start_shows_free_chat(qapp, tmp_path) -> None:
    """Phase 1E.1: Firefly starts in 自由对话 — no strip, no project card —
    even though a last project is remembered."""
    store = LearningStore(tmp_path / "restore_learning.sqlite3")
    store.initialize()
    settings = _FakeSettings()
    first = LearningModeController(store=store, settings=settings)
    first.enter_mode()
    first.create_course("自动控制原理")

    # "Restart Firefly": fresh controller + fresh console over the same files.
    fresh = LearningModeController(store=store, settings=settings)
    console, _, controller = _make_console(qapp, tmp_path, controller=fresh)
    assert controller.state.enabled is False
    console._restore_learning_runtime()
    # Cold start stays free chat: nothing activated, nothing displayed.
    assert controller.state.enabled is False
    assert controller.state.active_course_id is None
    assert controller.state.active_session_id is None
    assert console.mode_strip.isVisibleTo(console) is False
    assert console.companion.context_status.view.mode == "自由对话"
    assert console.companion.context_status.view.course_name == ""
    # ...but the project is still remembered for the explicit 继续 flow.
    remembered = controller.last_course_tuple()
    assert remembered is not None and remembered[1] == "自动控制原理"
    assert console.runner.asked == []
    console.close()


def test_restart_clears_stale_reference(env) -> None:
    store, settings, controller, _ = env
    _enter_with_course(store, controller)
    store.delete_course(controller.state.active_course_id)
    fresh = LearningModeController(store=store, settings=settings)
    assert fresh.restore_runtime() is False
    assert settings.learning_last_course_id == ""


def test_restart_does_not_revive_an_active_session(env) -> None:
    """A leftover `active` session row (unclean shutdown) is never attached to
    the new run; the run stays in free chat with no session."""
    store, settings, controller, _ = env
    course_id = _enter_with_course(store, controller)
    leftover = controller.state.active_session_id
    assert store.get_session(leftover).status == "active"

    fresh = LearningModeController(store=store, settings=settings)
    assert fresh.restore_runtime() is True
    assert fresh.state.active_session_id is None
    assert fresh.build_runtime_context().session_active is False
    # The leftover row is untouched by startup validation (no session write).
    assert store.get_session(leftover).status == "active"


# 7. LearningRuntimeContext is read-only -------------------------------------


def test_runtime_context_does_not_write_store(env) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    concept = store.add_concept(course_id, "传递函数", created_at=T0)
    store.touch_concept(controller.state.active_session_id, concept.id, "study")
    store.apply_mastery_update(concept.id, 2, "low", "phase1e-test")

    before = (
        len(store.list_courses()),
        len(store.list_concepts(course_id)),
        len(store.list_sessions(course_id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(concept.id)),
        store.get_concept(concept.id).mastery_level,
    )
    for _ in range(3):
        context = controller.build_runtime_context()
        block = controller.context_block()
        assert context.course_id == course_id
        assert context.concept_name == "传递函数"
        assert context.concept_mastery == 2
        assert block is not None and "自动控制原理" in block
    after = (
        len(store.list_courses()),
        len(store.list_concepts(course_id)),
        len(store.list_sessions(course_id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(concept.id)),
        store.get_concept(concept.id).mastery_level,
    )
    assert before == after
    assert service.active is None


def test_runtime_context_block_mentions_project_and_asks_nothing(env) -> None:
    store, _, controller, _ = env
    _enter_with_course(store, controller)
    block = controller.context_block()
    assert "学习模式" in block
    assert "自动控制原理" in block
    # No fabricated mastery claims, no forced quizzing.
    assert "不虚构掌握程度" in block
    assert "不强制出题" in block


def test_runtime_context_due_count_is_read_only(env) -> None:
    store, _, controller, _ = env
    course_id = _enter_with_course(store, controller)
    concept = store.add_concept(course_id, "传递函数", created_at=T0)
    store.create_review_item(concept.id, due_at=T0, interval_days=1)

    context = controller.build_runtime_context()
    assert context.due_review_count == 1
    # Reading the due count must not consume/mark the review item.
    item = store.list_review_items(concept.id)[0]
    assert item.status == "pending"


def test_runner_injects_learning_context_without_db_writes(tmp_path, qapp) -> None:
    """The runner injects the read-only learning block through the existing
    turn_context channel and performs no store write of its own."""
    from types import SimpleNamespace

    store = LearningStore(tmp_path / "runner_learning.sqlite3")
    store.initialize()
    controller = LearningModeController(store=store, settings=_FakeSettings())
    course_id = _enter_with_course(store, controller)
    concept = store.add_concept(course_id, "传递函数", created_at=T0)
    store.touch_concept(controller.state.active_session_id, concept.id, "study")

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2,
                  turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    from ui.character_conversation_runner import CharacterConversationRunner

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("什么是传递函数？")

    block = captured["turn_context"]
    assert block is not None
    assert "学习模式" in block
    assert "自动控制原理" in block          # active course
    assert "传递函数" in block              # recent concept
    assert "掌握 0/5" in block              # recent mastery (read-only)
    assert "进行中" in block                # current session
    # Read-only: injecting the context wrote nothing.
    assert store.list_assessments() == []
    assert store.get_concept(concept.id).mastery_level == 0
    assert store.list_mastery_audit(concept.id) == []


def test_runner_skips_learning_context_when_mode_off(tmp_path, qapp) -> None:
    from types import SimpleNamespace

    store = LearningStore(tmp_path / "runner_off.sqlite3")
    store.initialize()
    controller = LearningModeController(store=store, settings=_FakeSettings())
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2,
                  turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    from ui.character_conversation_runner import CharacterConversationRunner

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("什么是传递函数？")
    assert captured["turn_context"] is None


# 8. assessment still goes through the Rule Engine ---------------------------


def test_assessment_intent_records_through_rule_engine(env) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    concept = store.add_concept(course_id, "传递函数", created_at=T0)

    question = controller.handle_text("考考我")
    assert question == "传递函数的定义是什么？"
    # Question only: nothing recorded yet, mastery untouched.
    assert store.list_assessments() == []
    assert store.get_concept(concept.id).mastery_level == 0

    reply = controller.handle_text("零极点比值")
    assert reply is not None
    assert service.state == AssessmentState.RECORDED
    records = store.list_assessments(concept.id)
    assert len(records) == 1 and records[0].source == "quiz"
    # Mastery moved only through the Rule Engine's single write boundary.
    assert store.get_concept(concept.id).mastery_level == 1
    assert len(store.list_mastery_audit(concept.id)) == 1
    assert len(store.list_review_items(concept.id)) == 1


def test_ordinary_chat_after_assessment_stays_isolated(env) -> None:
    store, _, controller, service = env
    course_id = _enter_with_course(store, controller)
    store.add_concept(course_id, "传递函数", created_at=T0)
    controller.handle_text("考考我")
    controller.handle_text("零极点比值")
    assert len(store.list_assessments()) == 1

    assert controller.handle_text("谢谢，那什么是根轨迹？") is None
    assert len(store.list_assessments()) == 1
