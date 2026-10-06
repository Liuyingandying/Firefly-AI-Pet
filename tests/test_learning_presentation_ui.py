"""Phase 2C: asynchronous real TutorTurn through Firefly's ChatView."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QTextBrowser

from core.settings_manager import SettingsManager
from learning.bridge_session import BridgeLearningSession
from learning.bridge_dialog import LearningBridgeDialog
from learning.identity import ensure_learner_id
from learning.resource_manager import ResourceManager
from learning.teach_mcp_client import _tool_payload
from learning.teach_mcp_stdio import StdioMcpTransport
from ui.v2.console import CompanionConsole


TEACH_ROOT = Path(os.environ.get("FIREFLY_TEACH_MCP_ROOT", ""))
TEACH_PYTHON = Path(os.environ.get("FIREFLY_TEACH_MCP_PYTHON", sys.executable))


@pytest.fixture()
def gui_course(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    learner_id = ensure_learner_id(settings)
    manager = ResourceManager(tmp_path / "courses")
    pdf = tmp_path / "course.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    course = manager.import_pdf(pdf, learner_id, title="RC 电路充放电")

    class Runner:
        agent_event = None
        session_video = None
        video_study = None
        _screen_vision_settings = None
        learning_controller = None

        def ask(self, text, learning_result=None):
            raise AssertionError("learning answer reached the ordinary chat runner")

    console = CompanionConsole(Runner())
    yield app, settings, manager, course, console
    console.close()
    app.processEvents()


def _messages(console):
    return [widget.toPlainText() for widget in console.chat.findChildren(QTextBrowser)]


def _wait_until(app, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "timed out waiting for a learning UI event"


def test_transport_config_uses_installed_zcode_mcp_layout(tmp_path, monkeypatch):
    config = tmp_path / ".zcode" / "cli" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcp": {"servers": {"teach-mcp": {
        "command": "python", "args": ["server.py"], "timeoutMs": 30000,
    }}}}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    transport = BridgeLearningSession._configured_teach_transport()
    assert transport._argv == ["python", "server.py"]
    assert transport._timeout_s == 30


def test_real_mcp_question_append_ack_then_answer(gui_course, tmp_path, caplog):
    if (os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1"
            or not os.environ.get("FIREFLY_TEACH_MCP_ROOT")):
        pytest.skip("explicit external teach-mcp integration opt-in required")
    if not TEACH_PYTHON.is_file() or not (TEACH_ROOT / "server.py").is_file():
        pytest.skip("local teach-mcp runtime is unavailable")
    app, settings, manager, course, console = gui_course
    fixture_root = tmp_path / "isolated_teach_mcp"
    package = fixture_root / "knowledge" / "circuit" / "rc_circuit"
    package.parent.mkdir(parents=True)
    shutil.copytree(TEACH_ROOT / "knowledge" / "circuit" / "rc_circuit", package)
    db = fixture_root / "state" / "session.db"
    runner = tmp_path / "run_isolated_teach_mcp.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import server, learning_tools\n"
        "server.DB_PATH = Path(sys.argv[2])\n"
        "learning_tools.DB_PATH = server.DB_PATH\n"
        "server._db().close()\n"
        "server.mcp.run()\n",
        encoding="utf-8",
    )
    calls = []
    append_done = threading.Event()
    ui_thread_id = threading.get_ident()

    def transport():
        session = StdioMcpTransport(
            TEACH_PYTHON, ["-u", runner, TEACH_ROOT, db],
            env={"TEACH_MCP_TEST_MODE": str(fixture_root), "PYTHONIOENCODING": "utf-8"},
            cwd=TEACH_ROOT, timeout_s=30,
        )
        original_call = session.call_tool
        def checked_call(name, arguments):
            calls.append((name, threading.get_ident()))
            if name == "tutor_mark_presented":
                assert append_done.is_set(), "mark preceded ChatView append"
            return original_call(name, arguments)
        session.call_tool = checked_call
        return session

    with transport() as seed:
        started = _tool_payload(seed.call_tool("start_learning", {
            "learner_id": settings.learning_learner_id, "topic": "RC 电路充放电",
        }))
        session_id = started["session_id"]
        opened = _tool_payload(seed.call_tool("tutor_open_turn", {
            "session_id": session_id, "concept_id": "tau", "course_id": course.course_id,
        }))
        assert opened["status"] == "success"
    calls.clear()

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=transport,
    )
    console.set_learning_bridge_session(bridge)
    delivered = []
    bridge.question_ready.connect(lambda question_id, text: delivered.append((question_id, text)))
    assert bridge.start_presentation(course.course_id)
    _wait_until(app, lambda: len(delivered) == 1)
    assert len(delivered) == 1
    question_id, display = delivered[0]
    assert not any(display in message for message in _messages(console))
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT question_presented FROM tutor_turns WHERE session_id=?", (session_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tutor_answers WHERE session_id=?", (session_id,)).fetchone()[0] == 0

    # The only ACK is the real ChatView append path, after append succeeds.
    append = console.chat.append_assistant
    def checked_append(text):
        append(text)
        append_done.set()
    console.chat.append_assistant = checked_append
    console.show_learning_question(question_id, display)
    def presented():
        with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT question_presented FROM tutor_turns WHERE session_id=?", (session_id,)).fetchone()
            return row is not None and row[0] == 1
    _wait_until(app, presented)
    assert any(display in message for message in _messages(console))
    assert any(name == "tutor_mark_presented" for name, _tid in calls)

    feedback = []
    bridge.feedback_ready.connect(feedback.append)
    bridge.feedback_ready.connect(console.show_learning_result)
    console.input.setText("A")
    console._send()
    _wait_until(app, lambda: bool(feedback))
    messages = _messages(console)
    assert any("A" == message.strip() for message in messages)
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
        answer = conn.execute(
            "SELECT question_id FROM tutor_answers WHERE session_id=?", (session_id,),
        ).fetchall()
    assert answer == [(question_id,)]
    assert any(name == "tutor_submit_answer" for name, _tid in calls)
    assert all(tid != ui_thread_id for _name, tid in calls)
    assert any(feedback[0] in message for message in messages)
    assert console.learning.state.enabled is False
    assert not list(fixture_root.rglob("result.json"))
    events = ["question_received", "question_rendered", "presentation_ack", "answer_submitted", "feedback_received"]
    positions = [caplog.text.index(f"[LEARNING_UI] event={event}") for event in events]
    assert positions == sorted(positions)
    bridge.clear()


def test_chat_append_failure_cannot_ack(gui_course):
    _app, _settings, _manager, _course, console = gui_course
    acked = []
    console.set_learning_bridge_session(type("Bridge", (), {
        "presentation_ack": lambda _self, *args: acked.append(args),
    })())
    def failed_append(_text):
        raise RuntimeError("append failed")
    console.chat.append_assistant = failed_append
    with pytest.raises(RuntimeError, match="append failed"):
        console.show_learning_question("Q1", "题干\nA. 甲\nB. 乙")
    assert acked == []


def test_resume_button_requests_firefly_presentation(gui_course, monkeypatch):
    _app, settings, manager, course, _console = gui_course
    monkeypatch.setattr("learning.bridge_dialog.action_for_state", lambda _state: "resume")
    monkeypatch.setattr("learning.bridge_dialog.launch_learning_mode",
                        lambda *_a, **_kw: pytest.fail("Z Code launch on GUI resume"))
    dialog = LearningBridgeDialog(
        settings=settings, courses_root=manager.courses_root,
        presentation_enabled=True,
    )
    dialog._course_list.setCurrentRow(0)
    requested = []
    dialog.course_presentation_requested.connect(requested.append)
    dialog._on_launch()
    assert requested == [course.course_id]


@pytest.mark.parametrize("bad_question", [
    {"stem": '<function=Read>tool JSON</function>'},
    {"stem": None},
    {"options": None},
])
def test_invalid_question_never_reaches_chat_view(gui_course, bad_question):
    app, settings, manager, course, console = gui_course
    question = {
        "status": "success", "question_id": "Q1", "mode": "choice",
        "stem": "有效题干", "options": {"A": "甲", "B": "乙"},
    } | bad_question
    calls = []

    class FakeTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, arguments):
            calls.append(name)
            if name == "resume_learning":
                return {"status": "success", "session_id": "S1"}
            if name == "tutor_resume_turn":
                return {
                    "status": "success", "learner_id": settings.learning_learner_id,
                    "course_id": course.course_id, "session_id": "S1",
                    "stage": "CHECK", "pending_question": question,
                    "question_presented": False,
                }
            raise AssertionError(name)

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=FakeTransport,
    )
    console.set_learning_bridge_session(bridge)
    bridge.question_ready.connect(console.show_learning_question)
    errors = []
    bridge.presentation_error.connect(errors.append)
    before = _messages(console)
    assert bridge.start_presentation(course.course_id) is True
    _wait_until(app, lambda: bool(errors))
    assert _messages(console) == before
    assert "tutor_mark_presented" not in calls
    assert "tutor_submit_answer" not in calls
    bridge.clear()


def test_slow_mcp_resume_keeps_gui_event_loop_responsive(gui_course):
    app, settings, manager, course, _console = gui_course
    release = threading.Event()
    started = threading.Event()

    class SlowTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, _arguments):
            assert name == "resume_learning"
            started.set()
            assert release.wait(5)
            return {"status": "error"}

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=SlowTransport,
    )
    ticked = []
    QTimer.singleShot(0, lambda: ticked.append(True))
    began = time.monotonic()
    try:
        assert bridge.start_presentation(course.course_id)
        assert time.monotonic() - began < 0.2
        _wait_until(app, lambda: started.is_set() and bool(ticked), timeout=2)
    finally:
        release.set()
        bridge.clear()


def test_failed_chat_append_never_marks_presented(gui_course):
    app, settings, manager, course, console = gui_course
    calls = []
    question = {
        "status": "success", "question_id": "Q1", "mode": "choice",
        "stem": "有效题干", "options": {"A": "甲", "B": "乙"},
    }

    class FakeTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, _arguments):
            calls.append(name)
            if name == "resume_learning":
                return {"status": "success", "session_id": "S1"}
            if name == "tutor_resume_turn":
                return {
                    "status": "success", "learner_id": settings.learning_learner_id,
                    "course_id": course.course_id, "session_id": "S1",
                    "stage": "CHECK", "pending_question": question,
                    "question_presented": False,
                }
            raise AssertionError(name)

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=FakeTransport,
    )
    console.set_learning_bridge_session(bridge)
    delivered = []
    bridge.question_ready.connect(lambda qid, display: delivered.append((qid, display)))
    assert bridge.start_presentation(course.course_id)
    _wait_until(app, lambda: bool(delivered))
    def failed_append(_text):
        raise RuntimeError("append failed")
    console.chat.append_assistant = failed_append
    with pytest.raises(RuntimeError, match="append failed"):
        console.show_learning_question(*delivered[0])
    app.processEvents()
    assert "tutor_mark_presented" not in calls
    bridge.clear()


def test_advance_mcp_result_reaches_question_and_answer_in_chat(gui_course):
    """A real MCP envelope at ADVANCE resumes through the official next concept."""
    app, settings, manager, course, console = gui_course
    question = {
        "status": "success", "question_id": "Q-next", "mode": "choice",
        "stem": "下一知识点的已审核题目？", "options": {"A": "甲", "B": "乙"},
        "session_id": "S-advance",
    }
    state = {"stage": "ADVANCE", "pending": None, "presented": False}
    calls = []

    def snapshot():
        return {
            "status": "success", "learner_id": settings.learning_learner_id,
            "course_id": course.course_id, "session_id": "S-advance",
            "stage": state["stage"], "pending_question": state["pending"],
            "question_presented": state["presented"],
        }

    class FakeTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, arguments):
            calls.append(name)
            if name == "resume_learning":
                payload = {"status": "success", "session_id": "S-advance"}
            elif name == "tutor_resume_turn":
                payload = snapshot()
            elif name == "tutor_advance":
                assert state["stage"] == "ADVANCE"
                state["stage"] = "TEACH"
                payload = snapshot()
            elif name == "tutor_next_question":
                assert state["stage"] == "TEACH"
                state["stage"], state["pending"] = "CHECK", question
                payload = snapshot()
            elif name == "tutor_mark_presented":
                assert "下一知识点的已审核题目？" in arguments["rendered_text"]
                assert "A. 甲" in arguments["rendered_text"]
                state["presented"] = True
                payload = snapshot()
            elif name == "tutor_submit_answer":
                assert state["presented"] is True
                assert arguments["question_id"] == "Q-next"
                assert arguments["student_answer"] == "A"
                payload = {"status": "success", "correct": True, "feedback": "回答已记录。"}
            else:
                raise AssertionError(name)
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=FakeTransport,
    )
    console.set_learning_bridge_session(bridge)
    bridge.question_ready.connect(console.show_learning_question)
    bridge.feedback_ready.connect(console.show_learning_result)
    try:
        assert bridge.start_presentation(course.course_id)
        _wait_until(app, lambda: any("下一知识点的已审核题目？" in m for m in _messages(console)))
        _wait_until(app, lambda: "tutor_mark_presented" in calls)
        console.input.setText("A")
        console._send()
        _wait_until(app, lambda: any("回答已记录。" in m for m in _messages(console)))
        assert calls.count("tutor_advance") == 1
        assert calls.index("tutor_advance") < calls.index("tutor_next_question")
        assert calls.index("tutor_mark_presented") < calls.index("tutor_submit_answer")
    finally:
        bridge.clear()


@pytest.mark.parametrize("turn_topic_matches", [True, False])
def test_legacy_empty_course_reference_requires_topic_match(gui_course, turn_topic_matches):
    app, settings, manager, course, console = gui_course
    question = {
        "status": "success", "question_id": "Q-legacy", "mode": "choice",
        "stem": "旧会话的已审核题目？", "options": {"A": "甲", "B": "乙"},
        "session_id": "S-legacy",
    }

    class FakeTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, _arguments):
            if name == "resume_learning":
                payload = {
                    "status": "success", "session_id": "S-legacy", "topic": "RC 电路充放电",
                }
            elif name == "tutor_resume_turn":
                payload = {
                    "status": "success", "learner_id": settings.learning_learner_id,
                    "course_id": "", "session_id": "S-legacy",
                    "topic": "RC 电路充放电" if turn_topic_matches else "另一门课程",
                    "stage": "CHECK", "pending_question": question,
                    "question_presented": True,
                }
            else:
                raise AssertionError(name)
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=FakeTransport,
    )
    console.set_learning_bridge_session(bridge)
    received = []
    errors = []
    bridge.question_ready.connect(lambda _qid, text: received.append(text))
    bridge.presentation_error.connect(errors.append)
    try:
        assert bridge.start_presentation(course.course_id)
        _wait_until(app, lambda: bool(received or errors))
        if turn_topic_matches:
            assert "旧会话的已审核题目？" in received[0]
            assert errors == []
        else:
            assert received == []
            assert errors == ["学习服务暂时无法完成操作，请稍后重试。"]
    finally:
        bridge.clear()


def test_advance_without_reviewed_questions_shows_safe_fallback(gui_course):
    app, settings, manager, course, _console = gui_course
    calls = []

    class FakeTransport:
        def start(self):
            pass

        def close(self):
            pass

        def call_tool(self, name, _arguments):
            calls.append(name)
            if name == "resume_learning":
                return {"status": "success", "session_id": "S-advance"}
            if name == "tutor_resume_turn":
                return {
                    "status": "success", "learner_id": settings.learning_learner_id,
                    "course_id": course.course_id, "session_id": "S-advance",
                    "stage": "ADVANCE", "pending_question": None,
                }
            if name == "tutor_advance":
                return {"status": "error", "error_code": "QUESTION_BANK_INSUFFICIENT"}
            raise AssertionError(name)

    bridge = BridgeLearningSession(
        settings, courses_root=manager.courses_root,
        presentation_transport_factory=FakeTransport,
    )
    questions = []
    errors = []
    bridge.question_ready.connect(lambda *_args: questions.append(True))
    bridge.presentation_error.connect(errors.append)
    try:
        assert bridge.start_presentation(course.course_id)
        _wait_until(app, lambda: bool(errors))
        assert errors == ["下一知识点尚缺足够的已审核题目，暂时无法继续出题。"]
        assert questions == []
        assert calls == ["resume_learning", "tutor_resume_turn", "tutor_advance"]
    finally:
        bridge.clear()
