"""Generic light-glass transient popover shell for Phase 8A.3 overlays."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import theme


class PopoverBase(QWidget):
    """Frameless, always-on-top, transient overlay with shared glass styling.

    Provides only generic behavior: glass surface, rounded corners, soft
    shadow, Escape-to-close, anchor show, and a transient-overlay marker.
    No business logic lives here.
    """

    dismissed = Signal()
    is_transient_overlay = True

    def __init__(self, *, width: int = theme.POPOVER_WIDTH, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Popover")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        root = QVBoxLayout(self)
        root.setContentsMargins(
            theme.SHADOW_MARGIN, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN
        )
        root.setSpacing(0)

        self._card = theme.GlassPanel(theme.RADIUS_CARD, self)
        theme.apply_soft_shadow(self._card)
        root.addWidget(self._card)

        self.content_layout = QVBoxLayout(self._card)
        self.content_layout.setContentsMargins(
            theme.SPACE_LG, theme.SPACE_MD, theme.SPACE_LG, theme.SPACE_LG
        )
        self.content_layout.setSpacing(theme.SPACE_XS)

        self.setFixedWidth(width)

        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._esc.activated.connect(self.dismiss)

    def show_at(self, point: QPoint) -> None:
        self.adjustSize()
        self.move(point)
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        self.dismissed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)
