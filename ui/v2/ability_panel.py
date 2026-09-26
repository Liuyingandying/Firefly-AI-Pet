"""AbilityPanel — capability launcher list of the companion console.

ui美化（「流萤」发光紫视觉语言 · 初版浅底菜单, v7 还原）:

- 竖排菜单: 图标 + 文字, 项间距 8px（呼吸感）, 宿主容器为白卡
- 线性图标: 视频阅读=播放 / 学习模式=书本 / 屏幕视觉=显示器 /
  文档分析=文档 / 科研助手=烧杯 / 设置=齿轮（theme.vector_icon）
- 选中态: 淡紫填充 + 紫色文字 + 左侧 2px 紫色竖条（互斥单选）; hover 同色

Pure signal shell: each entry just emits ``requested(capability)``. The
console (or an app-level host) maps capabilities to existing entry points —
no business logic lives here, so nothing is duplicated.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme

# (capability_id, icon_kind, label) — icon_kind 见 theme.VectorIcon.draw_kind
CAPABILITIES = (
    ("video", "play", "视频阅读"),
    ("study", "book", "学习模式"),
    ("screen_vision", "monitor", "屏幕视觉"),
    ("document", "document", "文档分析"),
    ("research", "flask", "科研助手"),
    ("settings", "gear", "设置"),
)


def _menu_button_style() -> str:
    """选中态: 淡紫填充 + 紫字 + 左侧 2px 紫色竖条; hover 同色淡填充."""
    return (
        "QToolButton {"
        f"  color: rgba{theme.V2.TEXT_MAIN};"
        "  background: transparent; border: none;"
        f"  border-left: 2px solid transparent;"
        f"  border-radius: {theme.V2.RADIUS_CARD}px;"
        f"  padding: 7px 10px; text-align: left;"
        f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
        "}"
        "QToolButton:hover {"
        f"  background: rgba{theme.V2.PRIMARY_SOFT};"
        f"  color: rgba{theme.V2.PRIMARY};"
        "}"
        "QToolButton:checked {"
        f"  background: rgba{theme.V2.PRIMARY_SOFT};"
        f"  color: rgba{theme.V2.PRIMARY};"
        f"  border-left: 2px solid rgba{theme.V2.PRIMARY};"
        "  font-weight: 600;"
        "}"
    )


def _sync_icon(button: QToolButton, icon_kind: str, checked: bool) -> None:
    """选中/未选中切换图标颜色（QIcon 颜色不随 QSS 伪态变化, 手动重设）."""
    color = theme.V2.PRIMARY if checked else theme.V2.TEXT_SECONDARY
    button.setIcon(theme.vector_icon(icon_kind, color, 16))


class AbilityPanel(QWidget):
    """Six quick entries mirroring the existing capabilities. Emits only."""

    requested = Signal(str)  # capability id from CAPABILITIES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 关键: plain QWidget 子类默认不绘制 QSS 的 background/border
        # （宿主在 console 设置的白卡背景因此不会渲染）, 必须开启
        # WA_StyledBackground。
        self.setAttribute(Qt.WA_StyledBackground, True)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)  # 项间距 8px: 增加呼吸感

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        for capability, icon_kind, label in CAPABILITIES:
            button = QToolButton(self)
            button.setText(label)
            _sync_icon(button, icon_kind, False)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumHeight(36)
            button.setStyleSheet(_menu_button_style())
            button.toggled.connect(
                lambda checked, b=button, kind=icon_kind: _sync_icon(b, kind, checked)
            )
            button.clicked.connect(
                lambda _=False, capability=capability: self.requested.emit(capability)
            )
            self._group.addButton(button)
            setattr(self, f"_{capability}_button", button)
            column.addWidget(button)

        # A11y / test label surface: buttons are reachable by capability id.
        self._buttons = {
            capability: getattr(self, f"_{capability}_button")
            for capability, _, _ in CAPABILITIES
        }

    def button(self, capability: str) -> QToolButton:
        return self._buttons[capability]

    def set_active(self, capability: str | None) -> None:
        """同步选中态（宿主在模式切换时调用; None = 全部取消选中）."""
        for key, button in self._buttons.items():
            button.setChecked(key == capability)

    # -- small typography helper reused by callers (kept here to avoid dup) --

    @staticmethod
    def hint_text(text: str) -> str:
        return text


__all__ = ["AbilityPanel", "CAPABILITIES"]
