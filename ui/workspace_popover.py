"""Workspace selector popover anchored to the vertical toolbar."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from . import theme
from .popover_base import PopoverBase


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


class _PathRow(QFrame):
    """Compact single-line workspace path with middle elision and a tooltip."""

    activated = Signal()

    def __init__(self, path: Path, *, selected: bool = False, valid: bool = True, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self._selected = selected
        self._hovered = False
        self._valid = valid
        self.setObjectName("wsPathRow")
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor if not selected else Qt.ArrowCursor)
        self.setAccessibleName(str(self.path))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_SM, theme.SPACE_XS, theme.SPACE_SM, theme.SPACE_XS)
        layout.setSpacing(theme.SPACE_SM)

        self._icon = theme.VectorIcon("workspace", theme.TEXT_SECONDARY, 20, self)
        layout.addWidget(self._icon, 0, Qt.AlignVCenter)

        self._label = QLabel(self)
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self._label, 1)

        self._apply_content()
        self._refresh_style()

    def set_path(self, path: Path, *, valid: bool = True) -> None:
        self.path = Path(path)
        self._valid = valid
        self._apply_content()

    def _apply_content(self) -> None:
        if self._selected:
            icon_color = theme.CYAN_ACCENT
        elif self._valid:
            icon_color = theme.TEXT_SECONDARY
        else:
            icon_color = theme.UNAVAILABLE_STATUS
        self._icon.set_color(icon_color)
        self._label.setStyleSheet(
            theme.primary_label_style() if self._valid else theme.secondary_label_style()
        )
        metrics = self._label.fontMetrics()
        elided = metrics.elidedText(str(self.path), Qt.ElideMiddle, theme.POPOVER_TEXT_WIDTH)
        self._label.setText(elided)
        if self._valid:
            self.setToolTip(str(self.path))
        else:
            self.setToolTip(f"{self.path}\nWorkspace no longer exists")

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
            if not self._selected:
                self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_style(self) -> None:
        self.setStyleSheet(
            theme.interactive_surface_style(
                "wsPathRow",
                selected=self._selected,
                hovered=self._hovered,
            )
        )


class WorkspacePopover(PopoverBase):
    workspace_activated = Signal(str)
    quick_tools_requested = Signal()
    # Compatibility signal for OverlayCoordinator's existing context seam.
    sessions_requested = Signal()

    def __init__(self, manager, parent=None):
        super().__init__(width=theme.POPOVER_WIDTH, parent=parent)
        self._manager = manager
        self._recent_rows: list[_PathRow] = []

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(theme.SPACE_SM)
        header = QLabel("Workspace")
        header.setStyleSheet(theme.primary_label_style(size=11))
        header_row.addWidget(header)
        header_row.addStretch(1)
        switch = QPushButton("Quick Tools")
        switch.setObjectName("switchQuickTools")
        switch.setCursor(Qt.PointingHandCursor)
        switch.setStyleSheet(theme.link_button_style("switchQuickTools"))
        switch.clicked.connect(self._request_quick_tools)
        header_row.addWidget(switch)
        self.content_layout.addLayout(header_row)

        self._current_section = QLabel("CURRENT")
        self._current_section.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(self._current_section)

        self._current_row = _PathRow(self._manager.current(), selected=True, valid=True)
        self.content_layout.addWidget(self._current_row)

        self._recent_section = QLabel("RECENT")
        self._recent_section.setStyleSheet(theme.section_label_style())
        self.content_layout.addWidget(self._recent_section)

        self._recent_layout = QVBoxLayout()
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(theme.SPACE_XXS)
        self.content_layout.addLayout(self._recent_layout)

        browse = QPushButton("Browse...")
        browse.setObjectName("browse")
        browse.setCursor(Qt.PointingHandCursor)
        browse.setStyleSheet(theme.popover_button_style("browse"))
        browse.clicked.connect(self._browse_clicked)
        self.content_layout.addWidget(browse)

        self._status = QLabel("")
        self._status.setStyleSheet(theme.secondary_label_style())
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        self.content_layout.addWidget(self._status)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(2600)
        self._status_timer.timeout.connect(self._clear_status)

        self.refresh()

    def _request_quick_tools(self) -> None:
        self.quick_tools_requested.emit()
        self.sessions_requested.emit()

    def refresh(self) -> None:
        current = self._manager.current()
        self._current_row.set_path(current, valid=True)
        self._rebuild_recent(current)
        self.adjustSize()

    def recent_rows(self) -> list[_PathRow]:
        return list(self._recent_rows)

    def _rebuild_recent(self, current: Path) -> None:
        self._clear_recent_rows()
        current_key = _norm(current)
        for path in self._manager.recents():
            if _norm(path) == current_key:
                continue
            row = _PathRow(path, selected=False, valid=path.is_dir())
            row.activated.connect(lambda p=path: self._on_recent_clicked(p))
            self._recent_layout.addWidget(row)
            self._recent_rows.append(row)
        self._recent_section.setVisible(bool(self._recent_rows))

    def _clear_recent_rows(self) -> None:
        for row in self._recent_rows:
            row.hide()
            self._recent_layout.removeWidget(row)
            row.deleteLater()
        self._recent_rows.clear()

    def _on_recent_clicked(self, path: Path) -> None:
        result = self._manager.add_or_select(path)
        if result is None:
            self._show_status("That workspace no longer exists.")
            return
        self.refresh()
        self.workspace_activated.emit(str(result))

    def _browse_clicked(self) -> None:
        current = self._manager.current()
        selected = QFileDialog.getExistingDirectory(self, "Choose workspace", str(current))
        if not selected:
            return
        result = self._manager.add_or_select(selected)
        if result is None:
            self._show_status("That folder cannot be used as a workspace.")
            return
        self.refresh()
        self.workspace_activated.emit(str(result))

    def _show_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(True)
        self.adjustSize()
        self._status_timer.start()

    def _clear_status(self) -> None:
        self._status.clear()
        self._status.setVisible(False)
        self.adjustSize()
