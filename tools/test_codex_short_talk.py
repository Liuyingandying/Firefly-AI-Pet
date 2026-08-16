"""Codex Short Talk / Quick Ask — offline tests (feature/codex-short-talk).

No online Codex calls. Verifies the Codex Quick Ask feature end to end at the
unit level: availability (Ask Codex opens an input), read-only argv safety,
CodexJsonlAdapter JSONL -> AgentEvent mapping, ShortAskPanel integration
(streaming progress + final + scrolling + error + cancel), workspace handling,
and isolation (no session persistence, no lifecycle-source writes, no Workflow
coupling).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QProcess
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import ui.short_ask as sa
from core.agent_adapters import CodexJsonlAdapter, make_adapter
from core.agent_events import STATUS_RECONNECTING, AgentEvent, AgentEventType, ErrorCategory
from core.session_manager import SessionManager
from ui.quick_chat_protocol import build_codex_args
from ui.short_ask import ShortAskPanel, ShortTalkState


# -- A. availability -------------------------------------------------------

def test_ask_codex_opens_input(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True):
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        assert shell.short_ask.agent == "codex"
        assert shell.short_ask._input.isEnabled(), "Ask Codex opens an input, not a notice"
        assert shell.short_ask._title.text() == "Ask Codex"
        assert "isn't available" not in shell.short_ask._status.text().lower()
        shell.short_ask.reset()


def test_ask_codex_read_routes_to_codex_short_talk(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("解释一下 Python decorator 是什么")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "codex"
        assert ask_mock.call_args.kwargs.get("persistent") is False, (
            "Codex Short Talk is ephemeral single-turn"
        )
        assert ask_mock.call_args.kwargs.get("sandbox") is None, (
            "sandbox is not overridden; the runner default is read-only"
        )
        assert not shell.recommendation_card.has_pending
        shell.short_ask.reset()


def test_ask_codex_write_stays_read_only(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("重构这个模块")
        assert ask_mock.call_count == 0, "read-only Codex Short Talk must not run a write task"
        assert shell.recommendation_card.has_pending
        assert shell.recommendation_card.pending_agent == "codex"
        shell.short_ask.reset()


# -- B. argv safety --------------------------------------------------------

def test_codex_argv_read_only_and_ephemeral() -> None:
    ws = PROJECT_DIR  # has .git -> no --skip-git-repo-check
    args = build_codex_args("解释 decorator", ws, effort="low", persistent=False)
    assert args[0] == "exec"
    assert "--json" in args
    assert "--ephemeral" in args
    sandbox_idx = args.index("--sandbox")
    assert args[sandbox_idx + 1] == "read-only"
    assert "workspace-write" not in args
    assert "danger-full-access" not in args
    c_idx = args.index("-c")
    assert args[c_idx + 1] == "model_reasoning_effort=low"
    assert args[-1] == "解释 decorator"


def test_codex_argv_no_git_adds_skip_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        args = build_codex_args("hi", Path(td), effort="low", persistent=False)
        assert "--skip-git-repo-check" in args


# -- C. JSON parser --------------------------------------------------------

def test_codex_jsonl_maps_agent_events() -> None:
    adapter = CodexJsonlAdapter()
    events = []
    for line in (
        '{"type":"thread.started","thread_id":"thread-1"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"你好，这是回答。"}}',
        '{"type":"turn.completed"}',
    ):
        events.extend(adapter.feed_line(line))
    types = [e.type for e in events]
    assert AgentEventType.SESSION in types
    assert AgentEventType.FINAL in types
    assert AgentEventType.STATUS in types
    final = next(e for e in events if e.type == AgentEventType.FINAL)
    assert final.text == "你好，这是回答。"
    session = next(e for e in events if e.type == AgentEventType.SESSION)
    assert session.session_id == "thread-1"


def test_codex_jsonl_error_maps_provider() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_line(
        '{"type":"turn.failed","message":"provider exploded"}'
    )
    err = next(e for e in events if e.type == AgentEventType.ERROR)
    assert err.error_code == ErrorCategory.PROVIDER


# -- D. streaming ----------------------------------------------------------

def test_codex_streaming_to_panel(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running("Connecting…")
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.STARTED))
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.STATUS, status="thinking"))
    assert panel.state == ShortTalkState.THINKING
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.FINAL, text="完整回答"))
    assert panel.state == ShortTalkState.COMPLETE
    assert panel.full_answer() == "完整回答"
    assert panel._output.text() == "完整回答"
    assert panel._status.text().startswith("Done")
    panel.close()


# -- E. scrolling ----------------------------------------------------------

def test_codex_long_answer_scrolls(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running()
    long_text = "word " * 2000
    panel.set_answer(long_text)
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    assert panel._output.text() == long_text, "full answer retained, not clipped"
    assert bar.maximum() > bar.minimum(), "long Codex answer scrolls"
    assert panel._output.height() <= 110
    panel.close()


# -- F. cancel -------------------------------------------------------------

def test_codex_stop_kills_only_owned_process(app: QApplication) -> None:
    from ui.process_launcher import QuickAskRunner

    runner = QuickAskRunner(session_manager=None)

    class FakeProcess:
        def processId(self) -> int:
            return 4242

        def state(self):
            return QProcess.Running

    runner._process = FakeProcess()
    with patch.object(QProcess, "startDetached", return_value=True) as sd:
        runner.stop()
    assert sd.called
    assert sd.call_args[0][0] == "taskkill.exe"
    assert sd.call_args[0][1] == ["/PID", "4242", "/T", "/F"]
    assert all("/IM" not in a for a in sd.call_args[0][1]), "must not kill every codex.exe"


# -- G. error --------------------------------------------------------------

def test_codex_error_opens_fallback(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running()
    panel.on_agent_event(
        AgentEvent.make("codex", AgentEventType.ERROR, text="boom", error_code=ErrorCategory.PROVIDER)
    )
    assert panel.state == ShortTalkState.ERROR
    assert panel._primary_btn.isVisible()
    assert panel._primary_btn.text() == "Open Codex"
    panel.close()


# -- H. workspace ----------------------------------------------------------

def test_codex_missing_workspace_fails_gracefully(app: QApplication) -> None:
    from ui.process_launcher import QuickAskRunner

    runner = QuickAskRunner(session_manager=None)
    failed = []
    runner.failed.connect(failed.append)
    ok = runner.ask("codex", "hello", Path("Z:/definitely/missing"), effort="low", persistent=False)
    assert ok is False
    assert failed and "工作区不存在" in failed[0]


# -- I. isolation ----------------------------------------------------------

def test_codex_ephemeral_does_not_persist_session(app: QApplication) -> None:
    from ui.process_launcher import QuickAskRunner

    sm = SessionManager()
    runner = QuickAskRunner(session_manager=sm)
    runner._agent = "codex"
    runner._workspace = PROJECT_DIR
    runner._persistent = False
    runner._adapter = make_adapter("codex")
    runner._telemetry = None
    runner._consume_json_line('{"type":"thread.started","thread_id":"thread-ephemeral"}')
    assert not sm.has("codex", PROJECT_DIR), "ephemeral Codex Short Talk must not persist a thread"


def test_codex_short_talk_no_lifecycle_source_write(app: QApplication) -> None:
    from ui.process_launcher import QuickAskRunner

    sources = PROJECT_DIR / "runtime" / "sources"
    before = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()

    runner = QuickAskRunner(session_manager=None)
    runner._agent = "codex"
    runner._workspace = PROJECT_DIR
    runner._persistent = False
    runner._adapter = make_adapter("codex")
    runner._telemetry = None
    for line in (
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        '{"type":"turn.completed"}',
    ):
        runner._consume_json_line(line)
    runner._on_finished(0, 0)

    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            assert before.get(p.name) == p.read_bytes(), (
                f"Codex Short Talk must not write {p.name}"
            )


def test_hard_timeout_shows_retry_and_open(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running()
    stops = []
    retries = []
    panel.stop_requested.connect(lambda: stops.append(True))
    panel.retry_requested.connect(lambda: retries.append(True))
    panel._on_hard_timeout()
    assert panel.state == ShortTalkState.ERROR
    assert panel._status.text() == "Codex is taking too long."
    assert panel._primary_btn.text() == "Retry"
    assert panel._secondary_btn.text() == "Open Codex"
    assert stops == [True], "hard timeout cancels the owned process"
    # late cancel/finished must not override the timeout UI
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.CANCELLED))
    assert panel.state == ShortTalkState.ERROR
    panel.reset_with_note("No text returned.")
    assert panel.state == ShortTalkState.ERROR
    panel._on_primary()
    assert retries == [True], "Retry emits retry_requested"
    panel.close()


def test_hard_timeout_timer_scoped_to_short_talk(app: QApplication) -> None:
    from ui.short_ask import HARD_TIMEOUT_MS

    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running()
    assert panel._hard_timeout_timer.interval() == HARD_TIMEOUT_MS
    assert panel._hard_timeout_timer.isActive()
    panel.show_done()
    assert not panel._hard_timeout_timer.isActive()
    panel.close()


# -- J. hard-timeout deadline semantics (production bug fix) ---------------

def test_hard_timeout_survives_first_token(app: QApplication) -> None:
    """The hard deadline is wall-clock from Send, not a "no first token" timer."""
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    assert panel._hard_timeout_timer.isActive()
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="first"))
    assert panel.state == ShortTalkState.STREAMING
    assert panel._hard_timeout_timer.isActive(), "first token must not disarm the hard timeout"
    assert not panel._slow_timer.isActive(), "slow/very-slow progress cues end on first token"
    assert not panel._very_slow_timer.isActive()
    panel.close()


def test_hard_timeout_wallclock_not_reset_by_status(app: QApplication) -> None:
    """Ongoing reconnect/status events must not extend the deadline."""
    orig = sa.HARD_TIMEOUT_MS
    sa.HARD_TIMEOUT_MS = 200
    try:
        panel = sa.ShortAskPanel()
        panel.show_input("codex")
        panel.set_running("Connecting…")
        for _ in range(12):
            panel.on_agent_event(
                AgentEvent.make("codex", AgentEventType.STATUS, status=STATUS_RECONNECTING)
            )
            QTest.qWait(40)
        assert panel.state == ShortTalkState.ERROR, (
            "deadline must fire on time even while status events keep arriving"
        )
        panel.close()
    finally:
        sa.HARD_TIMEOUT_MS = orig


def test_hard_timeout_stops_once_and_shows_recovery(app: QApplication) -> None:
    orig = sa.HARD_TIMEOUT_MS
    sa.HARD_TIMEOUT_MS = 150
    try:
        panel = sa.ShortAskPanel()
        panel.show_input("codex")
        panel.set_running("Connecting…")
        stops = []
        panel.stop_requested.connect(lambda: stops.append(True))
        QTest.qWait(400)
        assert panel.state == ShortTalkState.ERROR
        assert panel._status.text() == "Codex is taking too long."
        assert panel._primary_btn.text() == "Retry" and panel._primary_btn.isVisible()
        assert panel._secondary_btn.text() == "Open Codex" and panel._secondary_btn.isVisible()
        assert len(stops) == 1, "runner.stop must be requested exactly once"
        panel.close()
    finally:
        sa.HARD_TIMEOUT_MS = orig


def test_late_final_and_finished_after_timeout_stay_timeout(app: QApplication) -> None:
    """A late FINAL / completed / finished(exit=0) must not flip the UI to Done."""
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running("Connecting…")
    panel._on_hard_timeout()
    assert panel.state == ShortTalkState.ERROR
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.FINAL, text="late answer"))
    panel.on_agent_event(AgentEvent.make("codex", AgentEventType.STATUS, status="completed"))
    panel.finish_turn("late answer")
    panel.reset_with_note("No text returned.")
    assert panel.state == ShortTalkState.ERROR
    assert panel._status.text() == "Codex is taking too long."
    assert not panel._status.text().startswith("Done")
    panel.close()


def test_late_error_after_timeout_stays_timeout(app: QApplication) -> None:
    """A late provider ERROR must not replace the timeout recovery UI."""
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.set_running("Connecting…")
    panel._on_hard_timeout()
    assert panel.state == ShortTalkState.ERROR
    panel.on_agent_event(
        AgentEvent.make("codex", AgentEventType.ERROR, text="boom", error_code=ErrorCategory.PROVIDER)
    )
    assert panel.state == ShortTalkState.ERROR
    assert panel._status.text() == "Codex is taking too long."
    assert panel._primary_btn.text() == "Retry"
    assert panel._secondary_btn.text() == "Open Codex"
    panel.close()


def test_retry_starts_a_fresh_turn(shell) -> None:
    """After timeout, Retry re-arms a new request and clears the timeout state."""
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("hello")
        assert ask_mock.call_count == 1
        shell.short_ask._on_hard_timeout()
        assert shell.short_ask.state == ShortTalkState.ERROR
        shell.short_ask._on_primary()  # _retry_active -> retry_requested
        assert ask_mock.call_count == 2, "Retry launches a fresh ask"
        assert shell.short_ask.state == ShortTalkState.CONNECTING
        assert shell.short_ask._hard_timeout_timer.isActive(), "new turn re-arms the deadline"
        shell.short_ask.reset()


def test_runner_rejects_concurrent_ask(shell) -> None:
    """The single-process guard is the retry-isolation boundary: a new ask cannot
    start while the old process is still running."""
    from ui.process_launcher import QuickAskRunner

    runner = QuickAskRunner(session_manager=None)

    class FakeProcess:
        def state(self):
            return QProcess.Running

    runner._process = FakeProcess()
    runner._agent = "codex"
    failed = []
    runner.failed.connect(failed.append)
    ok = runner.ask("codex", "hi", PROJECT_DIR, effort="low", persistent=False)
    assert ok is False
    assert failed and "已有" in failed[0]


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    # Unit-level tests (no shell).
    test_codex_argv_read_only_and_ephemeral()
    test_codex_argv_no_git_adds_skip_check()
    test_codex_jsonl_maps_agent_events()
    test_codex_jsonl_error_maps_provider()
    test_codex_streaming_to_panel(app)
    test_codex_long_answer_scrolls(app)
    test_codex_stop_kills_only_owned_process(app)
    test_codex_error_opens_fallback(app)
    test_codex_missing_workspace_fails_gracefully(app)
    test_codex_ephemeral_does_not_persist_session(app)
    test_codex_short_talk_no_lifecycle_source_write(app)
    test_hard_timeout_shows_retry_and_open(app)
    test_hard_timeout_timer_scoped_to_short_talk(app)
    test_hard_timeout_survives_first_token(app)
    test_hard_timeout_wallclock_not_reset_by_status(app)
    test_hard_timeout_stops_once_and_shows_recovery(app)
    test_late_final_and_finished_after_timeout_stay_timeout(app)
    test_late_error_after_timeout_stays_timeout(app)
    test_runner_rejects_concurrent_ask(app)

    # Shell-level tests.
    shell = VisualShell(None)
    try:
        test_ask_codex_opens_input(shell)
        test_ask_codex_read_routes_to_codex_short_talk(shell)
        test_ask_codex_write_stays_read_only(shell)
        test_retry_starts_a_fresh_turn(shell)
    finally:
        shell.shutdown()

    print("Codex Short Talk tests passed.")


if __name__ == "__main__":
    main()
