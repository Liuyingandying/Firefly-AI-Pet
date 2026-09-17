"""Sidebar — left menu column of the companion console (UI V2).

Phase UI-4B-3: Anthropic 简洁工作区 + 星穹铁道菜单式导航. The column keeps
the SAME CharacterHeader / AbilityPanel instances the console owns (signal
wiring and the ability→console mapping are untouched); this module only
re-homes them and restyles the ability buttons into a quiet menu list:

- identity card: avatar/name strip + 「AI 智能伙伴」 tag + ● 在线 status
- menu: text-only entries (emoji dropped), hover = pale cyan + cyan text
- bottom: low-key auxiliary actions (设置 / 关于 / 新建对话)

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
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.v2.ability_panel import CAPABILITIES
from ui.v2.recent_sessions import RecentSessionsCard, UserInfoCard

SIDEBAR_WIDTH = 240

_MENU_BUTTON_STYLE = (
    "QToolButton {"
    f"  color: rgba{theme.V2.TEXT_MAIN};"
    "  background: transparent; border: none; border-radius: 10px;"
    f"  padding: 7px 4px; text-align: left;"
    f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
    "}"
    "QToolButton:hover {"
    f"  background: rgba{theme.V2.CARD_BG_USER};"
    f"  color: rgba{theme.V2.PRIMARY_BLUE};"
    "}"
)

_BOTTOM_BUTTON_STYLE = (
    "QPushButton {"
    f"  color: rgba{theme.V2.TEXT_SECONDARY};"
    "  background: transparent; border: none; border-radius: 8px;"
    f"  padding: 4px 8px; text-align: left;"
    f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
    "}"
    "QPushButton:hover {"
    f"  color: rgba{theme.V2.TEXT_MAIN};"
    f"  background: rgba{theme.V2.CARD_BG_USER};"
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
    """Left column: identity card + ability menu + low-key bottom actions."""

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

        # -- Identity card: avatar/name strip + tag + online status ---------
        identity_card = QFrame(self)
        identity_card.setObjectName("identityCard")
        identity_card.setStyleSheet(
            f"#identityCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 16px;"
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
        meta_row.addWidget(_small_label("● 在线", theme.MINT_STATUS))
        identity_layout.addLayout(meta_row)

        # -- Ability menu: restyle the existing buttons in place ------------
        # (AbilityPanel's file/signals are untouched; emoji is dropped and
        # the entries become text-only menu rows.)
        self._apply_menu_style(ability)

        # -- Bottom auxiliary actions ---------------------------------------
        self._action_buttons: dict[str, QPushButton] = {}
        for action_id, label in (("settings", "设置"), ("about", "关于"),
                                 ("new_chat", "新建对话")):
            button = QPushButton(label, self)
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
        actions_row.addWidget(self._action_buttons["new_chat"])

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
        root.setSpacing(12)
        root.addWidget(identity_card)
        root.addWidget(_small_label("能力", theme.V2.TEXT_SECONDARY))
        root.addWidget(ability)
        # v1.4: 最近会话卡片占据中部弹性行 (stretch 1), 内部 QScrollArea 滚动,
        # 数量多时不再把底部 用户卡/动作行 挤出窗口; 少于可视高度时自然留白。
        root.addWidget(self._recent_card, 1)
        root.addWidget(_divider(self))
        root.addLayout(actions_row)
        root.addWidget(self._user_card)

    # ---------------------------------------------------------------- utils

    @staticmethod
    def _apply_menu_style(ability: QWidget) -> None:
        """Restyle the AbilityPanel buttons into text-only menu entries.

        Only visual properties are touched — signals, object identity and the
        console's ability→handler mapping stay exactly as they are.
        """
        labels = {cap: label for cap, _, label in CAPABILITIES}
        for capability, _, _ in CAPABILITIES:
            button = ability.button(capability)
            button.setText(labels[capability])  # drop the leading emoji
            button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumHeight(36)
            button.setStyleSheet(_MENU_BUTTON_STYLE)

    def action_button(self, action_id: str) -> QPushButton:
        return self._action_buttons[action_id]

    def set_current_session(self, session_id: str | None) -> None:
        """Delegate: highlight the active session row in the recent card."""
        self._recent_card.set_current_session(session_id)

    def refresh_recent_sessions(self) -> None:
        """Delegate: repopulate the recent list from the provider."""
        self._recent_card.reload()


__all__ = ["Sidebar", "SIDEBAR_WIDTH"]
