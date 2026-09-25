"""Pet left-click interaction model (2026-09 hover spec) — offline tests.

Default shows the character alone. Hovering reveals the two bars + exit
pill; leaving the region collapses them after the 300 ms grace delay. A
single valid left click toggles the "打开 UI" entry (never the console
directly); drags never toggle. Right-click/wheel isolation and the
0.8-scale pass are preserved.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import VisualShell
from ui import theme
from ui.overlay_coordinator import OverlayCoordinator, PresentationState
from ui.pet_overlay import PetOverlay

ROOT = PROJECT_DIR
STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}

PET_ONLY = PresentationState.PET_ONLY
CONTROLS = PresentationState.CONTROLS
CHAT = PresentationState.CHAT


def _settle(app: QApplication) -> None:
    app.processEvents()


def _fresh_pet() -> PetOverlay:
    return PetOverlay(ROOT / "assets" / "animations", STATE_GIF)


def _click_once(pet: PetOverlay) -> None:
    QTest.mouseClick(pet, Qt.LeftButton, pos=pet.rect().center())


def _double_click(pet: PetOverlay, pos: QPoint) -> None:
    """Real Qt double-click chain: Press, Release, DblClick, Release.

    ``QTest.mouseDClick`` emits only the bare dblclick event (no release), so it
    does not model a real double click by itself.
    """
    QTest.mousePress(pet, Qt.LeftButton, pos=pos)
    QTest.mouseRelease(pet, Qt.LeftButton, pos=pos)
    QTest.mouseDClick(pet, Qt.LeftButton, pos=pos)
    QTest.mouseRelease(pet, Qt.LeftButton, pos=pos)


def _minimal_shell(greeting_on_startup: bool):
    """A bare pet/dock/bubble/toolbar + coordinator with a temp settings manager."""
    from core.settings_manager import SettingsManager
    from ui.agent_dock import AgentDock
    from ui.speech_bubble import SpeechBubble
    from ui.vertical_toolbar import VerticalToolbar

    td = tempfile.TemporaryDirectory()
    pet = _fresh_pet()
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    prefs = SettingsManager(Path(td.name) / "prefs.json")
    prefs.set_greeting_on_startup(greeting_on_startup)
    coordinator = OverlayCoordinator(pet, dock, bubble, toolbar, settings_manager=prefs)
    return pet, dock, bubble, toolbar, coordinator, td


# -- A. startup -----------------------------------------------------------

def test_startup_pet_only(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    assert shell.pet.isVisible()
    assert not shell.dock.isVisible()
    assert not shell.toolbar.isVisible()
    assert not shell.bubble.isVisible()
    assert shell.coordinator.presentation_state == PET_ONLY


# -- B/C/D. single click toggles the "打开 UI" entry -----------------------

def test_pet_only_click_controls(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    _click_once(shell.pet)
    _settle(app)
    assert shell.dock.isVisible() and shell.toolbar.isVisible()
    assert shell.coordinator.chrome.open_ui_entry.isVisible()
    assert not shell.bubble.isVisible()


def test_controls_click_chat(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    _click_once(shell.pet)
    _click_once(shell.pet)
    _settle(app)
    assert shell.dock.isVisible() and shell.toolbar.isVisible()
    assert not shell.coordinator.chrome.open_ui_entry.isVisible()  # toggled back off


def test_chat_click_pet_only(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    for _ in range(3):
        _click_once(shell.pet)
    _settle(app)
    assert shell.coordinator.chrome.open_ui_entry.isVisible()  # odd clicks keep it up


# -- E. full six-click cycle ---------------------------------------------

def test_six_click_cycle(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    for i in range(6):
        _click_once(shell.pet)
        _settle(app)
        assert shell.coordinator.chrome.open_ui_entry.isVisible() == (i % 2 == 0)
    assert shell.dock.isVisible() and shell.toolbar.isVisible()


# -- F. no doubleClickInterval delay -------------------------------------

def test_no_interval_delay(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    _click_once(shell.pet)
    # No qWait for the system double-click interval: the toggle is immediate.
    assert shell.coordinator.chrome.open_ui_entry.isVisible()
    assert shell.dock.isVisible() and shell.toolbar.isVisible()


# -- G/H. double/triple click = ordinary repeated toggles ----------------

def test_double_click_two_transitions(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    clicks: list[bool] = []
    shell.pet.left_clicked.connect(lambda: clicks.append(True))
    _double_click(shell.pet, shell.pet.rect().center())
    _settle(app)
    assert clicks == [True, True]
    assert not shell.coordinator.chrome.open_ui_entry.isVisible()  # even clicks leave it off


def test_triple_click_three_transitions(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    for _ in range(3):
        _click_once(shell.pet)
    _settle(app)
    assert shell.coordinator.chrome.open_ui_entry.isVisible()


# -- I/J. drag vs click ---------------------------------------------------

def test_drag_zero_transitions(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    clicks: list[bool] = []
    shell.pet.left_clicked.connect(lambda: clicks.append(True))
    start = shell.pet.rect().center()
    finish = start + QPoint(-60, -40)
    QTest.mousePress(shell.pet, Qt.LeftButton, pos=start)
    QTest.mouseMove(shell.pet, pos=finish, delay=20)
    QTest.mouseRelease(shell.pet, Qt.LeftButton, pos=finish)
    _settle(app)
    assert clicks == []
    assert not shell.coordinator.chrome.open_ui_entry.isVisible()  # drag never toggles it


def test_small_movement_one_transition(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    shell.coordinator.chrome.fade_ms = 0
    shell.coordinator.chrome.reset_hidden()
    _settle(app)
    clicks: list[bool] = []
    shell.pet.left_clicked.connect(lambda: clicks.append(True))
    start = shell.pet.rect().center()
    finish = start + QPoint(2, 1)
    QTest.mousePress(shell.pet, Qt.LeftButton, pos=start)
    QTest.mouseMove(shell.pet, pos=finish, delay=10)
    QTest.mouseRelease(shell.pet, Qt.LeftButton, pos=finish)
    _settle(app)
    assert clicks == [True]
    assert shell.coordinator.chrome.open_ui_entry.isVisible()


# -- K/L/M. right-click + wheel isolation ---------------------------------

def test_right_click_scale_unaffected(app: QApplication) -> None:
    pet = _fresh_pet()
    pet.show()
    toggled: list[int] = []
    pet.scale_mode_toggled.connect(lambda: toggled.append(1))
    try:
        QTest.mouseClick(pet, Qt.RightButton, pos=pet.rect().center())
        _settle(app)
        assert toggled == []
        QTest.qWait(QApplication.doubleClickInterval() + 120)
        assert toggled == [1]
    finally:
        pet.shutdown()


def test_double_right_click_hides_unaffected(app: QApplication) -> None:
    pet = _fresh_pet()
    pet.show()
    hides: list[int] = []
    pet.hide_requested.connect(lambda: hides.append(1))
    try:
        QTest.mouseDClick(pet, Qt.RightButton, pos=pet.rect().center())
        _settle(app)
        assert hides == [1]
    finally:
        pet.shutdown()


def test_wheel_scale_unaffected(app: QApplication) -> None:
    pet = _fresh_pet()
    pet.show()
    deltas: list[int] = []
    pet.scale_wheel.connect(deltas.append)
    try:
        pos = QPointF(pet.rect().center())
        event = QWheelEvent(
            pos, pos, QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False,
        )
        QApplication.sendEvent(pet, event)
        _settle(app)
        assert deltas == [1]
    finally:
        pet.shutdown()


# -- N/O. greeting behavior ----------------------------------------------

def test_chat_bubble_always_visible(app: QApplication) -> None:
    pet, dock, bubble, toolbar, coordinator, td = _minimal_shell(greeting_on_startup=True)
    try:
        coordinator.set_presentation_state(CHAT)
        _settle(app)
        assert bubble.isVisible()
        assert "Firefly" in bubble._text.text()
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        td.cleanup()


def test_chat_greeting_off_blank_bubble(app: QApplication) -> None:
    pet, dock, bubble, toolbar, coordinator, td = _minimal_shell(greeting_on_startup=False)
    try:
        coordinator.set_presentation_state(CHAT)
        _settle(app)
        assert bubble.isVisible()
        assert bubble._text.text() == ""  # bubble exists, no greeting copy
    finally:
        coordinator.close_overlays()
        pet.shutdown()
        td.cleanup()


# -- P. PET_ONLY never cancels a running task ----------------------------

def test_pet_only_does_not_cancel(app: QApplication, shell: VisualShell) -> None:
    shell.coordinator.show_shell_pet_only()
    _settle(app)
    shell.short_ask.set_running("Connecting…")
    shell.short_ask.show()
    _settle(app)
    assert shell.short_ask.isVisible()

    shell.coordinator.set_presentation_state(PET_ONLY)
    _settle(app)
    assert not shell.short_ask.isVisible()
    assert shell.short_ask.running  # backend state preserved, never cancelled
    shell.short_ask.reset()


# -- Q. full cycle at 0.8 scale ------------------------------------------

def test_scale_08_cycle(app: QApplication, shell: VisualShell) -> None:
    theme.set_ui_scale(0.8)
    _settle(app)
    try:
        shell.coordinator.show_shell_pet_only()
        shell.coordinator.chrome.fade_ms = 0
        shell.coordinator.chrome.reset_hidden()
        _settle(app)
        assert shell.pet.isVisible() and not shell.dock.isVisible()

        _click_once(shell.pet)
        _settle(app)
        assert shell.dock.isVisible() and shell.toolbar.isVisible()
        assert shell.coordinator.chrome.open_ui_entry.isVisible() and not shell.bubble.isVisible()

        _click_once(shell.pet)
        _settle(app)
        assert not shell.coordinator.chrome.open_ui_entry.isVisible()
    finally:
        theme.set_ui_scale(1.0)
        shell.coordinator.set_presentation_state(PET_ONLY)
        _settle(app)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        shell = VisualShell(
            None,
            sessions_file=Path(td) / "sessions.json",
            workspace_settings_file=Path(td) / "ui_settings.json",
        )
        try:
            test_startup_pet_only(app, shell)
            test_pet_only_click_controls(app, shell)
            test_controls_click_chat(app, shell)
            test_chat_click_pet_only(app, shell)
            test_six_click_cycle(app, shell)
            test_no_interval_delay(app, shell)
            test_double_click_two_transitions(app, shell)
            test_triple_click_three_transitions(app, shell)
            test_drag_zero_transitions(app, shell)
            test_small_movement_one_transition(app, shell)
            test_pet_only_does_not_cancel(app, shell)
            test_scale_08_cycle(app, shell)
        finally:
            shell.shutdown()

    test_right_click_scale_unaffected(app)
    test_double_right_click_hides_unaffected(app)
    test_wheel_scale_unaffected(app)
    test_chat_bubble_always_visible(app)
    test_chat_greeting_off_blank_bubble(app)

    print("Pet click UI modes tests passed.")


if __name__ == "__main__":
    main()
