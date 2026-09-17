"""Lightweight Settings popover for Firefly (Phase 8B.4).

A transient, content-adaptive, light-glass sibling of the Workspace and Session
popovers. It owns no persistence: every toggle writes straight through the
shared Qt-free SettingsManager. Reset position is a one-shot action, not a
stored preference.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core import windows_autostart

from . import theme
from .popover_base import PopoverBase


TOGGLE_KEYS = (
    ("notifications_enabled", "Task notifications"),
    ("keep_awake_enabled", "Keep awake"),
    ("greeting_on_startup", "Greeting on startup"),
    ("launch_on_startup", "Launch on startup"),
    ("screen_vision_fast_mode", "Fast screen vision"),
)

TOGGLE_TOOLTIPS = {
    "screen_vision_fast_mode":
        "ON: One-shot screen understanding, TJU-Qwen first (fastest).\n"
        "OFF: Resilient fallback chain (TJU -> GLM -> DeepSeek).",
}


class _PillToggle(QPushButton):
    """Small low-priority on/off pill. No new dependencies, theme tokens only."""

    def __init__(self, state: bool, parent=None):
        super().__init__(parent)
        self._state = bool(state)
        self.setFixedSize(44, 20)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh()

    @property
    def state(self) -> bool:
        return self._state

    def set_state(self, state: bool) -> None:
        self._state = bool(state)
        self._refresh()

    def _refresh(self) -> None:
        if self._state:
            background_css = theme.css_color(theme.GLASS_BACKGROUND_SELECTED)
            border_css = theme.css_color(theme.CYAN_ACCENT)
            color_css = theme.css_color(theme.TEXT_PRIMARY)
            text = "On"
        else:
            background_css = "transparent"
            border_css = theme.css_color(theme.GLASS_BORDER)
            color_css = theme.css_color(theme.TEXT_SECONDARY)
            text = "Off"
        self.setText(text)
        self.setStyleSheet(
            "QPushButton {"
            f" background-color: {background_css};"
            f" border: 1px solid {border_css};"
            " border-radius: 9px;"
            f" color: {color_css};"
            f" font-family: '{theme.FONT_FAMILY}';"
            f" font-size: {theme.FONT_SIZE_SMALL}pt;"
            f" font-weight: {theme.FONT_WEIGHT_MEDIUM};"
            " padding: 0px 6px;"
            "}"
        )


class _ToggleRow(QFrame):
    def __init__(
        self,
        label: str,
        key: str,
        state: bool,
        on_toggled: Callable[[str, bool], None],
        parent=None,
        tooltip: str = "",
    ):
        super().__init__(parent)
        self.key = key
        self._on_toggled = on_toggled

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_SM)

        name = QLabel(label)
        name.setStyleSheet(theme.primary_label_style())
        if tooltip:
            name.setToolTip(tooltip)
        layout.addWidget(name, 1)

        self._toggle = _PillToggle(state, self)
        self._toggle.clicked.connect(self._on_click)
        layout.addWidget(self._toggle, 0, Qt.AlignVCenter)

    @property
    def state(self) -> bool:
        return self._toggle.state

    def set_state(self, state: bool) -> None:
        self._toggle.set_state(state)

    def _on_click(self) -> None:
        self._toggle.set_state(not self._toggle.state)
        self._on_toggled(self.key, self._toggle.state)


class SettingsPopover(PopoverBase):
    reset_position_requested = Signal()
    memory_requested = Signal()

    def __init__(self, manager, parent=None, autostart=None):
        super().__init__(width=theme.POPOVER_WIDTH, parent=parent)
        self._manager = manager
        self._autostart = autostart if autostart is not None else windows_autostart
        self._rows: dict[str, _ToggleRow] = {}
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(3200)
        self._status_timer.timeout.connect(self._clear_status)

        header = QLabel("Settings")
        header.setStyleSheet(theme.primary_label_style(size=11))
        self.content_layout.addWidget(header)

        general = QLabel("GENERAL")
        general.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(general)

        for key, label in TOGGLE_KEYS:
            row = _ToggleRow(
                label, key, self._initial_state(key), self._on_toggle, self._card,
                tooltip=TOGGLE_TOOLTIPS.get(key, ""),
            )
            self._rows[key] = row
            self.content_layout.addWidget(row)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(self._status_style())
        self._status_label.setWordWrap(True)
        self._status_label.setVisible(False)
        self.content_layout.addWidget(self._status_label)

        separator = QFrame(self._card)
        separator.setFixedHeight(1)
        separator.setStyleSheet(theme.separator_style(vertical=False))
        self.content_layout.addWidget(separator)

        self._reset_btn = QPushButton("Reset position")
        self._reset_btn.setObjectName("resetPosition")
        self._reset_btn.setCursor(Qt.PointingHandCursor)
        self._reset_btn.setStyleSheet(theme.link_button_style("resetPosition"))
        self._reset_btn.clicked.connect(self.reset_position_requested.emit)
        self.content_layout.addWidget(self._reset_btn, 0, Qt.AlignLeft)

        memory_separator = QFrame(self._card)
        memory_separator.setFixedHeight(1)
        memory_separator.setStyleSheet(theme.separator_style(vertical=False))
        self.content_layout.addWidget(memory_separator)

        memory = QLabel("MEMORY")
        memory.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(memory)

        self._memory_btn = QPushButton("View Memory")
        self._memory_btn.setObjectName("viewMemory")
        self._memory_btn.setCursor(Qt.PointingHandCursor)
        self._memory_btn.setToolTip("Review, export, or clear Firefly memory")
        self._memory_btn.setStyleSheet(theme.link_button_style("viewMemory"))
        self._memory_btn.clicked.connect(self.memory_requested.emit)
        self.content_layout.addWidget(self._memory_btn, 0, Qt.AlignLeft)

        # Phase 2.2: read-only AI provider status (no switches, no edits).
        ai_separator = QFrame(self._card)
        ai_separator.setFixedHeight(1)
        ai_separator.setStyleSheet(theme.separator_style(vertical=False))
        self.content_layout.addWidget(ai_separator)

        ai_header = QLabel("AI STATUS")
        ai_header.setStyleSheet(theme.section_label_style())
        ai_header.setToolTip(
            "Read-only view of the providers Firefly currently uses. "
            "Routing cannot be changed here."
        )
        self.content_layout.addWidget(ai_header)

        self._ai_status_label = QLabel("")
        self._ai_status_label.setObjectName("aiStatus")
        self._ai_status_label.setWordWrap(True)
        self._ai_status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._ai_status_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {theme.FONT_SIZE_SMALL}pt;"
        )
        self.content_layout.addWidget(self._ai_status_label)

        self._manager.connect(self._on_settings_changed)
        self.refresh()

    # -- state in -------------------------------------------------------

    def refresh(self) -> None:
        for key, row in self._rows.items():
            row.set_state(self._initial_state(key))
        self._refresh_ai_status()
        self.adjustSize()

    def _refresh_ai_status(self) -> None:
        """Render the read-only provider status snapshot. Never throws:
        a failing status view must not break Settings."""
        try:
            from core.providers.status import ProviderStatusService

            text = ProviderStatusService().render_text()
        except Exception:  # noqa: BLE001 - read-only view degrades to empty
            text = "AI status unavailable"
        self._ai_status_label.setText(text)

    # -- test helpers ---------------------------------------------------

    def toggle_state(self, key: str) -> bool:
        row = self._rows.get(key)
        return row.state if row is not None else False

    # -- listeners ------------------------------------------------------

    def _on_toggle(self, key: str, value: bool) -> None:
        if key == "notifications_enabled":
            self._manager.set_notifications_enabled(value)
        elif key == "keep_awake_enabled":
            self._manager.set_keep_awake_enabled(value)
        elif key == "greeting_on_startup":
            self._manager.set_greeting_on_startup(value)
        elif key == "launch_on_startup":
            self._on_toggle_launch_on_startup(value)
        elif key == "screen_vision_fast_mode":
            self._manager.set_screen_vision_fast_mode(value)

    def _initial_state(self, key: str) -> bool:
        if key == "launch_on_startup":
            return bool(self._autostart.is_enabled())
        return bool(getattr(self._manager, key))

    def _on_toggle_launch_on_startup(self, value: bool) -> None:
        """Enable/disable the Startup shortcut; revert to real state on failure.

        The shortcut's on-disk existence is the source of truth, so after the
        operation we re-read it and snap the toggle back to reality instead of
        claiming success. A lightweight inline message explains the failure.
        """
        try:
            if value:
                self._autostart.enable()
            else:
                self._autostart.disable()
        except Exception:
            pass
        actual = bool(self._autostart.is_enabled())
        if actual != value:
            self._rows["launch_on_startup"].set_state(actual)
            self._show_status("Couldn't enable startup." if value else "Couldn't disable startup.")

    def _show_status(self, text: str) -> None:
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))
        self._status_timer.start()
        self.adjustSize()

    def _clear_status(self) -> None:
        self._status_label.setText("")
        self._status_label.setVisible(False)
        self.adjustSize()

    def _status_style(self) -> str:
        return (
            f"color: {theme.css_color(theme.ERROR_STATUS)}; background: {theme.TRANSPARENT}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(theme.FONT_SIZE_SMALL)}pt;"
        )

    def _on_settings_changed(self, _prefs) -> None:
        self.refresh()
