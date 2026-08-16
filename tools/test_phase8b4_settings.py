"""Phase 8B.4 SettingsManager + SettingsPopover + wiring tests.

Covers the 30 required cases plus an end-to-end wiring smoke that mirrors
app.py (settings gate the NotificationManager and KeepAwakeService live).
"""

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

from core.keep_awake import KeepAwakeService
from core.models import AgentState, LifecycleState
from core.notification_manager import NotificationManager
from core.session_manager import SessionManager
from core.settings_manager import SettingsManager
from core.workspace_manager import WorkspaceManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
from ui.session_popover import SessionPopover
from ui.settings_popover import SettingsPopover
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


class FakeBackend:
    def __init__(self):
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> None:
        self.acquire_calls += 1

    def release(self) -> None:
        self.release_calls += 1


def _make_shell(root: Path, prefs_file: Path | None = None):
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
    if prefs_file is None:
        prefs_file = root / "config" / "pet_preferences.json"
    settings = SettingsManager(prefs_file)
    settings_popover = SettingsPopover(settings)
    coordinator = OverlayCoordinator(
        pet,
        dock,
        bubble,
        toolbar,
        workspace_popover=workspace_popover,
        session_popover=session_popover,
        permission_card=card,
        settings_popover=settings_popover,
        settings_manager=settings,
    )
    return (
        pet,
        dock,
        bubble,
        toolbar,
        workspace_popover,
        session_popover,
        card,
        settings,
        settings_popover,
        coordinator,
    )


def _teardown(coordinator, pet, app: QApplication) -> None:
    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()


# -- SettingsManager (cases 1-8) -----------------------------------------


