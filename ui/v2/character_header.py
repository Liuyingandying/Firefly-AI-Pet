"""CharacterHeader — the 流萤 identity strip of the companion console.

Pure rendering widget: it never reads state sources itself. The console feeds
it via ``set_state`` / ``set_task`` / ``set_ability`` (derived from the
existing ``AgentEvent`` stream and runtime activity state), so the header
stays a dumb view with no core imports.

The avatar is a code-drawn glowing orb (theme accent), not a licensed asset.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme

_STATUS_LABELS = {
    "idle": "待机",
    "working": "工作中",
    "tool_running": "运行工具",
    "waiting_input": "等你输入",
    "success": "完成",
    "error": "出错",
}

# Phase UI-2.5: status colors remapped onto the V2 deep-space palette so the
# header reads correctly on the dark console background.
_STATUS_COLORS = {
    "idle": theme.V2.TEXT_SECONDARY,
    "working": theme.V2.PRIMARY_BLUE,
    "tool_running": theme.V2.PRIMARY_BLUE,
    "waiting_input": theme.WAITING_STATUS,
    "success": theme.MINT_STATUS,
    "error": theme.ERROR_STATUS,
}

_TASK_DEFAULT = "空闲中，随时找我聊天、读视频、看文档～"


class _AvatarOrb(QFrame):
    """Glowing circular avatar placeholder drawn from the V2 palette."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(48, 48)
        self._style(active=True)

    def _style(self, active: bool) -> None:
        border = theme.V2.ACCENT_PURPLE if active else theme.V2.BORDER_SOFT
        glow = theme.V2.GLOW_PURPLE if active else theme.V2.GLOW_BLUE
        self.setStyleSheet(
            f"background: qradialgradient(cx:0.4, cy:0.35, radius:1.0, "
            f"fx:0.4, fy:0.35, stop:0 rgba{theme.V2.CARD_BG_USER}, "
            f"stop:0.6 rgba{theme.V2.ACCENT_PURPLE}, stop:1 rgba{glow}); "
            f"border: 2px solid rgba{border}; border-radius: 24px;"
        )

    def set_active(self, active: bool) -> None:
        self._style(active=active)


class CharacterHeader(QWidget):
    """Identity strip: avatar + name + state chip + current task + ability."""

    def __init__(
        self,
        name: str = "流萤",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._state = "idle"
        self._task = _TASK_DEFAULT
        self._ability = ""

        self._avatar = _AvatarOrb(self)
        self._name_label = QLabel(name)
        self._name_label.setStyleSheet(
            f"color: {theme.css_color(theme.V2.TEXT_MAIN)}; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt; font-weight: 700;"
        )
        self._status_chip = QLabel("待机")
        self._status_chip.setStyleSheet(self._chip_style(theme.V2.TEXT_SECONDARY))
        self._task_label = QLabel(self._task)
        self._task_label.setWordWrap(True)
        # 描述必须能横向扩展、垂直按需增高（绝不 elide 截断）。
        self._task_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        self._task_label.setStyleSheet(
            f"color: {theme.css_color(theme.V2.TEXT_SECONDARY)}; font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        self._ability_label = QLabel("")
        self._ability_label.setStyleSheet(
            f"color: {theme.css_color(theme.V2.ACCENT_PURPLE)}; font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(self._name_label)
        name_row.addWidget(self._status_chip)
        name_row.addStretch(1)
        text_col.addLayout(name_row)
        text_col.addWidget(self._task_label)
        text_col.addWidget(self._ability_label)

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(12)
        root.addWidget(self._avatar)
        root.addLayout(text_col, 1)

    @staticmethod
    def _chip_style(color: tuple[int, int, int, int]) -> str:
        """Dark glass chip with a colored label — V2 deep-space style."""
        return (
            f"background: rgba{theme.V2.CARD_BG}; "
            f"color: {theme.css_color(color)}; "
            f"border: 1px solid rgba{theme.V2.BORDER_SOFT}; "
            f"border-radius: 9px; padding: 2px 10px; font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

    def set_state(self, state: str) -> None:
        """Update the state chip (consumes RuntimeActivityState values)."""
        self._state = state
        color = _STATUS_COLORS.get(state, theme.V2.TEXT_SECONDARY)
        label = _STATUS_LABELS.get(state, state)
        self._status_chip.setText(label)
        self._status_chip.setStyleSheet(self._chip_style(color))
        self._avatar.set_active(state not in ("idle", "error"))

    def set_task(self, text: str) -> None:
        """Show the current/last task line (head of the last assistant turn)."""
        self._task = (text or _TASK_DEFAULT).strip() or _TASK_DEFAULT
        if len(self._task) > 90:
            self._task = self._task[:90] + "…"
        self._task_label.setText(self._task)

    def set_ability(self, text: str) -> None:
        """Show the most recent capability that ran (e.g. 视频阅读/学习模式)."""
        self._ability = (text or "").strip()
        self._ability_label.setText(
            f"最近能力：{self._ability}" if self._ability else ""
        )
        self._ability_label.setVisible(bool(self._ability))

    @property
    def state(self) -> str:
        return self._state


__all__ = ["CharacterHeader"]
