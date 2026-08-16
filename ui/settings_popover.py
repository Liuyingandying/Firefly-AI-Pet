"""Lightweight Settings popover for Firefly (Phase 8B.4).

A transient, content-adaptive, light-glass sibling of the Workspace and Session
popovers. It owns no persistence: every toggle writes straight through the
shared Qt-free SettingsManager. Reset position is a one-shot action, not a
stored preference.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import theme
from .popover_base import PopoverBase


TOGGLE_KEYS = (
    ("notifications_enabled", "Task notifications"),
    ("keep_awake_enabled", "Keep awake"),
    ("greeting_on_startup", "Greeting on startup"),
)


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
    ):
        super().__init__(parent)
        self.key = key
        self._on_toggled = on_toggled

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_SM)

        name = QLabel(label)
        name.setStyleSheet(theme.primary_label_style())
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

    def __init__(self, manager, parent=None):
        super().__init__(width=theme.POPOVER_WIDTH, parent=parent)
        self._manager = manager
        self._rows: dict[str, _ToggleRow] = {}

        header = QLabel("Settings")
        header.setStyleSheet(theme.primary_label_style(size=11))
        self.content_layout.addWidget(header)

        general = QLabel("GENERAL")
        general.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(general)

        for key, label in TOGGLE_KEYS:
            row = _ToggleRow(label, key, bool(getattr(manager, key)), self._on_toggle, self._card)
            self._rows[key] = row
            self.content_layout.addWidget(row)

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

        self._manager.connect(self._on_settings_changed)
        self.refresh()

    # -- state in -------------------------------------------------------

    def refresh(self) -> None:
        for key, row in self._rows.items():
            row.set_state(bool(getattr(self._manager, key)))
        self.adjustSize()

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

    def _on_settings_changed(self, _prefs) -> None:
        self.refresh()
