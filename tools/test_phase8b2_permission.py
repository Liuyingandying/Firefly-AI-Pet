"""Phase 8B.2 PermissionCard + transient policy tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import state_broker
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.models import AgentState, LifecycleState
from core.session_manager import SessionManager
from core.workspace_manager import WorkspaceManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
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


def _write_source(sources_dir: Path, agent: str, state: str, ts: int) -> None:
    path = sources_dir / f"{agent}.json"
    path.write_text(
        json.dumps({"agent": agent, "source": "hook", "state": state, "timestamp": ts}),
        encoding="utf-8",
    )


def _push_agent_state(coordinator, dock, agent_id, state_name, ts):
    agent_state = AgentState(agent_id, LifecycleState(state_name), ts, "hook")
    dock.set_agent_state(agent_id, state_name)
    coordinator.on_agent_state(agent_id, agent_state)
    return agent_state


def _make_shell(root: Path):
    ws = root / "ws"
    ws.mkdir()
    store = WorkspaceStore(root / "config" / "ui_settings.json", ws)
    wm = WorkspaceManager(store)
    sm = SessionManager()
    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    workspace_popover = WorkspacePopover(wm)
    session_popover = SessionPopover(sm, wm)
    card = PermissionCard()
    coordinator = OverlayCoordinator(
        pet,
        dock,
        bubble,
        toolbar,
        workspace_popover=workspace_popover,
        session_popover=session_popover,
        permission_card=card,
    )
    return pet, dock, bubble, toolbar, workspace_popover, session_popover, card, coordinator


def test_broker_priority(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sources = root / "sources"
    sources.mkdir()
    _write_source(sources, "claude", "waiting", 1000)
    _write_source(sources, "codex", "working", 2000)

    resolved = state_broker.resolve(now_ms=3000, sources_dir=sources)
    assert resolved["state"] == "waiting"
    assert resolved["agent"] == "claude"


def test_permission_card(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()

    push = lambda agent, st, ts: _push_agent_state(coordinator, dock, agent, st, ts)

    # 1. claude waiting -> card visible
    push("claude", "waiting", 1000)
    app.processEvents()
    assert card.isVisible()
    assert card._title.text() == "Claude needs your approval"

    # 14. dock waiting state stays correct
    assert dock.state_for("claude") == "waiting"

    # 3. waiting -> idle -> hidden
    push("claude", "idle", 2000)
    app.processEvents()
    assert not card.isVisible()
    assert dock.state_for("claude") == "idle"

    # 2. codex waiting -> visible
    push("codex", "waiting", 3000)
    app.processEvents()
    assert card.isVisible()
    assert card._title.text() == "Codex needs your approval"

    # 4. waiting -> success -> hidden
    push("codex", "success", 4000)
    app.processEvents()
    assert not card.isVisible()

    # 5. both waiting
    push("claude", "waiting", 5000)
    push("codex", "waiting", 6000)
    app.processEvents()
    assert card.isVisible()
    assert card._title.text() == "2 agents need approval"
    assert card._agents_label.isVisible()

    # 6. one resolves, other stays
    push("claude", "working", 7000)
    app.processEvents()
    assert card.isVisible()
    assert card._title.text() == "Codex needs your approval"

    # 7. both resolve -> hidden
    push("codex", "idle", 8000)
    app.processEvents()
    assert not card.isVisible()

    # 8. card appears closes SpeechBubble
    coordinator.toggle_bubble()
    app.processEvents()
    assert bubble.isVisible()
    push("claude", "waiting", 9000)
    app.processEvents()
    assert card.isVisible()
    assert not bubble.isVisible()

    # 9. card appears closes WorkspacePopover
    push("claude", "idle", 10000)
    coordinator._show_context("workspace")
    app.processEvents()
    assert wp.isVisible()
    push("claude", "waiting", 11000)
    app.processEvents()
    assert card.isVisible()
    assert not wp.isVisible()

    # 10. card appears closes SessionPopover
    push("claude", "idle", 12000)
    coordinator._show_context("sessions")
    app.processEvents()
    assert sp.isVisible()
    push("codex", "waiting", 13000)
    app.processEvents()
    assert card.isVisible()
    assert not sp.isVisible()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


def test_permission_view(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    viewed = []
    coordinator.permission_view_requested.connect(viewed.append)
    coordinator.show_shell()
    app.processEvents()

    # both waiting; codex is newer -> primary
    _push_agent_state(coordinator, dock, "claude", "waiting", 1000)
    _push_agent_state(coordinator, dock, "codex", "waiting", 2000)
    app.processEvents()
    assert card.isVisible()
    assert card._primary_agent == "codex"

    before = dict(coordinator._waiting)

    # 11. View emits correct agent_id
    QTest.mouseClick(card._view_btn, Qt.LeftButton, pos=card._view_btn.rect().center())
    app.processEvents()
    assert viewed == ["codex"]

    # 12. View does not change waiting state
    assert coordinator._waiting == before
    assert card.isVisible()

    # 13. View does not auto-approve (card still in waiting state)
    assert card._title.text() == "2 agents need approval"
    assert not card._view_btn.isEnabled()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


def test_overlay_ux(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()

    # bubble is visible after show_shell
    assert bubble.isVisible()

    # opening a business popover hides the bubble
    coordinator._show_context("workspace")
    app.processEvents()
    assert wp.isVisible()
    assert not bubble.isVisible()

    # closing the popover does NOT auto re-show the bubble
    wp.dismiss()
    app.processEvents()
    assert not wp.isVisible()
    assert not bubble.isVisible()

    # normal pet click toggle still works
    coordinator.toggle_bubble()
    app.processEvents()
    assert bubble.isVisible()
    coordinator.toggle_bubble()
    app.processEvents()
    assert not bubble.isVisible()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        test_broker_priority(base / "broker")
        test_permission_card(app, base / "lifecycle")
        test_permission_view(app, base / "view")
        test_overlay_ux(app, base / "ux")
    print("Phase 8B.2 permission tests passed.")


if __name__ == "__main__":
    main()
