"""Phase 1E.1 — Learning Mode exit / restore semantics.

Frozen semantics under test:

    last_course_id   = "上一次学的是哪门课"   -> persisted, allowed to survive
    active_course_id = "这一运行期正在学哪门课" -> runtime only

- exit_mode(): enabled=False, active_course_id=None, active_session_id=None,
  the active StudySession is ENDED, last_course_id is KEPT.
- cold start: ALWAYS free chat. Nothing is activated from last_course_id; no
  StudySession is created; no active session is revived.
- re-entering learning mode only OFFERS the remembered project; the course is
  activated only after the user explicitly confirms (继续 chip / "继续学习"),
  and that starts a NEW session for this run.

The 20 required points are labelled [n] in the test names/comments.
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
        grade_answer=lambda concept, course, q, a: _pass_grading(concept),
    )
    controller = LearningModeController(
        store=store, settings=settings, assessment_service=service)
    return store, settings, controller, service


def _restart(store, settings, service=None):
    """Simulate a Firefly restart: a brand-new controller over the same files."""
    return LearningModeController(
        store=store, settings=settings, assessment_service=service)


def _make_console(qapp, controller, tmp_path):
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
    console.learning = controller
    runner.learning_controller = controller
    return console, runner


# ---------------------------------------------------------------------------
# [1]-[6] exit semantics
# ---------------------------------------------------------------------------


def test_01_06_exit_semantics(env) -> None:
    store, settings, controller, service = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    session_id = controller.state.active_session_id

    # [1] learning mode with an active course
    assert controller.state.enabled is True
    assert course_id is not None and session_id is not None
    assert controller.build_runtime_context().course_id == course_id

    reply = controller.exit_mode()

    # [2] enabled=False  [3] active_course_id=None  [4] active_session_id=None
    assert controller.state.enabled is False
    assert controller.state.active_course_id is None
    assert controller.state.active_session_id is None
    assert controller.state.active_course_name is None
    # [5] last_course_id is kept (the project is a long-lived asset)
    assert settings.learning_last_course_id == course_id
    # [6] the active session is ENDED
    ended = store.get_session(session_id)
    assert ended is not None and ended.status == "ended"
    assert store.get_active_session(course_id) is None
    # Runtime context is gone; ordinary chat is unmodified.
    assert controller.build_runtime_context().enabled is False
    assert controller.context_block() is None
    assert "休息" in reply


def test_exit_releases_in_flight_assessment(env) -> None:
    store, _, controller, service = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    store.add_concept(course_id, "传递函数", created_at=T0)
    controller.handle_text("考考我")
    assert service.state == AssessmentState.WAITING_ANSWER

    controller.exit_mode()
    assert service.active is None
    # The pending question is dropped: the next message is not graded.
    assert controller.handle_text("零极点比值") is None
    assert store.list_assessments() == []


# ---------------------------------------------------------------------------
# [7]-[12] restart semantics (cold start == FREE_CHAT)
# ---------------------------------------------------------------------------


def test_07_12_restart_is_free_chat(env) -> None:
    store, settings, controller, service = env
    # Exited learning mode yesterday: session ended, project remembered.
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    first_session_id = controller.state.active_session_id
    controller.exit_mode()

    # [7] restart: new controller over the same store/settings
    restarted = _restart(store, settings)

    # [8] default FREE_CHAT  [9] no active course  [10] no active session
    assert restarted.state.enabled is False
    assert restarted.state.active_course_id is None
    assert restarted.state.active_session_id is None
    assert restarted.state.active_course_name is None
    assert restarted.build_runtime_context().session_active is False
    assert restarted.context_block() is None
    assert restarted.status_line() == ""
    # [11] restart created NO new StudySession
    assert len(store.list_sessions(course_id)) == 1
    assert store.get_session(first_session_id).status == "ended"
    # [12] last_course_id is still readable
    assert settings.learning_last_course_id == course_id
    assert restarted.last_course().id == course_id
    assert restarted.restore_runtime() is True  # validation only
    assert restarted.state.enabled is False     # ...never activates


def test_12b_restart_never_writes_sessions_or_mastery(env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    controller.exit_mode()
    before = (
        len(store.list_sessions(course_id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(course_id)),
    )
    restarted = _restart(store, settings)
    restarted.restore_runtime()
    restarted.build_runtime_context()
    after = (
        len(store.list_sessions(course_id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_mastery_audit(course_id)),
    )
    assert before == after


def test_crash_leftover_session_is_not_resumed(env) -> None:
    """Unclean shutdown (killed while in learning mode) still restarts into
    free chat; the leftover `active` row is not attached to the new run."""
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    leftover = controller.state.active_session_id
    # No exit_mode(): simulate a crash.
    assert store.get_session(leftover).status == "active"

    restarted = _restart(store, settings)
    restarted.restore_runtime()
    assert restarted.state.enabled is False
    assert restarted.state.active_session_id is None
    assert restarted.build_runtime_context().session_active is False
    # Startup validation does not write to the store.
    assert store.get_session(leftover).status == "active"


# ---------------------------------------------------------------------------
# [13]-[16] continue flow (offer -> explicit confirm -> new session)
# ---------------------------------------------------------------------------


def test_13_enter_mode_offers_last_course(env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()

    restarted = _restart(store, settings)
    reply = restarted.enter_mode()
    assert "上次我们学到「自动控制原理」，要继续吗？" == reply
    # [14] the course is only OFFERED: not activated by entering the mode.
    assert restarted.state.enabled is True
    assert restarted.state.active_course_id is None
    assert restarted.state.active_session_id is None
    assert restarted.build_runtime_context().course_id is None


def test_14_console_offers_continue_without_activating(qapp, tmp_path, env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()

    restarted = _restart(store, settings)
    console, _ = _make_console(qapp, restarted, tmp_path)
    console.learning.enter_mode(); console._apply_learning_state(); console._show_learning_entry()  # legacy entry

    # Nothing is activated and no project is shown as active yet: the strip
    # may say the mode is on, but it must NOT name a course before the user
    # confirms, and the status card stays empty.
    assert restarted.state.active_course_id is None
    assert restarted.state.active_session_id is None
    assert "自动控制原理" not in console.mode_strip.text()
    assert console.mode_strip.text() == "📘 学习模式 · 未选择项目"
    assert console.companion.context_status.view.mode == "学习模式"
    assert console.companion.context_status.view.course_name == ""

    # Phase 2-UX: the entry surface is the welcome card, which leads with the
    # 继续 action for the remembered project.
    from core.learning.ui import ACTION_CONTINUE
    from ui.v2.course_picker import LearningEntryCardView

    cards = [
        console.chat._column.itemAt(i).widget()
        for i in range(console.chat._column.count())
        if isinstance(console.chat._column.itemAt(i).widget(), LearningEntryCardView)
    ]
    assert len(cards) == 1
    entry = cards[0]
    assert entry.card.last_course_id == settings.learning_last_course_id
    assert entry.card.last_course_name == "自动控制原理"
    assert (ACTION_CONTINUE, "继续 自动控制原理") in entry.card.actions

    # [15] only the user's click activates the course.
    entry.continue_requested.emit()
    assert restarted.state.active_course_id == settings.learning_last_course_id
    assert restarted.state.active_course_name == "自动控制原理"
    assert console.mode_strip.isVisibleTo(console) is True
    assert "自动控制原理" in console.mode_strip.text()
    assert console.companion.context_status.view.course_name == "自动控制原理"
    console.close()


def test_15_16_continue_creates_a_new_session(env) -> None:
    store, settings, controller, _ = env
    # "Yesterday": Session A opened and closed by an ordinary exit.
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    session_a = controller.state.active_session_id
    controller.exit_mode()
    assert store.get_session(session_a).status == "ended"

    # "Today": restart, then the user explicitly continues.
    restarted = _restart(store, settings)
    restarted.restore_runtime()
    restarted.enter_mode()
    reply = restarted.continue_last_course()

    # [15] the course is activated only by this explicit action
    assert reply is not None and "自动控制原理" in reply
    assert restarted.state.active_course_id == course_id
    # [16] a NEW session for this run; the two runs are never merged
    session_b = restarted.state.active_session_id
    assert session_b is not None and session_b != session_a
    sessions = store.list_sessions(course_id)
    assert len(sessions) == 2
    assert sorted(s.status for s in sessions) == ["active", "ended"]
    assert [s.id for s in sessions if s.status == "active"] == [session_b]


def test_16b_continue_after_crash_still_opens_a_new_session(env) -> None:
    """Leftover active row from a crash: continuing must NOT append to it."""
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    crashed = controller.state.active_session_id
    # No exit_mode(): simulate a crash.

    restarted = _restart(store, settings)
    restarted.restore_runtime()
    restarted.enter_mode()
    restarted.continue_last_course()

    new_session = restarted.state.active_session_id
    assert new_session is not None and new_session != crashed
    assert store.get_session(crashed).status == "ended"
    active = [s for s in store.list_sessions(course_id) if s.status == "active"]
    assert [s.id for s in active] == [new_session]


# ---------------------------------------------------------------------------
# [17] natural-language continue from free chat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["继续学习", "继续上次的课", "继续上次"])
def test_17_natural_language_continue_from_free_chat(env, text) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    controller.exit_mode()

    restarted = _restart(store, settings)
    assert restarted.state.enabled is False
    reply = restarted.handle_text(text)

    assert reply is not None and "自动控制原理" in reply
    assert restarted.state.enabled is True
    assert restarted.state.active_course_id == course_id
    assert restarted.state.active_session_id is not None
    assert "自动控制原理" in restarted.status_line()


def test_17b_natural_language_continue_is_the_only_free_chat_interception(env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()
    restarted = _restart(store, settings)

    for text in ("你好", "什么是传递函数？", "今天天气不错", "考考我",
                 "帮我复习", "我有哪些学习项目", "换一个项目"):
        assert restarted.handle_text(text) is None, text
    assert restarted.state.enabled is False
    assert restarted.state.active_course_id is None


def test_17c_natural_language_continue_with_nothing_remembered(env) -> None:
    store, settings, controller, _ = env
    restarted = _restart(store, settings)
    reply = restarted.handle_text("继续学习")
    # Falls back to the normal entry prompt instead of failing silently.
    assert reply is not None and "学习项目" in reply
    assert restarted.state.enabled is True
    assert restarted.state.active_course_id is None


def test_17d_console_free_chat_continue_shows_project(qapp, tmp_path, env) -> None:
    """User-visible cold-start path: 自由对话 -> 「继续学习」 -> strip + card show
    the project, and the message never reaches the ordinary chat runner."""
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()

    restarted = _restart(store, settings)
    console, runner = _make_console(qapp, restarted, tmp_path)
    console._restore_learning_runtime()  # startup: stays 自由对话
    assert console.companion.context_status.view.mode == "自由对话"
    assert console.mode_strip.isVisibleTo(console) is False

    console.input.setText("继续学习")
    console._send()

    assert runner.asked == []                       # handled by learning mode
    assert restarted.state.active_course_id is not None
    assert console.mode_strip.isVisibleTo(console) is True
    assert console.mode_strip.text() == "📘 学习模式 · 自动控制原理"
    assert console.companion.context_status.view.course_name == "自动控制原理"
    console.close()


# ---------------------------------------------------------------------------
# [18] stale last_course_id
# ---------------------------------------------------------------------------


def test_18_stale_last_course_cleared_on_restart(env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("物理光学")
    stale_id = controller.state.active_course_id
    controller.exit_mode()
    store.delete_course(stale_id)
    settings.learning_last_course_id = stale_id  # keep the stale reference

    restarted = _restart(store, settings)
    assert restarted.restore_runtime() is False      # no crash
    assert settings.learning_last_course_id == ""     # safely cleaned
    assert restarted.state.enabled is False
    assert restarted.state.active_course_id is None


def test_18b_stale_reference_degrades_into_normal_picker(env) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("物理光学")
    controller.exit_mode()
    stale_id = settings.learning_last_course_id
    store.delete_course(stale_id)
    settings.learning_last_course_id = stale_id

    restarted = _restart(store, settings)
    reply = restarted.enter_mode()
    # Normal entry prompt (no 继续 offer), no crash.
    assert "上次我们学到" not in reply
    assert "先建一个学习项目" in reply
    assert restarted.state.active_course_id is None
    # A stale reference is also cleared by the explicit continue attempt.
    assert restarted.continue_last_course() is None
    assert settings.learning_last_course_id == ""


def _chip_labels(card) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [button.text() for button in card.findChildren(QPushButton)]


def test_18d_picker_offers_only_the_remembered_project_when_alone(qapp, env) -> None:
    """The 继续 chip is offered; 「换一个项目」 chips appear only when other
    projects exist (no dead controls)."""
    from ui.v2.course_picker import CoursePickerCard

    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()
    restarted = _restart(store, settings)
    remembered = restarted.last_course_tuple()

    only = CoursePickerCard(restarted.list_course_tuples(),
                            continue_course=remembered)
    labels = _chip_labels(only)
    assert "继续 自动控制原理" in labels
    assert "＋ 新建学习项目" in labels
    # The remembered project is not duplicated as a plain chip.
    assert "自动控制原理" not in labels

    store.create_course("物理光学")
    several = CoursePickerCard(restarted.list_course_tuples(),
                               continue_course=remembered)
    labels = _chip_labels(several)
    assert "继续 自动控制原理" in labels
    assert "物理光学" in labels          # 换一个项目 chip


def test_18c_last_course_survives_exit_without_being_activated(env) -> None:
    """[9] Do not over-correct: last_course_id must NOT be wiped on exit."""
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("物理光学")
    course_id = controller.state.active_course_id
    controller.exit_mode()

    restarted = _restart(store, settings)
    restarted.restore_runtime()
    assert restarted.state.enabled is False           # free chat
    assert restarted.state.active_course_id is None   # not activated
    # ...but Firefly still knows which project was last studied.
    assert settings.learning_last_course_id == course_id
    assert restarted.last_course().name == "物理光学"


# ---------------------------------------------------------------------------
# [19]-[20] isolation guarantees
# ---------------------------------------------------------------------------


def test_19_ordinary_chat_unaffected_by_cold_start(env, qapp, tmp_path) -> None:
    store, settings, controller, _ = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.exit_mode()

    restarted = _restart(store, settings)
    console, runner = _make_console(qapp, restarted, tmp_path)
    console._restore_learning_runtime()  # startup path
    console.input.setText("什么是传递函数？")
    console._send()
    assert runner.asked == ["什么是传递函数？"]
    console.close()


def test_20_assessment_loop_unchanged_after_continue(env) -> None:
    store, settings, controller, service = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    concept = store.add_concept(course_id, "传递函数", created_at=T0)
    controller.exit_mode()

    # Restart -> free chat -> explicit continue -> full quiz loop.
    restarted = _restart(store, settings, service)
    restarted.restore_runtime()
    assert restarted.state.enabled is False
    restarted.handle_text("继续学习")
    assert restarted.state.active_course_id == course_id

    question = restarted.handle_text("考考我")
    assert question == "传递函数的定义是什么？"
    assert store.list_assessments() == []            # question only
    assert store.get_concept(concept.id).mastery_level == 0

    reply = restarted.handle_text("零极点比值")
    assert reply is not None
    assert service.state == AssessmentState.RECORDED
    records = store.list_assessments(concept.id)
    assert len(records) == 1 and records[0].source == "quiz"
    assert store.get_concept(concept.id).mastery_level == 1   # via Rule Engine
    assert len(store.list_mastery_audit(concept.id)) == 1
    assert len(store.list_review_items(concept.id)) == 1


def test_20b_assessment_after_restart_uses_the_new_session(env) -> None:
    store, settings, controller, service = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    store.add_concept(course_id, "传递函数", created_at=T0)
    old_session = controller.state.active_session_id
    controller.exit_mode()

    restarted = _restart(store, settings, service)
    restarted.handle_text("继续学习")
    new_session = restarted.state.active_session_id
    assert new_session is not None and new_session != old_session

    restarted.handle_text("考考我")
    restarted.handle_text("零极点比值")
    record = store.list_assessments()[0]
    # Evidence is attributed to this run's session, never yesterday's.
    assert record.session_id == new_session


def test_intent_continue_markers() -> None:
    for text in ("继续学习", "继续上次", "继续上次的课", "继续我的学习", "接着学"):
        assert parse_learning_intent(text).intent is LearningIntent.CONTINUE_LEARNING
    # A bare "继续" is NOT a continue-learning command (it is a course switch).
    assert parse_learning_intent("继续").intent is None
