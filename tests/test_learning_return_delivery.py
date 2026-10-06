"""Return Channel runtime delivery regression (Phase 9).

Locks the corrected delivery semantics:
- dialog emits course_launched BEFORE any modal UI; the normal path never
  opens a modal "success" box;
- the watcher attaches immediately on click;
- success is the CHAIN task_submitted -> result_received -> message_visible;
  "process started" alone is never treated as delivery.
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

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_skill_evidence(tmp_path, monkeypatch):
    """Real SQLite readiness evidence; never reads/writes the user's Z Code DB."""
    import sqlite3
    db = tmp_path / "zcode.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE session (id TEXT, directory TEXT, time_created INTEGER);
            CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
            CREATE TABLE part (message_id TEXT, data TEXT);
        """)
        for sid in ('sess-course-123', 'sess-A', 'sess-B', 'sess-FIRST', 'sess-SECOND', 'sess-rollover-new'):
            con.execute('INSERT INTO message VALUES (?, ?, 1, ?)',
                        (sid+'-m', sid, json.dumps({'role': 'assistant', 'finish': 'stop'})))
            con.execute('INSERT INTO part VALUES (?, ?)',
                        (sid+'-m', json.dumps({'type': 'tool', 'tool': 'Skill', 'state': {'status': 'completed'}})))
    monkeypatch.setattr('learning.launcher.default_session_db', lambda: db)
    return db


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _fake_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\n")
    return path


@pytest.fixture()
def course(tmp_path):
    from core.settings_manager import SettingsManager
    from learning.course_binding import save_binding
    from learning.identity import ensure_learner_id
    from learning.resource_manager import ResourceManager

    manager = ResourceManager(tmp_path / "courses")
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    learner = ensure_learner_id(settings)
    result = manager.import_pdf(_fake_pdf(tmp_path / "a.pdf"), learner, title="投递课程")
    save_binding(result.course_dir, result.course_id, curriculum_id="curr-d1")
    return {
        "manager": manager,
        "settings": settings,
        "learner": learner,
        "course_id": result.course_id,
        "course_dir": result.course_dir,
    }


# 1+2. emit precedes modal UI; normal path has no modal success box ----------


def test_1_and_2_dialog_success_path_has_no_modal_box():
    source = (PROJECT_DIR / "learning" / "bridge_dialog.py").read_text(encoding="utf-8")
    tree = __import__("ast").parse(source)
    launch_fn = next(
        n for n in __import__("ast").walk(tree)
        if isinstance(n, __import__("ast").FunctionDef) and n.name == "_on_launch"
    )
    body = __import__("ast").unparse(launch_fn)
    assert "QMessageBox.information" not in body, "正常流程禁止模态成功框"
    emit_index = body.find("course_launched.emit")
    assert emit_index != -1
    # nothing modal may appear before the emit (error warnings live only in
    # the failure branches and are allowed to sit textually earlier)
    before_emit = body[:emit_index]
    assert "QMessageBox.information" not in before_emit, "emit 之前出现模态调用"
    # emit happens before the dialog closes
    assert body.find("self.accept()") > emit_index


# 3. click -> immediate watcher attach ----------------------------------------


def test_3_click_attaches_watcher_immediately(qapp, tmp_path, monkeypatch, course):
    attached = []
    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 999,
    )
    from learning.bridge_dialog import LearningBridgeDialog
    from learning.bridge_session import BridgeLearningSession
    from learning.learning_result import write_result, build_result

    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)
    original_attach = session.attach_launch
    session.attach_launch = lambda result: (attached.append(result), original_attach(result))

    dialog = LearningBridgeDialog(settings=course["settings"], courses_root=course["manager"].courses_root)
    dialog.course_launched.connect(session.attach_launch)  # same wiring as app.py
    dialog.refresh()
    dialog._course_list.setCurrentRow(0)
    dialog._launch_button.click()
    qapp.processEvents()

    assert len(attached) == 1
    assert attached[0].plan.task_id.startswith("task-")
    assert session._timer is not None  # interactive init poller is alive
    session.clear()  # stop the poller (otherwise it outlives the test)
    dialog.close()


# 4. full chain: task_submitted -> result_received -> message_visible ---------


def test_4_chain_delivers_message_to_console(qapp, tmp_path, monkeypatch, course):
    """Interactive chain: task_submitted -> init session ready -> binding
    written -> TUI opened -> chat shows the non-modal notice."""
    from core.settings_manager import SettingsManager
    from learning.bridge_session import BridgeLearningSession
    from learning.launcher import launch_learning_mode
    from ui.v2.console import CompanionConsole

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 4241,
    )

    class _Runner:
        agent_event = None
        session_video = None
        video_study = None
        _screen_vision_settings = None
        learning_controller = None

        def ask(self, text, learning_result=None):
            return True

    console = CompanionConsole(_Runner())
    console.learning._store = __import__(
        "core.learning.store", fromlist=["LearningStore"]
    ).LearningStore(tmp_path / "ls.sqlite3")
    console.learning._store.initialize()

    settings = course["settings"]
    learner = course["learner"]
    manager = course["manager"]
    course_dir = course["course_dir"]

    displayed = []
    console.show_learning_result = lambda text: displayed.append(text)  # noqa: E731

    session = BridgeLearningSession(settings, courses_root=manager.courses_root)
    session.result_ready.connect(console.show_learning_result)
    session.agent_timeout.connect(
        lambda _tid: console.show_learning_result("学习代理仍在处理中，请稍候……")
    )

    from learning.launcher import launch_learning_mode

    launched = launch_learning_mode(
        course["course_id"], "resume", settings=settings, courses_root=manager.courses_root
    )
    session.attach_launch(launched)
    qapp.processEvents()

    # phase 1: submitted — phase hint shown, NO teaching content yet
    assert launched.spawned and launched.plan.task_id
    assert len(displayed) == 1
    assert "正在" in displayed[0]

    # phase 2: the init run writes the official --json document
    init_json = course_dir / "bridge" / f"init-{launched.plan.task_id}.json"
    init_json.write_text(
        json.dumps({"sessionId": "sess-course-123", "response": "OK"}),
        encoding="utf-8",
    )
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: opened.append((str(cdir), sid)) or 4242,
    )
    session._poll()
    qapp.processEvents()

    assert opened == [(str(course_dir), "sess-course-123")]
    # binding now carries the opaque Z Code session reference
    from learning.course_binding import load_binding

    binding = load_binding(course_dir)
    assert binding["zcode_session_id"] == "sess-course-123"
    # A terminal launcher PID is only a request; the user must verify the TUI.
    assert any("请确认终端中显示了当前课程" in t for t in displayed)
    session.clear()
    console.close()


# 5. process_started alone is NOT delivery -------------------------------------


def test_5_process_started_alone_is_not_delivery(qapp, tmp_path, monkeypatch, course):
    from learning.bridge_session import BridgeLearningSession

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 123,
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)
    texts = []
    session.result_ready.connect(texts.append)
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: opened.append((cdir, sid)),
    )

    from learning.launcher import launch_learning_mode

    launched = launch_learning_mode(
        course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root
    )
    session.attach_launch(launched)
    # spawn succeeded, but the init run has not produced init.json yet
    teaching_shown = [t for t in texts if "正在" not in t]
    assert teaching_shown == [], "结果未到达前不得展示教学内容/打开会话"
    assert opened == [], "init 未完成前不得打开 TUI"
    assert session.has_pending_question is False
    session.clear()


# ---------------------------------------------------------------------------
# Phase 12: interactive-mode additions


def test_default_mode_does_not_intercept_answers(qapp, course):
    """Interactive default: even WITH a pending-style state, ordinary chat
    text is never intercepted as a learning answer (Return Channel is
    embedded-only)."""
    from learning.bridge_session import BridgeLearningSession

    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)
    assert session.handle_incoming_text("B") is None
    assert session.handle_incoming_text("退出学习") is None
    assert session.handle_incoming_text("随便聊聊") is None


def test_course_a_b_bind_their_own_sessions(qapp, tmp_path, monkeypatch, course):
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding, save_binding
    from learning.launcher import launch_learning_mode

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 555,
    )
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: opened.append((cdir.name, sid)) or 1,
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)

    # course A init
    la = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    session.attach_launch(la)
    (course["course_dir"] / "bridge" / f"init-{la.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-A"}), encoding="utf-8"
    )
    session._poll()
    # course B init
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.4 B")
    rb = course["manager"].import_pdf(pdf, course["learner"], title="B课")
    save_binding(rb.course_dir, rb.course_id, curriculum_id="curr-B")
    lb = launch_learning_mode(rb.course_id, "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    session.attach_launch(lb)
    (rb.course_dir / "bridge" / f"init-{lb.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-B"}), encoding="utf-8"
    )
    session._poll()

    assert load_binding(course["course_dir"])["zcode_session_id"] == "sess-A"
    assert load_binding(rb.course_dir)["zcode_session_id"] == "sess-B"
    assert [(c, s) for c, s in opened] == [
        (course["course_dir"].name, "sess-A"),
        (rb.course_dir.name, "sess-B"),
    ]
    session.clear()


def test_deleted_zcode_session_is_rebuilt_fresh(qapp, tmp_path, monkeypatch, course):
    """Z Code session loss never blocks the course: every launch creates a
    NEW session (fresh task_id + fresh init.json), and the binding is
    updated to the new opaque reference."""
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding, save_binding
    from learning.launcher import launch_learning_mode

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 556,
    )
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: 1,
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)

    l1 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    session.attach_launch(l1)
    (course["course_dir"] / "bridge" / f"init-{l1.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-FIRST"}), encoding="utf-8"
    )
    session._poll()
    first = load_binding(course["course_dir"])["zcode_session_id"]

    # "delete" the Z Code session and run again: a fresh init produces a new id
    l2 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    assert l2.plan.task_id != l1.plan.task_id
    session.attach_launch(l2)
    (course["course_dir"] / "bridge" / f"init-{l2.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-SECOND"}), encoding="utf-8"
    )
    session._poll()
    second = load_binding(course["course_dir"])["zcode_session_id"]
    assert second == "sess-SECOND" and first == "sess-FIRST" and first != second
    session.clear()


# ---------------------------------------------------------------------------
# Session Reuse Gate (Phase 7): existing -> reuse, missing -> rebuild


def test_reuse_gate_first_launch_creates_session(qapp, tmp_path, monkeypatch, course):
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding
    from learning.launcher import launch_learning_mode

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 601,
    )
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: opened.append((cdir.name, sid)) or 1,
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)

    l1 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    assert l1.plan.mode == "task-injected"  # first launch must bootstrap
    assert "--prompt" in l1.plan.argv and "--json" in l1.plan.argv
    session.attach_launch(l1)
    (course["course_dir"] / "bridge" / f"init-{l1.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-A"}), encoding="utf-8"
    )
    session._poll()
    assert load_binding(course["course_dir"])["zcode_session_id"] == "sess-A"
    session.clear()


def test_reuse_gate_second_launch_reuses_same_session(qapp, tmp_path, monkeypatch, course):
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding, save_binding
    from learning.launcher import launch_learning_mode

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 602,
    )
    monkeypatch.setattr("learning.launcher._ensure_tui_runtime", lambda node, cli, env: None)
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda cdir, sid: opened.append((cdir.name, sid)) or 1,
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)

    # first: bootstrap init creates the session
    l1 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    session.attach_launch(l1)
    (course["course_dir"] / "bridge" / f"init-{l1.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-A"}), encoding="utf-8"
    )
    session._poll()

    # second: existing session -> --resume it, NEVER a new -p init run
    # (session A exists in Z Code's own db -> existence probe passes)
    monkeypatch.setattr(
        "learning.launcher.zcode_session_exists",
        lambda sid, course_workspace=None: sid == "sess-A",
    )
    l2 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    assert l2.plan.mode == "session-reuse"
    assert l2.plan.argv == []  # no --prompt/--json bootstrap injection
    assert "--prompt" not in l2.plan.argv
    assert opened == [(course["course_dir"].name, "sess-A")]
    assert load_binding(course["course_dir"])["zcode_session_id"] == "sess-A"
    # task.json still refers to the FIRST task (no new init task created)
    task = json.loads((course["course_dir"] / "bridge" / "task.json").read_text(encoding="utf-8"))
    assert task["task_id"] == l1.plan.task_id
    session.clear()


def test_reuse_gate_missing_session_triggers_rebuild(qapp, tmp_path, monkeypatch, course, caplog):
    from learning.diagnostics import LOGGER_NAME
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding, save_binding
    from learning.launcher import launch_learning_mode

    monkeypatch.setattr(
        "learning.launcher._spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: 603,
    )
    monkeypatch.setattr(
        "learning.launcher.zcode_session_exists",
        lambda sid, course_workspace=None: False,  # session A deleted/unrecoverable
    )
    session = BridgeLearningSession(course["settings"], courses_root=course["manager"].courses_root)
    save_binding(course["course_dir"], course["course_id"], zcode_session_id="sess-DELETED")

    l2 = launch_learning_mode(course["course_id"], "resume", settings=course["settings"], courses_root=course["manager"].courses_root)
    assert l2.plan.mode == "task-injected"  # rebuild via bootstrap init
    assert "--prompt" in l2.plan.argv
    prompt = l2.plan.argv[l2.plan.argv.index("--prompt") + 1]
    assert "action=resume" in prompt  # rebuild still resumes real progress
    assert "[ZCODE_SESSION_REBUILD]" in caplog.text
    assert "sess-DELETED" in caplog.text

    # init completes with a NEW session -> binding moves to it
    session.attach_launch(l2)
    (course["course_dir"] / "bridge" / f"init-{l2.plan.task_id}.json").write_text(
        json.dumps({"sessionId": "sess-B"}), encoding="utf-8"
    )
    session._poll()
    assert load_binding(course["course_dir"])["zcode_session_id"] == "sess-B"
    session.clear()


def test_rollover_attach_shows_gentle_copy(qapp, tmp_path, monkeypatch, course):
    """Phase 6: rollover attach must show the gentle copy — never
    "context exceeded"/"token limit"/"restart" wording."""
    from learning.bridge_session import BridgeLearningSession
    from learning.course_binding import load_binding
    from learning.launcher import LaunchPlan, LaunchResult

    session = BridgeLearningSession(
        course["settings"], courses_root=course["manager"].courses_root
    )
    texts = []
    session.result_ready.connect(texts.append)
    plan = LaunchPlan(
        course_id="crs-000000000000",
        action="resume",
        learner_id="ff-00000000",
        workspace=course["course_dir"] / "workspace",
        context_file=course["course_dir"] / "workspace/.firefly/learning_context.json",
        argv=[],
        cwd=course["course_dir"] / "workspace",
        mode="session-rollover",
        task_id="task-rol",
    )
    session.attach_launch(LaunchResult(plan=plan, spawned=False))
    assert texts and texts[0] == "学习会话已整理，继续当前课程。"
    assert session._timer is not None and session._timer.isActive()
    opened = []
    monkeypatch.setattr(
        "learning.bridge_session.open_learning_session",
        lambda course_dir, session_id: opened.append(session_id),
    )
    (course["course_dir"] / "bridge" / "init-task-rol.json").write_text(
        json.dumps({"sessionId": "sess-rollover-new"}), encoding="utf-8"
    )
    session._poll()
    assert opened == ["sess-rollover-new"]
    assert load_binding(course["course_dir"])["zcode_session_id"] == "sess-rollover-new"
    joined = " ".join(texts).lower()
    for forbidden in ("token", "上下文", "超限", "重新开始", "111%"):
        assert forbidden not in joined, f"用户文案不得出现 {forbidden!r}"
    session.clear()
