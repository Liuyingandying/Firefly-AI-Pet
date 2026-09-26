"""Sidebar — left menu column of the companion console (UI V2).

ui美化版（「流萤」发光紫视觉语言）:

- 身份卡: 白卡 16px 圆角; 「在线」状态点为慢呼吸动效
  (ui.v2.motion.BreathingDot)
- 能力菜单: AbilityPanel 自带线性图标 + 选中态（淡紫填充 + 紫字 +
  左侧 2px 紫竖条）, 此处不再二次改样式
- 「新对话」: 加号图标按钮, hover 背景浅紫
- 底部: 设置 / 关于 对齐左下角; 用户条弱化（图标 + 小字）, 不抢主视觉

No business logic: the bottom buttons only emit ``action_requested``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.v2 import motion
from ui.v2.recent_sessions import RecentSessionsCard, UserInfoCard

SIDEBAR_WIDTH = 240

_BOTTOM_BUTTON_STYLE = (
    "QPushButton {"
    f"  color: rgba{theme.V2.TEXT_SECONDARY};"
    "  background: transparent; border: none;"
    f"  border-radius: {theme.V2.RADIUS_CARD}px;"
    f"  padding: 4px 8px; text-align: left;"
    f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
    "}"
    "QPushButton:hover {"
    f"  color: rgba{theme.V2.PRIMARY};"
    f"  background: rgba{theme.V2.PRIMARY_SOFT};"
    "}"
)

_NEW_CHAT_STYLE = (
    "QToolButton {"
    f"  color: rgba{theme.V2.PRIMARY};"
    f"  background: rgba{theme.V2.PRIMARY_SOFT};"
    f"  border: 1px solid rgba{theme.V2.PRIMARY_GLOW};"
    f"  border-radius: {theme.V2.RADIUS_CARD}px;"
    f"  padding: 6px 12px; text-align: left;"
    f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
    "}"
    "QToolButton:hover {"
    f"  background: rgba{theme.V2.PRIMARY_SOFT};"
    f"  border: 1px solid rgba{theme.V2.PRIMARY};"
    "}"
)


def _small_label(text: str, color: tuple[int, int, int, int]) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: rgba{color}; background: transparent;"
        f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
    )
    return label


def _divider(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color: rgba{theme.V2.BORDER_SOFT};")
    return line


class Sidebar(QWidget):
    """Left column: identity card + ability menu + recent chats + bottom."""

    action_requested = Signal(str)  # "settings" | "about" | "new_chat"
    session_clicked = Signal(str)   # recent-session title (display-only)
    rename_requested = Signal(str)  # recent-session id
    delete_requested = Signal(str)  # recent-session id

    def __init__(
        self,
        header: QWidget,
        ability: QWidget,
        recent_provider: Any | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.header = header
        self.ability = ability
        self.setFixedWidth(SIDEBAR_WIDTH)

        # Re-home the shared instances into this column. They keep their own
        # object identity and signal wiring; we only change their parent.
        header.setParent(self)
        ability.setParent(self)

        # -- Identity card: avatar/name strip + breathing online dot ---------
        identity_card = QFrame(self)
        identity_card.setObjectName("identityCard")
        identity_card.setStyleSheet(
            f"#identityCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: {theme.V2.RADIUS_CONTAINER}px;"
            f"}}"
        )
        identity_layout = QVBoxLayout(identity_card)
        identity_layout.setContentsMargins(10, 10, 10, 8)
        identity_layout.setSpacing(4)
        identity_layout.addWidget(header)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(4, 0, 4, 0)
        meta_row.setSpacing(6)
        meta_row.addWidget(_small_label("AI 智能伙伴", theme.V2.TEXT_SECONDARY))
        meta_row.addStretch(1)
        meta_row.addWidget(motion.BreathingDot(theme.MINT_STATUS, diameter=8))
        meta_row.addWidget(_small_label("在线", theme.V2.TEXT_SECONDARY))
        identity_layout.addLayout(meta_row)

        # -- 「新对话」: 加号图标 + hover 浅紫 --------------------------------
        self._new_chat_button = QToolButton(self)
        self._new_chat_button.setText("新对话")
        self._new_chat_button.setIcon(
            theme.vector_icon("plus", theme.V2.PRIMARY, 14)
        )
        self._new_chat_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._new_chat_button.setCursor(Qt.PointingHandCursor)
        self._new_chat_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._new_chat_button.setStyleSheet(_NEW_CHAT_STYLE)
        self._new_chat_button.clicked.connect(
            lambda: self.action_requested.emit("new_chat")
        )

        # -- Bottom auxiliary actions (左下角对齐) ----------------------------
        self._action_buttons: dict[str, QPushButton] = {}
        for action_id, label, icon_kind in (
            ("settings", "设置", "gear"),
            ("about", "关于", "sparkle"),
        ):
            button = QPushButton(label, self)
            if icon_kind:
                button.setIcon(theme.vector_icon(icon_kind, theme.V2.TEXT_SECONDARY, 13))
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(_BOTTOM_BUTTON_STYLE)
            button.clicked.connect(
                lambda _=False, action_id=action_id: self.action_requested.emit(action_id)
            )
            self._action_buttons[action_id] = button

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(4, 0, 4, 0)
        actions_row.setSpacing(4)
        actions_row.addWidget(self._action_buttons["settings"])
        actions_row.addWidget(self._action_buttons["about"])
        actions_row.addStretch(1)

        # -- Recent sessions + user info (left-bottom navigation) -----------
        # Display-only: the card emits session_clicked (relayed upward); the
        # user card reads the OS username and never touches any account system.
        self._recent_card = RecentSessionsCard(provider=recent_provider, parent=self)
        self._recent_card.session_clicked.connect(self.session_clicked)
        self._recent_card.rename_requested.connect(self.rename_requested)
        self._recent_card.delete_requested.connect(self.delete_requested)
        self._user_card = UserInfoCard(parent=self)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)
        root.addWidget(identity_card)
        # v7 还原: 「能力」标题放在白卡菜单外部（初版布局）
        root.addWidget(_small_label("能力", theme.V2.TEXT_SECONDARY))
        root.addWidget(ability)
        root.addWidget(self._new_chat_button)
        # 最近会话卡片占据中部弹性行 (stretch 1), 内部 QScrollArea 滚动,
        # 数量多时不再把底部 用户卡/动作行 挤出窗口。
        root.addWidget(self._recent_card, 1)
        root.addWidget(_divider(self))
        root.addLayout(actions_row)
        root.addWidget(self._user_card)

    # ---------------------------------------------------------------- utils

    @staticmethod
    def _apply_menu_style(ability: QWidget) -> None:
        """Back-compat shim: the menu style now lives in AbilityPanel itself."""
        from ui.v2.ability_panel import _menu_button_style

        for child in ability.findChildren(QToolButton):
            child.setStyleSheet(_menu_button_style())

    def action_button(self, action_id: str) -> QPushButton:
        return self._action_buttons[action_id]

    def new_chat_button(self) -> QToolButton:
        return self._new_chat_button

    def set_current_session(self, session_id: str | None) -> None:
        """Delegate: highlight the active session row in the recent card."""
        self._recent_card.set_current_session(session_id)

    def refresh_recent_sessions(self) -> None:
        """Delegate: repopulate the recent list from the provider."""
        self._recent_card.reload()


__all__ = ["Sidebar", "SIDEBAR_WIDTH"]
