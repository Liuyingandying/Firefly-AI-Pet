"""Hover-chrome interaction tests (2026-09 hover spec).

Covers: default-hidden chrome, hover reveal, 300 ms delayed collapse with
re-enter cancel, single-click "打开 UI" entry toggle, auto-collapse re-arm,
popover collapse suppression, exit/console signal routing, drag never
toggling the entry, and screen-edge clamping of the chrome pills.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.pet_overlay import PetOverlay
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


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def cluster(qapp):
    pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    coordinator = OverlayCoordinator(pet, dock, bubble, toolbar)
    coordinator.chrome.fade_ms = 0  # deterministic show/hide in tests
    coordinator.show_shell_pet_only()
    # Offscreen platforms synthesize an Enter when a window shows; pin the
    # "character alone" baseline so assertions don't depend on that.
    coordinator.chrome.reset_hidden()
    yield coordinator
    coordinator.close_overlays()
    pet.shutdown()


def _send_hover(cluster, widget, enter: bool) -> None:
    """Drive the hover path directly (the cursor check is bypassed because
    the offscreen cursor never sits over the cluster)."""
    cluster.chrome.entered() if enter else cluster.chrome.left()


def _chrome_visible(coordinator: OverlayCoordinator) -> bool:
    return (
        coordinator.dock.isVisible()
        and coordinator.toolbar.isVisible()
        and coordinator.chrome.exit_button.isVisible()
    )


# -- default + hover reveal -------------------------------------------------


def test_default_hides_all_chrome(cluster):
    assert cluster.pet.isVisible()
    assert not cluster.dock.isVisible()
    assert not cluster.toolbar.isVisible()
    assert not cluster.chrome.exit_button.isVisible()
    assert not cluster.chrome.open_ui_entry.isVisible()


def test_hover_reveals_bars_and_exit(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    assert _chrome_visible(cluster)
    assert not cluster.chrome.open_ui_entry.isVisible()  # entry needs a click


def test_filter_ignores_synthetic_enter(cluster, qapp):
    """An Enter for a window the cursor is NOT over (startup/show
    synthesis) must not reveal the chrome; a genuine one does."""
    cluster.chrome.reset_hidden()
    qapp.processEvents()

    cluster.chrome._cursor_inside = lambda widget: False
    QApplication.sendEvent(cluster.toolbar, QEvent(QEvent.Enter))
    qapp.processEvents()
    assert not _chrome_visible(cluster)

    cluster.chrome._cursor_inside = lambda widget: True
    QApplication.sendEvent(cluster.toolbar, QEvent(QEvent.Enter))
    qapp.processEvents()
    assert _chrome_visible(cluster)


def test_hover_entry_only_with_bars(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    cluster.on_pet_clicked()
    qapp.processEvents()
    assert cluster.chrome.open_ui_entry.isVisible()


# -- collapse ---------------------------------------------------------------


def test_leave_collapses_after_grace(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    _send_hover(cluster, cluster.pet, enter=False)
    qapp.processEvents()
    assert _chrome_visible(cluster)  # inside the 300 ms grace window
    QTest.qWait(450)
    assert not cluster.dock.isVisible()
    assert not cluster.toolbar.isVisible()
    assert not cluster.chrome.exit_button.isVisible()


def test_reenter_cancels_collapse(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    _send_hover(cluster, cluster.pet, enter=False)
    QTest.qWait(120)
    _send_hover(cluster, cluster.pet, enter=True)  # back inside before the grace ends
    qapp.processEvents()
    QTest.qWait(450)
    assert _chrome_visible(cluster)


# -- click toggles the "打开 UI" entry ---------------------------------------


def test_click_toggles_entry(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    cluster.on_pet_clicked()
    qapp.processEvents()
    assert cluster.chrome.open_ui_entry.isVisible()
    cluster.on_pet_clicked()
    qapp.processEvents()
    assert not cluster.chrome.open_ui_entry.isVisible()
    assert _chrome_visible(cluster)  # bars stay while the mouse is inside


def test_collapse_resets_entry(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    cluster.on_pet_clicked()
    qapp.processEvents()
    assert cluster.chrome.open_ui_entry.isVisible()
    _send_hover(cluster, cluster.pet, enter=False)
    QTest.qWait(450)
    assert not cluster.chrome.open_ui_entry.isVisible()
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    assert _chrome_visible(cluster)
    assert not cluster.chrome.open_ui_entry.isVisible()  # re-armed


# -- signal routing ---------------------------------------------------------


def test_exit_button_routes_exit_signal(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    seen = []
    cluster.exit_requested.connect(lambda: seen.append(1))
    QTest.mouseClick(
        cluster.chrome.exit_button, Qt.LeftButton, pos=cluster.chrome.exit_button.rect().center()
    )
    qapp.processEvents()
    assert seen == [1]


def test_entry_button_routes_console_signal(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    seen = []
    cluster.console_requested.connect(lambda: seen.append(1))
    QTest.mouseClick(
        cluster.chrome.open_ui_entry,
        Qt.LeftButton,
        pos=cluster.chrome.open_ui_entry.rect().center(),
    )
    qapp.processEvents()
    assert seen == [1]


# -- suppression + drag + clamping ------------------------------------------


def test_open_panel_blocks_collapse(cluster, qapp):
    class _FakePopover:
        def isVisible(self):
            return True

        def dismiss(self):
            pass

    cluster.workspace_popover = _FakePopover()
    try:
        _send_hover(cluster, cluster.pet, enter=True)
        qapp.processEvents()
        _send_hover(cluster, cluster.pet, enter=False)
        QTest.qWait(450)
        assert _chrome_visible(cluster)  # anchor panel holds the chrome up
    finally:
        cluster.workspace_popover = None


def test_drag_never_toggles_entry(cluster, qapp):
    _send_hover(cluster, cluster.pet, enter=True)
    qapp.processEvents()
    start = cluster.pet.rect().center()
    finish = start + QPoint(-60, -40)
    QTest.mousePress(cluster.pet, Qt.LeftButton, pos=start)
    QTest.mouseMove(cluster.pet, pos=finish, delay=20)
    QTest.mouseRelease(cluster.pet, Qt.LeftButton, pos=finish)
    qapp.processEvents()
    assert not cluster.chrome.open_ui_entry.isVisible()


def test_chrome_clamped_inside_screen(cluster, qapp):
    cluster.pet.move(QPoint(4, 4))
    cluster.reposition()
    qapp.processEvents()
    screen = cluster.pet.screen().availableGeometry()
    for pill in (
        cluster.chrome.exit_button,
        cluster.chrome.open_ui_entry,
        cluster.toolbar,
        cluster.dock,
    ):
        geo = pill.frameGeometry()
        assert geo.right() <= screen.right() + 1
        assert geo.bottom() <= screen.bottom() + 1
        assert geo.left() >= screen.left() - 1
        assert geo.top() >= screen.top() - 1
