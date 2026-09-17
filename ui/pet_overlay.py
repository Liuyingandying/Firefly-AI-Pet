"""The character-first Firefly overlay for Phase 8A.1."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImageReader, QMovie, QPen, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from . import theme


class PetOverlay(QWidget):
    left_clicked = Signal()  # one valid left click (not a drag), emitted on release
    position_changed = Signal()
    drag_started = Signal()
    drag_finished = Signal()
    reset_requested = Signal()
    hide_requested = Signal()  # double right click / close -> hide the pet, never quit
    scale_mode_toggled = Signal()
    scale_wheel = Signal(int)  # +1 wheel-up, -1 wheel-down
    scale_exit_requested = Signal()

    DROP_HINT_TEXT = "放这里，我帮你记着"

    def __init__(
        self,
        assets_dir: Path,
        state_gif: dict[str, str],
        *,
        max_dimension: int = theme.PET_MAX_DIMENSION,
    ):
        super().__init__(None)
        self._assets_dir = Path(assets_dir)
        self._state_gif = dict(state_gif)
        self._base_max_dimension = max_dimension
        self._max_dimension = max_dimension
        self._original_size: QSize | None = None
        self._source_path: Path | None = None
        self._current_state: str | None = None
        self._paused = False
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False
        self._shutting_down = False

        # Scratchpad v1: optional drop handler (set via set_drop_handler).
        # System drag-drop (QDrag) is a separate event family from the
        # internal mouse press/move/release drag, so accepting drops never
        # interferes with click / move / wheel behaviour.
        self._drop_handler = None
        self._drop_hover = False
        self._toast_text: str | None = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(1600)
        self._toast_timer.timeout.connect(self._clear_toast)


        self._right_click_timer = QTimer(self)
        self._right_click_timer.setSingleShot(True)
        self._right_click_timer.setInterval(QApplication.doubleClickInterval())
        self._right_click_timer.timeout.connect(self.scale_mode_toggled.emit)

        self.setWindowTitle("Firefly")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFocusPolicy(Qt.StrongFocus)

        self._label = QLabel(self)
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(theme.transparent_window_style())

        self._movie = QMovie(self)
        self._movie.setCacheMode(QMovie.CacheAll)
        self._label.setMovie(self._movie)

        self.apply_state("idle")

    # ------------------------------------------------------- Scratchpad v1

    def set_drop_handler(self, handler) -> None:
        """Install the Scratchpad drop controller (duck-typed protocol):

        - ``can_accept(mime) -> bool``
        - ``handle(mime) -> object`` with ``.ok`` / ``.message``

        ``None`` disables drop handling entirely.
        """
        self._drop_handler = handler
        self.setAcceptDrops(handler is not None)

    def dragEnterEvent(self, event) -> None:
        if self._drop_handler is not None and self._drop_handler.can_accept(
            event.mimeData()
        ):
            self._drop_hover = True
            self.update()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._drop_hover:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._drop_hover = False
        self.update()
        event.accept()

    def dropEvent(self, event) -> None:
        self._drop_hover = False
        if self._drop_handler is None:
            event.ignore()
            return
        outcome = self._drop_handler.handle(event.mimeData())
        self._toast_text = outcome.message if outcome is not None else None
        if outcome is not None and outcome.ok:
            self._toast_text = "记下啦"
        if self._toast_text:
            self._toast_timer.start()
        self.update()
        if outcome is not None and outcome.ok:
            event.acceptProposedAction()
        else:
            event.accept()  # consumed either way; the hint is the feedback

    def _clear_toast(self) -> None:
        self._toast_text = None
        self.update()

    @property
    def current_state(self) -> str:
        return self._current_state or "idle"

    def apply_state(self, state: str) -> None:
        if state == self._current_state:
            return
        self._current_state = state
        self._load_movie(state)

    def _load_movie(self, state: str) -> None:
        gif_name = self._state_gif.get(state, "idle.gif")
        path = self._assets_dir / gif_name
        if not path.exists():
            path = self._assets_dir / "idle.gif"
        self._source_path = path

        scaled = self._scaled_size(path)
        self._restart_movie(scaled, reset_frame=(state == "sleeping"))
        self._apply_geometry(scaled)

    def _restart_movie(self, scaled: QSize, *, reset_frame: bool) -> None:
        """(Re)load the current frame source at ``scaled`` and keep playing.

        QMovie only honors setScaledSize() before start(); once the movie is
        running a later setScaledSize() is ignored and the label keeps painting
        the previously-scaled frames. Re-applying the source at the new size
        forces the frames to actually resize. The current frame and paused state
        are restored so playback is visually uninterrupted.
        """
        frame = self._movie.currentFrameNumber()
        self._movie.stop()
        self._movie.setFileName(str(self._source_path))
        self._movie.setScaledSize(scaled)
        self._movie.start()
        if reset_frame:
            self._movie.jumpToFrame(0)
        elif frame > 0:
            self._movie.jumpToFrame(frame)
        self._movie.setPaused(self._paused or self.current_state == "sleeping")

    def _apply_geometry(self, scaled: QSize) -> None:
        width = scaled.width() + theme.scaled_px(theme.PET_HORIZONTAL_PADDING)
        height = scaled.height() + theme.scaled_px(theme.PET_BOTTOM_PADDING)
        self.setFixedSize(width, height)
        self._label.setGeometry((width - scaled.width()) // 2, 0, scaled.width(), scaled.height())
        self.update()

    def _scaled_size(self, path: Path) -> QSize:
        size = QImageReader(str(path)).size()
        if size.isEmpty():
            size = QSize(192, 208)
        self._original_size = size
        return self._scaled_from_original()

    def _scaled_from_original(self) -> QSize:
        size = self._original_size or QSize(192, 208)
        width, height = size.width(), size.height()
        longest = max(width, height)
        if longest > 0 and longest != self._max_dimension:
            ratio = self._max_dimension / longest
            width = max(1, int(round(width * ratio)))
            height = max(1, int(round(height * ratio)))
        return QSize(width, height)

    def apply_scale(self) -> None:
        """Re-derive the character size from the current ui_scale.

        Kept separate from geometry-only resizing: a live QMovie ignores a
        setScaledSize() issued after start(), which is exactly why the first
        implementation only shrank the window (a crop) while the character
        stayed at its old render size. Re-loading the immutable original frame
        source at the new size makes the character itself actually scale.
        """
        self._max_dimension = theme.scaled_px(self._base_max_dimension)
        scaled = self._scaled_from_original()
        self._restart_movie(scaled, reset_frame=False)
        self._apply_geometry(scaled)

    def pause(self) -> None:
        self._paused = True
        self._movie.setPaused(True)

    def resume(self) -> None:
        self._paused = False
        self._movie.setPaused(self.current_state == "sleeping")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = self.width() / 2
        glow_y = self.height() - 18
        glow_width = min(theme.scaled(theme.PET_GLOW_WIDTH), self.width() - 8)
        glow_x = (self.width() - glow_width) / 2
        gradient = QRadialGradient(center, glow_y, glow_width * 0.56)
        gradient.setColorAt(0.0, theme.qcolor(theme.PET_GLOW_INNER))
        gradient.setColorAt(1.0, theme.qcolor(theme.PET_GLOW_OUTER))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(
            QRectF(
                glow_x,
                self.height() - theme.scaled(theme.PET_GLOW_HEIGHT) - 11,
                glow_width,
                theme.scaled(theme.PET_GLOW_HEIGHT),
            )
        )
        super().paintEvent(event)
        self._paint_scratchpad_layers(painter)

    def _paint_scratchpad_layers(self, painter: QPainter) -> None:
        """Scratchpad v1 feedback: a soft accept ring while a compatible
        drag hovers, and a short-lived toast pill after a drop.  Both are
        painted inside the existing window bounds — no geometry changes, no
        animations, and the character GIF itself stays untouched."""
        if not (self._drop_hover or self._toast_text):
            return
        painter.save()
        ring_rect = QRectF(
            self.width() * 0.06,
            self.height() * 0.04,
            self.width() * 0.88,
            self.height() - self.height() * 0.08 - theme.scaled(theme.PET_BOTTOM_PADDING),
        )
        if self._drop_hover:
            ring_color = theme.qcolor(theme.CYAN_ACCENT)
            painter.setPen(QPen(ring_color, 2.2, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.setOpacity(0.75)
            painter.drawRoundedRect(ring_rect, 18, 18)
            painter.setOpacity(0.28)
            painter.setBrush(ring_color)
            painter.drawRoundedRect(ring_rect.adjusted(5, 5, -5, -5), 14, 14)
            painter.setOpacity(1.0)
            label = self.DROP_HINT_TEXT
            color = theme.qcolor(theme.CYAN_ACCENT)
        else:
            label = self._toast_text or ""
            color = theme.qcolor(theme.CYAN_ACCENT)
        if label:
            painter.setOpacity(0.95)
            font = painter.font()
            font.setPointSize(max(10, theme.scaled(11)))
            font.setBold(True)
            painter.setFont(font)
            band = QRectF(0, self.height() - theme.scaled(theme.PET_BOTTOM_PADDING) - 2,
                          self.width(), theme.scaled(theme.PET_BOTTOM_PADDING))
            painter.setPen(QPen(theme.qcolor((20, 30, 34, 220))))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                painter.drawText(band.adjusted(dx, dy, dx, dy),
                                 Qt.AlignHCenter | Qt.AlignVCenter, label)
            painter.setPen(color)
            painter.drawText(band, Qt.AlignHCenter | Qt.AlignVCenter, label)
        painter.restore()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._arm_left_press(event)
            event.accept()
            return
        if event.button() == Qt.RightButton:
            if self._right_click_timer.isActive():
                # A second right press within the system double-click interval
                # is a double click: cancel the pending toggle and hide the
                # pet. Qt only synthesizes a mouseDoubleClickEvent when the two
                # clicks are within the drag-distance threshold, so this timer
                # check is what makes the hiding purely interval-based.
                self._right_click_timer.stop()
                self.hide_requested.emit()
            else:
                self._right_click_timer.start()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self._right_click_timer.stop()
            self.hide_requested.emit()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            # The second press of a double click is delivered here instead of
            # mousePressEvent; arm a click so the trailing release counts once
            # (a real double click resolves to exactly 2, never 3).
            self._arm_left_press(event)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton and self._drag_offset is not None:
            current = event.globalPosition().toPoint()
            if self._press_global is not None:
                distance = (current - self._press_global).manhattanLength()
                if distance >= QApplication.startDragDistance() and not self._dragging:
                    self._dragging = True
                    self.drag_started.emit()
            if self._dragging:
                self.move(self._clamped_top_left(current - self._drag_offset, current))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            was_click = self._drag_offset is not None and not self._dragging
            was_drag = self._dragging
            self._drag_offset = None
            self._press_global = None
            self._dragging = False
            if was_drag:
                self.drag_finished.emit()
            elif was_click:
                self.left_clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _arm_left_press(self, event) -> None:
        self._press_global = event.globalPosition().toPoint()
        self._drag_offset = self._press_global - self.frameGeometry().topLeft()
        self._dragging = False

    def _clamped_top_left(self, proposed: QPoint, cursor: QPoint) -> QPoint:
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        if screen is None:
            return proposed
        available = screen.availableGeometry()
        maximum_x = max(available.left(), available.right() - self.width() + 1)
        maximum_y = max(available.top(), available.bottom() - self.height() + 1)
        return QPoint(
            min(max(proposed.x(), available.left()), maximum_x),
            min(max(proposed.y(), available.top()), maximum_y),
        )

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.position_changed.emit()

    def contextMenuEvent(self, event) -> None:
        # Right-click is handled via mousePressEvent / mouseDoubleClickEvent;
        # swallow the context-menu event so it never double-triggers Scale Mode.
        event.accept()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta > 0:
            self.scale_wheel.emit(1)
        elif delta < 0:
            self.scale_wheel.emit(-1)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.scale_exit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._shutting_down:
            self._movie.stop()
            event.accept()
            return
        event.ignore()
        self.hide_requested.emit()

    def shutdown(self) -> None:
        self._shutting_down = True
        self._right_click_timer.stop()
        self._movie.stop()
        self.close()
