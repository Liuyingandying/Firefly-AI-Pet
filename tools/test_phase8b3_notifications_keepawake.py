"""Phase 8B.3 NotificationManager + KeepAwakeService tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.keep_awake import KeepAwakeBackend, KeepAwakeService
from core.models import AgentState, LifecycleState
from core.notification_manager import NotificationEvent, NotificationManager
from core.session_manager import SessionManager
from core.state_monitor import StateMonitor
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


def _agent_state(agent: str, state: str, ts: int) -> AgentState:
    return AgentState(agent, LifecycleState(state), ts, "hook")


def _event(agent: str, kind: str, ts: int, priority: int | None = None) -> NotificationEvent:
    if kind == "error":
        title, message = "Claude ran into a problem", "Open the agent to check details."
    else:
        title, message = "Claude finished", "Everything looks good."
    if priority is None:
        priority = 6 if kind == "error" else 5
    return NotificationEvent(agent, kind, title, message, ts, priority)


def _write_source(sources_dir: Path, agent: str, state: str, ts: int) -> None:
    path = sources_dir / f"{agent}.json"
    path.write_text(
        json.dumps({"agent": agent, "source": "hook", "state": state, "timestamp": ts}),
        encoding="utf-8",
    )


def _push_agent_state(coordinator, dock, agent_id, state_name, ts):
    agent_state = _agent_state(agent_id, state_name, ts)
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


class FakeBackend:
    def __init__(self):
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> None:
        self.acquire_calls += 1

    def release(self) -> None:
        self.release_calls += 1


# -- NotificationManager (cases 1-9) -------------------------------------


def test_notification_manager(app: QApplication) -> None:
    # 1. initial idle is not a notification
    mgr = NotificationManager()
    events: list[NotificationEvent] = []
    mgr.connect(events.append)
    mgr.on_agent_state(_agent_state("claude", "idle", 1000))
    assert events == []

    # 2. initial success (historical snapshot) is not a notification
    mgr2 = NotificationManager()
    events2: list[NotificationEvent] = []
    mgr2.connect(events2.append)
    mgr2.on_agent_state(_agent_state("claude", "success", 1000))
    assert events2 == []

    # 3. working -> success notifies exactly once
    mgr3 = NotificationManager()
    events3: list[NotificationEvent] = []
    mgr3.connect(events3.append)
    mgr3.on_agent_state(_agent_state("claude", "working", 1000))
    mgr3.on_agent_state(_agent_state("claude", "success", 2000))
    assert len(events3) == 1
    assert events3[0].kind == "success"
    assert events3[0].agent_id == "claude"
    assert "finished" in events3[0].title.lower()

    # 4. thinking -> success notifies once
    mgr4 = NotificationManager()
    events4: list[NotificationEvent] = []
    mgr4.connect(events4.append)
    mgr4.on_agent_state(_agent_state("codex", "thinking", 1000))
    mgr4.on_agent_state(_agent_state("codex", "success", 2000))
    assert len(events4) == 1 and events4[0].kind == "success"

    # 5. repeated success snapshots do not re-notify
    mgr5 = NotificationManager()
    events5: list[NotificationEvent] = []
    mgr5.connect(events5.append)
    mgr5.on_agent_state(_agent_state("claude", "working", 1000))
    mgr5.on_agent_state(_agent_state("claude", "success", 2000))
    mgr5.on_agent_state(_agent_state("claude", "success", 3000))
    assert len(events5) == 1
    mgr5b = NotificationManager()
    events5b: list[NotificationEvent] = []
    mgr5b.connect(events5b.append)
    mgr5b.on_agent_state(_agent_state("codex", "success", 1000))
    mgr5b.on_agent_state(_agent_state("codex", "success", 2000))
    assert events5b == []

    # 6. working -> error notifies once
    mgr6 = NotificationManager()
    events6: list[NotificationEvent] = []
    mgr6.connect(events6.append)
    mgr6.on_agent_state(_agent_state("claude", "working", 1000))
    mgr6.on_agent_state(_agent_state("claude", "error", 2000))
    assert len(events6) == 1 and events6[0].kind == "error"

    # 7. waiting never emits a normal notification (PermissionCard owns it)
    mgr7 = NotificationManager()
    events7: list[NotificationEvent] = []
    mgr7.connect(events7.append)
    mgr7.on_agent_state(_agent_state("claude", "working", 1000))
    mgr7.on_agent_state(_agent_state("claude", "waiting", 2000))
    assert events7 == []

    # 8. idle is not a notification
    mgr8 = NotificationManager()
    events8: list[NotificationEvent] = []
    mgr8.connect(events8.append)
    mgr8.on_agent_state(_agent_state("claude", "working", 1000))
    mgr8.on_agent_state(_agent_state("claude", "idle", 2000))
    assert events8 == []

    # 9. sleeping is not a notification
    mgr9 = NotificationManager()
    events9: list[NotificationEvent] = []
    mgr9.connect(events9.append)
    mgr9.on_agent_state(_agent_state("claude", "working", 1000))
    mgr9.on_agent_state(_agent_state("claude", "sleeping", 2000))
    assert events9 == []

    # idle ends an episode, so a later success is a fresh notified episode
    mgrA = NotificationManager()
    eventsA: list[NotificationEvent] = []
    mgrA.connect(eventsA.append)
    mgrA.on_agent_state(_agent_state("claude", "working", 1000))
    mgrA.on_agent_state(_agent_state("claude", "success", 2000))
    mgrA.on_agent_state(_agent_state("claude", "idle", 3000))
    mgrA.on_agent_state(_agent_state("claude", "working", 4000))
    mgrA.on_agent_state(_agent_state("claude", "success", 5000))
    assert len(eventsA) == 2

    # chatgpt / manual are outside the notifiable set entirely
    mgrB = NotificationManager()
    eventsB: list[NotificationEvent] = []
    mgrB.connect(eventsB.append)
    mgrB.on_agent_state(_agent_state("chatgpt", "success", 1000))
    mgrB.on_agent_state(_agent_state("manual", "error", 2000))
    assert eventsB == []

    # success cooldown: a fresh success right after an error is held back
    mgrC = NotificationManager()
    eventsC: list[NotificationEvent] = []
    mgrC.connect(eventsC.append)
    mgrC.on_agent_state(_agent_state("claude", "working", 1000))
    mgrC.on_agent_state(_agent_state("claude", "error", 2000))
    assert len(eventsC) == 1 and eventsC[0].kind == "error"
    mgrC.on_agent_state(_agent_state("claude", "success", 2500))
    assert len(eventsC) == 1  # within 3s cooldown, suppressed


# -- Routing + transient priority (cases 10-12) --------------------------


def test_notification_routing(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()

    # 12. workspace popover open -> success deferred, not shown
    coordinator._show_context("workspace")
    app.processEvents()
    assert wp.isVisible()
    assert not bubble.isVisible()
    coordinator.on_notification(_event("codex", "success", 1000))
    app.processEvents()
    assert coordinator._pending_notification is not None
    assert coordinator._pending_notification.kind == "success"
    assert not bubble.isVisible()
    # dismiss -> flush shows the deferred success
    wp.dismiss()
    app.processEvents()
    assert coordinator._pending_notification is None
    assert bubble.isVisible()
    assert "finished" in bubble._text.text()

    # 10. error overrides a pending success
    coordinator.toggle_bubble()  # hide bubble again
    app.processEvents()
    coordinator._show_context("sessions")
    app.processEvents()
    assert sp.isVisible()
    coordinator.on_notification(_event("claude", "success", 2000))
    coordinator.on_notification(_event("claude", "error", 3000))
    app.processEvents()
    assert coordinator._pending_notification is not None
    assert coordinator._pending_notification.kind == "error"
    sp.dismiss()
    app.processEvents()
    assert bubble.isVisible()
    assert "problem" in bubble._text.text()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


def test_notification_permission_defer(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()

    # 11. PermissionCard visible -> normal notification never preempts it
    _push_agent_state(coordinator, dock, "claude", "waiting", 1000)
    app.processEvents()
    assert card.isVisible()
    assert not bubble.isVisible()
    coordinator.on_notification(_event("claude", "success", 2000))
    app.processEvents()
    assert coordinator._pending_notification is not None
    assert not bubble.isVisible()
    # waiting resolves -> card hides -> deferred notification flushes
    _push_agent_state(coordinator, dock, "claude", "idle", 3000)
    app.processEvents()
    assert not card.isVisible()
    assert bubble.isVisible()
    assert "finished" in bubble._text.text()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


def test_notification_bubble_auto_dismiss(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()
    coordinator.toggle_bubble()  # hide the greeting
    app.processEvents()
    assert not bubble.isVisible()

    # 13. notification appears...
    coordinator.on_notification(_event("claude", "success", 1000))
    app.processEvents()
    assert bubble.isVisible()
    assert "finished" in bubble._text.text()
    # ...auto-dismisses...
    bubble._auto_hide.timeout.emit()
    app.processEvents()
    assert not bubble.isVisible()
    # ...and a manual pet click restores the default greeting
    coordinator.toggle_bubble()
    app.processEvents()
    assert bubble.isVisible()
    assert "Firefly" in bubble._text.text()

    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


# -- KeepAwakeService (cases 14-24) --------------------------------------


def test_keep_awake(app: QApplication) -> None:
    backend = FakeBackend()
    svc = KeepAwakeService(backend=backend)

    # 14. claude working -> acquire
    svc.on_agent_state(_agent_state("claude", "working", 1))
    assert svc.is_active and backend.acquire_calls == 1

    # 15. codex thinking -> stays active, no duplicate acquire
    svc.on_agent_state(_agent_state("codex", "thinking", 2))
    assert svc.is_active and backend.acquire_calls == 1

    # 16. waiting keeps the workflow awake
    svc.on_agent_state(_agent_state("claude", "waiting", 3))
    assert svc.is_active and backend.acquire_calls == 1

    # 17. claude idle while codex working -> no release
    svc.on_agent_state(_agent_state("claude", "idle", 4))
    svc.on_agent_state(_agent_state("codex", "working", 5))
    assert svc.is_active and backend.release_calls == 0

    # 18. both idle -> release
    svc.on_agent_state(_agent_state("codex", "idle", 6))
    assert not svc.is_active and backend.release_calls == 1

    # 19. success/error are not busy states
    backend2 = FakeBackend()
    svc2 = KeepAwakeService(backend=backend2)
    svc2.on_agent_state(_agent_state("claude", "success", 1))
    svc2.on_agent_state(_agent_state("codex", "error", 2))
    assert not svc2.is_active and backend2.acquire_calls == 0

    # 20. repeated working does not re-acquire
    svc2.on_agent_state(_agent_state("claude", "working", 3))
    assert svc2.is_active and backend2.acquire_calls == 1
    svc2.on_agent_state(_agent_state("claude", "working", 4))
    assert backend2.acquire_calls == 1

    # 21. repeated idle does not re-release
    svc2.on_agent_state(_agent_state("claude", "idle", 5))
    assert backend2.release_calls == 1
    svc2.on_agent_state(_agent_state("claude", "idle", 6))
    assert backend2.release_calls == 1

    # 22. shutdown always releases
    backend3 = FakeBackend()
    svc3 = KeepAwakeService(backend=backend3)
    svc3.on_agent_state(_agent_state("claude", "working", 1))
    assert backend3.acquire_calls == 1
    svc3.shutdown()
    assert backend3.release_calls == 1 and not svc3.is_active
    svc3.shutdown()  # idempotent
    assert backend3.release_calls == 1

    # 23. non-Windows backend is a safe no-op (logic still tracks busy)
    backend4 = KeepAwakeBackend(platform="linux")
    assert backend4._fn is None
    svc4 = KeepAwakeService(backend=backend4)
    svc4.on_agent_state(_agent_state("claude", "working", 1))
    assert svc4.is_active
    svc4.shutdown()
    assert not svc4.is_active

    # 24. ChatGPT / manual never participate in the aggregate
    backend5 = FakeBackend()
    svc5 = KeepAwakeService(backend=backend5)
    svc5.on_agent_state(_agent_state("chatgpt", "working", 1))
    assert not svc5.is_active and backend5.acquire_calls == 0
    svc5.on_agent_state(_agent_state("manual", "working", 2))
    assert not svc5.is_active and backend5.acquire_calls == 0


def test_windows_backend_real(app: QApplication) -> None:
    """On real Windows, SetThreadExecutionState acquire+release must succeed."""
    if os.name != "nt":
        return
    backend = KeepAwakeBackend()
    assert backend._fn is not None
    svc = KeepAwakeService(backend=backend)
    svc.on_agent_state(_agent_state("claude", "working", 1))
    assert svc.is_active
    svc.shutdown()
    assert not svc.is_active


# -- End-to-end wiring smoke ---------------------------------------------


def test_end_to_end_wiring(app: QApplication, root: Path) -> None:
    """Mirror app.py: StateMonitor -> coordinator + NotificationManager + KeepAwake."""
    root.mkdir(parents=True, exist_ok=True)
    sources = root / "sources"
    sources.mkdir()
    pet, dock, bubble, toolbar, wp, sp, card, coordinator = _make_shell(root)
    coordinator.show_shell()
    app.processEvents()

    mgr = NotificationManager()
    mgr.connect(coordinator.on_notification)
    ka = KeepAwakeService(backend=FakeBackend())

    monitor = StateMonitor(sources, root / "state.json")

    def _on_agent_state(agent_id, state):
        coordinator.on_agent_state(agent_id, state)
        mgr.on_agent_state(state)
        ka.on_agent_state(state)

    monitor.agent_state_changed.connect(_on_agent_state)
    monitor.start()
    try:
        _write_source(sources, "claude", "working", 1000)
        monitor.poll_once(1000)
        app.processEvents()
        assert ka.is_active

        _write_source(sources, "claude", "success", 2000)
        monitor.poll_once(2000)
        app.processEvents()
        assert bubble.isVisible()
        assert "finished" in bubble._text.text()

        _write_source(sources, "claude", "idle", 3000)
        monitor.poll_once(3000)
        app.processEvents()
        assert not ka.is_active
    finally:
        monitor.stop()
        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        test_notification_manager(app)
        test_notification_routing(app, base / "routing")
        test_notification_permission_defer(app, base / "permission")
        test_notification_bubble_auto_dismiss(app, base / "bubble")
        test_keep_awake(app)
        test_windows_backend_real(app)
        test_end_to_end_wiring(app, base / "wiring")
    print("Phase 8B.3 notification + keep-awake tests passed.")


if __name__ == "__main__":
    main()
