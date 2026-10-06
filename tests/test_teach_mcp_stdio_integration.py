"""Real stdio MCP TutorTurn chain against an isolated teach-mcp fact store."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from learning.student_presentation import feedback_to_display, question_to_display
from learning.teach_mcp_client import TeachMcpClient
from learning.teach_mcp_stdio import StdioMcpTransport


TEACH_ROOT = Path(os.environ.get("FIREFLY_TEACH_MCP_ROOT", ""))
TEACH_PYTHON = Path(os.environ.get("FIREFLY_TEACH_MCP_PYTHON", sys.executable))


def _payload(envelope: dict) -> dict:
    """Read setup-tool output; TutorTurn calls use TeachMcpClient itself."""
    assert not envelope.get("isError"), envelope
    if isinstance(envelope.get("structuredContent"), dict):
        return envelope["structuredContent"]
    blocks = envelope["content"]
    assert len(blocks) == 1 and blocks[0]["type"] == "text"
    return json.loads(blocks[0]["text"])


def test_real_mcp_tutor_question_display_mark_and_answer(tmp_path):
    if (os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1"
            or not os.environ.get("FIREFLY_TEACH_MCP_ROOT")):
        pytest.skip("explicit external teach-mcp integration opt-in required")
    if not TEACH_PYTHON.is_file() or not (TEACH_ROOT / "server.py").is_file():
        pytest.skip("local teach-mcp runtime is unavailable")

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
    learner_id = f"ff-phase2a-{uuid.uuid4().hex[:8]}"
    course_id = f"crs-phase2a-{uuid.uuid4().hex[:8]}"
    env = {"TEACH_MCP_TEST_MODE": str(fixture_root), "PYTHONIOENCODING": "utf-8"}

    with StdioMcpTransport(
        TEACH_PYTHON,
        ["-u", runner, TEACH_ROOT, db],
        env=env,
        cwd=TEACH_ROOT,
        timeout_s=30,
    ) as transport:
        # Official tools seed a test learner and TutorTurn; no direct SQL writes.
        started = _payload(transport.call_tool("start_learning", {
            "learner_id": learner_id, "topic": "RC 电路充放电",
        }))
        assert started["status"] == "success"
        session_id = started["session_id"]
        opened = _payload(transport.call_tool("tutor_open_turn", {
            "session_id": session_id, "concept_id": "tau", "course_id": course_id,
        }))
        assert opened["status"] == "success"

        client = TeachMcpClient(transport.call_tool)
        resumed = client.tutor_resume_turn(session_id)
        assert resumed["status"] == "success"
        assert resumed["course_id"] == course_id
        assert resumed["pending_question"] is None

        next_turn = client.tutor_next_question(session_id)
        assert next_turn["status"] == "success"
        question = next_turn["pending_question"]
        assert question["question_id"]
        assert question == client.tutor_resume_turn(session_id)["pending_question"]
        display = question_to_display(question)
        assert question["stem"] in display
        assert all(f"{label}. {body}" in display for label, body in question["options"].items())
        assert session_id not in display

        marked = client.tutor_mark_presented(session_id, question["question_id"], display)
        assert marked["status"] == "success"
        assert marked["question_presented"] is True
        assert marked["pending_question"]["question_id"] == question["question_id"]

        answered = client.tutor_submit_answer(session_id, question["question_id"], "A")
        assert answered["status"] == "success"
        assert answered["question_id"] == question["question_id"]
        assert feedback_to_display(answered) == answered["feedback"]

    assert db.is_file() and db.is_relative_to(fixture_root)
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
        saved = conn.execute(
            "SELECT question_id, session_id FROM tutor_answers WHERE session_id=?",
            (session_id,),
        ).fetchall()
        assert saved == [(question["question_id"], session_id)]
        result = conn.execute(
            "SELECT question_id FROM results WHERE session_id=?", (session_id,)
        ).fetchall()
        assert result == [(question["question_id"],)]
        owner = conn.execute(
            "SELECT learner_id, course_id FROM tutor_turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        assert owner == (learner_id, course_id)
    assert not list(fixture_root.rglob("result.json"))
