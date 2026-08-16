"""Low-density three-agent dock anchored below Firefly."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from . import theme


AGENTS = (
    ("claude", "Claude", theme.CLAUDE_ORANGE),
    ("codex", "Codex", theme.CODEX_SLATE),
    ("chatgpt", "ChatGPT", theme.CHATGPT_GREEN),
)
NO_TELEMETRY_STATE = "unavailable"


class AgentItem(QFrame):
    activated = Signal(str)

    def __init__(self, agent_id: str, name: str, icon_color, state: str, parent=None):
        super().__init__(parent)
        self.agent_id = agent_id
        self._display_name = name
        self._selected = False
        self._hovered = False
        self._compact = False
        self.setObjectName("agentItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAccessibleName(f"{name} agent")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_SM, theme.SPACE_XS, theme.SPACE_SM, theme.SPACE_XS)
        layout.setSpacing(theme.SPACE_SM)
        self._layout = layout

        self._icon = theme.VectorIcon(agent_id, icon_color, 27, self)
        layout.addWidget(self._icon, 0, Qt.AlignVCenter)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(0)
        self._name_label = QLabel(name)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._name_label.setStyleSheet(theme.primary_label_style())
        copy.addWidget(self._name_label)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(0)
        self._dot = theme.StatusDot(state, self)
        status_row.addWidget(self._dot)
        status_row.addStretch(1)
        copy.addLayout(status_row)
        layout.addLayout(copy, 1)
        self.set_state(state)
        self._refresh_style()

    @property
    def state(self) -> str:
        return self._dot.state

    @property
    def selected(self) -> bool:
        return self._selected

    def set_state(self, state: str) -> None:
        self._dot.set_state(state)
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        if self._compact:
            self.setToolTip(self._display_name)
        else:
            status = "No telemetry" if self._dot.state == NO_TELEMETRY_STATE else self._dot.state.title()
            self.setToolTip(f"{self._display_name} · {status}")

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self._refresh_style()

    def apply_scale(self) -> None:
        compact = theme.ui_scale() < theme.FULL_DOCK_SCALE_THRESHOLD
        self._icon.setFixedSize(theme.scaled_px(27), theme.scaled_px(27))
        self._dot.setFixedSize(theme.scaled_px(10), theme.scaled_px(10))
        self._name_label.setStyleSheet(theme.primary_label_style())
        if compact != self._compact:
            self._compact = compact
            self._name_label.setVisible(not compact)
            self._refresh_tooltip()
        if compact:
            self._layout.setContentsMargins(
                0,
                theme.scaled_px(theme.SPACE_XS),
                0,
                theme.scaled_px(theme.SPACE_XS),
            )
            self._layout.setSpacing(theme.scaled_px(theme.SPACE_XXS))
        else:
            self._layout.setContentsMargins(
                theme.scaled_px(theme.SPACE_SM),
                theme.scaled_px(theme.SPACE_XS),
                theme.scaled_px(theme.SPACE_SM),
                theme.scaled_px(theme.SPACE_XS),
            )
            self._layout.setSpacing(theme.scaled_px(theme.SPACE_SM))

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
            self.activated.emit(self.agent_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_style(self) -> None:
        self.setStyleSheet(
            theme.interactive_surface_style(
                "agentItem",
                selected=self._selected,
                hovered=self._hovered,
            )
        )


class AgentDock(QWidget):
    agent_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Agents")
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

        self._items: dict[str, AgentItem] = {}
        self._separators: list[QFrame] = []
        for index, (agent_id, name, color) in enumerate(AGENTS):
            if index:
                separator = QFrame(card)
                separator.setFixedSize(1, 27)
                separator.setStyleSheet(theme.separator_style(vertical=True))
                layout.addWidget(separator, 0, Qt.AlignVCenter)
                self._separators.append(separator)
            item = AgentItem(agent_id, name, color, NO_TELEMETRY_STATE, card)
            item.activated.connect(self.select_agent)
            layout.addWidget(item, 1)
            self._items[agent_id] = item

        self.select_agent("codex", emit_signal=False)

    @property
    def selected_agent(self) -> str:
        for agent_id, item in self._items.items():
            if item.selected:
                return agent_id
        return ""

    def state_for(self, agent_id: str) -> str | None:
        item = self._items.get(agent_id)
        return item.state if item else None

    def set_agent_state(self, agent_id: str, state: str) -> None:
        item = self._items.get(agent_id)
        if item is not None:
            item.set_state(state)

    def apply_scale(self) -> None:
        compact = theme.ui_scale() < theme.FULL_DOCK_SCALE_THRESHOLD
        self._compact = compact
        base = theme.COMPACT_DOCK_SIZE if compact else theme.DOCK_SIZE
        self.setFixedSize(theme.scaled_px(base.width()), theme.scaled_px(base.height()))
        self._root_layout.setContentsMargins(
            theme.scaled(theme.SHADOW_MARGIN),
            theme.scaled(theme.SHADOW_MARGIN - 3),
            theme.scaled(theme.SHADOW_MARGIN),
            theme.scaled(theme.SHADOW_MARGIN),
        )
        self._card_layout.setContentsMargins(
            theme.scaled_px(theme.SPACE_XS),
            theme.scaled_px(theme.SPACE_XS),
            theme.scaled_px(theme.SPACE_XS),
            theme.scaled_px(theme.SPACE_XS),
        )
        self._card_layout.setSpacing(theme.scaled_px(theme.SPACE_XXS))
        for separator in self._separators:
            separator.setFixedSize(theme.scaled_px(1), theme.scaled_px(27))
        for item in self._items.values():
            item.apply_scale()

    def select_agent(self, agent_id: str, *, emit_signal: bool = True) -> None:
        if agent_id not in self._items:
            return
        for key, item in self._items.items():
            item.set_selected(key == agent_id)
        if emit_signal:
            self.agent_selected.emit(agent_id)
