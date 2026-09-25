"""Hover-chrome interaction tests (2026-09 hover spec, cursor-poll model).

Covers: default-hidden chrome, hover reveal (pet/bars/bubble/Ask pill all
hold the chrome), 300 ms delayed collapse with re-enter cancel, single-click
"打开 UI" entry toggle with auto-collapse re-arm, panel collapse
suppression, exit/console signal routing, drag never toggling the entry,
stale synthetic events being inert, and screen-edge clamping.

The physical cursor is injected via ``chrome._cursor_pos`` so tests never
depend on the real desktop cursor position.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

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

OFFSCREEN = QPoint(-30000, -30000)


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
    # Park the pet far off-screen and neutralize the screen clamp: the
    # physical cursor may sit anywhere on the real desktop, and its Enter
    # events would legitimately re-reveal the chrome and break assertions.
    coordinator.pet.move(OFFSCREEN)
    coordinator._clamp_point = lambda point, widget, available: point
    # Pin the "character alone" baseline (offscreen synthesizes Enter on
    # every window show).
    coordinator.chrome.reset_hidden()
    yield coordinator
    coordinator.close_overlays()
    pet.shutdown()


def _hover_at(cluster, pos, qapp):
    """Move the injected cursor to pos and run one poll tick."""
    cluster.chrome._cursor_pos = lambda: pos
    cluster.chrome.on_tick()
    qapp.processEvents()


def _pet_center(cluster):
    return cluster.pet.frameGeometry().center()


def _chrome_visible(cluster) -> bool:
    return (
        cluster.dock.isVisible()
        and cluster.toolbar.isVisible()
        and cluster.chrome.exit_button.isVisible()
    )


# -- default + hover reveal -------------------------------------------------


def test_default_hides_all_chrome(cluster):
    assert cluster.pet.isVisible()
    assert not cluster.dock.isVisible()
    assert not cluster.toolbar.isVisible()
    assert not cluster.chrome.exit_button.isVisible()
    assert not cluster.chrome.open_ui_entry.isVisible()


def test_hover_pet_reveals_bars_and_exit(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    assert _chrome_visible(cluster)
    assert not cluster.chrome.open_ui_entry.isVisible()  # entry needs a click


def test_stale_synthetic_enter_is_inert(cluster, qapp):
    """Enter/Leave events no longer drive the chrome at all — a stale
    synthetic Enter delivered to a hidden widget must do nothing."""
    QApplication.sendEvent(cluster.toolbar, QEvent(QEvent.Enter))
    qapp.processEvents()
    assert not _chrome_visible(cluster)
    assert cluster.chrome._shown is False


# -- the whole cluster holds the chrome --------------------------------------


def test_bubble_and_ask_pill_hold_chrome(cluster, qapp):
    """When the chrome is up, hovering the bubble / Ask pill (they overlap
    the character) must hold it; leaving everything collapses it and
    dismisses the greeting."""
    cluster.bubble.show_greeting()
    cluster.bubble.show()
    ask_pill = QWidget()
    ask_pill.resize(80, 30)
    ask_pill.move(cluster.bubble.frameGeometry().topRight())
    ask_pill.show()
    cluster.chrome._region.append(ask_pill)

    # Reveal via the pet first (a hidden chrome is not revived by the
    # bubble alone — that would undo the × hide).
    _hover_at(cluster, _pet_center(cluster), qapp)
    assert _chrome_visible(cluster)
    cluster.on_pet_clicked()
    assert cluster.chrome.open_ui_entry.isVisible()

    # Hover the bubble: still held.
    _hover_at(cluster, cluster.bubble.frameGeometry().center(), qapp)
    QTest.qWait(450)
    assert _chrome_visible(cluster)
    assert cluster.chrome.open_ui_entry.isVisible()

    # Hover the Ask pill: still held.
    _hover_at(cluster, ask_pill.frameGeometry().center(), qapp)
    QTest.qWait(450)
    assert _chrome_visible(cluster)
    assert cluster.chrome.open_ui_entry.isVisible()

    # Leave the whole region: everything collapses, entry re-arms.
    _hover_at(cluster, QPoint(-25000, -25000), qapp)
    QTest.qWait(450)
    assert cluster.chrome._shown is False
    assert not cluster.chrome.open_ui_entry.isVisible()
    assert not cluster.bubble.isVisible()  # greeting dismissed with the chrome


# -- collapse ----------------------------------------------------------------


def test_leave_collapses_after_grace(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    assert _chrome_visible(cluster)  # inside the region
    _hover_at(cluster, QPoint(-25000, -25000), qapp)  # cursor leaves
    assert _chrome_visible(cluster)  # inside the 300 ms grace window
    QTest.qWait(450)
    assert cluster.chrome._shown is False  # logical collapse happened


def test_reenter_cancels_collapse(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    _hover_at(cluster, QPoint(-25000, -25000), qapp)
    QTest.qWait(120)
    _hover_at(cluster, _pet_center(cluster), qapp)  # back before grace ends
    QTest.qWait(450)
    assert _chrome_visible(cluster)


# -- click toggles the "打开 UI" entry ---------------------------------------


def test_click_toggles_entry(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    cluster.on_pet_clicked()
    assert cluster.chrome.open_ui_entry.isVisible()
    cluster.on_pet_clicked()
    assert not cluster.chrome.open_ui_entry.isVisible()
    assert _chrome_visible(cluster)  # bars stay while the cursor is inside


def test_collapse_resets_entry(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    cluster.on_pet_clicked()
    assert cluster.chrome.open_ui_entry.isVisible()
    _hover_at(cluster, QPoint(-25000, -25000), qapp)
    QTest.qWait(450)
    assert cluster.chrome._shown is False  # collapsed despite the bubble
    assert cluster.chrome._armed is False  # entry re-armed
    _hover_at(cluster, _pet_center(cluster), qapp)
    assert _chrome_visible(cluster)
    assert not cluster.chrome.open_ui_entry.isVisible()  # needs a fresh click


def test_hide_x_hint_bubble_does_not_revive_chrome(cluster, qapp):
    """Regression: after × hides everything, the recovery hint bubble is
    visible and the cursor sits near it — the poll must NOT count the
    hint bubble as a hover region member, or the bars pop back in and
    fade out again (淡出→弹出→再淡出 flicker)."""
    cluster.bubble.show_greeting()
    cluster.bubble.show()
    _hover_at(cluster, _pet_center(cluster), qapp)
    # Click ×: pet + chrome hidden, recovery bubble shows near the pet.
    QTest.mouseClick(
        cluster.chrome.exit_button,
        Qt.LeftButton,
        pos=cluster.chrome.exit_button.rect().center(),
    )
    qapp.processEvents()
    assert cluster.chrome._shown is False
    assert cluster.bubble.isVisible()  # recovery hint is up

    # Cursor parked over the hint bubble for several poll ticks.
    cluster.chrome._cursor_pos = lambda: cluster.bubble.frameGeometry().center()
    QTest.qWait(600)
    assert cluster.chrome._shown is False  # must stay hidden
    assert not cluster.dock.isVisible()


# -- signal routing ---------------------------------------------------------


def test_exit_button_hides_pet(cluster, qapp):
    """× = 隐藏流萤（人物+浮层），不是退出；托盘仍是真正的退出入口。"""
    _hover_at(cluster, _pet_center(cluster), qapp)
    cluster.on_pet_clicked()  # entry shown
    seen = []
    cluster.exit_requested.connect(lambda: seen.append(1))
    QTest.mouseClick(
        cluster.chrome.exit_button,
        Qt.LeftButton,
        pos=cluster.chrome.exit_button.rect().center(),
    )
    qapp.processEvents()
    assert seen == []  # 不退出
    assert not cluster.pet.isVisible()  # 人物隐藏
    assert cluster.chrome._shown is False  # 浮层收起
    assert not cluster.chrome.open_ui_entry.isVisible()


def test_entry_button_routes_console_signal(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
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
        _hover_at(cluster, _pet_center(cluster), qapp)
        _hover_at(cluster, QPoint(-25000, -25000), qapp)
        QTest.qWait(450)
        assert _chrome_visible(cluster)  # anchor panel holds the chrome up
    finally:
        cluster.workspace_popover = None


def test_greeting_bubble_hides_with_collapse(cluster, qapp):
    """Regression: a greeting bubble never auto-hides and used to pin the
    bars/entry forever (anchored_panel_open counted it as a panel)."""
    cluster.bubble.show_greeting()
    cluster.bubble.show()
    _hover_at(cluster, _pet_center(cluster), qapp)
    cluster.on_pet_clicked()  # 打开 UI entry shown
    _hover_at(cluster, QPoint(-25000, -25000), qapp)
    QTest.qWait(450)
    assert cluster.chrome._shown is False  # collapsed despite the bubble
    assert not cluster.chrome.open_ui_entry.isVisible()
    assert not cluster.bubble.isVisible()  # greeting dismissed with the chrome


def test_transient_message_survives_collapse(cluster, qapp):
    """A notification bubble keeps its own auto-hide; collapse leaves it."""
    cluster.bubble.show_message("t", "m", duration_ms=6_000)
    _hover_at(cluster, _pet_center(cluster), qapp)
    _hover_at(cluster, QPoint(-25000, -25000), qapp)
    QTest.qWait(450)
    assert cluster.chrome._shown is False  # collapsed
    assert not cluster.chrome.open_ui_entry.isVisible()
    assert cluster.bubble.isVisible()  # still expiring on its own timer


# -- drag + clamping ---------------------------------------------------------


def test_drag_never_toggles_entry(cluster, qapp):
    _hover_at(cluster, _pet_center(cluster), qapp)
    start = cluster.pet.rect().center()
    finish = start + QPoint(-60, -40)
    QTest.mousePress(cluster.pet, Qt.LeftButton, pos=start)
    QTest.mouseMove(cluster.pet, pos=finish, delay=20)
    QTest.mouseRelease(cluster.pet, Qt.LeftButton, pos=finish)
    qapp.processEvents()
    assert not cluster.chrome.open_ui_entry.isVisible()


def test_chrome_clamped_inside_screen(cluster, qapp):
    # Restore real screen clamping (the fixture neutralized it to keep the
    # cluster off-screen for the hover tests).
    del cluster._clamp_point
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
