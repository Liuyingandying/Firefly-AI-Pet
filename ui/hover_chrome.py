"""Hover chrome: the transient controls around the Firefly character.

Interaction model (2026-09 hover spec):

- Default shows the character alone. The two bars (top platform dock +
  side function toolbar), the top-right "×" exit pill and the "打开 UI"
  entry are hidden and never intercept clicks while hidden.
- Hovering ANY part of the cluster — the character, the two bars, the
  greeting bubble, the Ask pill, the exit pill or the open-UI entry —
  reveals the bars and the exit pill. Leaving the whole region starts a
  300 ms grace collapse; re-entering cancels it.
- A single left-click on the character toggles the "打开 UI" entry (it
  never opens the console directly). Auto-collapse re-arms the entry.
  PetOverlay reports clicks only for genuine clicks — drags never reach
  the toggle.
- While an anchored panel (popovers, permission card, Short Ask,
  PageLens, ...) is open the collapse is suppressed, so working a
  dropdown never yanks the bars away.

Detection is a lightweight cursor poll (150 ms) against the union of the
tracked geometries instead of per-window Enter/Leave events: overlapping
always-on-top windows re-target Enter/Leave unpredictably (z-order,
show/hide synthesis), which made the event-based approach flaky.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QObject,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from . import theme

POLL_MS = 150
COLLAPSE_DELAY_MS = 300
FADE_MS = 180

_EXIT_SIZE = 36
# Exit pill anchor: hugging the character's right edge at head height.
_EXIT_ANCHOR_INSET_X = 14
_EXIT_ANCHOR_OFFSET_Y = 6
_ENTRY_SIZE = (88, 32)

_PILL_STYLE = """
    QWidget#chromePill {{
        background-color: {bg};
        border: 1px solid {border};
        border-radius: {radius}px;
    }}
    QWidget#chromePill:hover {{
        background-color: {hover_bg};
        border: 1px solid {hover_border};
    }}
    QLabel {{
        background: transparent;
        border: none;
        color: {label_color};
        font-size: {font_px}px;
        font-weight: 700;
    }}
