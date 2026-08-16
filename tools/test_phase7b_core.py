"""Qt-free tests for Firefly AI Companion Phase 7B."""

import json
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.quick_chat_protocol import (
    SessionRegistry,
    build_claude_args,
    build_codex_args,
    parse_claude_event,
    parse_codex_event,
)
from ui.workspace_store import MAX_RECENT_WORKSPACES, WorkspaceStore


def test_workspace_store(root: Path):
    settings = root / "config" / "ui_settings.json"
    workspaces = []
    for i in range(7):
        p = root / f"ws{i}"
        p.mkdir()
        workspaces.append(p)

    store = WorkspaceStore(settings, workspaces[0])
    assert store.current_workspace == workspaces[0]
    assert store.quick_ask_effort == "low"
    assert store.conversation_enabled is True
    store.set_quick_ask_effort("medium")
    store.set_conversation_enabled(False)
    for p in workspaces:
        store.set_workspace(p)
    assert store.current_workspace == workspaces[-1].resolve()
    assert len(store.recent_workspaces) == MAX_RECENT_WORKSPACES

    parsed = json.loads(settings.read_text(encoding="utf-8"))
    assert parsed["quick_ask_effort"] == "medium"
    assert parsed["conversation_enabled"] is False
    reloaded = WorkspaceStore(settings, workspaces[0])
    assert reloaded.quick_ask_effort == "medium"
    assert reloaded.conversation_enabled is False


def test_session_registry(root: Path):
    a = root / "a"
    b = root / "b"
    a.mkdir()
    b.mkdir()
    sessions = SessionRegistry()
    sessions.set("codex", a, "codex-a")
    sessions.set("claude", a, "claude-a")
    sessions.set("codex", b, "codex-b")
    assert sessions.get("codex", a) == "codex-a"
    assert sessions.get("claude", a) == "claude-a"
    sessions.clear("codex", a)
    assert sessions.get("codex", a) is None
    assert sessions.get("claude", a) == "claude-a"
    assert sessions.get("codex", b) == "codex-b"


def test_args(root: Path):
    ws = root / "nogit"
    ws.mkdir()
    args = build_codex_args("hello", ws, effort="low", persistent=True)
    assert args[:4] == ["exec", "--sandbox", "read-only", "--json"]
    assert "model_reasoning_effort=low" in args
    assert "--skip-git-repo-check" in args
    assert "--ephemeral" not in args

    resumed = build_codex_args("follow up", ws, effort="medium", session_id="thr-1", persistent=True)
    assert "resume" in resumed
    assert resumed[-3:] == ["resume", "thr-1", "follow up"]
    assert "model_reasoning_effort=medium" in resumed

    temp = build_codex_args("one shot", ws, persistent=False)
    assert "--ephemeral" in temp

    claude = build_claude_args("hello", effort="low", persistent=True)
    assert claude[0] == "-p"
    assert "stream-json" in claude
    assert "--include-partial-messages" in claude
    assert "--no-session-persistence" not in claude

    claude_resume = build_claude_args("again", effort="high", session_id="session-1", persistent=True)
    assert "--resume" in claude_resume
    assert "session-1" in claude_resume
    assert claude_resume[-1] == "again"

    claude_temp = build_claude_args("once", persistent=False)
    assert "--no-session-persistence" in claude_temp


def test_parsers():
    e = parse_codex_event({"type": "thread.started", "thread_id": "thr-123"})
    assert e.session_id == "thr-123"
    e = parse_codex_event({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}})
    assert e.final == "hello"

    e = parse_claude_event({
        "type": "stream_event",
        "session_id": "sess-1",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}},
    })
    assert e.session_id == "sess-1"
    assert e.delta == "你"
    e = parse_claude_event({"type": "result", "session_id": "sess-1", "result": "完成", "is_error": False})
    assert e.final == "完成"


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_workspace_store(root)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_session_registry(root)
        test_args(root)
    test_parsers()
    print("Phase 7B core tests passed.")


if __name__ == "__main__":
    main()
