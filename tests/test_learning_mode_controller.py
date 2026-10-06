"""LearningModeController tests (Phase 1B).

Covers the frozen shell contract:
- learning mode entry does NOT require a Bilibili link / any material
- course ("学习项目") lifecycle: create / select / switch / list / delete
- StudySession starts only after a course is selected; one active session
- last-course persistence via settings and safe restore
- no duplicate courses, no ambient writes, mastery untouched
- ordinary chat keeps flowing to the normal runner/provider path
"""

from __future__ import annotations

import pytest

from core.learning.controller import LearningModeController
from core.learning.intents import parse_learning_intent
from core.learning.store import LearningStore


class _FakeSettings:
    """In-memory stand-in for SettingsManager's learning keys."""

    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    settings = _FakeSettings()
    controller = LearningModeController(store=store, settings=settings)
    return store, settings, controller


# 1. enter learning mode -----------------------------------------------------


def test_enter_mode_enables(env) -> None:
    _, _, controller = env
    reply = controller.enter_mode()
    assert controller.state.enabled is True
    assert "学习模式" in reply


# 2. no Bilibili requirement -------------------------------------------------


def test_enter_mode_does_not_require_bilibili(env) -> None:
    _, _, controller = env
    reply = controller.enter_mode()
    assert "B站" not in reply
    assert "视频链接" not in reply


# 3. no courses -> prompt to create ------------------------------------------


def test_enter_mode_without_courses_prompts_create(env) -> None:
    _, _, controller = env
    reply = controller.enter_mode()
    assert "先建一个学习项目" in reply or "建一个学习项目" in reply


# 4-6. create course ---------------------------------------------------------


def test_create_course_writes_store_and_sets_active(env) -> None:
    store, settings, controller = env
    controller.enter_mode()
    reply = controller.handle_text("自动控制原理")
    assert "自动控制原理" in reply
    courses = store.list_courses()
    assert len(courses) == 1
    assert courses[0].name == "自动控制原理"
    assert controller.state.active_course_id == courses[0].id
    assert controller.state.active_course_name == "自动控制原理"
    assert settings.learning_last_course_id == courses[0].id


# 7-8. StudySession lifecycle -------------------------------------------------


def test_session_starts_only_after_course_selected(env) -> None:
    store, _, controller = env
    controller.enter_mode()
    # Entering the mode alone never creates a session.
    assert store.get_active_session("any") is None
    assert controller.state.active_session_id is None
    controller.create_course("自动控制原理")
    assert controller.state.active_session_id is not None
    active = store.get_active_session(controller.state.active_course_id)
    assert active is not None and active.status == "active"


# 9-10. two courses coexist ---------------------------------------------------


def test_two_courses_coexist(env) -> None:
    store, _, controller = env
    controller.create_course("自动控制原理")
    controller.exit_mode()
    controller.enter_mode()
    controller.create_course("物理光学")
    assert len(store.list_courses()) == 2
    names = {course.name for course in store.list_courses()}
    assert names == {"自动控制原理", "物理光学"}


# 11-12. course switch ends old session, starts new ---------------------------


def test_switch_course_ends_old_session_and_starts_new(env) -> None:
    store, _, controller = env
    controller.create_course("自动控制原理")
    first_session_id = controller.state.active_session_id
    first_course_id = controller.state.active_course_id
    controller.create_course("物理光学")
    assert controller.state.active_session_id != first_session_id
    assert controller.state.active_course_id != first_course_id
    # The old session is closed; exactly one active session exists.
    old = store.get_session(first_session_id)
    assert old is not None and old.status == "ended"
    active_sessions = [
        s for s in store.list_sessions(first_course_id) if s.status == "active"
    ]
    assert active_sessions == []


# 13-15. last-course persistence / restore / stale cleanup --------------------


def test_last_course_persists_and_restores(env, tmp_path) -> None:
    store, settings, controller = env
    controller.create_course("自动控制原理")
    assert settings.learning_last_course_id == controller.state.active_course_id

    # "Restart": a fresh controller over the same store + settings.
    restarted = LearningModeController(store=store, settings=settings)
    restarted.enter_mode()
    reply = restarted.continue_last_course()
    assert reply is not None
    assert restarted.state.active_course_name == "自动控制原理"
    assert "自动控制原理" in reply


def test_stale_last_course_degrades_safely(env) -> None:
    store, settings, controller = env
    controller.create_course("自动控制原理")
    stale_id = settings.learning_last_course_id
    store.delete_course(stale_id)
    settings.learning_last_course_id = stale_id  # keep stale reference

    restarted = LearningModeController(store=store, settings=settings)
    reply = restarted.continue_last_course()
    assert reply is None
    assert settings.learning_last_course_id == ""  # stale ref cleared


