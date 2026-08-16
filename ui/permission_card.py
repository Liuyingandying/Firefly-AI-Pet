"""High-priority permission notice anchored to Firefly.

Shown when Claude or Codex enters the `waiting` lifecycle state. This is a
notification entry point only: it detects, notifies, and offers a View action
that opens the agent's native interface. It never approves, denies, or alters
any approval policy.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}
PERMISSION_AGENTS = frozenset(AGENT_NAMES)


class PermissionCard(QWidget):
    view_requested = Signal(str)

    def __init__(self, parent=None):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        super().__init__(parent, flags)
        self.setWindowTitle("Firefly Permission")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedWidth(theme.POPOVER_WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(
            theme.SHADOW_MARGIN, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN, theme.SHADOW_MARGIN
        )
        root.setSpacing(0)

        self._card = theme.GlassPanel(theme.RADIUS_CARD, self)
        theme.apply_soft_shadow(self._card)
        root.addWidget(self._card)

        body = QVBoxLayout(self._card)
        body.setContentsMargins(theme.SPACE_LG, theme.SPACE_MD, theme.SPACE_LG, theme.SPACE_LG)
        body.setSpacing(theme.SPACE_XS)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        self._dot = theme.StatusDot("waiting", self._card)
        header.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._title = QLabel("")
        self._title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(self._title, 1)
        body.addLayout(header)

        self._subtitle = QLabel("Waiting for permission")
        self._subtitle.setStyleSheet(theme.secondary_label_style())
        body.addWidget(self._subtitle)

        self._agents_label = QLabel("")
        self._agents_label.setStyleSheet(theme.secondary_label_style())
        self._agents_label.setVisible(False)
        body.addWidget(self._agents_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(theme.SPACE_XS)
        actions.addStretch(1)
        self._view_btn = QPushButton("View")
        self._view_btn.setObjectName("viewPermission")
        self._view_btn.setCursor(Qt.PointingHandCursor)
        self._view_btn.setStyleSheet(theme.popover_button_style("viewPermission"))
        self._view_btn.clicked.connect(self._on_view_clicked)
        actions.addWidget(self._view_btn)
        body.addLayout(actions)

        self._primary_agent: str | None = None

    def _on_view_clicked(self) -> None:
        if self._primary_agent is None:
            return
        # One-shot per waiting episode: prevents spawning duplicate agent
        # terminals on repeated clicks without any approval side effects.
        self._view_btn.setEnabled(False)
        self.view_requested.emit(self._primary_agent)

    def show_for(self, agent_ids: list[str], primary_agent_id: str) -> None:
        self._primary_agent = primary_agent_id
        self._view_btn.setEnabled(True)

        names = [AGENT_NAMES.get(a, a.title()) for a in agent_ids]
        if len(names) == 1:
            self._title.setText(f"{names[0]} needs your approval")
            self._agents_label.setVisible(False)
        else:
            self._title.setText(f"{len(names)} agents need approval")
            self._agents_label.setText("  ·  ".join(names))
            self._agents_label.setVisible(True)

        self.adjustSize()
        self.show()
        self.raise_()

    def hide_card(self) -> None:
        self._primary_agent = None
        self.hide()
