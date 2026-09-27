"""Learning Return Channel v0.1 tests (Phase 14).

Covers: result schema, atomic write, task_id matching, stale rejection,
question display, answer routing, ordinary-chat non-interference, pending
isolation (course/learner), Firefly-never-judges, no-mastery writes,
restart/resume rebuild, agent timeout hint, malformed result, agent failure.

Success is NOT "process started": the chain under test is
task_injected -> result_received -> message_visible.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from learning._storage import atomic_write_json  # noqa: E402
from learning.bridge_session import BridgeLearningSession  # noqa: E402
from learning.course_binding import save_binding  # noqa: E402
from learning.identity import ensure_learner_id  # noqa: E402
from learning.learning_result import (  # noqa: E402
    ResultError,
    build_result,
    display_lines,
    read_result,
    result_path,
    write_result,
)
from learning.resource_manager import ResourceManager  # noqa: E402
from learning.result_watcher import LearningResultWatcher  # noqa: E402


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def course(tmp_path):
    """One managed course with a curriculum binding (resume-ready)."""
    from core.settings_manager import SettingsManager

    manager = ResourceManager(tmp_path / "courses")
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    learner = ensure_learner_id(settings)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\nreturn-channel\n")
    result = manager.import_pdf(pdf, learner, title="回路课程")
    save_binding(result.course_dir, result.course_id, curriculum_id="curr-rc")
    return {
        "manager": manager,
        "settings": settings,
        "learner": learner,
        "course_id": result.course_id,
        "course_dir": result.course_dir,
    }


def _valid_result(task_id: str, course_id: str, **overrides) -> dict:
    payload = build_result(
        task_id=task_id,
        course_id=course_id,
        action="resume",
        status="ok",
        message_type="lesson",
        display_text="继续上次的进度。",
        created_at="2026-09-27T03:00:00Z",
    )
    payload.update(overrides)
    return payload


# 1. result schema ------------------------------------------------------------


def test_1_result_schema_validation():
    ok = build_result(
        task_id="task-a",
        course_id="crs-000000000000",
        action="resume",
        status="ok",
        message_type="question",
        display_text="讲解",
        question={"question_id": "RC-E01", "text": "τ 时电压？", "options": ["3.16V", "6.32V"]},
        session_id="ff-x-20260927-000000-abcd",
    )
    assert read_result if False else ok["question"]["question_id"] == "RC-E01"

    for bad_override, code in (
        ({"status": "finished"}, "RESULT_INVALID"),
        ({"message_type": "poem"}, "RESULT_INVALID"),
        ({"schema_version": 2}, "RESULT_VERSION_UNSUPPORTED"),
        ({"unexpected": 1}, "RESULT_INVALID"),
    ):
        from learning.learning_result import validate_result

        data = _valid_result("task-a", "crs-000000000000", **bad_override)
        with pytest.raises(ResultError) as excinfo:
            validate_result(data)
        assert excinfo.value.code == code

    # question message without question payload is invalid
    from learning.learning_result import validate_result

    data = _valid_result("task-a", "crs-000000000000")
    data["message_type"] = "question"
    with pytest.raises(ResultError):
        validate_result(data)


# 2. atomic result write -------------------------------------------------------


def test_2_atomic_result_write(tmp_path):
    result = _valid_result("task-w", "crs-000000000000")
    written = write_result(tmp_path, result)
    assert written.is_file()
    assert not (tmp_path / "bridge" / "result.json.tmp").exists()  # tmp consumed
    assert read_result(tmp_path)["task_id"] == "task-w"


# 3-4. watcher: task_id matching + stale rejection ------------------------------


def test_3_watcher_matches_task_id(qapp, tmp_path):
    seen = []
    watcher = LearningResultWatcher(tmp_path, "task-ok", timeout_s=0.05)
    watcher.result_received.connect(lambda r: seen.append(r))
    watcher.start()
    write_result(tmp_path, _valid_result("task-ok", "crs-000000000000"))
    watcher._tick()
    assert len(seen) == 1 and seen[0]["task_id"] == "task-ok"
    watcher.stop()


def test_4_watcher_rejects_stale_result(qapp, tmp_path):
    seen = []
    watcher = LearningResultWatcher(tmp_path, "task-new", timeout_s=600)
    watcher.result_received.connect(lambda r: seen.append(r))
    watcher.start()
    write_result(tmp_path, _valid_result("task-OLD", "crs-000000000000"))
    watcher._tick()  # stale: refused, watcher keeps waiting
    assert seen == []
    write_result(tmp_path, _valid_result("task-new", "crs-000000000000"))
    watcher._tick()
    assert len(seen) == 1 and seen[0]["task_id"] == "task-new"
    watcher.stop()


# 5. question display -----------------------------------------------------------


def test_5_question_display_lines():
    result = build_result(
        task_id="task-q",
        course_id="crs-000000000000",
        action="resume",
        status="ok",
        message_type="question",
        display_text="上次你学到了 RC 电路的 quiz 阶段，继续。",
        question={"question_id": "RC-E02", "text": "t=τ 时电压最接近？", "options": ["3.16V", "6.32V", "10V"]},
        session_id="s-1",
    )
    lines = display_lines(result)
    assert lines[0].startswith("上次你学到了")
    assert "t=τ 时电压最接近？" in lines[1]
    assert lines[2].splitlines() == ["A. 3.16V", "B. 6.32V", "C. 10V"]


# 6-9. answer routing + isolation -----------------------------------------------


def _make_session(qapp, tmp_path, monkeypatch, course, learner=None):
    submitted = []
    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: submitted.append(argv) or 777,
    )
    # the session shares the course fixture's settings so the answer launch
    # passes the learner check (same learner imported the course)
    session = BridgeLearningSession(
        course["settings"],
        courses_root=course["manager"].courses_root,
        mode="embedded",  # Return Channel is frozen as experimental
    )
    return session, submitted


def _feed_question_result(session, course, task_id="task-r1"):
    result = build_result(
        task_id=task_id,
        course_id=course["course_id"],
        action="resume",
        status="ok",
        message_type="question",
        display_text="继续教学。",
        question={"question_id": "RC-E02", "text": "题干", "options": ["A1", "B1"]},
        session_id="sess-teach-1",
    )
    session._on_result(result)
    return result


def test_6_and_8_answer_routing_enters_learning(qapp, tmp_path, monkeypatch, course):
    session, argv_captures = _make_session(qapp, tmp_path, monkeypatch, course)
    assert session.handle_incoming_text("B") is None  # no pending: normal chat
    _feed_question_result(session, course)
    assert session.has_pending_question is True

    reply = session.handle_incoming_text("我觉得是 B，因为时间常数充到 63.2%")
    assert reply is not None and "判题" in reply
    assert len(argv_captures) == 1
    argv = " ".join(argv_captures[0])
    assert "--prompt" in argv_captures[0]  # task injected, not GUI-only
    action_path = course["course_dir"] / "bridge" / "learning_action.json"
    payload = json.loads(action_path.read_text(encoding="utf-8"))
    assert payload["question_id"] == "RC-E02"  # verbatim teach-mcp id
    assert payload["student_answer"].startswith("我觉得是 B")  # raw user text
    assert payload["action"] == "answer"
    # pending consumed until the next question result arrives
    assert session.has_pending_question is False


def test_7_ordinary_chat_not_routed(qapp, tmp_path, monkeypatch, course):
    session, argv_captures = _make_session(qapp, tmp_path, monkeypatch, course)
    for text in ("今天天气怎么样", "讲个笑话", "退出学习"):
        assert session.handle_incoming_text(text) is None
    assert argv_captures == []


def test_9_course_isolation(qapp, tmp_path, monkeypatch, course):
    session, argv_captures = _make_session(qapp, tmp_path, monkeypatch, course)
    _feed_question_result(session, course)
    assert session.has_pending_question

    # another course launches: previous pending must be dropped
    other = course["manager"].import_pdf(
        _pdf(tmp_path / "b.pdf", b"%PDF-1.4 other"), course["learner"], title="B课"
    )
    from learning.launcher import LaunchPlan, LaunchResult

    plan = LaunchPlan(
        course_id=other.course_id,
        action="resume",
        learner_id=course["learner"],
        workspace=other.course_dir / "workspace",
        context_file=other.course_dir / "workspace/.firefly/learning_context.json",
        argv=["node", "zcode.cjs", "--prompt", "x", "--cwd", "ws"],
        cwd=other.course_dir / "workspace",
        task_id="task-other",
    )
    session.attach_launch(LaunchResult(plan=plan, spawned=False))
    assert session.has_pending_question is False
    # answering for course A now must NOT create an answer task for it
    reply = session.handle_incoming_text("B")
    assert reply is None
    assert argv_captures == []


def _pdf(path: Path, content: bytes = b"%PDF-1.4\n") -> Path:
    path.write_bytes(content)
    return path


# 10. learner isolation (answer launch re-checks manifest learner) --------------


def test_10_answer_launch_rechecks_learner(tmp_path, fake_env_unused=None):
    manager = ResourceManager(tmp_path / "courses")
    course = manager.import_pdf(_pdf(tmp_path / "a.pdf"), "ff-11111111", title="A")
    from learning.launcher import launch_learning_mode
    from core.settings_manager import SettingsManager

    settings = SettingsManager(preferences_file=tmp_path / "p.json")
    from learning.identity import ensure_learner_id

    ensure_learner_id(settings)  # a DIFFERENT learner than the importer
    with pytest.raises(Exception) as excinfo:
        launch_learning_mode(
            course.course_id,
            "answer",
            settings=settings,
            courses_root=manager.courses_root,
            answer={"question_id": "q1", "student_answer": "B"},
            dry_run=True,
        )
    assert getattr(excinfo.value, "code", "") == "LEARNER_MISMATCH"


# 11-12. Firefly never judges / never writes mastery ----------------------------


def test_11_firefly_records_answer_verbatim_only(tmp_path, monkeypatch, course, qapp):
    session, argv_captures = _make_session(qapp, tmp_path, monkeypatch, course)
    _feed_question_result(session, course)
    session.handle_incoming_text("B")
    payload = json.loads(
        (course["course_dir"] / "bridge" / "learning_action.json").read_text(
            encoding="utf-8"
        )
    )
    # Firefly stores only WHAT was answered — no verdict fields of any kind
    assert set(payload) == {
        "schema_version",
        "task_id",
        "course_id",
        "action",
        "question_id",
        "student_answer",
        "created_at",
    }
    assert "correct" not in payload and "mastery" not in payload


def test_12_result_cannot_carry_authoritative_mastery(tmp_path):
    data = _valid_result("task-m", "crs-000000000000")
    data["mastery"] = 0.9
    with pytest.raises(ResultError) as excinfo:
        write_result(tmp_path, data)
    assert excinfo.value.code == "RESULT_FORBIDDEN_FIELD"


# 13. restart/resume rebuilds pending from a fresh result ------------------------


def test_13_restart_rebuilds_pending_from_resume_result(
    qapp, tmp_path, monkeypatch, course
):
    session, _argv = _make_session(qapp, tmp_path, monkeypatch, course)
    session.clear()  # simulate Firefly restart: nothing remembered
    assert session.has_pending_question is False
    _feed_question_result(session, course, task_id="task-after-restart")
    assert session.has_pending_question is True
    assert session._pending.session_id == "sess-teach-1"


# 14. agent timeout --------------------------------------------------------------


def test_14_timeout_hints_once_without_failure(qapp, tmp_path):
    hints = []
    results = []
    watcher = LearningResultWatcher(
        tmp_path, "task-slow", timeout_s=0.0, poll_ms=10, slow_poll_ms=20
    )
    watcher.result_received.connect(lambda r: results.append(r))
    watcher.still_working.connect(lambda tid: hints.append(tid))
    watcher.start()
    watcher._tick()  # no result file -> timeout branch fires (timeout_s=0)
    assert hints == ["task-slow"]  # gentle hint, once
    watcher._tick()  # keeps watching (no result yet), no crash, no failure
    assert results == []
    write_result(tmp_path, _valid_result("task-slow", "crs-000000000000"))
    watcher._tick()  # a late result is STILL accepted after the hint
    assert len(results) == 1 and results[0]["task_id"] == "task-slow"
    watcher.stop()


# 15. malformed result ------------------------------------------------------------


def test_15_malformed_result_surfaces_error(qapp, tmp_path):
    invalids = []
    watcher = LearningResultWatcher(tmp_path, "task-bad", timeout_s=600)
    watcher.result_received.connect(lambda r: invalids.append(("ok", r)))
    watcher.result_invalid.connect(lambda msg: invalids.append(("bad", msg)))
    watcher.start()
    (tmp_path / "bridge").mkdir(parents=True, exist_ok=True)
    result_path(tmp_path).write_text("{half written", encoding="utf-8")
    watcher._tick()
    assert invalids and invalids[0][0] == "bad"
    watcher.stop()


# 16. agent failure status is DISPLAYED, not hidden -------------------------------


def test_16_agent_failure_status_reaches_display():
    result = build_result(
        task_id="task-f",
        course_id="crs-000000000000",
        action="resume",
        status="error",
        message_type="error",
        display_text="学习服务不可用，请稍后重试。",
    )
    lines = display_lines(result)
    assert lines == ["学习服务不可用，请稍后重试。"]
