"""CharacterHeader — the 流萤 identity strip of the companion console.

Pure rendering widget: it never reads state sources itself. The console feeds
it via ``set_state`` / ``set_task`` / ``set_ability`` (derived from the
existing ``AgentEvent`` stream and runtime activity state), so the header
stays a dumb view with no core imports.

ui美化版（「流萤」发光紫视觉语言）:

- 头像: 品牌紫渐变球 + 微弱外发光 0 0 12px rgba(155,109,255,0.3),
  呼应萤火虫发光的产品意象
- 副标题精简为「待机中」, 去掉冗余描述
- 状态 chip 跟随品牌紫 / 状态色
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme

_STATUS_LABELS = {
    "idle": "待机中",
    "working": "工作中",
    "tool_running": "运行工具",
    "waiting_input": "等你输入",
    "success": "完成",
    "error": "出错",
}

# 状态色: 主品牌紫为工作态, 其余沿用语义色。
_STATUS_COLORS = {
    "idle": theme.V2.TEXT_SECONDARY,
    "working": theme.V2.PRIMARY,
    "tool_running": theme.V2.PRIMARY,
    "waiting_input": theme.WAITING_STATUS,
    "success": theme.MINT_STATUS,
    "error": theme.ERROR_STATUS,
}

_TASK_DEFAULT = "待机中"


class _AvatarOrb(QWidget):
    """Glowing brand-purple orb with a soft outer halo (萤火微光)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(48, 48)
        self._active = True
        # 外发光 0 0 12px rgba(155,109,255,0.3) — QSS 无 box-shadow 的等价实现
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(12)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(155, 109, 255, 90))
        self.setGraphicsEffect(self._glow)

    def set_active(self, active: bool) -> None:
        self._active = active
        self._glow.setColor(
            QColor(155, 109, 255, 90 if active else 28)
        )
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        gradient = QRadialGradient(19, 17, 26)
        top = theme.V2.PRIMARY_HOVER if self._active else theme.V2.TEXT_SECONDARY
        gradient.setColorAt(0.0, QColor(214, 199, 255, 255))
        gradient.setColorAt(0.55, QColor(*top[:3], 255))
        gradient.setColorAt(1.0, QColor(*theme.V2.PRIMARY[:3], 120))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(1.5, 1.5, 45, 45))
        # 高光点
        painter.setBrush(QColor(255, 255, 255, 170))
        painter.drawEllipse(QRectF(14, 11, 7, 7))


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
        self._status_chip = QLabel("待机中")
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
            f"color: {theme.css_color(theme.V2.PRIMARY)}; font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
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
        """Light chip with a colored label — 品牌紫视觉语言."""
        return (
            f"background: rgba{theme.V2.PRIMARY_SOFT}; "
            f"color: {theme.css_color(color)}; "
            f"border: 1px solid rgba{theme.V2.PRIMARY_GLOW}; "
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
