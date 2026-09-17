"""Firefly-styled Quick Tools popover.

Renders one card per registered Quick Tool.  The popover is provider-agnostic:
external plugins register their own manifests (and optional capability note)
into the registry, and this popover only renders them.  Each card carries an
On/Off toggle (plugin enablement) — no Open button: Quick Tools is the plugin
enable/disable manager, not a launcher.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.quick_tools import QuickToolManifest, QuickToolsRegistry

from . import theme
from .popover_base import PopoverBase


class _QuickToolCard(QFrame):
    """One compact plugin card: icon + name + availability + On/Off toggle.

    ``availability`` (dot + READY/OFFLINE text) and ``enabled`` (the On/Off
    toggle) are independent: availability reflects plugin dependencies,
    enabled reflects the user's choice.
    """

    enabled_changed = Signal(str, bool)  # (tool_id, enabled)
    open_requested = Signal(str)         # (tool_id) — explicit "打开" action

    # Cards that expose an explicit open/launch action (launch the plugin's own
    # UI). Kept as a small explicit set so unrelated cards stay toggle-only.
    _OPENABLE_TOOL_IDS = ("tju-info-retrieval",)

    def __init__(
        self,
        manifest: QuickToolManifest,
        parent=None,
        *,
        is_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.manifest = manifest
        self.setObjectName("quickToolCard")
        self.setStyleSheet(
            theme.interactive_surface_style("quickToolCard", selected=False, hovered=True)
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        root.setSpacing(theme.SPACE_XS)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        icon = theme.VectorIcon(manifest.icon, theme.CYAN_ACCENT, 22, self)
        header.addWidget(icon, 0, Qt.AlignVCenter)
        name = QLabel(manifest.name)
        name.setStyleSheet(theme.primary_label_style())
        header.addWidget(name, 1, Qt.AlignVCenter)
        self._dot = theme.StatusDot("unavailable", self)
        header.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._status = QLabel("OFFLINE")
        self._status.setStyleSheet(theme.secondary_label_style())
        header.addWidget(self._status, 0, Qt.AlignVCenter)

        # On/Off enablement toggle — the primary (only) card interaction.
        self._toggle = QPushButton("On")
        self._toggle.setObjectName("pluginToggle")
        self._toggle.setCheckable(True)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setStyleSheet(self._toggle_style())
        self._toggle.setFixedWidth(theme.scaled_px(44))
        self._toggle.setFixedHeight(theme.scaled_px(24))
        self._toggle.setChecked(bool(is_enabled(manifest.id)) if is_enabled else True)
        self._refresh_toggle_text()
        self._toggle.toggled.connect(self._on_toggled)
        header.addWidget(self._toggle, 0, Qt.AlignVCenter)
        root.addLayout(header)

        description = QLabel(manifest.description)
        description.setWordWrap(True)
        description.setStyleSheet(theme.secondary_label_style())
        root.addWidget(description)

        self._capability = QLabel("")
        self._capability.setStyleSheet(theme.secondary_label_style())
        self._capability.setVisible(False)
        root.addWidget(self._capability)

        self._guard_note = QLabel("")
        self._guard_note.setWordWrap(True)
        self._guard_note.setStyleSheet(theme.secondary_label_style())
        self._guard_note.setVisible(False)
        root.addWidget(self._guard_note)

        # AUTH_REQUIRED 状态说明（区别于 capability note——那个已被 _guard_note 占用）。
        self._status_note = QLabel("")
        self._status_note.setWordWrap(True)
        self._status_note.setStyleSheet(
            "color: rgba(200, 140, 40, 255); background: transparent;"
            "font-family: " + theme.FONT_FAMILY + "; font-size: 8pt;"
        )
        self._status_note.setVisible(False)
        root.addWidget(self._status_note)

        # 轻量「打开」动作（仅 openable 卡片）：启动插件自己的 UI，绝不从 UI
        # 直接 subprocess——统一走 PluginLoader/plugin.open。
        self._open_btn = QPushButton("打开", self)
        self._open_btn.setObjectName("pluginOpenBtn")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.setStyleSheet(self._open_style())
        self._open_btn.setFixedHeight(theme.scaled_px(24))
        self._open_btn.clicked.connect(
            lambda _=False: self.open_requested.emit(self.manifest.id)
        )
        self._open_btn.setVisible(self.manifest.id in self._OPENABLE_TOOL_IDS)
        root.addWidget(self._open_btn, 0, Qt.AlignLeft)

        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)
        self._feedback.setStyleSheet(theme.secondary_label_style())
        self._feedback.setVisible(False)
        root.addWidget(self._feedback)

    @staticmethod
    def _open_style() -> str:
        return (
            "QPushButton {"
            "  background: rgba(235, 237, 241, 200);"
            "  border: 1px solid rgba(205, 209, 216, 220);"
            "  border-radius: 12px;"
            "  color: rgba(90, 96, 108, 255);"
            "  font-family: " + theme.FONT_FAMILY + "; font-size: 8pt; font-weight: 700;"
            "  padding: 2px 12px;"
            "}"
            "QPushButton:hover {"
            "  border: 1px solid rgba(64, 174, 255, 160);"
            "  color: rgba(28, 120, 200, 255);"
            "}"
        )

    def show_feedback(self, message: str) -> None:
        """Transient user-facing outcome of the open action (never a traceback)."""
        message = (message or "").strip()
        self._feedback.setText(message)
        self._feedback.setVisible(bool(message))

    @staticmethod
    def _toggle_style() -> str:
        return (
            "QPushButton {"
            "  background: rgba(235, 237, 241, 200);"
            "  border: 1px solid rgba(205, 209, 216, 220);"
            "  border-radius: 12px;"
            "  color: rgba(90, 96, 108, 255);"
            "  font-family: " + theme.FONT_FAMILY + "; font-size: 8pt; font-weight: 700;"
            "}"
            "QPushButton:checked {"
            "  background: rgba(64, 174, 255, 40);"
            "  border: 1px solid rgba(64, 174, 255, 200);"
            "  color: rgba(28, 120, 200, 255);"
            "}"
            "QPushButton:hover {"
            "  border: 1px solid rgba(64, 174, 255, 160);"
            "}"
        )

    def _refresh_toggle_text(self) -> None:
        self._toggle.setText("On" if self._toggle.isChecked() else "Off")

    def _on_toggled(self, _checked: bool) -> None:
        self._refresh_toggle_text()
        self.enabled_changed.emit(self.manifest.id, self._toggle.isChecked())

    def set_enabled(self, enabled: bool) -> None:
        """Sync the toggle from the outside (e.g. persisted state)."""
        self._toggle.blockSignals(True)
        self._toggle.setChecked(bool(enabled))
        self._toggle.blockSignals(False)
        self._refresh_toggle_text()

    @property
    def is_enabled(self) -> bool:
        return self._toggle.isChecked()

    @property
    def status_text(self) -> str:
        return self._status.text()

    def set_capability(self, capability: str, note: str = "") -> None:
        self._capability.setText(capability)
        self._capability.setVisible(bool(capability))
        self._guard_note.setText(note)
        self._guard_note.setVisible(bool(note))

    def set_status(self, status: str) -> None:
        # 唯一状态源 = status_provider()（plugin.status()）。这里只做展示映射：
        # AUTH_REQUIRED → "AUTH REQUIRED"（同一下划线原文，仅 UI 排版）。
        display = "AUTH REQUIRED" if status == "AUTH_REQUIRED" else (status or "OFFLINE")
        self._status.setText(display)
        self._dot.set_state("success" if status == "READY" else "unavailable")
        # AUTH_REQUIRED 时给一行轻量说明（登录需恢复；Open 仍可用）。
        self._status_note.setText(
            "天津大学登录状态需要恢复" if status == "AUTH_REQUIRED" else ""
        )
        self._status_note.setVisible(status == "AUTH_REQUIRED")


class QuickToolsPopover(PopoverBase):
    workspace_requested = Signal()

    def __init__(
        self,
        registry: QuickToolsRegistry,
        parent=None,
        *,
        plugin_is_enabled: Callable[[str], bool] | None = None,
        plugin_set_enabled: Callable[[str, bool], None] | None = None,
    ) -> None:
        super().__init__(width=theme.POPOVER_WIDTH, parent=parent)
        self.registry = registry
        # Optional enablement controller (normally the PluginLoader bridge).
        # Without a controller every card defaults to On and toggling is a
        # no-op for persistence, keeping the popover usable standalone/tests.
        self._plugin_is_enabled = plugin_is_enabled or (lambda tool_id: True)
        self._plugin_set_enabled = plugin_set_enabled
        self._cards: dict[str, _QuickToolCard] = {}

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        title = QLabel("Quick Tools")
        title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(title)
        header.addStretch(1)
        switch = QPushButton("Workspace")
        switch.setObjectName("switchWorkspace")
        switch.setCursor(Qt.PointingHandCursor)
        switch.setStyleSheet(theme.link_button_style("switchWorkspace"))
        switch.clicked.connect(self.workspace_requested.emit)
        header.addWidget(switch)
        self.content_layout.addLayout(header)

        section = QLabel("LOCAL TOOLS")
        section.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(section)

        for registration in registry.all():
            self._add_card(registration)

    def _add_card(self, registration) -> None:
        card = _QuickToolCard(
            registration.manifest, self, is_enabled=self._plugin_is_enabled
        )
        card.enabled_changed.connect(self._on_tool_enabled_changed)
        card.open_requested.connect(self._on_tool_open_requested)
        card.set_status(registration.status_provider())
        if registration.capability:
            card.set_capability(registration.capability, registration.capability_note)
        if registration.status_changed is not None:
            registration.status_changed.connect(
                lambda status, _message, current=card: current.set_status(status)
            )
        self._cards[registration.manifest.id] = card
        self.content_layout.addWidget(card)

    def _on_tool_enabled_changed(self, tool_id: str, enabled: bool) -> None:
        """Toggle only changes the plugin's enabled state — never opens,
        navigates, or switches a workspace."""
        if self._plugin_set_enabled is not None:
            self._plugin_set_enabled(tool_id, enabled)

    def _on_tool_open_requested(self, tool_id: str) -> None:
        """Explicit「打开」→ the plugin's own open (plugin.open / open_ui).

        Never spawns a subprocess from the UI: every entry (Quick Tools /
        科研助手 / chat command) shares the SAME plugin.open_ui()."""
        registration = self.registry.get(tool_id)
        card = self._cards.get(tool_id)
        if registration is None:
            return
        try:
            message = registration.open_handler()
        except Exception:  # noqa: BLE001 - never leak internals to the user
            message = "无法启动，请检查本地检索系统环境。"
        if card is not None:
            card.show_feedback(message if isinstance(message, str) else "")
        # open_ui 返回后重读真实状态（GUI launch 失败不得污染插件 health）。
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        """每次打开都重新读取 status_provider()（= plugin.status()）——绝不用旧缓存。"""
        super().showEvent(event)
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - 状态刷新失败不得影响浮窗
            pass

    def refresh(self) -> None:
        for registration in self.registry.all():
            card = self._cards.get(registration.manifest.id)
            if card is not None:
                card.set_status(registration.status_provider())
        self.adjustSize()

    def card_status(self, tool_id: str) -> str:
        card = self._cards.get(tool_id)
        return card.status_text if card is not None else ""

    def close(self) -> bool:
        return super().close()

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
