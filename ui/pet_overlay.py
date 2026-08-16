"""The character-first Firefly overlay for Phase 8A.1."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImageReader, QMovie, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from . import theme


class PetOverlay(QWidget):
    clicked = Signal()
    position_changed = Signal()
    drag_started = Signal()
    drag_finished = Signal()
    reset_requested = Signal()
    quit_requested = Signal()

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
        self._max_dimension = max_dimension
        self._current_state: str | None = None
        self._paused = False
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._dragging = False
        self._shutting_down = False

        self.setWindowTitle("Firefly")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setStyleSheet(theme.transparent_window_style())

        self._label = QLabel(self)
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(theme.transparent_window_style())

        self._movie = QMovie(self)
        self._movie.setCacheMode(QMovie.CacheAll)
        self._label.setMovie(self._movie)

        self.apply_state("idle")

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

        scaled = self._scaled_size(path)
        self._movie.stop()
        self._movie.setFileName(str(path))
        self._movie.setScaledSize(scaled)
        self._movie.start()
        if state == "sleeping":
            self._movie.jumpToFrame(0)
        self._movie.setPaused(self._paused or state == "sleeping")

        width = scaled.width() + theme.PET_HORIZONTAL_PADDING
        height = scaled.height() + theme.PET_BOTTOM_PADDING
        self.setFixedSize(width, height)
        self._label.setGeometry((width - scaled.width()) // 2, 0, scaled.width(), scaled.height())
        self.update()

    def _scaled_size(self, path: Path) -> QSize:
        size = QImageReader(str(path)).size()
        if size.isEmpty():
            size = QSize(192, 208)
        width, height = size.width(), size.height()
        longest = max(width, height)
        if longest > 0 and longest != self._max_dimension:
            ratio = self._max_dimension / longest
            width = max(1, int(round(width * ratio)))
            height = max(1, int(round(height * ratio)))
        return QSize(width, height)

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
        glow_width = min(theme.PET_GLOW_WIDTH, self.width() - 8)
        glow_x = (self.width() - glow_width) / 2
        gradient = QRadialGradient(center, glow_y, glow_width * 0.56)
        gradient.setColorAt(0.0, theme.qcolor(theme.PET_GLOW_INNER))
        gradient.setColorAt(1.0, theme.qcolor(theme.PET_GLOW_OUTER))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(
            QRectF(
                glow_x,
                self.height() - theme.PET_GLOW_HEIGHT - 11,
                glow_width,
                theme.PET_GLOW_HEIGHT,
            )
        )
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

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
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

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
        menu = QMenu(self)
        menu.addAction("Show / Hide greeting", self.clicked.emit)
        menu.addSeparator()
        if self._paused:
            menu.addAction("Resume animation", self.resume)
        else:
            menu.addAction("Pause animation", self.pause)
        menu.addAction("Reset position", self.reset_requested.emit)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_requested.emit)
        menu.exec(event.globalPos())

    def closeEvent(self, event) -> None:
        if self._shutting_down:
            self._movie.stop()
            event.accept()
            return
        event.ignore()
        self.quit_requested.emit()

    def shutdown(self) -> None:
        self._shutting_down = True
        self._movie.stop()
        self.close()
