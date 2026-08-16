"""Session overview popover anchored to the vertical toolbar.

Shows, per agent, whether a native Claude session / Codex thread exists for the
current workspace, plus lightweight Continue / New actions. Agent lifecycle
(state dots) stays sourced from StateMonitor and is intentionally independent
of session existence.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.quick_ask_metrics import session_hash_prefix

from . import theme
from .popover_base import PopoverBase


AGENTS = (
    ("claude", "Claude", theme.CLAUDE_ORANGE, "session"),
    ("codex", "Codex", theme.CODEX_SLATE, "thread"),
    ("chatgpt", "ChatGPT", theme.CHATGPT_GREEN, None),
)


class _AgentSection(QFrame):
    continue_requested = Signal(str)
    new_requested = Signal(str)

    def __init__(self, agent_id, name, color, kind, *, managed, parent=None):
        super().__init__(parent)
        self.agent_id = agent_id
        self._managed = managed
        self._kind = kind
        self.setObjectName("agentSection")
        self.setStyleSheet(
            theme.interactive_surface_style("agentSection", selected=False, hovered=False)
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE_SM, theme.SPACE_XS, theme.SPACE_SM, theme.SPACE_XS)
        root.setSpacing(theme.SPACE_XXS)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(theme.SPACE_SM)

        icon = theme.VectorIcon(agent_id, color, 20, self)
        top.addWidget(icon, 0, Qt.AlignVCenter)

        name_label = QLabel(name)
        name_label.setStyleSheet(theme.primary_label_style())
        top.addWidget(name_label, 0, Qt.AlignVCenter)

        self._dot = theme.StatusDot("unavailable", self)
        top.addWidget(self._dot, 0, Qt.AlignVCenter)

        top.addStretch(1)

        self._status_label = QLabel("No active session")
        self._status_label.setStyleSheet(theme.secondary_label_style())
        top.addWidget(self._status_label, 0, Qt.AlignVCenter)

        root.addLayout(top)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(theme.SPACE_XS)
        actions.addStretch(1)

        self._continue_btn = QPushButton("Continue")
        self._continue_btn.setObjectName("continue")
        self._continue_btn.setCursor(Qt.PointingHandCursor)
        self._continue_btn.setStyleSheet(theme.popover_button_style("continue"))
        self._continue_btn.clicked.connect(lambda: self.continue_requested.emit(self.agent_id))
        actions.addWidget(self._continue_btn)

        self._new_btn = QPushButton("New")
        self._new_btn.setObjectName("new")
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setStyleSheet(theme.popover_button_style("new"))
        self._new_btn.clicked.connect(lambda: self.new_requested.emit(self.agent_id))
        actions.addWidget(self._new_btn)

        root.addLayout(actions)

        self._continue_btn.setVisible(False)
        if not managed:
            self._new_btn.setVisible(False)

    @property
    def session_text(self) -> str:
        return self._status_label.text()

    @property
    def has_continue(self) -> bool:
        return self._continue_btn.isVisible()

    @property
    def has_new(self) -> bool:
        return self._new_btn.isVisible()

    def set_state(self, state: str) -> None:
        self._dot.set_state(state)

    def set_session(self, ref) -> None:
        if not self._managed:
            self._status_label.setText("No managed session")
            self._status_label.setToolTip("")
            self._continue_btn.setVisible(False)
            self._new_btn.setVisible(False)
            return
        if ref is not None:
            self._status_label.setText(f"Current {self._kind}")
            # Never surface the full native id in UI text; only a short hash
            # prefix for diagnosis.
            self._status_label.setToolTip(
                session_hash_prefix(ref.native_session_id) or ""
            )
            self._continue_btn.setVisible(True)
        else:
            self._status_label.setText("No active session")
            self._status_label.setToolTip("")
            self._continue_btn.setVisible(False)
        self._new_btn.setVisible(True)


class SessionPopover(PopoverBase):
    continue_requested = Signal(str)
    new_requested = Signal(str)
    workspace_requested = Signal()

    def __init__(self, session_manager, workspace_manager, parent=None):
        super().__init__(width=theme.POPOVER_WIDTH, parent=parent)
        self._session_manager = session_manager
        self._workspace_manager = workspace_manager
        self._sections: dict[str, _AgentSection] = {}

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        title = QLabel("Sessions")
        title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(title)
        header.addStretch(1)
        switch = QPushButton("Workspace")
        switch.setObjectName("switchWorkspace")
        switch.setCursor(Qt.PointingHandCursor)
        switch.setStyleSheet(theme.link_button_style("switchWorkspace"))
        switch.clicked.connect(lambda: self.workspace_requested.emit())
        header.addWidget(switch)
        self.content_layout.addLayout(header)

        ws_section = QLabel("WORKSPACE")
        ws_section.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(ws_section)

        self._workspace_label = QLabel()
        self._workspace_label.setStyleSheet(theme.primary_label_style())
        self.content_layout.addWidget(self._workspace_label)

        for agent_id, name, color, kind in AGENTS:
            section = _AgentSection(agent_id, name, color, kind, managed=kind is not None)
            section.continue_requested.connect(self.continue_requested.emit)
            section.new_requested.connect(self.new_requested.emit)
            self._sections[agent_id] = section
            self.content_layout.addWidget(section)

        self._session_manager.connect(self._on_session_changed)
        self._workspace_manager.connect(self._on_workspace_changed)
        self.refresh()

    # -- state in -------------------------------------------------------

    def set_agent_state(self, agent_id: str, state: str) -> None:
        section = self._sections.get(agent_id)
        if section is not None:
            section.set_state(state)

    def refresh(self) -> None:
        current = self._workspace_manager.current()
        self._workspace_label.setText(current.name or str(current))
        self._workspace_label.setToolTip(str(current))
        for agent_id, section in self._sections.items():
            if agent_id == "chatgpt":
                section.set_session(None)
            else:
                section.set_session(self._session_manager.get(agent_id, current))
        self.adjustSize()

    # -- test helpers ---------------------------------------------------

    def agent_session_text(self, agent_id: str) -> str:
        section = self._sections.get(agent_id)
        return section.session_text if section is not None else ""

    def agent_has_continue(self, agent_id: str) -> bool:
        section = self._sections.get(agent_id)
        return section.has_continue if section is not None else False

    def agent_has_new(self, agent_id: str) -> bool:
        section = self._sections.get(agent_id)
        return section.has_new if section is not None else False

    # -- listeners ------------------------------------------------------

    def _on_session_changed(self, _change) -> None:
        self.refresh()

    def _on_workspace_changed(self, _path) -> None:
        self.refresh()
