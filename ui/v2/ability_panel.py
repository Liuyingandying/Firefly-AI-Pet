"""AbilityPanel — capability launcher strip of the companion console.

Pure signal shell: each entry just emits ``requested(capability)``. The
console (or an app-level host) maps capabilities to existing entry points —
no business logic lives here, so nothing is duplicated.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from ui import theme

CAPABILITIES = (
    ("video", "📺", "视频阅读"),
    ("study", "📚", "学习模式"),
    ("screen_vision", "👁", "屏幕视觉"),
    ("document", "📄", "文档分析"),
    ("research", "🔬", "科研助手"),
    ("settings", "⚙", "设置"),
)


class AbilityPanel(QWidget):
    """Six quick entries mirroring the existing capabilities. Emits only."""

    requested = Signal(str)  # capability id from CAPABILITIES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        # 3 列平均分配可用宽度，防止列被压缩导致按钮文字省略。
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        for index, (capability, glyph, label) in enumerate(CAPABILITIES):
            button = QToolButton(self)
            button.setText(f"{glyph} {label}")
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QToolButton {{"
                f"  color: {theme.qcolor(theme.TEXT_PRIMARY)};"
                f"  background: rgba{theme.GLASS_BACKGROUND};"
                f"  border: 1px solid rgba{theme.GLASS_BORDER};"
                f"  border-radius: 10px; padding: 6px 10px;"
                f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
                f"}}"
                f"QToolButton:hover {{"
                f"  background: rgba{theme.GLASS_BACKGROUND_HOVER};"
                f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
                f"}}"
            )
            button.clicked.connect(
                lambda _=False, capability=capability: self.requested.emit(capability)
            )
            setattr(self, f"_{capability}_button", button)
            row, col = divmod(index, 3)
            grid.addWidget(button, row, col)

        # A11y / test label surface: buttons are reachable by capability id.
        self._buttons = {
            capability: getattr(self, f"_{capability}_button")
            for capability, _, _ in CAPABILITIES
        }

    def button(self, capability: str) -> QToolButton:
        return self._buttons[capability]

    # -- small typography helper reused by callers (kept here to avoid dup) --

    @staticmethod
    def hint_text(text: str) -> str:
        return text


__all__ = ["AbilityPanel", "CAPABILITIES"]
