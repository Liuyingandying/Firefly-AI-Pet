"""Render the real Phase 8A.1 widgets into a deterministic visual QA image."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication

from ui.agent_dock import AgentDock
from ui.pet_overlay import PetOverlay
from ui.speech_bubble import SpeechBubble
from ui import theme
from ui.vertical_toolbar import VerticalToolbar


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "phase8a1_preview.png"
STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def main() -> int:
    app = QApplication.instance() or QApplication([])
    pet = PetOverlay(ROOT / "assets" / "animations", STATE_GIF)
    dock = AgentDock()
    bubble = SpeechBubble()
    toolbar = VerticalToolbar()
    widgets = (bubble, dock, toolbar, pet)
    for widget in widgets:
        widget.show()
    app.processEvents()

    canvas = QImage(1536, 864, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(QColor(248, 251, 255))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    background = QLinearGradient(0, 0, canvas.width(), canvas.height())
    background.setColorAt(0.0, QColor(252, 253, 255))
    background.setColorAt(0.68, QColor(247, 251, 255))
    background.setColorAt(1.0, QColor(239, 248, 254))
    painter.fillRect(QRectF(0, 0, canvas.width(), canvas.height()), background)

    pet_point = QPoint(1079, 348)
    placements = (
        (
            bubble,
            QPoint(
                pet_point.x() - bubble.width() + theme.BUBBLE_PET_OVERLAP,
                pet_point.y() - bubble.height() + theme.BUBBLE_VERTICAL_OVERLAP,
            ),
        ),
        (
            dock,
            QPoint(
                pet_point.x() + pet.width() // 2 - dock.width() // 2,
                pet_point.y() + pet.height() + theme.DOCK_ANCHOR_GAP,
            ),
        ),
        (
            toolbar,
            QPoint(
                pet_point.x() + pet.width() + theme.TOOLBAR_ANCHOR_GAP,
                pet_point.y() + pet.height() // 2 - toolbar.height() // 2,
            ),
        ),
        (pet, pet_point),
    )
    for widget, point in placements:
        painter.drawPixmap(point, widget.grab())
    painter.end()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(OUTPUT), "PNG"):
        return 1
    for widget in widgets:
        widget.close()
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
