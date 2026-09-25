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

_EXIT_SIZE = 28
# Exit pill center, relative to the character outline's top-right corner.
_EXIT_ANCHOR_RIGHT = 24
_EXIT_ANCHOR_ABOVE = 0
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
    """Small rounded top-level pill (exit button / open-UI entry).

    The pill is painted in paintEvent() — stylesheet backgrounds silently
    fail on WA_TranslucentBackground top-level windows, which is exactly
    what made the exit control invisible on light documents.
    """

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
        paint_background: bool = True,
    ):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(_scaled(width), _scaled(height))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)

        self._bg = bg
        self._border = border
        self._hover_bg = hover_bg
        self._hover_border = hover_border
        self._label_color = label_color
        self._accent = accent
        self._font_px = _scaled(font_px)
        self._radius = min(_scaled(height) // 2, _scaled(14))
        self._paint_background = paint_background is True
        self._hovered = False

        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setGeometry(0, 0, self.width(), self.height())
        font = self._label.font()
        font.setPixelSize(self._font_px)
        font.setBold(True)
        self._label.setFont(font)
        self._label.setStyleSheet(f"color: {label_color}; background: transparent;")

    def paintEvent(self, event) -> None:
        if not self._paint_background:
            # Transparent variant: the bare glyph carries the whole look.
            return
        from PySide6.QtGui import QColor, QPainter, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        hovered = self._hovered
        painter.setBrush(
            QColor(self._hover_bg) if hovered else QColor(self._bg)
        )
        painter.setPen(
            QPen(
                QColor(self._hover_border) if hovered else QColor(self._border),
                1,
            )
        )
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), self._radius, self._radius)
        painter.end()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._label.setStyleSheet(
            f"color: {self._accent}; background: transparent;"
        )
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._label.setStyleSheet(
            f"color: {self._label_color}; background: transparent;"
        )
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ExitPill(ChromePill):
    def __init__(self):
        # Bare pink-purple × on a fully transparent background (user spec).
        # 点击 = 隐藏流萤（不退出），托盘图标找回。
        super().__init__(
            "×",
            "隐藏流萤（托盘图标找回）",
            _EXIT_SIZE,
            _EXIT_SIZE,
            bg="transparent",
            border="transparent",
            hover_bg="transparent",
            hover_border="transparent",
            label_color="#C77DFF",
            accent="#E3A8FF",
            font_px=17,
            paint_background=False,
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
        # × = 隐藏流萤（人物+全部浮层），不是退出；真正退出走托盘菜单。
        self.exit_button.clicked.connect(self._hide_pet)
        self.open_ui_entry = OpenUiPill()
        self.open_ui_entry.clicked.connect(coordinator.console_requested.emit)

        # Region members: cursor inside any VISIBLE one holds the chrome up.
        # The bubble/Ask pill overlap the character, so they count too.
        self._region = [pet, toolbar, dock, self.exit_button, self.open_ui_entry]
        for extra in (bubble, ask_pill):
            if extra is not None:
                self._region.append(extra)
        self._bubble = bubble
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
        inside = False
        for widget in self._region:
            if not widget.isVisible() or not widget.frameGeometry().contains(cursor):
                continue
            if widget is self._bubble and not self._shown:
                # 隐藏态下的提示气泡（"点托盘找回"）不算悬停区域：
                # 否则它会自己把信息栏再拉起来（淡出→弹出→再淡出）。
                continue
            inside = True
            break
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

    def _hide_pet(self) -> None:
        """× 点击：隐藏人物与全部浮层（不退出应用，托盘图标找回）。"""
        self.reset_hidden()
        self.coordinator.hide_firefly()

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
        """Anchor the exit pill just off the character's top-right shoulder
        and the entry above the character, clamped so both stay visible and
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

        # Anchor to the character's visible outline, not the padded window:
        # the GIF carries transparent margins, so window-edge anchoring left
        # the exit pill floating far from the body.
        body = self.pet.visible_body_rect()
        if body is not None:
            # The body rect is in WINDOW-LOCAL coordinates — translate by
            # the pet's top-left before comparing with global screen
            # geometry.  Mixing the two landed the pill at the screen's
            # top-left corner.
            body = body.translated(pet_geo.topLeft())
            anchor_top = body.top()
            anchor_right = body.right()
            anchor_bottom = body.bottom()
            anchor_center_x = body.center().x()
        else:
            anchor_top = pet_geo.top()
            anchor_right = pet_geo.right()
            anchor_bottom = pet_geo.bottom()
            anchor_center_x = pet_geo.center().x()

        # "×" floats just off the character's top-right shoulder — right of
        # the hair, level above the head top (user-tuned offset/size).
        exit_point = QPoint(
            anchor_right
            + _scaled(_EXIT_ANCHOR_RIGHT)
            - self.exit_button.width() // 2,
            anchor_top
            - _scaled(_EXIT_ANCHOR_ABOVE)
            - self.exit_button.height() // 2,
        )
        self.exit_button.move(clamp(exit_point, self.exit_button, available))

        entry_point = QPoint(
            anchor_center_x - self.open_ui_entry.width() // 2,
            anchor_top - self.open_ui_entry.height() - _scaled(6),
        )
        entry_point = clamp(entry_point, self.open_ui_entry, available)
        if entry_point.y() >= anchor_top:
            # Not enough headroom (pet near the screen top): drop the entry
            # below the character instead of covering it.
            entry_point.setY(anchor_bottom + _scaled(6))
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
