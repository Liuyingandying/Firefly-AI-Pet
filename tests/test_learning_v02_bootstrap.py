import json
import sqlite3
import time
from pathlib import Path

from learning.launcher import build_learning_bootstrap_prompt


def test_multiline_prompt_bypasses_cmd_batch_wrapper(tmp_path):
    from learning.launcher import _prompt_runtime_argv

    root = tmp_path / "zcode"
    (root / "bin").mkdir(parents=True)
    for name in ("zcode.cmd", "zcode.mjs"):
        (root / "bin" / name).write_text("", encoding="utf-8")
    (root / "node.exe").write_text("", encoding="utf-8")
    argv = _prompt_runtime_argv(root / "bin" / "zcode.cmd")
    assert argv == [str(root / "node.exe"), str(root / "bin" / "zcode.mjs")]


def test_interactive_bootstrap_dispatches_tutor_without_return_channel(tmp_path):
    path = tmp_path / "workspace" / ".firefly" / "learning_context.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"delivery_mode": "interactive"}), encoding="utf-8")
    prompt = build_learning_bootstrap_prompt(path, "resume")
    assert prompt.startswith("/skill firefly-learning ")
    assert "FIREFLY_LEARNING_CONTEXT" in prompt
    assert "action=resume" in prompt
    assert "tutor_next_question" in prompt
    assert "tutor_mark_presented" in prompt
    # Headless launch now passes the prompt directly to Node, so the compact
    # resume contract can name the pending/presentation gates explicitly.
    assert len(prompt) < 320
    assert "结果文件（本轮结束前必须写入" not in prompt


def test_embedded_bootstrap_keeps_legacy_return_channel(tmp_path):
    path = tmp_path / "workspace" / ".firefly" / "learning_context.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"delivery_mode": "embedded"}), encoding="utf-8")
    prompt = build_learning_bootstrap_prompt(path, "answer")
    assert "结果文件（本轮结束前必须写入" in prompt
    assert "message_type=question" in prompt


def test_interactive_rollover_ignores_stale_result_question(tmp_path, monkeypatch):
    from learning import launcher
    from learning.course_binding import save_binding

    course = tmp_path / "crs-000000000000"
    workspace = course / "workspace"
    (course / "bridge").mkdir(parents=True)
    save_binding(course, course_id=course.name, zcode_session_id="sess-old")
    (course / "bridge" / "result.json").write_text(
        json.dumps({
            "message_type": "question",
            "question": {"question_id": "RC-E01"},
            "opaque_refs": {"session_id": "teach-old"},
        }), encoding="utf-8",
    )
    spawned = []
    old_init = course / "bridge" / "init-task-older.json"
    old_init.write_text("prior evidence", encoding="utf-8")
    monkeypatch.setattr(launcher, "_runtime_argv", lambda runtime: ["zcode"])
    monkeypatch.setattr(launcher, "_ensure_zcode_provider_env", lambda env, runtime: None)
    monkeypatch.setattr(
        launcher, "_spawn_detached",
        lambda argv, cwd, env, flags, stdout_path=None: spawned.append(argv) or 12345,
    )
    holder = {}
    launcher._run_rollover_init(
        course, workspace, "resume", "learner", holder, "sess-old", 180000,
        Path("zcode.cmd"),
    )
    snapshot = json.loads(
        (course / "bridge" / "rollover_snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["pending_question_id"] is None
    assert snapshot["teach_mcp_session_id"] is None
    assert "RC-E01" not in " ".join(spawned[0])
    assert "/skill firefly-learning" in " ".join(spawned[0])
    assert "等待真人" in " ".join(spawned[0])
    assert old_init.read_text(encoding="utf-8") == "prior evidence"


def test_init_session_fallback_requires_current_task_session(tmp_path, monkeypatch):
    from learning import launcher

    course = tmp_path / "crs-000000000000"
    bridge = course / "bridge"
    bridge.mkdir(parents=True)
    (bridge / "init-task-new.json").write_text("", encoding="utf-8")
    db = tmp_path / "zcode.sqlite"
    workspace = str(course / "workspace")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session(id TEXT,directory TEXT,time_created INTEGER)")
        conn.execute("CREATE TABLE message(id TEXT,session_id TEXT,sequence INTEGER,data TEXT)")
        conn.execute("CREATE TABLE part(message_id TEXT,data TEXT)")
        conn.execute("INSERT INTO session VALUES(?,?,?)", (
            "sess-old", workspace, int(time.time() * 1000) - 60000,
        ))
    monkeypatch.setattr(launcher, "default_session_db", lambda: db)
    assert launcher.read_init_session_id(
        course, task_id="task-new", exclude_session_id="sess-old",
        require_complete=True,
    ) is None
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO session VALUES(?,?,?)", (
            "sess-new", workspace, int(time.time() * 1000),
        ))
    assert launcher.read_init_session_id(
        course, task_id="task-new", exclude_session_id="sess-old",
        require_complete=True,
    ) is None
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO message VALUES(?,?,?,?)", (
            "msg-first", "sess-new", 1, json.dumps({"role": "assistant", "finish": "stop"}),
        ))
    assert launcher.read_init_session_id(
        course, task_id="task-new", exclude_session_id="sess-old",
        require_complete=True,
    ) == "sess-new"
    assert launcher.read_init_session_id(
        course, task_id="task-new", exclude_session_id="sess-old",
        require_complete=True, require_skill_invoked=True,
    ) is None
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO part VALUES(?,?)", (
            "msg-first", json.dumps({
                "type": "tool", "tool": "Skill", "state": {"status": "completed"},
            }),
        ))
    assert launcher.read_init_session_id(
        course, task_id="task-new", exclude_session_id="sess-old",
        require_complete=True, require_skill_invoked=True,
    ) == "sess-new"
