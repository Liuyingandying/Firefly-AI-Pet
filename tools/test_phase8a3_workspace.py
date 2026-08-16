"""Phase 8A.3 WorkspacePopover tests: WorkspaceManager + UI wiring."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import shutil
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


def _gone_temp_path(name: str) -> Path:
    """A path under the OS temp dir that is guaranteed not to exist."""
    return Path(tempfile.gettempdir()) / name


def test_stale_temp_current_not_restored() -> None:
    """A persisted current that no longer exists must not be restored as current."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        valid = root / "valid"
        valid.mkdir()
        stale = _gone_temp_path("tmp_firefly_gone_current_xyz")
        settings = root / "config" / "ui_settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({
                "current_workspace": str(stale),
                "recent_workspaces": [str(stale), str(valid)],
            }),
            encoding="utf-8",
        )
        store = WorkspaceStore(settings, valid)
        assert store.current_workspace != stale
        assert store.current_workspace == valid.resolve()


def test_stale_temp_recent_filtered() -> None:
    """A stale temp recent is dropped; valid recents (both sides) are kept."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        valid_a = root / "a"
        valid_b = root / "b"
        valid_a.mkdir()
        valid_b.mkdir()
        stale = _gone_temp_path("tmp_firefly_gone_recent_xyz")
        settings = root / "config" / "ui_settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({
                "current_workspace": str(valid_a),
                "recent_workspaces": [str(valid_a), str(stale), str(valid_b)],
            }),
            encoding="utf-8",
        )
        store = WorkspaceStore(settings, valid_a)
        recents = [str(x) for x in store.recent_workspaces]
        assert str(valid_a) in recents
        assert str(valid_b) in recents
        assert str(stale) not in recents, "stale temp recent must be filtered out"


def test_non_temp_missing_recent_kept() -> None:
    """A missing non-temp path (e.g. a disconnected drive) is kept conservatively."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        valid = root / "valid"
        valid.mkdir()
        missing = PROJECT_DIR / "__firefly_missing_ws_test__"  # not under %TEMP%, gone
        settings = root / "config" / "ui_settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({
                "current_workspace": str(valid),
                "recent_workspaces": [str(valid), str(missing)],
            }),
            encoding="utf-8",
        )
        store = WorkspaceStore(settings, valid)
        recents = [str(x) for x in store.recent_workspaces]
        assert str(missing) in recents, "missing non-temp path must be kept (conservative)"


def test_temp_store_does_not_touch_real_config() -> None:
    """A test store must never modify the real user ui_settings.json."""
    from ui.workspace_store import SETTINGS_FILE

    before = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = root / "ws"
        ws.mkdir()
        store = WorkspaceStore(root / "config" / "ui_settings.json", ws)
        manager = WorkspaceManager(store)
        manager.set_current(ws)
        store.prune_stale_recents()
    after = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    assert before == after, "test store must not write the real ui_settings.json"


def test_selecting_deleted_workspace_keeps_current() -> None:
    """Selecting a deleted workspace leaves the current unchanged (no switch)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws_a = root / "a"
        ws_b = root / "b"
        ws_a.mkdir()
        ws_b.mkdir()
        store = WorkspaceStore(root / "config" / "ui_settings.json", ws_a)
        manager = WorkspaceManager(store)
        manager.set_current(ws_a)
        before = manager.current()
        shutil.rmtree(ws_b)
        result = manager.add_or_select(ws_b)
        assert result is None
        assert manager.current() == before


def test_quick_ask_rejects_missing_workspace() -> None:
    """QuickAsk must not construct a QProcess for a nonexistent workspace."""
    from unittest.mock import patch

    from ui.process_launcher import QuickAskRunner

    runner = QuickAskRunner(session_manager=None)
    failed = []
    runner.failed.connect(failed.append)
    with patch("ui.process_launcher.QProcess") as qp:
        ok = runner.ask("codex", "hello", Path("Z:/definitely/missing/ws"), effort="low", persistent=False)
    assert ok is False
    assert failed and "工作区不存在" in failed[0]
    assert not qp.called, "must not construct a QProcess for a missing workspace"


def test_prune_stale_recents_persists() -> None:
    """prune_stale_recents removes stale temp entries from the on-disk file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        valid = root / "valid"
        valid.mkdir()
        stale = _gone_temp_path("tmp_firefly_gone_prune_xyz")
        settings = root / "config" / "ui_settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({
                "current_workspace": str(valid),
                "recent_workspaces": [str(valid), str(stale)],
            }),
            encoding="utf-8",
        )
        store = WorkspaceStore(settings, valid)
        removed = store.prune_stale_recents()
        assert removed >= 1
        on_disk = json.loads(settings.read_text(encoding="utf-8"))
        assert str(stale) not in on_disk["recent_workspaces"]


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
    test_stale_temp_current_not_restored()
    test_stale_temp_recent_filtered()
    test_non_temp_missing_recent_kept()
    test_temp_store_does_not_touch_real_config()
    test_selecting_deleted_workspace_keeps_current()
    test_quick_ask_rejects_missing_workspace()
    test_prune_stale_recents_persists()
    test_ui(app)
    print("Phase 8A.3 workspace tests passed.")


if __name__ == "__main__":
    main()
