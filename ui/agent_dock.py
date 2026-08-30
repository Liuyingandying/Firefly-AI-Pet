"""Low-density agent launcher dock anchored below Firefly.

Four fixed launchers: Claude | Codex | Qwen | Z Code.

Claude / Codex / Qwen open their CLI in the currently selected workspace
(the single source of truth held by WorkspaceManager), each in a new visible
console. Z Code just opens/activates the desktop app — it never uses the
workspace as a CLI cwd. Launch failures surface a lightweight message via
``launch_message`` and never crash Firefly.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .agent_launcher import (
    launch_claude,
    launch_codex,
    launch_qwen_yolo,
    launch_zcode,
)

LAUNCHERS = (
    ("claude", "Claude", "C", theme.CLAUDE_ORANGE,
     "Open Claude CLI in current workspace"),
    ("codex", "Codex", "CX", theme.CODEX_SLATE,
     "Open Codex CLI in current workspace"),
    ("qwen", "Qwen", "Q", theme.QWEN_VIOLET,
     "Open Qwen CLI in YOLO mode"),
    ("zcode", "Z Code", "Z", theme.ZCODE_BLUE,
     "Open Z Code"),
)

_LAUNCHER_FUNCS = {
    "claude": launch_claude,
    "codex": launch_codex,
    "qwen": launch_qwen_yolo,
    "zcode": launch_zcode,
}


class _LauncherItem(QFrame):
    activated = Signal(str)

    def __init__(self, launcher_id: str, name: str, letter: str, color, tooltip: str, parent=None):
        super().__init__(parent)
        self._launcher_id = launcher_id
        self._tooltip_text = tooltip
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_XS, theme.SPACE_XS, theme.SPACE_XS, theme.SPACE_XS)
        layout.setSpacing(0)
        self._letter = QLabel(letter)
        self._letter.setAlignment(Qt.AlignCenter)
        self._letter.setStyleSheet(
            f"color: {theme.css_color(color)}; font-weight: {theme.FONT_WEIGHT_MEDIUM}; "
            f"font-size: 11pt; font-family: '{theme.FONT_FAMILY}';"
        )
        layout.addWidget(self._letter, 1)
        self._name = QLabel(name)
        self._name.setAlignment(Qt.AlignCenter)
        self._name.setStyleSheet(theme.secondary_label_style(size=7))
        layout.addWidget(self._name)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self._launcher_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class AgentDock(QWidget):
    launch_message = Signal(str)
    launch_agent = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Launchers")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedSize(theme.DOCK_SIZE)
        self._compact = False

        root = QHBoxLayout(self)
        root.setContentsMargins(theme.SHADOW_MARGIN, theme.SHADOW_MARGIN - 3, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN)
        root.setSpacing(0)
        self._root_layout = root

        card = theme.GlassPanel(theme.RADIUS_PILL, self)
        theme.apply_soft_shadow(card)
        root.addWidget(card)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(theme.SPACE_XS, theme.SPACE_XS, theme.SPACE_XS, theme.SPACE_XS)
        layout.setSpacing(theme.SPACE_XXS)
        self._card_layout = layout

        self._items: dict[str, _LauncherItem] = {}
        self._separators: list[QFrame] = []
        for index, (launcher_id, name, letter, color, tooltip) in enumerate(LAUNCHERS):
            if index:
                separator = QFrame(card)
                separator.setFixedSize(1, 27)
                separator.setStyleSheet(theme.separator_style(vertical=True))
                layout.addWidget(separator, 0, Qt.AlignVCenter)
                self._separators.append(separator)
            item = _LauncherItem(launcher_id, name, letter, color, tooltip, card)
            item.activated.connect(self._on_item_activated)
            layout.addWidget(item, 1)
            self._items[launcher_id] = item

    def _on_item_activated(self, launcher_id: str) -> None:
        self.launch_agent.emit(launcher_id)

    def apply_scale(self) -> None:
        if hasattr(theme, "apply_dock_scale"):
            theme.apply_dock_scale(self, self._items.values(), self._separators, self._card_layout, self._root_layout)