# 16. duplicate course name ---------------------------------------------------


def test_duplicate_course_not_created(env) -> None:
    store, _, controller = env
    controller.create_course("自动控制原理")
    reply = controller.create_course("自动控制原理")
    assert "已经有" in reply
    assert len(store.list_courses()) == 1


# 17. list courses ------------------------------------------------------------


def test_list_courses(env) -> None:
    _, _, controller = env
    controller.create_course("自动控制原理")
    controller.create_course("物理光学")
    reply = controller.list_courses()
    assert "自动控制原理" in reply
    assert "物理光学" in reply
    assert "1." in reply


# 18. delete requires confirmation ---------------------------------------------


def test_delete_course_requires_confirmation(env) -> None:
    store, _, controller = env
    controller.enter_mode()
    controller.create_course("物理光学")
    course_id = controller.state.active_course_id
    reply = controller.request_delete_course("物理光学")
    assert "确定" in reply and "删除" in reply
    # Not deleted yet.
    assert store.get_course(course_id) is not None
    # Confirmation deletes.
    confirmed = controller.handle_text("确定")
    assert "已删除" in confirmed
    assert store.get_course(course_id) is None
    assert controller.state.active_course_id is None


def test_delete_course_cancellation(env) -> None:
    store, _, controller = env
    controller.create_course("物理光学")
    course_id = controller.state.active_course_id
    controller.request_delete_course("物理光学")
    controller.handle_text("算了不删")
    assert store.get_course(course_id) is not None


# 19-20. exit learning mode ----------------------------------------------------


def test_exit_mode_ends_session_and_disables(env) -> None:
    store, _, controller = env
    controller.create_course("自动控制原理")
    session_id = controller.state.active_session_id
    reply = controller.exit_mode()
    assert controller.state.enabled is False
    assert controller.state.active_course_id is None
    assert controller.state.active_session_id is None
    ended = store.get_session(session_id)
    assert ended is not None and ended.status == "ended"
    assert "休息" in reply


def test_exit_mode_returns_to_free_chat(env) -> None:
    _, _, controller = env
    controller.create_course("自动控制原理")
    controller.exit_mode()
    # Learning commands are inert; ordinary text passes through.
    assert controller.handle_text("退出学习模式") is None
    assert controller.handle_text("什么是传递函数") is None


# 21. ordinary chat unaffected ------------------------------------------------


def test_ordinary_chat_not_intercepted(env) -> None:
    _, _, controller = env
    controller.enter_mode()
    assert controller.handle_text("什么是传递函数") is None
    assert controller.handle_text("今天天气不错") is None


# 24-25. mastery zero-write ----------------------------------------------------


def test_learning_mode_never_writes_mastery(env) -> None:
    store, _, controller = env
    controller.enter_mode()
    controller.create_course("自动控制原理")
    controller.handle_text("什么是闭环传递函数")
    controller.exit_mode()
    # No concepts were ever created; no mastery audit exists.
    course_id = store.list_courses()[0].id
    assert store.list_concepts(course_id) == []
    assert store.list_mastery_audit(course_id) == []


# 26. ambient never writes the store -------------------------------------------


def test_ambient_cannot_write_store(env) -> None:
    store, _, controller = env
    controller.enter_mode()
    controller.handle_text("我看到一个概念叫根轨迹")  # ordinary chat
    assert store.list_courses() == []
    assert not hasattr(store, "add_candidate")


# 27. video study stays available ----------------------------------------------


def test_video_study_still_available(env) -> None:
    from core.video_study import is_study_entry

    controller = env[2]
    controller.enter_mode()
    # The video-study phrase is still recognized independently of mode.
    assert is_study_entry("陪我学习这个视频") is True


# -- intent layer --------------------------------------------------------------


def test_intent_parsing() -> None:
    assert parse_learning_intent("退出学习模式").intent.value == "exit_learning"
    assert parse_learning_intent("我有哪些学习项目").intent.value == "list_courses"
    assert parse_learning_intent("新建一个自动控制原理学习项目").course_name == "自动控制原理"
    assert parse_learning_intent("我要学物理光学").course_name == "物理光学"
    assert parse_learning_intent("切换到物理光学").course_name == "物理光学"
    assert parse_learning_intent("继续自动控制原理").course_name == "自动控制原理"
    assert parse_learning_intent("继续学习").intent.value == "continue_learning"
    assert parse_learning_intent("删除物理光学这个学习项目").course_name == "物理光学"
    assert parse_learning_intent("什么是传递函数").intent is None
