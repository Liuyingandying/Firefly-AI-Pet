"""Phase 8B.1 SessionManager + SessionPopover tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.session_manager import KIND_SESSION, KIND_THREAD, SessionManager, workspace_key
from core.workspace_manager import WorkspaceManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.pet_overlay import PetOverlay
from ui.process_launcher import QuickAskRunner
from ui.quick_chat_protocol import SessionRegistry, build_claude_args, build_codex_args
from ui.session_popover import SessionPopover
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar
from ui.workspace_popover import WorkspacePopover
from ui.workspace_store import WorkspaceStore


STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def test_session_manager_core(root: Path) -> None:
    a = root / "a"
    b = root / "b"
    a.mkdir()
    b.mkdir()
    mgr = SessionManager()

    # 1. claude session set/get
    ref = mgr.set("claude", a, "sess-claude-a")
    assert ref is not None and ref.agent_id == "claude" and ref.kind == KIND_SESSION
    assert mgr.get_native_id("claude", a) == "sess-claude-a"
    assert mgr.has("claude", a)

    # 2. codex thread set/get (native thread kind)
    ref = mgr.set("codex", a, "thr-codex-a")
    assert ref is not None and ref.kind == KIND_THREAD
    assert mgr.get_native_id("codex", a) == "thr-codex-a"

    # 3. same workspace two agents don't conflict
    assert mgr.get_native_id("claude", a) == "sess-claude-a"
    assert mgr.get_native_id("codex", a) == "thr-codex-a"

    # 4. different workspace no session cross-talk
    assert mgr.get_native_id("claude", b) is None
    mgr.set("claude", b, "sess-claude-b")
    assert mgr.get_native_id("claude", a) == "sess-claude-a"
    assert mgr.get_native_id("claude", b) == "sess-claude-b"

    # 5. normalized workspace key
    assert mgr.get_native_id("claude", a / ".") == "sess-claude-a"
    assert workspace_key("CLAUDE", a) == workspace_key("claude", a)

    # 6. clear a single session
    mgr.clear("claude", a)
    assert mgr.get_native_id("claude", a) is None

    # 7. clear one agent doesn't affect another
    assert mgr.get_native_id("codex", a) == "thr-codex-a"

    # 8. workspace switch reads correct session
    refs = mgr.for_workspace(b)
    assert refs.get("claude") is not None
    assert refs["claude"].native_session_id == "sess-claude-b"
    assert "codex" not in refs

    # 9. malformed / invalid session id does not crash
    assert mgr.set("claude", a, "") is None
    assert mgr.set("claude", a, None) is None
    assert mgr.set("claude", a, 123) is None  # type: ignore[arg-type]
    assert mgr.set("claude", a, "   ") is None
    assert mgr.get_native_id("claude", a) is None

    # 10. callback dedup
    events = []
    mgr.connect(events.append)
    mgr.set("codex", a, "thr-1")
    mgr.set("codex", a, "thr-1")  # duplicate -> no new event
    assert len(events) == 1
    assert events[0].ref is not None and events[0].ref.native_session_id == "thr-1"
    mgr.set("codex", a, "thr-2")  # change -> new event
    assert len(events) == 2
    assert events[1].ref is not None and events[1].ref.native_session_id == "thr-2"
    mgr.clear("codex", a)  # clear -> event with ref None
    assert len(events) == 3
    assert events[2].ref is None and events[2].agent_id == "codex"


def test_runner_migration(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir()
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)

    runner._agent = "codex"
    runner._workspace = ws
    runner._persistent = True

    # 11. codex parsed thread id written to SessionManager
    runner._consume_json_line(json.dumps({"type": "thread.started", "thread_id": "thr-9"}))
    assert mgr.get_native_id("codex", ws) == "thr-9"
    assert mgr.get("codex", ws).kind == KIND_THREAD

    # 12. claude parsed session id written to SessionManager
    runner._agent = "claude"
    runner._consume_json_line(json.dumps({
        "type": "stream_event",
        "session_id": "sess-9",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
    }))
    assert mgr.get_native_id("claude", ws) == "sess-9"
    assert mgr.get("claude", ws).kind == KIND_SESSION

    # 13. resume args still use the correct native id
    codex_args = build_codex_args("follow", ws, session_id="thr-9", persistent=True)
    assert codex_args[-3:] == ["resume", "thr-9", "follow"]
    claude_args = build_claude_args("again", session_id="sess-9", persistent=True)
    assert "--resume" in claude_args and "sess-9" in claude_args

    # 14. New clears the correct key (codex only, claude untouched)
    runner.clear_session("codex", ws)
    assert mgr.get_native_id("codex", ws) is None
    assert mgr.get_native_id("claude", ws) == "sess-9"

    # 15. Phase 7B old behavior stays compatible
    legacy = SessionRegistry()
    legacy.set("codex", ws, "old-thr")
    assert legacy.get("codex", ws) == "old-thr"
    assert runner.session_id("claude", ws) == "sess-9"

    runner.shutdown()


def test_session_popover_ui(root: Path, app: QApplication) -> None:
    ws_a = root / "alpha"
    ws_b = root / "beta"
    ws_a.mkdir()
    ws_b.mkdir()

    store = WorkspaceStore(root / "config" / "ui_settings.json", ws_a)
    wm = WorkspaceManager(store)
    sm = SessionManager()
    sm.set("claude", ws_a, "sess-a")
    sm.set("codex", ws_b, "thr-b")

    popover = SessionPopover(sm, wm)

    # 16. open
    popover.show_at(QPoint(0, 0))
    app.processEvents()
    assert popover.isVisible()

    # 20. has session / no session per current workspace (ws_a)
    assert popover.agent_session_text("claude") == "Current session"
    assert popover.agent_has_continue("claude") is True
    assert popover.agent_session_text("codex") == "No active session"
    assert popover.agent_has_continue("codex") is False

    # 21. claude / codex sections independent
    assert popover.agent_session_text("claude") != popover.agent_session_text("codex")

    # 22. chatgpt does not fabricate a managed session
    assert popover.agent_session_text("chatgpt") == "No managed session"
    assert popover.agent_has_continue("chatgpt") is False
    assert popover.agent_has_new("chatgpt") is False

    # 17. Esc closes
    QTest.keyClick(popover, Qt.Key_Escape)
    app.processEvents()
    assert not popover.isVisible()

    # 19. workspace change -> UI refresh
    popover.show_at(QPoint(0, 0))
    app.processEvents()
    assert popover.isVisible()
    wm.set_current(ws_b)
    app.processEvents()
    assert popover.agent_session_text("codex") == "Current thread"
    assert popover.agent_has_continue("codex") is True
    assert popover.agent_session_text("claude") == "No active session"

    popover.dismiss()
    app.processEvents()


def test_coordinator_outside_click(root: Path, app: QApplication) -> None:
    wsc = root / "wsc"
    wsc.mkdir()
    store = WorkspaceStore(root / "config_c" / "ui_settings.json", wsc)
    wm = WorkspaceManager(store)
    sm = SessionManager()
    sm.set("claude", wsc, "sess-x")

    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    workspace_popover = WorkspacePopover(wm)
    session_popover = SessionPopover(sm, wm)
    coordinator = OverlayCoordinator(
        pet,
        dock,
        bubble,
        toolbar,
        workspace_popover=workspace_popover,
        session_popover=session_popover,
    )

    try:
        coordinator.show_shell()
        app.processEvents()

        coordinator._show_context("sessions")
        app.processEvents()
        assert session_popover.isVisible()

        # 18. outside click closes (eventFilter dismissal contract)
        press = QMouseEvent(
            QEvent.MouseButtonPress,
            QPointF(0.0, 0.0),
            QPointF(0.0, 0.0),
            QPointF(100000.0, 100000.0),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        coordinator.eventFilter(None, press)
        app.processEvents()
        assert not session_popover.isVisible()
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_session_manager_core(root)
        test_runner_migration(root, app)
        test_session_popover_ui(root, app)
        test_coordinator_outside_click(root, app)
    print("Phase 8B.1 session tests passed.")


if __name__ == "__main__":
    main()
