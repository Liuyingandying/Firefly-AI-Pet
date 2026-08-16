"""Phase 8A.3 WorkspacePopover tests: WorkspaceManager + UI wiring."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.workspace_manager import WorkspaceManager
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.pet_overlay import PetOverlay
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar
from ui.workspace_popover import WorkspacePopover
from ui.workspace_store import MAX_RECENT_WORKSPACES, WorkspaceStore


STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def test_manager() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        settings = root / "config" / "ui_settings.json"
        workspaces = []
        for i in range(7):
            p = root / f"ws{i}"
            p.mkdir()
            workspaces.append(p)

        store = WorkspaceStore(settings, workspaces[0])
        manager = WorkspaceManager(store)

        # 1. read current workspace
        assert manager.current() == workspaces[0]

        # 2. read recent workspaces
        assert manager.recents() == [workspaces[0]]

        # 3. set_current with a valid path
        manager.set_current(workspaces[1])
        assert manager.current() == workspaces[1].resolve()

        # 4. recent update order (most recent first)
        manager.set_current(workspaces[2])
        manager.set_current(workspaces[3])
        assert manager.recents()[0] == workspaces[3].resolve()

        # 5. recent dedup (re-selecting the same workspace adds no duplicate)
        manager.set_current(workspaces[3])
        keys = [_norm(p) for p in manager.recents()]
        assert len(keys) == len(set(keys))

        # 6. recent max of 5
        for p in workspaces:
            manager.set_current(p)
        assert len(manager.recents()) == MAX_RECENT_WORKSPACES

        # 7. invalid path handling
        missing = root / "does_not_exist"
        assert manager.is_valid(missing) is False
        try:
            manager.set_current(missing)
            raise AssertionError("set_current should raise for invalid paths")
        except ValueError:
            pass
        assert manager.add_or_select(missing) is None

        # 8. original settings schema stays compatible
        parsed = json.loads(settings.read_text(encoding="utf-8"))
        assert set(parsed) == {
            "current_workspace",
            "recent_workspaces",
            "quick_ask_effort",
            "conversation_enabled",
        }
        assert isinstance(parsed["current_workspace"], str)
        assert isinstance(parsed["recent_workspaces"], list)

        # workspace_changed notification
        events: list[Path] = []
        manager.connect(events.append)
        manager.set_current(workspaces[4])
        assert events and events[-1] == workspaces[4].resolve()


def test_ui(app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws_a = root / "alpha"
        ws_b = root / "beta"
        ws_c = root / "gamma"
        for p in (ws_a, ws_b, ws_c):
            p.mkdir()

        store = WorkspaceStore(root / "config" / "ui_settings.json", ws_a)
        manager = WorkspaceManager(store)
        manager.set_current(ws_b)
        manager.set_current(ws_c)

        pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
        dock = AgentDock()
        bubble = SpeechBubble()
        toolbar = VerticalToolbar()
        popover = WorkspacePopover(manager)
        coordinator = OverlayCoordinator(
            pet, dock, bubble, toolbar, workspace_popover=popover
        )

        coordinator.show_shell()
        app.processEvents()

        workspace_item = toolbar._items["workspace"]

        # 9. click toolbar workspace -> popover visible
        QTest.mouseClick(workspace_item, Qt.LeftButton, pos=workspace_item.rect().center())
        app.processEvents()
        assert popover.isVisible()

        # 10. click again -> hidden
        QTest.mouseClick(workspace_item, Qt.LeftButton, pos=workspace_item.rect().center())
        app.processEvents()
        assert not popover.isVisible()

        # 11. Esc -> hidden
        QTest.mouseClick(workspace_item, Qt.LeftButton, pos=workspace_item.rect().center())
        app.processEvents()
        assert popover.isVisible()
        QTest.keyClick(popover, Qt.Key_Escape)
        app.processEvents()
        assert not popover.isVisible()

        # 12. click recent -> current workspace changes
        QTest.mouseClick(workspace_item, Qt.LeftButton, pos=workspace_item.rect().center())
        app.processEvents()
        assert popover.isVisible()
        target = next(
            (r for r in popover.recent_rows() if _norm(r.path) == _norm(ws_b)), None
        )
        assert target is not None
        QTest.mouseClick(target, Qt.LeftButton, pos=target.rect().center())
        app.processEvents()
        assert _norm(manager.current()) == _norm(ws_b)

        # 13. drag pet -> popover closes (still open from case 12)
        assert popover.isVisible()
        start = pet.rect().center()
        finish = start + QPoint(-40, -28)
        QTest.mousePress(pet, Qt.LeftButton, pos=start)
        QTest.mouseMove(pet, pos=finish, delay=20)
        QTest.mouseRelease(pet, Qt.LeftButton, pos=finish)
        app.processEvents()
        assert not popover.isVisible()

        # 14. popover stays inside available screen geometry
        QTest.mouseClick(workspace_item, Qt.LeftButton, pos=workspace_item.rect().center())
        app.processEvents()
        assert popover.isVisible()
        available = QApplication.primaryScreen().availableGeometry()
        geo = popover.frameGeometry()
        assert geo.left() >= available.left()
        assert geo.top() >= available.top()
        assert geo.right() <= available.right()
        assert geo.bottom() <= available.bottom()

        coordinator.close_overlays()
        pet.shutdown()
        app.processEvents()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    test_manager()
    test_ui(app)
    print("Phase 8A.3 workspace tests passed.")


if __name__ == "__main__":
    main()