"""


def _scaled(value: int | float) -> int:
    return int(theme.scaled(value))


class ChromePill(QWidget):
    """Small rounded top-level pill (exit button / open-UI entry)."""

    clicked = Signal()

    def __init__(
        self,
        text: str,
        tooltip: str,
        width: int,
        height: int,
        *,
        accent: str = "#35B8B8",
        label_color: str = "#33414B",
        bg: str = "#FFFFFF",
        border: str = "#DCE4EC",
        hover_bg: str = "#EAF6F6",
        hover_border: str = "#35B8B8",
        font_px: int = 13,
    ):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("chromePill")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(_scaled(width), _scaled(height))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        radius = _scaled(12) if width > 30 else _scaled(height) // 2
        self.setStyleSheet(
            _PILL_STYLE.format(
                radius=radius,
                bg=bg,
                border=border,
                hover_bg=hover_bg,
                hover_border=hover_border,
                label_color=label_color,
                font_px=_scaled(font_px),
            )
        )

        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setGeometry(0, 0, self.width(), self.height())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ExitPill(ChromePill):
    def __init__(self):
        # Solid purple disc with a white ×: unmistakable on any background.
        super().__init__(
            "×",
            "退出程序",
            _EXIT_SIZE,
            _EXIT_SIZE,
            bg="#7C4DFF",
            border="#7C4DFF",
            hover_bg="#6936E8",
            hover_border="#6936E8",
            label_color="#FFFFFF",
            font_px=18,
        )


class OpenUiPill(ChromePill):
    def __init__(self):
        super().__init__("打开 UI", "打开主界面", *_ENTRY_SIZE, accent="#35B8B8")


class HoverChromeController(QObject):
    """Owns the hover-revealed chrome and the collapse grace timer."""

    def __init__(
        self,
        pet,
        toolbar,
        dock,
        coordinator,
        parent=None,
        *,
        bubble=None,
        ask_pill=None,
    ):
        super().__init__(parent)
        self.pet = pet
        self.toolbar = toolbar
        self.dock = dock
        self.coordinator = coordinator
        self.fade_ms = FADE_MS

        self.exit_button = ExitPill()
        self.exit_button.clicked.connect(coordinator.exit_requested.emit)
        self.open_ui_entry = OpenUiPill()
        self.open_ui_entry.clicked.connect(coordinator.console_requested.emit)

        # Region members: cursor inside any VISIBLE one holds the chrome up.
        # The bubble/Ask pill overlap the character, so they count too.
        self._region = [pet, toolbar, dock, self.exit_button, self.open_ui_entry]
        for extra in (bubble, ask_pill):
            if extra is not None:
                self._region.append(extra)
        # The chrome widgets fade; the pet/bubble/ask keep their own lifecycle.
        self._fading = (toolbar, dock, self.exit_button, self.open_ui_entry)

        self._shown = False
        self._armed = False
        self._anims: dict[QWidget, QPropertyAnimation] = {}

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(COLLAPSE_DELAY_MS)
        self._collapse_timer.timeout.connect(self._collapse)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self.on_tick)
        self._poll_timer.start()

    # -- cursor injection for tests ---------------------------------------

    def _cursor_pos(self) -> QPoint:
        return QCursor.pos()

    # -- poll tick ---------------------------------------------------------

    def on_tick(self) -> None:
        cursor = self._cursor_pos()
        inside = any(
            widget.isVisible() and widget.frameGeometry().contains(cursor)
            for widget in self._region
        )
        if inside:
            self._collapse_timer.stop()
            if not self._shown:
                self._shown = True
                self.coordinator.reposition()
                for widget in (self.toolbar, self.dock, self.exit_button):
                    self._fade_in(widget)
                if self._armed:
                    self._fade_in(self.open_ui_entry)
                self.pet.raise_()
            return
        if not self._shown:
            return
        if self.coordinator.anchored_panel_open():
            return  # hold; a dropdown/panel keeps the chrome anchored
        if not self._collapse_timer.isActive():
            self._collapse_timer.start()

    def toggle_entry(self) -> None:
        """Pet left-click: show the entry if hidden, hide it if shown."""
        self._shown = True
        self._collapse_timer.stop()
        if self._armed:
            self._armed = False
            self._fade_out(self.open_ui_entry)
        else:
            self._armed = True
            self.coordinator.reposition()
            self._fade_in(self.open_ui_entry)
        for widget in (self.toolbar, self.dock, self.exit_button):
            self._fade_in(widget)
        self.pet.raise_()

    # -- collapse ----------------------------------------------------------

    def _collapse(self) -> None:
        if self.coordinator.anchored_panel_open():
            # A panel is holding the chrome up; retry until it clears.
            self._collapse_timer.start()
            return
        self._shown = False
        self._armed = False
        for widget in self._fading:
            self._fade_out(widget)
        # A greeting bubble is part of the chrome — it never auto-hides on
        # its own, so it would pin the bars forever without this callback.
        self.coordinator.on_chrome_collapsed()

    # -- fade primitives ----------------------------------------------------

    def _stop_anim(self, widget: QWidget) -> None:
        anim = self._anims.pop(widget, None)
        if anim is None:
            return
        try:
            anim.stop()
        except RuntimeError:
            # DeleteWhenStopped already reclaimed the C++ object.
            pass

    def _track_anim(self, widget: QWidget, anim: QPropertyAnimation) -> None:
        self._anims[widget] = anim
        # Drop the reference when DeleteWhenStopped reclaims the object.
        anim.destroyed.connect(lambda _obj=None, w=widget: self._anims.pop(w, None))

    def _fade_in(self, widget: QWidget) -> None:
        self._stop_anim(widget)
        widget.setWindowOpacity(0.0)
        widget.show()
        widget.raise_()
        anim = QPropertyAnimation(widget, b"windowOpacity", widget)
        anim.setDuration(self.fade_ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._track_anim(widget, anim)

    def _fade_out(self, widget: QWidget) -> None:
        self._stop_anim(widget)
        if not widget.isVisible():
            return
        if self.fade_ms <= 0:
            widget.hide()
            widget.setWindowOpacity(1.0)
            return
        anim = QPropertyAnimation(widget, b"windowOpacity", widget)
        anim.setDuration(self.fade_ms)
        anim.setStartValue(widget.windowOpacity())
        anim.setEndValue(0.0)
        anim.finished.connect(self._finish_fade_out)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._track_anim(widget, anim)

    def _finish_fade_out(self) -> None:
        widget = self.sender().targetObject()
        if widget is not None:
            widget.hide()
            widget.setWindowOpacity(1.0)

    # -- geometry ------------------------------------------------------------

    def reposition_chrome(self) -> None:
        """Anchor the exit pill just above the character's top-right corner
        and the entry above the pet, clamped so both stay visible and
        clickable near screen edges."""
        clamp = self.coordinator._clamp_point
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()

        # "×" hugs the character's right edge at head height (user-marked
        # spot), slightly inside the body outline.
        exit_point = QPoint(
            pet_geo.right()
            - self.exit_button.width()
            + _scaled(_EXIT_ANCHOR_INSET_X),
            pet_geo.top() + _scaled(_EXIT_ANCHOR_OFFSET_Y),
        )
        self.exit_button.move(clamp(exit_point, self.exit_button, available))

        entry_point = QPoint(
            pet_geo.center().x() - self.open_ui_entry.width() // 2,
            pet_geo.top() - self.open_ui_entry.height() - _scaled(6),
        )
        entry_point = clamp(entry_point, self.open_ui_entry, available)
        if entry_point.y() >= pet_geo.top():
            # Not enough headroom (pet near the screen top): drop the entry
            # below the character instead of covering it.
            entry_point.setY(pet_geo.bottom() + _scaled(6))
            entry_point = clamp(entry_point, self.open_ui_entry, available)
        self.open_ui_entry.move(entry_point)

    def apply_scale(self) -> None:
        self.exit_button.setFixedSize(_scaled(_EXIT_SIZE), _scaled(_EXIT_SIZE))
        self.open_ui_entry.setFixedSize(
            _scaled(_ENTRY_SIZE[0]), _scaled(_ENTRY_SIZE[1])
        )
        self.reposition_chrome()

    def reset_hidden(self) -> None:
        """Force the deterministic hidden baseline (no chrome visible).

        Window platforms may synthesize an Enter event when a top-level
        shows under the cursor, which would legitimately reveal the chrome.
        Startup code and tests call this to pin the "character alone"
        baseline regardless of that synthesis.
        """
        self._shown = False
        self._armed = False
        self._collapse_timer.stop()
        for widget in self._fading:
            self._fade_out(widget)

    def close(self) -> None:
        self._poll_timer.stop()
        self._collapse_timer.stop()
        for widget in (self.exit_button, self.open_ui_entry):
            self._stop_anim(widget)
            widget.close()