def test_settings_manager_defaults_and_persistence(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    prefs = config / "pet_preferences.json"

    # 1-3 defaults
    mgr = SettingsManager(prefs)
    assert mgr.notifications_enabled is True
    assert mgr.keep_awake_enabled is True
    assert mgr.greeting_on_startup is True

    # 4 change persists
    mgr.set_notifications_enabled(False)
    assert prefs.exists()
    data = json.loads(prefs.read_text(encoding="utf-8"))
    assert data["notifications_enabled"] is False

    # 5 reload keeps the change
    reloaded = SettingsManager(prefs)
    assert reloaded.notifications_enabled is False
    assert reloaded.keep_awake_enabled is True
    assert reloaded.greeting_on_startup is True

    # 6 malformed config falls back to defaults without crashing
    prefs.write_text("{ not valid json !!", encoding="utf-8")
    broken = SettingsManager(prefs)
    assert broken.notifications_enabled is True
    assert broken.keep_awake_enabled is True
    assert broken.greeting_on_startup is True
    # a valid write after a malformed load still succeeds
    broken.set_keep_awake_enabled(False)
    data2 = json.loads(prefs.read_text(encoding="utf-8"))
    assert data2["keep_awake_enabled"] is False


def test_settings_manager_multi_write_keeps_fields(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    prefs = config / "pet_preferences.json"

    # unknown future keys survive rewrites (forward compatibility)
    prefs.write_text(json.dumps({"custom_future": "keep-me"}), encoding="utf-8")
    mgr = SettingsManager(prefs)
    assert mgr.notifications_enabled is True

    # 8 multiple writes never drop earlier fields
    mgr.set_notifications_enabled(False)
    mgr.set_keep_awake_enabled(False)
    mgr.set_greeting_on_startup(False)
    final = json.loads(prefs.read_text(encoding="utf-8"))
    assert final["notifications_enabled"] is False
    assert final["keep_awake_enabled"] is False
    assert final["greeting_on_startup"] is False
    assert final["custom_future"] == "keep-me"

    reloaded = SettingsManager(prefs)
    assert reloaded.notifications_enabled is False
    assert reloaded.keep_awake_enabled is False
    assert reloaded.greeting_on_startup is False


def test_settings_changed_signal(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    prefs = config / "pet_preferences.json"
    mgr = SettingsManager(prefs)
    events: list[dict] = []
    mgr.connect(events.append)
    mgr.set_notifications_enabled(False)
    assert len(events) == 1
    assert events[0]["notifications_enabled"] is False
    mgr.set_notifications_enabled(False)  # no-op -> no event
    assert len(events) == 1
    mgr.set_keep_awake_enabled(False)
    assert len(events) == 2


def test_settings_does_not_touch_workspace_schema(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    ws_file = config / "ui_settings.json"
    ws_file.write_text(
        json.dumps(
            {
                "current_workspace": str(Path("C:/ws")),
                "recent_workspaces": [str(Path("C:/ws"))],
                "quick_ask_effort": "medium",
                "conversation_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    # 7 settings writes never disturb the workspace file
    mgr = SettingsManager(config / "pet_preferences.json")
    mgr.set_notifications_enabled(False)
    mgr.set_greeting_on_startup(False)
    after = json.loads(ws_file.read_text(encoding="utf-8"))
    assert after["current_workspace"] == str(Path("C:/ws"))
    assert after["recent_workspaces"] == [str(Path("C:/ws"))]
    assert after["quick_ask_effort"] == "medium"
    assert after["conversation_enabled"] is False

    # WorkspaceStore still reads its own schema correctly afterwards
    store = WorkspaceStore(ws_file, Path("C:/ws"))
    assert store.current_workspace == Path("C:/ws")
    assert store.quick_ask_effort == "medium"
    assert store.conversation_enabled is False


# -- Notification gate (cases 9-13) --------------------------------------


def test_notification_gate_manager(app: QApplication) -> None:
    # 9-10 disabled: success and error are consumed but never emitted
    mgr = NotificationManager()
    mgr.set_enabled(False)
    events = []
    mgr.connect(events.append)
    mgr.on_agent_state(_agent_state("claude", "working", 1000))
    mgr.on_agent_state(_agent_state("claude", "success", 2000))
    mgr.on_agent_state(_agent_state("codex", "working", 3000))
    mgr.on_agent_state(_agent_state("codex", "error", 4000))
    assert events == []

    # 12 re-enable never replays the suppressed notices
    mgr.set_enabled(True)
    assert events == []

    # 13 a fresh episode after re-enable notifies normally
    mgr.on_agent_state(_agent_state("claude", "idle", 5000))
    mgr.on_agent_state(_agent_state("claude", "working", 6000))
    mgr.on_agent_state(_agent_state("claude", "success", 7000))
    assert len(events) == 1
    assert events[0].kind == "success"

    # 11 waiting never emits a normal notification, enabled or not
    mgr2 = NotificationManager()
    mgr2.set_enabled(False)
    events2 = []
    mgr2.connect(events2.append)
    mgr2.on_agent_state(_agent_state("claude", "working", 1000))
    mgr2.on_agent_state(_agent_state("claude", "waiting", 2000))
    assert events2 == []


def test_notification_gate_wiring(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    coordinator.show_shell()
    app.processEvents()

    mgr = NotificationManager()
    mgr.set_enabled(False)
    mgr.connect(coordinator.on_notification)

    def _push(agent_id: str, state_name: str, ts: int) -> None:
        agent_state = _agent_state(agent_id, state_name, ts)
        dock.set_agent_state(agent_id, state_name)
        coordinator.on_agent_state(agent_id, agent_state)
        mgr.on_agent_state(agent_state)

    coordinator.toggle_bubble()  # hide the startup greeting
    app.processEvents()
    assert not bubble.isVisible()

    # 9-10 notifications off -> success/error never reach the bubble
    _push("claude", "working", 1000)
    _push("claude", "success", 2000)
    _push("codex", "working", 3000)
    _push("codex", "error", 4000)
    app.processEvents()
    assert not bubble.isVisible()

    # 11 waiting still raises the PermissionCard while notifications are off
    _push("claude", "waiting", 5000)
    app.processEvents()
    assert card.isVisible()
    assert not bubble.isVisible()

    # 12 re-enable: no replay of the suppressed success/error
    mgr.set_enabled(True)
    app.processEvents()
    assert not bubble.isVisible()

    # 13 a fresh episode after re-enable shows a new success
    _push("claude", "idle", 6000)
    _push("claude", "working", 7000)
    _push("claude", "success", 8000)
    app.processEvents()
    assert bubble.isVisible()
    assert "finished" in bubble._text.text()

    _teardown(coordinator, pet, app)


# -- Keep Awake gate (cases 14-19) ---------------------------------------


def test_keep_awake_enabled_gate(app: QApplication) -> None:
    backend = FakeBackend()
    svc = KeepAwakeService(backend=backend)

    # 14 enabled + working -> acquire
    svc.on_agent_state(_agent_state("claude", "working", 1))
    assert svc.is_active and backend.acquire_calls == 1

    # 15 disable while active -> immediate release
    svc.set_enabled(False)
    assert not svc.is_active and backend.release_calls == 1

    # 16 disabled + working -> no acquire (lifecycle still recorded)
    svc.on_agent_state(_agent_state("claude", "working", 2))
    assert not svc.is_active and backend.acquire_calls == 1

    # 17 re-enable while still working -> immediate acquire
    svc.set_enabled(True)
    assert svc.is_active and backend.acquire_calls == 2

    # back to idle releases
    svc.on_agent_state(_agent_state("claude", "idle", 3))
    assert not svc.is_active and backend.release_calls == 2

    # 18 idle + enable -> no acquire
    backend2 = FakeBackend()
    svc2 = KeepAwakeService(backend=backend2)
    svc2.set_enabled(False)
    svc2.on_agent_state(_agent_state("claude", "working", 1))
    assert not svc2.is_active and backend2.acquire_calls == 0
    svc2.on_agent_state(_agent_state("claude", "idle", 2))
    svc2.set_enabled(True)
    assert not svc2.is_active and backend2.acquire_calls == 0

    # 19 shutdown always releases safely
    backend3 = FakeBackend()
    svc3 = KeepAwakeService(backend=backend3)
    svc3.on_agent_state(_agent_state("claude", "working", 1))
    assert backend3.acquire_calls == 1
    svc3.shutdown()
    assert backend3.release_calls == 1 and not svc3.is_active
    svc3.set_enabled(False)
    svc3.shutdown()  # idempotent, nothing extra to release
    assert backend3.release_calls == 1


# -- Greeting gate (cases 20-22) -----------------------------------------


def test_greeting_gate(app: QApplication, root: Path) -> None:
    on_root = root / "g_on"
    on_root.mkdir(parents=True, exist_ok=True)
    prefs_on = on_root / "config" / "pet_preferences.json"

    # 20 enabled -> startup bubble visible
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        on_root, prefs_on
    )
    coordinator.show_shell()
    app.processEvents()
    assert bubble.isVisible()
    assert "Firefly" in bubble._text.text()
    _teardown(coordinator, pet, app)

    off_root = root / "g_off"
    off_root.mkdir(parents=True, exist_ok=True)
    prefs_off = off_root / "config" / "pet_preferences.json"
    off = SettingsManager(prefs_off)
    off.set_greeting_on_startup(False)

    # 21 disabled -> startup bubble hidden
    pet2, dock2, bubble2, toolbar2, wp2, sp2, card2, settings2, settings_popover2, coordinator2 = (
        _make_shell(off_root, prefs_off)
    )
    coordinator2.show_shell()
    app.processEvents()
    assert not bubble2.isVisible()

    # 22 manual pet click still shows the greeting when startup is off
    coordinator2.toggle_bubble()
    app.processEvents()
    assert bubble2.isVisible()
    assert "Firefly" in bubble2._text.text()

    _teardown(coordinator2, pet2, app)


# -- Settings UI (cases 23-30) -------------------------------------------


def test_settings_popover_ui(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    coordinator.show_shell()
    app.processEvents()

    # 23 gear opens settings popover
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
    assert not bubble.isVisible()

    # 24 gear again closes it
    toolbar.select_action("settings")
    app.processEvents()
    assert not settings_popover.isVisible()

    # 25 Esc closes
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
    QTest.keyClick(settings_popover, Qt.Key_Escape)
    app.processEvents()
    assert not settings_popover.isVisible()

    # 26 outside click closes
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
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
    assert not settings_popover.isVisible()

    # 27 workspace/session are mutually exclusive with settings
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
    toolbar.select_action("workspace")
    app.processEvents()
    assert wp.isVisible()
    assert not settings_popover.isVisible()
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
    assert not wp.isVisible()
    coordinator._show_sessions()
    app.processEvents()
    assert sp.isVisible()
    assert not settings_popover.isVisible()

    _teardown(coordinator, pet, app)


def test_permission_card_closes_settings(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    coordinator.show_shell()
    app.processEvents()

    # 28 PermissionCard appears -> Settings closes
    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()
    agent_state = _agent_state("claude", "waiting", 1000)
    dock.set_agent_state("claude", "waiting")
    coordinator.on_agent_state("claude", agent_state)
    app.processEvents()
    assert card.isVisible()
    assert not settings_popover.isVisible()

    _teardown(coordinator, pet, app)


def test_settings_reset_position(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    coordinator.show_shell()
    app.processEvents()

    toolbar.select_action("settings")
    app.processEvents()
    assert settings_popover.isVisible()

    expected = pet.pos()  # default position after show_shell
    pet.move(QPoint(10, 10))
    app.processEvents()
    assert pet.pos() != expected

    received: list[bool] = []
    settings_popover.reset_position_requested.connect(lambda: received.append(True))

    # 29 Reset position emits the action, closes the popover, restores the pet
    settings_popover._reset_btn.click()
    app.processEvents()
    assert received == [True]
    assert not settings_popover.isVisible()
    assert pet.pos() == expected

    _teardown(coordinator, pet, app)


def test_toggles_sync_with_manager(app: QApplication, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    app.processEvents()

    # 30 initial toggles match the manager defaults
    assert settings_popover.toggle_state("notifications_enabled") is True
    assert settings_popover.toggle_state("keep_awake_enabled") is True
    assert settings_popover.toggle_state("greeting_on_startup") is True

    # flipping each toggle updates the manager and persists
    settings_popover._rows["notifications_enabled"]._toggle.click()
    app.processEvents()
    assert settings.notifications_enabled is False
    assert settings_popover.toggle_state("notifications_enabled") is False

    settings_popover._rows["keep_awake_enabled"]._toggle.click()
    app.processEvents()
    assert settings.keep_awake_enabled is False
    assert settings_popover.toggle_state("keep_awake_enabled") is False

    settings_popover._rows["greeting_on_startup"]._toggle.click()
    app.processEvents()
    assert settings.greeting_on_startup is False
    assert settings_popover.toggle_state("greeting_on_startup") is False

    data = json.loads(settings.preferences_file.read_text(encoding="utf-8"))
    assert data["notifications_enabled"] is False
    assert data["keep_awake_enabled"] is False
    assert data["greeting_on_startup"] is False

    _teardown(coordinator, pet, app)


# -- End-to-end wiring smoke ---------------------------------------------


def test_end_to_end_settings_wiring(app: QApplication, root: Path) -> None:
    """Mirror app.py: settings gate the NotificationManager + KeepAwake live."""
    root.mkdir(parents=True, exist_ok=True)
    pet, dock, bubble, toolbar, wp, sp, card, settings, settings_popover, coordinator = _make_shell(
        root
    )
    coordinator.show_shell()
    app.processEvents()

    mgr = NotificationManager()
    ka = KeepAwakeService(backend=FakeBackend())
    mgr.set_enabled(settings.notifications_enabled)
    ka.set_enabled(settings.keep_awake_enabled)
    mgr.connect(coordinator.on_notification)

    def _on_settings_changed(_prefs) -> None:
        mgr.set_enabled(settings.notifications_enabled)
        ka.set_enabled(settings.keep_awake_enabled)

    settings.connect(_on_settings_changed)

    def _push(agent_id: str, state_name: str, ts: int) -> None:
        agent_state = _agent_state(agent_id, state_name, ts)
        dock.set_agent_state(agent_id, state_name)
        coordinator.on_agent_state(agent_id, agent_state)
        mgr.on_agent_state(agent_state)
        ka.on_agent_state(agent_state)

    coordinator.toggle_bubble()  # hide the startup greeting
    app.processEvents()

    # toggling via the settings popover flips the live services
    settings_popover._rows["notifications_enabled"]._toggle.click()
    settings_popover._rows["keep_awake_enabled"]._toggle.click()
    app.processEvents()
    assert settings.notifications_enabled is False
    assert settings.keep_awake_enabled is False
    assert mgr.enabled is False
    assert ka.enabled is False

    # notifications off: working -> success suppressed in the UI
    _push("claude", "working", 1000)
    _push("claude", "success", 2000)
    app.processEvents()
    assert not bubble.isVisible()

    # keep awake off: claude working does not acquire
    assert not ka.is_active

    # waiting PermissionCard still works with notifications off
    _push("claude", "waiting", 3000)
    app.processEvents()
    assert card.isVisible()

    # re-enable both via the popover
    settings_popover._rows["notifications_enabled"]._toggle.click()
    settings_popover._rows["keep_awake_enabled"]._toggle.click()
    app.processEvents()
    assert settings.notifications_enabled is True
    assert settings.keep_awake_enabled is True
    assert mgr.enabled is True
    assert ka.enabled is True
    # claude is still waiting -> keep awake re-acquires immediately
    assert ka.is_active

    # resolve waiting; a fresh episode after re-enable shows a new success
    _push("claude", "idle", 4000)
    _push("claude", "working", 5000)
    _push("claude", "success", 6000)
    app.processEvents()
    assert bubble.isVisible()
    assert "finished" in bubble._text.text()

    _teardown(coordinator, pet, app)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_settings_manager_defaults_and_persistence(root / "mgr1")
        test_settings_manager_multi_write_keeps_fields(root / "mgr2")
        test_settings_changed_signal(root / "mgr3")
        test_settings_does_not_touch_workspace_schema(root / "mgr4")
        test_notification_gate_manager(app)
        test_notification_gate_wiring(app, root / "notify")
        test_keep_awake_enabled_gate(app)
        test_greeting_gate(app, root / "greet")
        test_settings_popover_ui(app, root / "ui")
        test_permission_card_closes_settings(app, root / "perm")
        test_settings_reset_position(app, root / "reset")
        test_toggles_sync_with_manager(app, root / "toggles")
        test_end_to_end_settings_wiring(app, root / "e2e")
    print("Phase 8B.4 settings tests passed.")


if __name__ == "__main__":
    main()
