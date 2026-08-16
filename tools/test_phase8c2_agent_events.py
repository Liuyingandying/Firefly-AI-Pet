"""Phase 8C.2 — unified AgentEvent adapter tests.

Covers the 24 required scenarios + a parser performance sanity check:
  Qt-free model/adapters, Claude stream-json -> SESSION/TEXT_DELTA/FINAL/ERROR,
  Codex JSONL -> SESSION/FINAL/TOOL/STATUS/ERROR, malformed-line tolerance,
  QuickAskRunner forwarding unified AgentEvents, SessionManager consumption,
  telemetry driven by first TEXT_DELTA, cancel -> CANCELLED, UI free of
  provider event names, internal/external lifecycle isolation, PermissionCard
  unaffected, ChatGPT never gets a fake adapter, Phase 7 compatibility parsers
  still pass, and 1000-line parser throughput well under the target.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.agent_adapters import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    ClaudeStreamAdapter,
    CodexJsonlAdapter,
    capabilities_for,
    make_adapter,
)
from core.agent_events import (
    STATUS_READING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.quick_ask_metrics import AskTelemetry
from core.session_manager import KIND_SESSION, KIND_THREAD, SessionManager
from ui.process_launcher import QuickAskRunner

# Provider-specific event names that must stay OUT of the UI layer.
PROVIDER_EVENT_TOKENS = (
    "thread.started",
    "stream_event",
    "content_block_delta",
    "item.completed",
    "message_start",
    "agent_message",
    "turn.started",
    "turn.failed",
    "thread_id",
)
UI_FILES = (
    PROJECT_DIR / "app.py",
    PROJECT_DIR / "ui" / "short_ask.py",
    PROJECT_DIR / "ui" / "process_launcher.py",
    PROJECT_DIR / "ui" / "quick_chat_protocol.py",
)


def _ws(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- 1-6. Claude adapter ---------------------------------------------------

def test_claude_session_event() -> None:
    adapter = ClaudeStreamAdapter()
    events = adapter.feed_event({"type": "system", "session_id": "sess-1"})
    session = [e for e in events if e.type == AgentEventType.SESSION]
    assert len(session) == 1 and session[0].session_id == "sess-1"
    # session id seen again on a later line emits only one SESSION event
    again = adapter.feed_event({"type": "stream_event", "session_id": "sess-1", "event": {"type": "message_start"}})
    assert not [e for e in again if e.type == AgentEventType.SESSION]


def test_claude_text_delta() -> None:
    adapter = ClaudeStreamAdapter()
    events = adapter.feed_event({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}},
    })
    deltas = [e for e in events if e.type == AgentEventType.TEXT_DELTA]
    assert len(deltas) == 1 and deltas[0].text == "你"
    # thinking / signature deltas are status-only, never TEXT_DELTA
    events = adapter.feed_event({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "..."}},
    })
    assert not [e for e in events if e.type == AgentEventType.TEXT_DELTA]
    assert [e for e in events if e.type == AgentEventType.STATUS]


def test_claude_delta_order() -> None:
    adapter = ClaudeStreamAdapter()
    texts: list[str] = []
    for piece in ["你", "好", "世", "界"]:
        for e in adapter.feed_line(json.dumps({
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}},
        })):
            if e.type == AgentEventType.TEXT_DELTA:
                texts.append(e.text)
    assert texts == ["你", "好", "世", "界"]


def test_claude_final() -> None:
    adapter = ClaudeStreamAdapter()
    events = adapter.feed_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "你好"}]}})
    finals = [e for e in events if e.type == AgentEventType.FINAL]
    assert len(finals) == 1 and finals[0].text == "你好"
    events = adapter.feed_event({"type": "result", "result": "世界", "is_error": False})
    finals = [e for e in events if e.type == AgentEventType.FINAL]
    assert len(finals) == 1 and finals[0].text == "世界"


def test_claude_error() -> None:
    adapter = ClaudeStreamAdapter()
    events = adapter.feed_event({"type": "result", "result": "boom", "is_error": True})
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert len(errors) == 1
    assert errors[0].error_code == ErrorCategory.PROVIDER
    assert "boom" in errors[0].text


def test_claude_malformed_no_crash() -> None:
    adapter = ClaudeStreamAdapter()
    events = adapter.feed_line("{not valid json")
    assert len(events) == 1
    assert events[0].type == AgentEventType.ERROR
    assert events[0].error_code == ErrorCategory.PROTOCOL
    assert adapter.feed_line("") == []  # blank line is ignored
    events = adapter.feed_line("[1,2,3]")  # JSON but not an object
    assert events[0].error_code == ErrorCategory.PROTOCOL


# -- 7-11. Codex adapter ---------------------------------------------------

def test_codex_session_event() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "thread.started", "thread_id": "thr-42"})
    sessions = [e for e in events if e.type == AgentEventType.SESSION]
    assert len(sessions) == 1 and sessions[0].session_id == "thr-42"


def test_codex_agent_text_final() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}})
    finals = [e for e in events if e.type == AgentEventType.FINAL]
    assert len(finals) == 1 and finals[0].text == "answer"
    # item.started agent_message has no text yet -> no FINAL
    events = adapter.feed_event({"type": "item.started", "item": {"type": "agent_message"}})
    assert not [e for e in events if e.type == AgentEventType.FINAL]


def test_codex_tool_status() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "item.started", "item": {"type": "command_execution"}})
    tools = [e for e in events if e.type == AgentEventType.TOOL]
    assert len(tools) == 1 and tools[0].tool_name == "command_execution"
    statuses = [e.status for e in events if e.type == AgentEventType.STATUS]
    assert STATUS_READING in statuses
    events = adapter.feed_event({"type": "item.completed", "item": {"type": "mcp_tool_call"}})
    tools = [e for e in events if e.type == AgentEventType.TOOL]
    assert len(tools) == 1 and tools[0].tool_name == "mcp_tool_call"


def test_codex_error() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "turn.failed", "message": "no can do"})
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert len(errors) == 1 and errors[0].error_code == ErrorCategory.PROVIDER
    assert "no can do" in errors[0].text
    events = adapter.feed_event({"type": "error", "error": {"message": "boom"}})
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert len(errors) == 1 and "boom" in errors[0].text


def test_codex_malformed_no_crash() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_line("this is not json")
    assert len(events) == 1 and events[0].error_code == ErrorCategory.PROTOCOL
    assert adapter.feed_line("") == []


# -- 12-13. Cross-cutting --------------------------------------------------

def test_adapters_qt_free() -> None:
    # core/__init__.py eagerly imports StateMonitor (Qt), so load the adapter
    # modules through a bare synthetic package to prove the adapter files
    # themselves have zero PySide6 dependency.
    code = (
        "import importlib.util, sys, types\n"
        "pkg = types.ModuleType('core'); pkg.__path__ = ['core']; sys.modules['core'] = pkg\n"
        "for name in ('core.agent_events', 'core.agent_adapters'):\n"
        "    spec = importlib.util.spec_from_file_location(name, name.replace('.', '/') + '.py')\n"
        "    mod = importlib.util.module_from_spec(spec)\n"
        "    sys.modules[name] = mod\n"
        "    spec.loader.exec_module(mod)\n"
        "assert 'PySide6' not in sys.modules, 'adapter modules must not import Qt'\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_DIR), check=True)


def test_native_id_not_in_ui_text(root: Path, app) -> None:
    ws = _ws(root, "ws-id")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    secret = "native-session-abc123xyz"
    statuses: list[str] = []
    partials: list[str] = []
    finals: list[str] = []
    failed: list[str] = []
    runner.status.connect(statuses.append)
    runner.partial.connect(partials.append)
    runner.finished.connect(lambda text, code: finals.append(text))
    runner.failed.connect(failed.append)
    runner._consume_json_line(json.dumps({"type": "system", "session_id": secret}))
    runner._consume_json_line(json.dumps({
        "type": "stream_event", "session_id": secret,
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    }))
    runner._consume_json_line(json.dumps({
        "type": "assistant", "session_id": secret,
        "message": {"content": [{"type": "text", "text": "world"}]},
    }))
    runner._on_finished(0, 0)
    ui_text = " ".join(statuses + partials + finals + failed)
    assert secret not in ui_text, "native session id leaked into UI-facing text"
    runner.shutdown()


# -- 14-19. QuickAskRunner integration ------------------------------------

def test_runner_forwards_unified_events(root: Path, app) -> None:
    ws = _ws(root, "ws-fwd")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    events: list[AgentEvent] = []
    runner.agent_event.connect(events.append)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._consume_json_line(json.dumps({
        "type": "stream_event", "session_id": "sess-x",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    }))
    runner._consume_json_line(json.dumps({"type": "result", "result": "done", "is_error": False}))
    assert events, "runner must forward AgentEvents"
    for ev in events:
        assert isinstance(ev, AgentEvent), "runner must forward AgentEvent, not raw provider dicts"
    types = {ev.type for ev in events}
    assert AgentEventType.SESSION in types
    assert AgentEventType.TEXT_DELTA in types
    assert AgentEventType.FINAL in types
    runner.shutdown()


def test_session_manager_from_session_event(root: Path, app) -> None:
    ws = _ws(root, "ws-sess")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._workspace = ws
    runner._persistent = True
    runner._agent = "codex"
    runner._consume_json_line(json.dumps({"type": "thread.started", "thread_id": "thr-9"}))
    assert mgr.get_native_id("codex", ws) == "thr-9"
    assert mgr.get("codex", ws).kind == KIND_THREAD
    runner._agent = "claude"
    runner._consume_json_line(json.dumps({
        "type": "stream_event", "session_id": "sess-9",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
    }))
    assert mgr.get_native_id("claude", ws) == "sess-9"
    assert mgr.get("claude", ws).kind == KIND_SESSION
    runner.shutdown()


def test_workspace_isolation_via_events(root: Path, app) -> None:
    ws_a = _ws(root, "ws-a")
    ws_b = _ws(root, "ws-b")
    mgr = SessionManager()
    r1 = QuickAskRunner(session_manager=mgr, parent=app)
    r1._agent = "claude"
    r1._workspace = ws_a
    r1._persistent = True
    r1._consume_json_line(json.dumps({
        "type": "stream_event", "session_id": "sess-a", "event": {"type": "message_start"},
    }))
    r2 = QuickAskRunner(session_manager=mgr, parent=app)
    r2._agent = "claude"
    r2._workspace = ws_b
    r2._persistent = True
    r2._consume_json_line(json.dumps({
        "type": "stream_event", "session_id": "sess-b", "event": {"type": "message_start"},
    }))
    assert mgr.get_native_id("claude", ws_a) == "sess-a"
    assert mgr.get_native_id("claude", ws_b) == "sess-b"
    r1.shutdown()
    r2.shutdown()


def test_telemetry_first_text_from_delta(root: Path, app) -> None:
    ws = _ws(root, "ws-tele")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._telemetry = AskTelemetry(agent="claude", workspace="w", created_at=0)
    runner._telemetry.milestone_t0 = 0
    # STATUS-only line: no T5
    runner._consume_json_line(json.dumps({"type": "stream_event", "event": {"type": "message_start"}}))
    assert runner._telemetry.milestone_t5 is None, "T5 must be driven by first TEXT_DELTA"
    # first TEXT_DELTA fires T5
    runner._consume_json_line(json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "h"}},
    }))
    t5 = runner._telemetry.milestone_t5
    assert t5 is not None
    # subsequent deltas do not move T5
    runner._consume_json_line(json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "i"}},
    }))
    assert runner._telemetry.milestone_t5 == t5
    runner.shutdown()


def test_final_telemetry(root: Path, app) -> None:
    ws = _ws(root, "ws-final")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._telemetry = AskTelemetry(agent="claude", workspace="w", created_at=0)
    runner._telemetry.milestone_t0 = 0
    recorded = []
    runner.telemetry.connect(recorded.append)
    runner._consume_json_line(json.dumps({"type": "stream_event", "session_id": "sess-x", "event": {"type": "message_start"}}))
    runner._consume_json_line(json.dumps({"type": "result", "result": "done", "is_error": False}))
    runner._on_finished(0, 0)
    assert len(recorded) == 1, "one final telemetry per turn"
    telemetry = recorded[0]
    assert telemetry.process_exit == 0
    assert telemetry.milestone_t6 is not None
    payload = telemetry.to_payload()
    assert payload["derived"]["total_ms"] is not None
    assert telemetry.validate_safe() == []
    runner.shutdown()


def test_cancel_cancelled_event(root: Path, app) -> None:
    ws = _ws(root, "ws-cancel")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    events: list[AgentEvent] = []
    runner.agent_event.connect(events.append)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._cancelled = True
    runner._on_finished(1, 0)
    assert any(e.type == AgentEventType.CANCELLED for e in events), "cancel must emit CANCELLED"
    runner.shutdown()


# -- 20-23. UI / lifecycle boundaries --------------------------------------

def test_ui_has_no_provider_event_names() -> None:
    for path in UI_FILES:
        source = path.read_text(encoding="utf-8")
        for token in PROVIDER_EVENT_TOKENS:
            assert token not in source, f"{path.name} must not reference provider event {token!r}"


def test_internal_events_no_external_lifecycle(root: Path, app) -> None:
    sources = PROJECT_DIR / "runtime" / "sources"
    before: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()

    ws = _ws(root, "ws-ext")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._consume_json_line(json.dumps({"type": "stream_event", "session_id": "sess-x", "event": {"type": "message_start"}}))
    runner._consume_json_line(json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    }))
    runner._consume_json_line(json.dumps({"type": "result", "result": "ok", "is_error": False}))
    runner._on_finished(0, 0)
    runner.shutdown()

    after: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "internal AgentEvent consumption must not touch external lifecycle sources"
    # No WAITING lifecycle was produced -> PermissionCard (driven by StateMonitor
    # over those sources) can never be raised by an internal ask.


def test_chatgpt_no_fake_adapter() -> None:
    assert make_adapter("chatgpt") is None
    assert make_adapter("") is None
    assert capabilities_for("chatgpt") is None
    assert make_adapter("claude").agent_id == "claude"
    assert make_adapter("codex").agent_id == "codex"
    assert CLAUDE_CAPABILITIES.streaming is True
    assert CODEX_CAPABILITIES.streaming is False


# -- 24. Phase 7 compatibility --------------------------------------------

def test_phase7_compat_parsers() -> None:
    from ui.quick_chat_protocol import parse_claude_event, parse_codex_event

    e = parse_codex_event({"type": "thread.started", "thread_id": "thr-123"})
    assert e.session_id == "thr-123"
    e = parse_codex_event({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}})
    assert e.final == "hello"
    e = parse_claude_event({
        "type": "stream_event", "session_id": "sess-1",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}},
    })
    assert e.session_id == "sess-1"
    assert e.delta == "你"
    e = parse_claude_event({"type": "result", "session_id": "sess-1", "result": "完成", "is_error": False})
    assert e.final == "完成"


# -- finalize + performance -------------------------------------------------

def test_finalize_synthesizes_process_error() -> None:
    adapter = ClaudeStreamAdapter()
    assert adapter.finalize(0) == []
    events = adapter.finalize(2)
    assert len(events) == 1
    assert events[0].type == AgentEventType.ERROR
    assert events[0].error_code == ErrorCategory.PROVIDER
    # an in-stream error prevents a duplicate synthesized error
    adapter = ClaudeStreamAdapter()
    adapter.feed_event({"type": "result", "result": "x", "is_error": True})
    assert adapter.finalize(2) == []


def test_parser_performance() -> None:
    n = 1000
    claude_lines = [
        json.dumps({
            "type": "stream_event", "session_id": f"s{i}",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
        })
        for i in range(n)
    ]
    codex_lines = [
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
        for i in range(n)
    ]
    claude_adapter = ClaudeStreamAdapter()
    codex_adapter = CodexJsonlAdapter()

    t0 = time.perf_counter()
    for line in claude_lines:
        claude_adapter.feed_line(line)
    t1 = time.perf_counter()
    for line in codex_lines:
        codex_adapter.feed_line(line)
    t2 = time.perf_counter()

    claude_ms = (t1 - t0) * 1000
    codex_ms = (t2 - t1) * 1000
    print(f"[perf] claude 1000 lines: {claude_ms:.1f}ms | codex 1000 lines: {codex_ms:.1f}ms")
    assert claude_ms + codex_ms < 500, f"parser too slow: {claude_ms + codex_ms:.1f}ms for 2000 lines"


def main() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_claude_session_event()
        test_claude_text_delta()
        test_claude_delta_order()
        test_claude_final()
        test_claude_error()
        test_claude_malformed_no_crash()
        test_codex_session_event()
        test_codex_agent_text_final()
        test_codex_tool_status()
        test_codex_error()
        test_codex_malformed_no_crash()
        test_adapters_qt_free()
        test_native_id_not_in_ui_text(root, app)
        test_runner_forwards_unified_events(root, app)
        test_session_manager_from_session_event(root, app)
        test_workspace_isolation_via_events(root, app)
        test_telemetry_first_text_from_delta(root, app)
        test_final_telemetry(root, app)
        test_cancel_cancelled_event(root, app)
        test_ui_has_no_provider_event_names()
        test_internal_events_no_external_lifecycle(root, app)
        test_chatgpt_no_fake_adapter()
        test_phase7_compat_parsers()
        test_finalize_synthesizes_process_error()
        test_parser_performance()
    print("Phase 8C.2 agent event tests passed.")


if __name__ == "__main__":
    main()
