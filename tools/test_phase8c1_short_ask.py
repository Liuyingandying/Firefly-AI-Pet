"""Phase 8C.1 Short Ask + hook isolation + telemetry tests.

Covers the 15 required scenarios: hook isolation (internal ask never pollutes
the external Claude lifecycle), external hooks untouched, per-workspace session
isolation, first-ask-creates / second-ask-resumes, cancel-then-ask-again,
T0–T6 telemetry ordering and safety, no mis-fired notification on first
snapshot, internal ask never triggers PermissionCard, ChatGPT selects no fake
backend, bounded long-response UI, Esc cancel/close, and overlay priority
(business popovers and PermissionCard stay above Short Ask).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
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

from core.models import AgentState, LifecycleState
from core.notification_manager import NotificationManager
from core.quick_ask_metrics import (
    AskTelemetry,
    MetricsWriter,
    session_hash_prefix,
    workspace_token,
)
from core.session_manager import SessionManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
from ui.process_launcher import QuickAskRunner, _claude_settings_env
from ui.quick_chat_protocol import build_claude_args, classify_short_ask
from ui.short_ask import MAX_ANSWER_CHARS, AskPill, ShortAskPanel
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar

STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def _ws(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- 1/2. Hook isolation args ---------------------------------------------

def test_hook_isolation_args() -> None:
    isolated = build_claude_args("hello", isolated=True)
    plain = build_claude_args("hello", isolated=False)
    assert "--safe-mode" in isolated, "internal ask must carry --safe-mode"
    assert "--safe-mode" not in plain, "external ask must NOT carry --safe-mode"
    # internal ask stays read-only + short.
    assert "--permission-mode" in isolated and "plan" in isolated
    assert "--resume" not in isolated
    # resume + isolation compose (second ask).
    resumed = build_claude_args("again", session_id="sid-1", persistent=True, isolated=True)
    assert "--safe-mode" in resumed and "--resume" in resumed and "sid-1" in resumed


def test_external_hooks_untouched() -> None:
    user_settings = Path.home() / ".claude" / "settings.json"
    if user_settings.exists():
        data = json.loads(user_settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert isinstance(hooks, dict) and hooks, "user global hooks must remain configured"
    # The isolated env reader is read-only: it returns config, never writes.
    env = _claude_settings_env()
    assert isinstance(env, dict)
    if env:
        assert "ANTHROPIC_BASE_URL" in env or "ANTHROPIC_AUTH_TOKEN" in env


# -- 3/4/5. Session ownership ---------------------------------------------

def test_session_workspace_isolation(root: Path) -> None:
    a = _ws(root, "ws-a")
    b = _ws(root, "ws-b")
    mgr = SessionManager()
    mgr.set("claude", a, "sess-a")
    mgr.set("claude", b, "sess-b")
    assert mgr.get_native_id("claude", a) == "sess-a"
    assert mgr.get_native_id("claude", b) == "sess-b"
    assert mgr.get_native_id("claude", root / "ws-a" / ".") == "sess-a"


def test_first_ask_creates_session(root: Path, app: QApplication) -> None:
    ws = _ws(root, "ws-c")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._consume_json_line(json.dumps({
        "type": "stream_event",
        "session_id": "new-session-1",
        "event": {"type": "message_start"},
    }))
    assert mgr.get_native_id("claude", ws) == "new-session-1"
    # A later ask reuses it via --resume.
    args = build_claude_args("follow", session_id=mgr.get_native_id("claude", ws), persistent=True, isolated=True)
    assert "--resume" in args and "new-session-1" in args
    assert "--safe-mode" in args


# -- 6. Cancel then ask again ---------------------------------------------

def test_cancel_then_ask_again(root: Path, app: QApplication) -> None:
    ws = _ws(root, "ws-d")
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    # Non-running stop is a safe no-op.
    runner.stop()
    runner.shutdown()


def test_app_cancel_then_ask_again(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.short_ask.show_input("claude")
        shell._on_short_ask_send("explain this error")
        assert shell.short_ask.running
        assert ask_mock.call_count == 1
        shell._on_short_ask_stop()
        # Stop is not instant: the turn stays alive in Cancelling… until the
        # transport has actually completed (Phase 8C.3 section 16).
        assert shell.short_ask.running
        assert shell.short_ask.is_cancelling()
        shell._on_short_ask_finished("", 1)  # killed process finishes
        assert not shell.short_ask.running
        assert shell.short_ask.state.value == "cancelled"
        shell.short_ask.show_input("claude")
        shell._on_short_ask_send("and now this")
        assert shell.short_ask.running
        assert ask_mock.call_count == 2


# -- 7/8. Telemetry -------------------------------------------------------

def test_telemetry_order_and_derived() -> None:
    t = AskTelemetry(agent="claude", workspace="firefly", created_at=1000)
    t.milestone_t0 = 1000
    t.milestone_t1 = 1110
    t.milestone_t2 = 1210
    t.milestone_t3 = 1215
    t.milestone_t4 = 1300
    t.milestone_t5 = 1900
    t.milestone_t6 = 2200
    assert t.first_event_ms() == 210
    assert t.first_text_ms() == 900
    assert t.total_ms() == 1200
    assert t.dispatch_ms() == t.total_ms()
    assert t.startup_ms() == 110
    # Order invariant: t0 <= t1 <= t2 <= ... <= t6.
    seq = [t.milestone_t0, t.milestone_t1, t.milestone_t2, t.milestone_t3, t.milestone_t4, t.milestone_t5, t.milestone_t6]
    assert seq == sorted(seq)


def test_telemetry_safe_fields() -> None:
    t = AskTelemetry(
        agent="claude",
        workspace="firefly",
        created_at=1,
        session_exists=True,
        session_hash=session_hash_prefix("native-session-abc"),
        isolated=True,
    )
    payload = t.to_payload()
    for key in payload:
        assert key not in {"prompt", "response", "api_key", "token", "env", "password"}
    assert t.validate_safe() == []
    assert len(t.session_hash or "") <= 12
    assert "secret prompt" not in json.dumps(payload)
    assert "secret answer" not in json.dumps(payload)


def test_telemetry_writer_ring_buffer() -> None:
    with tempfile.TemporaryDirectory() as td:
        writer = MetricsWriter(td, ring_size=3)
        for i in range(5):
            t = AskTelemetry(agent="claude", workspace="w", created_at=i)
            t.milestone_t0 = i
            t.milestone_t6 = i + 10
            writer.record(t)
        history = json.loads((Path(td) / "quick_ask_history.json").read_text(encoding="utf-8"))
        assert len(history) == 3, "ring buffer must stay bounded"
        assert (Path(td) / "quick_ask_latest.json").exists()
        assert all("created_at" in item for item in history)


# -- 9. First snapshot no mis-fired notification --------------------------

def test_first_snapshot_no_notification() -> None:
    nm = NotificationManager()
    events = []
    nm.connect(events.append)
    nm.on_agent_state(AgentState("claude", LifecycleState.SUCCESS, 1000, "hook"))
    assert events == [], "first snapshot must only baseline, not notify"
    nm.on_agent_state(AgentState("claude", LifecycleState.SUCCESS, 1200, "hook"))
    assert events == [], "same-kind repeat inside an episode is suppressed"
    nm.on_agent_state(AgentState("claude", LifecycleState.ERROR, 2000, "hook"))
    assert len(events) == 1 and events[0].kind == "error"


# -- 10. Internal ask never triggers PermissionCard -----------------------

def test_internal_ask_no_permission_card(shell) -> None:
    shell.short_ask.reset()  # clear any turn left running by an earlier shared-shell test
    with patch.object(shell.quick_ask, "ask", return_value=True):
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("summarize this workspace")
        # Internal asks write no source state, so the coordinator never sees
        # a "waiting" lifecycle -> PermissionCard stays dormant.
        assert shell.coordinator._waiting == {}
        assert shell.permission_card is not None and not shell.permission_card.isVisible()


# -- 11. ChatGPT selected -> no fake backend ------------------------------

def test_chatgpt_no_fake_backend(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("chatgpt", emit_signal=True)
        shell._on_short_ask_requested()
        assert ask_mock.call_count == 0, "ChatGPT must not send any backend"
        assert shell.short_ask._agent == "chatgpt"
        assert "isn't available" in shell.short_ask._status.text().lower()
        assert not shell.short_ask._input.isEnabled()


def test_codex_notice_no_online_call(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        assert ask_mock.call_count == 0, "Codex online calls must stay 0 this phase"
        assert shell.short_ask._agent == "codex"


# -- Complex prompt steering ----------------------------------------------

def test_complex_prompt_recommends_native(shell) -> None:
    shell.short_ask.reset()
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell.short_ask.show_input("claude")
        shell._on_short_ask_send("explain how to update this module")
        assert ask_mock.call_count == 0, "complex prompt must not auto-run"
        assert shell.short_ask._primary_btn.text() == "Open Claude"
        assert shell.short_ask._secondary_btn.isVisible()


def test_classify_short_ask() -> None:
    assert classify_short_ask("explain this error") == "simple"
    assert classify_short_ask("summarize this workspace") == "simple"
    assert classify_short_ask("fix this bug") == "complex"
    assert classify_short_ask("write a test for that") == "complex"


# -- 12. Long-response UI stays bounded -----------------------------------

def test_long_response_bounded(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    long_text = "word " * 2000  # ~10k chars
    panel.set_answer(long_text)
    assert panel._full_answer == long_text, "full answer stays in memory"
    assert len(panel._output.text()) <= MAX_ANSWER_CHARS + 1
    assert panel._output.text().endswith("…")
    panel.show_done()
    assert panel._truncated
    assert panel._primary_btn.isVisible() and panel._primary_btn.text() == "Open Claude"
    panel.close()


def test_short_response_not_truncated(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_answer("Short and sweet.")
    panel.show_done()
    assert not panel._truncated
    assert not panel._primary_btn.isVisible()
    panel.close()


# -- 13. Esc cancel / close -----------------------------------------------

def test_esc_cancel_and_close(app: QApplication) -> None:
    panel = ShortAskPanel()
    panel.show_input("claude")
    app.processEvents()
    assert panel.isVisible()
    QTest.keyClick(panel, Qt.Key_Escape)
    app.processEvents()
    assert not panel.isVisible(), "Esc while idle closes"

    panel.show_input("claude")
    panel.set_running("Ask Claude…")
    app.processEvents()
    stopped = []
    panel.stop_requested.connect(lambda: stopped.append(True))
    QTest.keyClick(panel, Qt.Key_Escape)
    app.processEvents()
    assert stopped == [True], "Esc while running requests a stop"
    # Esc while running cancels in place (Cancelling…) instead of closing.
    assert panel.isVisible(), "Esc while running does not close the panel"
    assert panel.is_cancelling()
    panel.on_cancelled()  # transport completes the cancel
    assert not panel.running
    panel.close()


# -- 14/15. Overlay priority ----------------------------------------------

def test_business_popover_priority(app: QApplication) -> None:
    from core.session_manager import SessionManager
    from core.settings_manager import SettingsManager
    from core.workspace_manager import WorkspaceManager
    from ui.session_popover import SessionPopover
    from ui.settings_popover import SettingsPopover
    from ui.workspace_popover import WorkspacePopover

    wm = WorkspaceManager()
    sm = SessionManager()
    settings = SettingsManager()
    workspace_popover = WorkspacePopover(wm)
    session_popover = SessionPopover(sm, wm)
    settings_popover = SettingsPopover(settings)
    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    permission_card = PermissionCard()
    short_ask = ShortAskPanel()
    coordinator = OverlayCoordinator(
        pet, dock, bubble, toolbar,
        workspace_popover=workspace_popover,
        session_popover=session_popover,
        settings_popover=settings_popover,
        settings_manager=settings,
        permission_card=permission_card,
        short_ask=short_ask,
    )
    try:
        coordinator.show_shell()
        app.processEvents()
        short_ask.show_input("claude")
        coordinator.show_short_ask()
        app.processEvents()
        assert short_ask.isVisible()
        coordinator._show_context("sessions")
        app.processEvents()
        assert not short_ask.isVisible(), "business popover outranks Short Ask"
        # Re-open, then settings popover also dismisses it.
        short_ask.show_input("claude")
        coordinator.show_short_ask()
        app.processEvents()
        coordinator._show_settings()
        app.processEvents()
        assert not short_ask.isVisible()
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


def test_permission_card_above_short_ask(app: QApplication) -> None:
    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    permission_card = PermissionCard()
    short_ask = ShortAskPanel()
    coordinator = OverlayCoordinator(
        pet, dock, bubble, toolbar,
        permission_card=permission_card,
        short_ask=short_ask,
    )
    try:
        coordinator.show_shell()
        app.processEvents()
        short_ask.show_input("claude")
        coordinator.show_short_ask()
        app.processEvents()
        assert short_ask.isVisible()
        # A real external Claude waiting episode raises PermissionCard.
        coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
        )
        app.processEvents()
        assert permission_card.isVisible()
        assert not short_ask.isVisible(), "PermissionCard outranks Short Ask"
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


# -- AskPill --------------------------------------------------------------

def test_ask_pill_signal(app: QApplication) -> None:
    pill = AskPill()
    clicks = []
    pill.ask_clicked.connect(lambda: clicks.append(True))
    pill.show()
    app.processEvents()
    QTest.mouseClick(pill, Qt.LeftButton, pos=pill.rect().center())
    app.processEvents()
    assert clicks == [True]
    pill.close()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_hook_isolation_args()
        test_external_hooks_untouched()
        test_session_workspace_isolation(root)
        test_first_ask_creates_session(root, app)
        test_cancel_then_ask_again(root, app)
        test_telemetry_order_and_derived()
        test_telemetry_safe_fields()
        test_telemetry_writer_ring_buffer()
        test_first_snapshot_no_notification()
        test_classify_short_ask()
        test_long_response_bounded(app)
        test_short_response_not_truncated(app)
        test_esc_cancel_and_close(app)
        test_business_popover_priority(app)
        test_permission_card_above_short_ask(app)
        test_ask_pill_signal(app)

        # App-level flows need a real shell; its SettingsManager reads config.
        shell = VisualShell(None)
        try:
            test_app_cancel_then_ask_again(shell)
            test_internal_ask_no_permission_card(shell)
            test_chatgpt_no_fake_backend(shell)
            test_codex_notice_no_online_call(shell)
            test_complex_prompt_recommends_native(shell)
        finally:
            shell.shutdown()

    print("Phase 8C.1 short ask tests passed.")


if __name__ == "__main__":
    main()
