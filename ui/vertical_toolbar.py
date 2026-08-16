"""Three-action vertical toolbar for the Phase 8 visual shell."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from . import theme


TOOLBAR_ACTIONS = (
    ("companion", "Companion", "star"),
    ("workspace", "Workspace", "hexagon"),
    ("settings", "Settings", "gear"),
)


class ToolbarItem(QFrame):
    activated = Signal(str)

    def __init__(self, action_id: str, tooltip: str, icon_kind: str, parent=None):
        super().__init__(parent)
        self.action_id = action_id
        self._selected = False
        self._hovered = False
        self.setObjectName("toolbarItem")
        self.setFixedSize(56, 58)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._icon = theme.VectorIcon(icon_kind, theme.TEXT_SECONDARY, 28, self)
        layout.addWidget(self._icon, 0, Qt.AlignCenter)
        self._refresh_style()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._icon.set_color(theme.CYAN_ACCENT if selected else theme.TEXT_SECONDARY)
        self._refresh_style()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.action_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_style(self) -> None:
        self.setStyleSheet(
            theme.toolbar_surface_style(
                "toolbarItem",
                selected=self._selected,
                hovered=self._hovered,
            )
        )


class VerticalToolbar(QWidget):
    action_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Toolbar")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedSize(theme.TOOLBAR_SIZE)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SHADOW_MARGIN - 2, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN - 2, theme.SHADOW_MARGIN)
        root.setSpacing(0)

        card = theme.GlassPanel(theme.RADIUS_TOOLBAR, self)
        theme.apply_soft_shadow(card)
        root.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(theme.SPACE_XXS, theme.SPACE_SM, theme.SPACE_XXS, theme.SPACE_SM)
        layout.setSpacing(theme.SPACE_XXS)
        layout.addStretch(1)

        self._items: dict[str, ToolbarItem] = {}
        for index, (action_id, tooltip, icon_kind) in enumerate(TOOLBAR_ACTIONS):
            if index:
                separator = QFrame(card)
                separator.setFixedSize(27, 1)
                separator.setStyleSheet(theme.separator_style(vertical=False))
                layout.addWidget(separator, 0, Qt.AlignHCenter)
            item = ToolbarItem(action_id, tooltip, icon_kind, card)
            item.activated.connect(self.select_action)
            layout.addWidget(item, 0, Qt.AlignHCenter)
            self._items[action_id] = item
        layout.addStretch(1)
        self.select_action("companion", emit_signal=False)

    @property
    def selected_action(self) -> str:
        for action_id, item in self._items.items():
            if item.selected:
                return action_id
        return ""

    def select_action(self, action_id: str, *, emit_signal: bool = True) -> None:
        if action_id not in self._items:
            return
        for key, item in self._items.items():
            item.set_selected(key == action_id)
        if emit_signal:
            self.action_requested.emit(action_id)
