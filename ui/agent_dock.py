"""Slim agent launcher dock anchored below Firefly.

Four fixed launchers: Claude | Codex | Qwen | Z Code.

Claude / Codex / Qwen open their CLI in the currently selected workspace
(the single source of truth held by WorkspaceManager), each in a new visible
console. Z Code just opens/activates the desktop app — it never uses the
workspace as a CLI cwd. Launch failures surface a lightweight message via
``launch_message`` and never crash Firefly.

The Qwen entry carries a small availability dot to the right of its label
that reflects whether Firefly's configured Tianjin University ``tju-llm`` API
is callable (``ui.tju_llm_health``). It does NOT reflect the Qwen CLI in any
way. The dot is purely informational — clicking it does nothing; clicking
anywhere else on the Qwen item still launches the CLI.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from character import CharacterDisplayNames
from . import theme
from .agent_launcher import (
    launch_claude,
    launch_codex,
    launch_qwen_yolo,
    launch_zcode,
)
from .tju_llm_health import TjuApiStatus, TjuLlmHealthChecker

LAUNCHERS = (
    ("claude", "Claude", "C", theme.CLAUDE_ORANGE,
     "Open Claude Code CLI in current workspace (local agent)"),
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


class _StatusDot(QWidget):
    """Tiny 7px TJU ``tju-llm`` availability dot shown right of the Qwen label.

    Colors: GREEN (tju-llm API callable), RED (tju-llm API clearly unusable),
    GRAY (unknown / still checking). This has nothing to do with the Qwen CLI
    binary or process. The dot is inert: it swallows its own mouse events so
    clicking it never launches anything and never reaches the item.
    """

    COLORS = {
        TjuApiStatus.UNKNOWN: theme.DOCK_STATUS_GRAY,
        TjuApiStatus.AVAILABLE: theme.DOCK_STATUS_GREEN,
        TjuApiStatus.UNAVAILABLE: theme.DOCK_STATUS_RED,
    }
    TOOLTIPS = {
        TjuApiStatus.UNKNOWN: "TJU tju-llm API status unknown",
        TjuApiStatus.AVAILABLE: "TJU tju-llm API available",
        TjuApiStatus.UNAVAILABLE: "TJU tju-llm API unavailable",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._status = TjuApiStatus.UNKNOWN
        self.setFixedSize(theme.DOCK_STATUS_DOT_SIZE, theme.DOCK_STATUS_DOT_SIZE)
        self.setToolTip(self.TOOLTIPS[self._status])
        self.setAccessibleName("TJU tju-llm API status")

    @property
    def status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        status = status if status in self.COLORS else TjuApiStatus.UNKNOWN
        if status != self._status:
            self._status = status
            self.setToolTip(self.TOOLTIPS[status])
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*self.COLORS[self._status]))
        painter.drawEllipse(self.rect())

    def mousePressEvent(self, event) -> None:
        event.accept()  # no action on the dot; never falls through to launch

    def mouseReleaseEvent(self, event) -> None:
        event.accept()


class _LauncherItem(QFrame):
    activated = Signal(str)

    def __init__(self, launcher_id: str, name: str, letter: str, color, tooltip: str,
                 *, status_dot: _StatusDot | None = None, parent=None):
        super().__init__(parent)
        self._launcher_id = launcher_id
        self._tooltip_text = tooltip
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.DOCK_ITEM_MARGIN, theme.DOCK_ITEM_MARGIN,
            theme.DOCK_ITEM_MARGIN, theme.DOCK_ITEM_MARGIN,
        )
        layout.setSpacing(0)
        self._letter = QLabel(letter)
        self._letter.setAlignment(Qt.AlignCenter)
        self._letter.setStyleSheet(
            f"color: {theme.css_color(color)}; font-weight: {theme.FONT_WEIGHT_MEDIUM}; "
            f"font-size: {theme.DOCK_LETTER_FONT_PT}pt; font-family: '{theme.FONT_FAMILY}';"
        )
        layout.addWidget(self._letter, 1)
        self._name = QLabel(name)
        self._name.setAlignment(Qt.AlignCenter)
        self._name.setStyleSheet(theme.secondary_label_style(size=theme.DOCK_NAME_FONT_PT))
        if status_dot is not None:
            name_row = QHBoxLayout()
            name_row.setContentsMargins(0, 0, 0, 0)
            name_row.setSpacing(theme.DOCK_STATUS_DOT_GAP)
            name_row.addStretch(1)
            name_row.addWidget(self._name)
            name_row.addWidget(status_dot)
            name_row.addStretch(1)
            layout.addLayout(name_row)
        else:
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

    def __init__(
        self,
        parent=None,
        *,
        display_names: CharacterDisplayNames | None = None,
    ):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        names = display_names or CharacterDisplayNames()
        self.setWindowTitle(f"{names.brand_name} Launchers")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedSize(theme.DOCK_SIZE)
        self._compact = False

        root = QHBoxLayout(self)
        root.setContentsMargins(
            theme.DOCK_SHADOW_MARGIN,
            theme.DOCK_SHADOW_MARGIN - 3,
            theme.DOCK_SHADOW_MARGIN,
            theme.DOCK_SHADOW_MARGIN,
        )
        root.setSpacing(0)
        self._root_layout = root

        card = theme.GlassPanel(theme.DOCK_CARD_RADIUS, self)
        theme.apply_soft_shadow(
            card,
            blur=theme.DOCK_SHADOW_BLUR,
            y_offset=theme.DOCK_SHADOW_OFFSET_Y,
            color=theme.DOCK_SHADOW_COLOR,
        )
        root.addWidget(card)
        self._card = card

        layout = QHBoxLayout(card)
        layout.setContentsMargins(
            theme.DOCK_CARD_MARGIN, theme.DOCK_CARD_MARGIN,
            theme.DOCK_CARD_MARGIN, theme.DOCK_CARD_MARGIN,
        )
        layout.setSpacing(theme.DOCK_ITEM_SPACING)
        self._card_layout = layout

        self._items: dict[str, _LauncherItem] = {}
        self._separators: list[QFrame] = []
        self._tju_dot: _StatusDot | None = None
        for index, (launcher_id, name, letter, color, tooltip) in enumerate(LAUNCHERS):
            if index:
                separator = QFrame(card)
                separator.setFixedSize(1, theme.DOCK_DIVIDER_HEIGHT)
                separator.setStyleSheet(theme.separator_style(vertical=True))
                layout.addWidget(separator, 0, Qt.AlignVCenter)
                self._separators.append(separator)
            status_dot = None
            if launcher_id == "qwen":
                status_dot = _StatusDot(card)
                self._tju_dot = status_dot
            item = _LauncherItem(
                launcher_id, name, letter, color, tooltip,
                status_dot=status_dot, parent=card,
            )
            item.activated.connect(self._on_item_activated)
            layout.addWidget(item, 1)
            self._items[launcher_id] = item

        # TJU tju-llm availability dot — background probe, never blocks the UI.
        self._tju_health = TjuLlmHealthChecker(parent=self)
        self._tju_health.status_changed.connect(self._on_tju_status)
        self.destroyed.connect(self._stop_tju_health)

    def _on_item_activated(self, launcher_id: str) -> None:
        self.launch_agent.emit(launcher_id)

    def _on_tju_status(self, status: str) -> None:
        if self._tju_dot is not None:
            self._tju_dot.set_status(status)

    def _stop_tju_health(self) -> None:
        self._tju_health.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._tju_health.start()

    def closeEvent(self, event) -> None:
        self._tju_health.stop()
        super().closeEvent(event)

    def set_display_names(self, display_names) -> None:
        """Refresh character labels (live switch)."""
        from character import CharacterDisplayNames

        self._display_names = display_names or CharacterDisplayNames()
        self.setWindowTitle(f"{self._display_names.brand_name} Launchers")

    def apply_scale(self) -> None:
        if hasattr(theme, "apply_dock_scale"):
            theme.apply_dock_scale(self, self._items.values(), self._separators, self._card_layout, self._root_layout)
