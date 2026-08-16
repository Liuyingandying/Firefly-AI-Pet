"""Phase 8C.3 — Short Talk UX state machine tests.

Covers the 32 required scenarios: READY->CONNECTING, STARTED, STATUS thinking,
first TEXT_DELTA immediately visible, ordered deltas, FINAL->COMPLETE,
ERROR category copy, CANCELLED, double-send guard, Esc running/idle, Stop only
running, per-turn agent lock + next-turn selection, resume/new session badge,
long-response truncation without window growth, Open Claude signal, ChatGPT no
backend, long-task recommendation + Ask anyway, slow-request ("Still working…",
20s Open Claude action, never auto-cancel), PermissionCard hide-but-not-cancel,
hidden-turn completion preservation and reopen, business popover policy,
telemetry duration display without leaking sensitive fields, and UI staying
free of provider raw event names.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.agent_events import (
    STATUS_THINKING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.models import AgentState, LifecycleState
from core.quick_ask_metrics import AskTelemetry
from core.session_manager import SessionManager
from core.settings_manager import SettingsManager
from core.workspace_manager import WorkspaceManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
from ui.session_popover import SessionPopover
from ui.settings_popover import SettingsPopover
from ui.short_ask import MAX_ANSWER_CHARS, MAX_ANSWER_HEIGHT, ShortAskPanel, ShortTalkState
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar
from ui.workspace_popover import WorkspacePopover

STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}

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


def _delta(panel: ShortAskPanel, text: str) -> None:
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text=text))


# -- 1/2. READY -> CONNECTING, STARTED -------------------------------------

def test_ready_to_connecting(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    assert panel.state == ShortTalkState.READY
    assert not panel.running
    panel.set_running("Connecting…")
    assert panel.state == ShortTalkState.CONNECTING
    assert panel.running
    panel.close()


def test_started_keeps_connecting(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.STARTED))
    assert panel.state == ShortTalkState.CONNECTING
    panel.close()


def test_session_marks_session_ready(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    assert not panel.session_ready
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.SESSION, session_id="sess-x"))
    assert panel.session_ready
    panel.close()


# -- 3. STATUS thinking -> thinking UI -------------------------------------

def test_status_thinking_ui(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.STATUS, status=STATUS_THINKING))
    assert panel.state == ShortTalkState.THINKING
    assert panel._status.text() == "Thinking…"
    # a late "connecting" system event must not regress the reliable state
    panel.on_agent_event(
        AgentEvent.make("claude", AgentEventType.STATUS, status="connecting")
    )
    assert panel.state == ShortTalkState.THINKING
    assert panel._status.text() == "Thinking…"
    panel.close()


# -- 4/5. First-token-first + ordered deltas --------------------------------

def test_first_delta_immediately_visible(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel.show()
    app.processEvents()
    assert panel._output.text() == ""
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="OK"))
    # first TEXT_DELTA is visible within the same event-loop pass (no FINAL wait).
    assert panel._output.text() == "OK"
    assert panel.full_answer() == "OK"
    assert panel.state == ShortTalkState.STREAMING
    app.processEvents()
    assert panel._output.text() == "OK"
    panel.close()


def test_deltas_appended_in_order(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running()
    for piece in ["你", "好", "世", "界"]:
        _delta(panel, piece)
    assert panel.full_answer() == "你好世界"
    assert panel._output.text() == "你好世界"
    panel.close()


# -- 6. FINAL -> COMPLETE --------------------------------------------------

def test_final_complete(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running()
    _delta(panel, "你好")
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text="你好世界"))
    assert panel.state == ShortTalkState.COMPLETE
    assert not panel.running
    assert panel._output.text() == "你好世界"
    assert panel._status.text().startswith("Done")
    assert panel._input.isEnabled()
    panel.close()


# -- 7. ERROR category -> short copy ----------------------------------------

def test_error_category_copy(app: QApplication) -> None:
    cases = [
        (ErrorCategory.AUTH, "Authentication issue"),
        (ErrorCategory.TIMEOUT, "Request timed out"),
        (ErrorCategory.TRANSPORT, "Connection problem"),
        (ErrorCategory.PROTOCOL, "Response format problem"),
        (ErrorCategory.PROVIDER, "Agent returned an error"),
        (ErrorCategory.PROCESS_START, "Failed to start agent"),
        (ErrorCategory.UNKNOWN, "Something went wrong"),
    ]
    for category, expected in cases:
        panel = ShortAskPanel()
        panel.show_input("claude")
        panel.set_running()
        panel.on_agent_event(
            AgentEvent.make("claude", AgentEventType.ERROR, text="boom", error_code=category)
        )
        assert panel.state == ShortTalkState.ERROR
        assert panel._status.text() == expected, f"{category.value}: {panel._status.text()!r}"
        assert panel._primary_btn.isVisible(), f"{category.value}: Open agent action available"
        panel.close()


# -- 8. CANCELLED -----------------------------------------------------------

def test_cancelled_state(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running()
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    assert panel.state == ShortTalkState.CANCELLED
    assert not panel.running
    assert panel._status.text() == "Cancelled"
    panel.reset()
    panel.close()


# -- 9. Double send blocked -------------------------------------------------

def test_double_send_blocked(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    sent = []
    panel.send_requested.connect(sent.append)
    panel._input.setText("hello")
    panel.set_running("Connecting…")
    panel._on_submit()  # running -> must be ignored
    assert sent == [], "Enter while running must not start a second ask"
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    panel._input.setText("again")
    panel._on_submit()
    assert sent == ["again"]
    panel.close()


def test_app_double_send_blocked(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("first question")
        assert ask_mock.call_count == 1
        shell._on_short_ask_send("second question")  # still running -> blocked
        assert ask_mock.call_count == 1, "no concurrent internal Claude CLI"
        shell.short_ask.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
        shell.short_ask.reset()


# -- 10/11. Esc running cancel / Esc idle close -----------------------------

def test_esc_running_cancels_in_place(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    app.processEvents()
    stopped = []
    panel.stop_requested.connect(lambda: stopped.append(True))
    QTest.keyClick(panel, Qt.Key_Escape)
    app.processEvents()
    assert stopped == [True], "Esc while running requests a stop"
    assert panel.isVisible(), "Esc while running does not close the panel"
    assert panel.is_cancelling()
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    assert not panel.running
    panel.close()


def test_esc_idle_closes(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    app.processEvents()
    QTest.keyClick(panel, Qt.Key_Escape)
    app.processEvents()
    assert not panel.isVisible(), "Esc while idle closes the panel"
    panel.close()


# -- 12. Stop only running --------------------------------------------------

def test_stop_only_running(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    stops = []
    opens = []
    panel.stop_requested.connect(lambda: stops.append(True))
    panel.open_agent_requested.connect(opens.append)
    panel._on_primary()  # not running -> Open agent, not stop
    assert stops == [] and opens == ["claude"]
    assert not panel._primary_btn.isVisible() or panel._primary_btn.text() != "Stop"
    panel.set_running("Connecting…")
    assert panel._primary_btn.text() == "Stop"
    panel._on_primary()  # running -> stop
    assert stops == [True]
    assert panel.is_cancelling()
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    panel.close()


# -- 13/14. Agent lock per turn + next-turn selection -----------------------

def test_agent_locked_during_turn(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("first")
        assert shell.short_ask.agent == "claude"
        assert ask_mock.call_args[0][0] == "claude"
        # switching the dock mid-turn must NOT change the running turn's agent
        shell.dock.select_agent("codex", emit_signal=True)
        assert shell.short_ask.agent == "claude"
        assert shell.short_ask.state == ShortTalkState.CONNECTING
        shell.short_ask.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
        shell.short_ask.reset()


def test_next_turn_uses_new_selected_agent(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("first")
        assert ask_mock.call_args[0][0] == "claude"
        shell._on_short_ask_finished("done", 0)  # turn completes (visible panel)
        assert shell.short_ask.state == ShortTalkState.COMPLETE
        shell.short_ask.dismiss()
        # next Ask adopts the newly selected agent
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        assert shell.short_ask.agent == "codex"
        assert "available" in shell.short_ask._status.text().lower()
        shell.short_ask.reset()


# -- 15/16. Resume / new session badge --------------------------------------

def test_resume_new_badge(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude", resume=False)
    assert panel._status.text() == "New session"
    panel.show_input("claude", resume=True)
    assert panel._status.text() == "Resuming session"
    assert panel._title.text() == "Ask Claude"
    panel.close()


def test_app_resume_indicator(shell) -> None:
    ws = shell.workspace_manager.current()
    shell.dock.select_agent("claude", emit_signal=True)
    shell.session_manager.clear("claude", ws)
    shell.short_ask.reset()
    shell._on_short_ask_requested()
    assert shell.short_ask._status.text() == "New session"
    shell.short_ask.dismiss()
    shell.session_manager.set("claude", ws, "sess-xyz")
    shell.short_ask.reset()
    shell._on_short_ask_requested()
    assert shell.short_ask._status.text() == "Resuming session"
    shell.short_ask.dismiss()


# -- 17/18. Long response truncation, bounded window -------------------------

def test_long_response_truncated(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    long_text = "word " * 2000
    panel.set_answer(long_text)
    assert panel.full_answer() == long_text, "full answer stays in memory"
    assert len(panel._output.text()) <= MAX_ANSWER_CHARS + 1
    assert panel._output.text().endswith("…")
    panel.show_done()
    assert panel._truncated
    assert panel._primary_btn.isVisible() and panel._primary_btn.text() == "Open Claude"
    panel.close()


def test_long_response_does_not_grow_window(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running()
    long_text = "word " * 2000
    for start in range(0, len(long_text), 50):
        _delta(panel, long_text[start:start + 50])
    app.processEvents()
    assert panel._output.height() <= MAX_ANSWER_HEIGHT
    assert len(panel._output.text()) <= MAX_ANSWER_CHARS + 1
    assert panel.height() < 500, "panel must stay compact for a long answer"
    panel.close()


# -- 19. Open Claude signal -------------------------------------------------

def test_open_agent_signal_not_continue(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_answer("x" * 900)
    panel.show_done()
    assert panel._truncated
    assert panel._primary_btn.text() == "Open Claude"
    assert "Continue" not in panel._primary_btn.text(), "label must not over-claim resume"
    opened = []
    panel.open_agent_requested.connect(opened.append)
    panel._on_primary()
    assert opened == ["claude"]
    panel.close()


# -- 20. ChatGPT no backend -------------------------------------------------

def test_chatgpt_no_backend(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("chatgpt", emit_signal=True)
        shell._on_short_ask_requested()
        assert ask_mock.call_count == 0
        assert shell.short_ask._agent == "chatgpt"
        assert shell.short_ask.state == ShortTalkState.READY, "notice is not an error"
        assert "isn't available" in shell.short_ask._status.text().lower()
        assert not shell.short_ask._input.isEnabled()


# -- 21/22. Long-task recommendation + Ask anyway ----------------------------

def test_long_task_recommendation(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell.short_ask.show_input("claude")
        shell._on_short_ask_send("explain how to update this module")
        assert ask_mock.call_count == 0, "complex prompt must not auto-run"
        assert shell.short_ask._status.text() == "This looks like a longer task. Open Claude instead?"
        assert shell.short_ask._primary_btn.text() == "Open Claude"
        assert shell.short_ask._secondary_btn.text() == "Ask anyway"
        assert shell.short_ask._secondary_btn.isVisible()
        shell.short_ask.reset()


def test_ask_anyway_continues(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell.short_ask.show_input("claude")
        shell._on_short_ask_send("explain how to update this module")
        assert ask_mock.call_count == 0
        shell._on_short_ask_force_send("explain how to update this module")  # Ask anyway bypass
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"
        assert shell.short_ask.running
        shell.short_ask.on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
        shell.short_ask.reset()


def test_ask_anyway_signal_preserves_prompt(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_recommendation("claude", "longer task", open_label="Open Claude", prompt="fix this bug")
    forced = []
    panel.force_send_requested.connect(forced.append)
    panel._on_secondary()
    assert forced == ["fix this bug"], "Ask anyway must re-send the same prompt"
    panel.close()


# -- 23/24/25. Slow-request feedback, never auto-cancel ----------------------

def test_slow_shows_still_working(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel._on_slow_timeout()
    assert panel._status.text() == "Still working…"
    assert panel.state == ShortTalkState.CONNECTING
    assert panel.running
    panel.close()


def test_very_slow_shows_open_action(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel._on_slow_timeout()
    panel._on_very_slow_timeout()
    assert panel._secondary_btn.isVisible()
    assert panel._secondary_btn.text() == "Open Claude"
    assert panel._secondary_action == "open_agent"
    opened = []
    panel.open_agent_requested.connect(opened.append)
    panel._on_secondary()  # user picks the light action
    assert opened == ["claude"]
    assert panel.running, "opening native agent does not cancel the running turn"
    panel.close()


def test_slow_request_never_autocancels(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    stops = []
    panel.stop_requested.connect(lambda: stops.append(True))
    panel._on_slow_timeout()
    panel._on_very_slow_timeout()
    assert stops == []
    assert panel.running
    assert panel.state == ShortTalkState.CONNECTING
    panel.close()


# -- 26/27. PermissionCard hides but never cancels ---------------------------

def _coordinator_with_panel(app, *, business: bool = False):
    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    permission_card = PermissionCard()
    short_ask = ShortAskPanel()
    kwargs = dict(permission_card=permission_card, short_ask=short_ask)
    if business:
        wm = WorkspaceManager()
        sm = SessionManager()
        settings = SettingsManager()
        kwargs.update(
            workspace_popover=WorkspacePopover(wm),
            session_popover=SessionPopover(sm, wm),
            settings_popover=SettingsPopover(settings),
            settings_manager=settings,
        )
    coordinator = OverlayCoordinator(pet, dock, bubble, toolbar, **kwargs)
    return pet, coordinator, short_ask, permission_card


def test_permission_card_hides_not_cancels(app: QApplication) -> None:
    pet, coordinator, short_ask, permission_card = _coordinator_with_panel(app)
    try:
        coordinator.show_shell()
        app.processEvents()
        short_ask.show_input("claude")
        short_ask.set_running("Connecting…")
        coordinator.show_short_ask()
        app.processEvents()
        assert short_ask.isVisible()
        coordinator.on_agent_state("claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook"))
        app.processEvents()
        assert permission_card.isVisible()
        assert not short_ask.isVisible(), "PermissionCard hides Short Ask UI"
        assert short_ask.running, "PermissionCard must NOT cancel the internal ask"
        assert short_ask.state == ShortTalkState.CONNECTING
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


# -- 28/29. Hidden turn completes, result preserved on reopen ----------------

def test_hidden_turn_completes_and_reopen_shows_result(app: QApplication) -> None:
    pet, coordinator, short_ask, permission_card = _coordinator_with_panel(app)
    try:
        coordinator.show_shell()
        app.processEvents()
        short_ask.show_input("claude")
        short_ask.set_running("Connecting…")
        coordinator.show_short_ask()
        app.processEvents()
        coordinator.on_agent_state("claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook"))
        app.processEvents()
        assert not short_ask.isVisible() and short_ask.running
        # background turn completes while hidden
        _delta(short_ask, "你好")
        short_ask.on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text="你好世界"))
        assert short_ask.state == ShortTalkState.COMPLETE
        assert short_ask.full_answer() == "你好世界"
        assert not short_ask.isVisible(), "must not pop up over the PermissionCard"
        assert permission_card.isVisible()
        # permission clears
        coordinator.on_agent_state("claude", AgentState("claude", LifecycleState.IDLE, 2000, "hook"))
        app.processEvents()
        assert not permission_card.isVisible()
        # user reopens -> sees the latest result
        coordinator.show_short_ask()
        app.processEvents()
        assert short_ask.isVisible()
        assert short_ask._output.text() == "你好世界"
        assert short_ask.state == ShortTalkState.COMPLETE
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


# -- 30. Business Popover policy --------------------------------------------

def test_business_popover_policy(app: QApplication) -> None:
    pet, coordinator, short_ask, _permission = _coordinator_with_panel(app, business=True)
    try:
        coordinator.show_shell()
        app.processEvents()
        # running turn: popover hides it but keeps the turn state
        short_ask.show_input("claude")
        short_ask.set_running("Connecting…")
        coordinator.show_short_ask()
        app.processEvents()
        coordinator._show_context("sessions")
        app.processEvents()
        assert not short_ask.isVisible(), "business popover outranks Short Ask"
        assert short_ask.running, "running turn must survive a business popover"
        short_ask.on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text="answer"))
        assert short_ask.state == ShortTalkState.COMPLETE
        # idle panel: popover closes it
        short_ask.reset()
        short_ask.show_input("claude")
        coordinator.show_short_ask()
        app.processEvents()
        coordinator._show_context("sessions")
        app.processEvents()
        assert not short_ask.isVisible()
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


# -- 31. Telemetry duration display, no leak --------------------------------

def test_telemetry_duration_no_leak(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running()
    secret_answer = "secret_payload_xyz_never_displayed"
    _delta(panel, "ok")
    t = AskTelemetry(agent="claude", workspace="firefly", created_at=1000)
    t.milestone_t0 = 1000
    t.milestone_t5 = 6700
    t.milestone_t6 = 7700
    panel.on_telemetry(t)
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=secret_answer))
    assert panel.state == ShortTalkState.COMPLETE
    assert "6.7s" in panel._status.text()
    assert "Done" in panel._status.text()
    exposed = panel._status.text() + (panel._status.toolTip() or "")
    # Duration UI shows only timing; it must not echo the answer content or
    # telemetry field names for keys/sessions.
    assert secret_answer not in exposed
    assert "prompt" not in exposed.lower()
    for field_name in ("api_key", "token", "session_id", "native-session"):
        assert field_name not in exposed.lower(), f"latency display leaked field {field_name}"
    panel.close()


# -- 32. UI never parses provider raw event names ---------------------------

def test_ui_no_provider_event_names() -> None:
    for path in UI_FILES:
        source = path.read_text(encoding="utf-8")
        for token in PROVIDER_EVENT_TOKENS:
            assert token not in source, f"{path.name} must not reference provider event {token!r}"


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    with tempfile.TemporaryDirectory() as td:
        test_ready_to_connecting(app)
        test_started_keeps_connecting(app)
        test_session_marks_session_ready(app)
        test_status_thinking_ui(app)
        test_first_delta_immediately_visible(app)
        test_deltas_appended_in_order(app)
        test_final_complete(app)
        test_error_category_copy(app)
        test_cancelled_state(app)
        test_double_send_blocked(app)
        test_esc_running_cancels_in_place(app)
        test_esc_idle_closes(app)
        test_stop_only_running(app)
        test_resume_new_badge(app)
        test_long_response_truncated(app)
        test_long_response_does_not_grow_window(app)
        test_open_agent_signal_not_continue(app)
        test_ask_anyway_signal_preserves_prompt(app)
        test_slow_shows_still_working(app)
        test_very_slow_shows_open_action(app)
        test_slow_request_never_autocancels(app)
        test_permission_card_hides_not_cancels(app)
        test_hidden_turn_completes_and_reopen_shows_result(app)
        test_business_popover_policy(app)
        test_telemetry_duration_no_leak(app)
        test_ui_no_provider_event_names()

        shell = VisualShell(None)
        try:
            test_app_double_send_blocked(shell)
            test_agent_locked_during_turn(shell)
            test_next_turn_uses_new_selected_agent(shell)
            test_app_resume_indicator(shell)
            test_chatgpt_no_backend(shell)
            test_long_task_recommendation(shell)
            test_ask_anyway_continues(shell)
        finally:
            shell.shutdown()

    print("Phase 8C.3 short talk UX tests passed.")


if __name__ == "__main__":
    main()
