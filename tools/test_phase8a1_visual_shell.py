"""Headless interaction smoke tests for the Phase 8A.1 visual shell."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.pet_overlay import PetOverlay
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar


ROOT = Path(__file__).resolve().parent.parent
STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def main() -> None:
    app = QApplication.instance() or QApplication([])
    pet = PetOverlay(ROOT / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    coordinator = OverlayCoordinator(pet, dock, bubble, toolbar)

    coordinator.show_shell()
    app.processEvents()

    assert all(widget.isVisible() for widget in (pet, dock, bubble, toolbar))
    assert dock.selected_agent == "codex"
    assert dock.state_for("claude") == "unavailable"
    assert dock.state_for("codex") == "unavailable"
    assert dock.state_for("chatgpt") == "unavailable"
    assert toolbar.selected_action == "companion"

    old_pet_position = pet.pos()
    old_positions = (dock.pos(), toolbar.pos(), bubble.pos())
    start = pet.rect().center()
    finish = start + QPoint(-48, -36)
    QTest.mousePress(pet, Qt.LeftButton, pos=start)
    QTest.mouseMove(pet, pos=finish, delay=20)
    QTest.mouseRelease(pet, Qt.LeftButton, pos=finish)
    app.processEvents()
    new_positions = (dock.pos(), toolbar.pos(), bubble.pos())
    assert pet.pos() != old_pet_position
    assert new_positions != old_positions

    pet.apply_state("working")
    assert pet.current_state == "working"
    coordinator.close_overlays()
    pet.shutdown()
    app.processEvents()
    print("Phase 8A.1 visual shell tests passed.")


if __name__ == "__main__":
    main()
