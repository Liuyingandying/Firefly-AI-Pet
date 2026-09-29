"""One-shot PDF selection overlay (PDF OCR Overlay Phase 3-C).

A transparent, always-on-top overlay covering exactly the captured region
of the user's PDF viewer window for ONE selection gesture. Phases:

  Phase 3-A  geometry/focus contract + one-gesture mouse capture
  Phase 3-B  request_arm (fresh capture → OCR → arm) + snapshot_failed
  Phase 3-C  explicit state machine (IDLE/CAPTURING/OCR/ARMED/SELECTING/
             COMPLETED/CANCELLED/FAILED), public ``cancel()`` for the
             app-level Escape hotkey, pre/post capture UI-hiding hooks
             (keep Firefly's own overlays out of the OCR frame), and
             selection feedback (live line highlight + transient summary).

Constraints honored: no AI, no ExplainBox, no auto OCR refresh, no focus
stealing (``Qt.WindowDoesNotAcceptFocus``), capture metadata keeps its
Phase 2-A coordinate semantics, the hit-test keeps the Phase 1 algorithm.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Callable, Sequence

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from core.pdf_text_hit_test import hit_test, line_rect, screen_to_image_rect
from core.pdf_viewport_context import ocr_lines_from_raw
from core.screen_vision.models import ScreenFrame

_CLICK_EPSILON = 1.5  # logical px per axis below which a release is a click
_DEFAULT_SUMMARY_DELAY_MS = 700
# Conservative visual guard: blocks only degenerate slivers. A tight
# one-line formula selection is ~30 image px tall (the 4-A.5 live gate
# proved a 350x55 crop works) — the 300px/400x250 guidance is VLM-quality
# advice, not a hard gate.
_DEFAULT_VISUAL_MIN_WIDTH = 100
_DEFAULT_VISUAL_MIN_HEIGHT = 18


class OverlayState(Enum):
    """Explicit overlay lifecycle (Phase 3-C P4)."""

    IDLE = "idle"
    CAPTURING = "capturing"  # fresh capture running off-GUI
    OCR = "ocr"  # OCR running off-GUI
    ARMED = "armed"  # awaiting the selection gesture
    SELECTING = "selecting"  # mouse pressed, dragging
    COMPLETED = "completed"  # summary banner shown before emitting text
    CANCELLED = "cancelled"
    FAILED = "failed"


class OverlayMode(Enum):
    """Selection interpretation mode (Phase 4-B).

    TEXT: drag → OCR hit-test → ``selection_completed(text)`` (unchanged).
    VISUAL_REGION: drag → no hit-test → ``visual_region_completed(frame,
    image_rect, lines)`` for the visual explain pipeline.
    """

    TEXT = "text"
    VISUAL_REGION = "visual_region"


class _SnapshotRelay(QObject):
    """Delivers capture/OCR worker results back to the GUI thread."""

    captured = Signal()  # capture returned — the caller restores its UI here
    ready = Signal(object, object)  # (ScreenFrame, list[OCRLine-dict])
    failed = Signal(str)


class PdfSelectionOverlay(QWidget):
    """One-gesture transparent overlay over a captured PDF window region."""

    selection_completed = Signal(str)
    selection_cancelled = Signal()
    # Phase 4-B: visual region selection (mode == VISUAL_REGION). Emits the
    # fresh ScreenFrame (for in-memory crop), the image-space rect, and the
    # OCR lines of that snapshot (classification/grounding only).
    visual_region_completed = Signal(object, object, object)
    # Fresh-capture → OCR cycle failed (window gone / OCR error); the shell
    # must NOT open the ExplainBox on this path.
    snapshot_failed = Signal(str)
    # State changes (Phase 3-C): the shell uses it to drive the temporary
    # Escape hotkey registration / cleanups.
    state_changed = Signal(object)  # OverlayState

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        summary_delay_ms: int = _DEFAULT_SUMMARY_DELAY_MS,
        visual_min_width: int = _DEFAULT_VISUAL_MIN_WIDTH,
        visual_min_height: int = _DEFAULT_VISUAL_MIN_HEIGHT,
    ) -> None:
        super().__init__(
            parent,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

        self._frame: ScreenFrame | None = None
        self._lines: list[Any] = []
        self._start_point: QPointF | None = None
        self._current_point: QPointF | None = None
        self._state = OverlayState.IDLE
        self._cancel_requested = False
        self._pending_keep_above: list[QWidget] = []
        self._before_capture: Callable[[], None] | None = None
        self._after_capture: Callable[[], None] | None = None
        self._after_capture_pending = False
        # P3 feedback: transient "已选择：..." summary before completion.
        self._summary_delay_ms = int(summary_delay_ms)
        self._summary_text = ""
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.timeout.connect(self._on_summary_timeout)
        # Phase 4-B: TEXT / VISUAL_REGION mode + conservative visual guard.
        self._mode = OverlayMode.TEXT
        self._visual_min_width = int(visual_min_width)
        self._visual_min_height = int(visual_min_height)
        self._reject_pending = False

        self._snapshot_relay = _SnapshotRelay(self)
        self._snapshot_relay.captured.connect(
            self._on_snapshot_captured, Qt.QueuedConnection
        )
        self._snapshot_relay.ready.connect(
            self._on_snapshot_ready, Qt.QueuedConnection
        )
        self._snapshot_relay.failed.connect(
            self._on_snapshot_failed, Qt.QueuedConnection
        )

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def state(self) -> OverlayState:
        return self._state

    @property
    def is_armed(self) -> bool:
        return self._state in (OverlayState.ARMED, OverlayState.SELECTING)

    @property
    def is_snapshot_busy(self) -> bool:
        return self._state in (OverlayState.CAPTURING, OverlayState.OCR)

    def _set_state(self, state: OverlayState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _to_idle(self) -> None:
        self._frame = None
        self._lines = []
        self._start_point = None
        self._current_point = None
        self._pending_keep_above = []
        self._summary_text = ""
        self._reject_pending = False
        self.hide()
        self._set_state(OverlayState.IDLE)

    # ------------------------------------------------------------------
    # mode (Phase 4-B)
    # ------------------------------------------------------------------

    @property
    def mode(self) -> OverlayMode:
        return self._mode

    def set_mode(self, mode: OverlayMode) -> None:
        """Switch TEXT / VISUAL_REGION (IDLE only — an armed gesture keeps
        the mode it started with)."""
        if self._state is OverlayState.IDLE:
            self._mode = mode

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def arm(
        self,
        frame: ScreenFrame,
        lines: Sequence[Any],
        *,
        keep_above: Sequence[QWidget] = (),
    ) -> None:
        """Arm one selection gesture over the captured window region.

        ``frame`` is the ScreenFrame the OCR lines were computed from (its
        metadata defines the overlay geometry and the coordinate mapping);
        ``lines`` are the OCR lines (``OCRLine`` or ``{"text","bbox"}``).
        ``keep_above`` lists Firefly windows that are re-raised after the
        overlay shows so the overlay never visually covers them.
        """
        if self._cancel_requested:
            self._cancel_requested = False
            self._to_idle()
            self.selection_cancelled.emit()
            return
        self._frame = frame
        self._lines = list(lines)
        self._start_point = None
        self._current_point = None
        self._apply_geometry()
        self.show()
        for widget in keep_above:
            widget.raise_()
        self._set_state(OverlayState.ARMED)

    def disarm(self) -> None:
        """Exit the overlay (hide + reset the gesture state)."""
        self._to_idle()

    def cancel(self) -> None:
        """Cancel the active mode (Phase 3-C P0/P4).

        Works while ARMED, SELECTING, COMPLETED (summary banner) and even
        while CAPTURING/OCR is still running: the pending snapshot result is
        discarded when it arrives. Emits ``selection_cancelled`` once and
        always returns to IDLE."""
        if self._state in (
            OverlayState.CAPTURING,
            OverlayState.OCR,
        ):
            self._cancel_requested = True
            return
        if self._state in (
            OverlayState.ARMED,
            OverlayState.SELECTING,
            OverlayState.COMPLETED,
        ):
            self._summary_timer.stop()
            self._to_idle()
            self.selection_cancelled.emit()

    # ------------------------------------------------------------------
    # Phase 3-B production entry — fresh capture → OCR → arm
    # ------------------------------------------------------------------

    def request_arm(
        self,
        capture_fn: Callable[[], ScreenFrame],
        ocr_fn: Callable[[bytes], Any],
        *,
        keep_above: Sequence[QWidget] = (),
        before_capture: Callable[[], None] | None = None,
        after_capture: Callable[[], None] | None = None,
    ) -> None:
        """Take a FRESH snapshot (capture → OCR → arm), never a stale one.

        ``before_capture`` runs on the GUI thread just before the capture
        thread starts (P2: hide Firefly overlays + GUI settle); the
        ``captured`` relay then invokes ``after_capture`` on the GUI thread
        as soon as the frame is in hand (P2: restore the UI while OCR runs).
        Any failure path also runs ``after_capture`` exactly once, so the UI
        is always restored. A busy/armed overlay rejects new requests
        (P4: never a second OCR worker); retriggering while ARMED/SELECTING
        is handled by the shell via ``cancel()``.
        """
        if self._state is not OverlayState.IDLE or self._cancel_requested:
            return
        self._pending_keep_above = list(keep_above)
        self._before_capture = before_capture
        self._after_capture = after_capture
        self._after_capture_pending = after_capture is not None
        self._set_state(OverlayState.CAPTURING)

        if before_capture is not None:
            try:
                before_capture()
            except Exception as exc:  # noqa: BLE001 - surfaced as failure
                self._run_after_capture()
                self._set_state(OverlayState.FAILED)
                self._set_state(OverlayState.IDLE)
                self.snapshot_failed.emit(f"{type(exc).__name__}: {exc}")
                return

        def work() -> None:
            try:
                frame = capture_fn()
                if frame is None or not getattr(frame, "image_bytes", None):
                    raise RuntimeError("capture returned an empty frame")
                self._snapshot_relay.captured.emit()
                raw = ocr_fn(frame.image_bytes)
                lines = ocr_lines_from_raw(raw)
                if not lines:
                    raise RuntimeError("OCR produced no text")
                self._snapshot_relay.ready.emit(frame, lines)
            except Exception as exc:  # noqa: BLE001 - surfaced as failure
                self._snapshot_relay.failed.emit(f"{type(exc).__name__}: {exc}")

        threading.Thread(
            target=work, daemon=True, name="PdfOverlaySnapshot"
        ).start()

    def _run_after_capture(self) -> None:
        if self._after_capture_pending and self._after_capture is not None:
            self._after_capture_pending = False
            self._after_capture()

    def _on_snapshot_captured(self) -> None:
        self._run_after_capture()
        if self._cancel_requested:
            return
        self._set_state(OverlayState.OCR)

    def _on_snapshot_ready(self, frame: ScreenFrame, lines: list) -> None:
        self._run_after_capture()
        self._before_capture = None
        self._after_capture = None
        if self._cancel_requested:
            self._cancel_requested = False
            self._to_idle()
            self.selection_cancelled.emit()
            return
        self.arm(frame, lines, keep_above=self._pending_keep_above)
        self._pending_keep_above = []

    def _on_snapshot_failed(self, message: str) -> None:
        self._run_after_capture()
        self._before_capture = None
        self._after_capture = None
        if self._cancel_requested:
            self._cancel_requested = False
            self._to_idle()
            self.selection_cancelled.emit()
            return
        self._set_state(OverlayState.FAILED)
        self._set_state(OverlayState.IDLE)
        self.snapshot_failed.emit(message)

    # ------------------------------------------------------------------
    # coordinates
    # ------------------------------------------------------------------

    def _apply_geometry(self) -> None:
        frame = self._frame
        if frame is None:
            return
        dpr = self.devicePixelRatioF() or 1.0
        scale_x = frame.scale_x or 1.0
        scale_y = frame.scale_y or 1.0
        ox, oy = frame.crop_offset
        width = frame.width / scale_x / dpr
        height = frame.height / scale_y / dpr
        self.setGeometry(round(ox / dpr), round(oy / dpr), round(width), round(height))

    def _local_to_screen_physical(self, pos: QPointF) -> tuple[float, float]:
        dpr = self.devicePixelRatioF() or 1.0
        ox, oy = self._frame.crop_offset if self._frame else (0, 0)
        return (ox + pos.x() * dpr, oy + pos.y() * dpr)

    def _current_screen_rect(self) -> tuple[float, float, float, float] | None:
        if self._frame is None or self._start_point is None or self._current_point is None:
            return None
        x1, y1 = self._local_to_screen_physical(self._start_point)
        x2, y2 = self._local_to_screen_physical(self._current_point)
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._state is not OverlayState.ARMED or event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._start_point = QPointF(event.position())
        self._current_point = QPointF(event.position())
        self._set_state(OverlayState.SELECTING)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._state is not OverlayState.SELECTING:
            super().mouseMoveEvent(event)
            return
        self._current_point = QPointF(event.position())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._state is not OverlayState.SELECTING or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._current_point = QPointF(event.position())
        rect = self._current_screen_rect()
        dx = abs(self._current_point.x() - self._start_point.x())
        dy = abs(self._current_point.y() - self._start_point.y())
        if rect is None or (dx <= _CLICK_EPSILON and dy <= _CLICK_EPSILON):
            self._to_idle()
            self.selection_cancelled.emit()
            event.accept()
            return
        frame = self._frame
        image_rect = screen_to_image_rect(
            rect, frame.crop_offset, frame.scale_x, frame.scale_y
        )
        if self._mode is OverlayMode.VISUAL_REGION:
            self._handle_visual_release(frame, image_rect)
            event.accept()
            return
        text = hit_test(self._lines, image_rect).strip()
        if not text:
            # Blank-region selection: complete with empty text (the shell
            # never opens the ExplainBox for it), no summary banner.
            self._to_idle()
            self.selection_completed.emit("")
            event.accept()
            return
        # P3: transient summary banner, then the real completion.
        self._summary_text = text
        self._set_state(OverlayState.COMPLETED)
        self.update()
        if self._summary_delay_ms > 0:
            self._summary_timer.start(self._summary_delay_ms)
        else:
            self._finish_with_text(text)
        event.accept()

    def _finish_with_text(self, text: str) -> None:
        self._to_idle()
        self.selection_completed.emit(text)

    def _handle_visual_release(self, frame, image_rect) -> None:
        """Phase 4-B: VISUAL_REGION release — no hit-test. A conservative
        size guard rejects sliver regions with a light banner instead of
        sending them to the VLM; a valid region emits the fresh frame +
        image rect + snapshot OCR lines for the app's crop/explain pipeline.
        """
        width = abs(image_rect[2] - image_rect[0])
        height = abs(image_rect[3] - image_rect[1])
        if width < self._visual_min_width or height < self._visual_min_height:
            self._reject_pending = True
            self._summary_text = "区域太小，请重新框选"
            self._set_state(OverlayState.COMPLETED)
            self.update()
            self._summary_timer.start(self._summary_delay_ms)
            return
        captured_frame = frame
        captured_lines = list(self._lines)
        self._to_idle()
        self.visual_region_completed.emit(captured_frame, image_rect, captured_lines)

    def _on_summary_timeout(self) -> None:
        if self._state is not OverlayState.COMPLETED:
            return
        if self._reject_pending:
            self._to_idle()
            self.selection_cancelled.emit()
            return
        self._finish_with_text(self._summary_text)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.key() == Qt.Key_Escape:
            self.cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # painting (dim + live line highlight + selection + summary banner)
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 36))
        if self._state is OverlayState.SELECTING:
            self._paint_live_hits(painter)
        if self._start_point is not None and self._current_point is not None:
            pen = QPen(QColor(90, 200, 250), 2)
            painter.setPen(pen)
            x1, y1 = self._start_point.x(), self._start_point.y()
            x2, y2 = self._current_point.x(), self._current_point.y()
            painter.drawRect(
                QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
            )
        if self._state is OverlayState.COMPLETED and self._summary_text:
            self._paint_summary(painter)
        painter.end()

    def _paint_live_hits(self, painter: QPainter) -> None:
        """P3: lightly highlight OCR lines the current selection touches."""
        rect = self._current_screen_rect()
        if rect is None or self._frame is None:
            return
        dpr = self.devicePixelRatioF() or 1.0
        # Selection in overlay-local coordinates.
        x1 = (rect[0] - self._frame.crop_offset[0]) / dpr
        y1 = (rect[1] - self._frame.crop_offset[1]) / dpr
        x2 = (rect[2] - self._frame.crop_offset[0]) / dpr
        y2 = (rect[3] - self._frame.crop_offset[1]) / dpr
        highlight = QColor(90, 200, 250, 46)
        for entry in self._lines:
            text = entry.get("text") if isinstance(entry, dict) else getattr(entry, "text", "")
            bbox = entry.get("bbox") if isinstance(entry, dict) else getattr(entry, "bbox", None)
            if not text or bbox is None:
                continue
            lx1, ly1, lx2, ly2 = line_rect(bbox)
            scale_x = self._frame.scale_x or 1.0
            scale_y = self._frame.scale_y or 1.0
            # Image coords → overlay-local (image * dpr / scale).
            ox1, oy1 = lx1 / scale_x * dpr, ly1 / scale_y * dpr
            ox2, oy2 = lx2 / scale_x * dpr, ly2 / scale_y * dpr
            if (
                min(x1, x2) <= ox2 and ox1 <= max(x1, x2)
                and min(y1, y2) <= oy2 and oy1 <= max(y1, y2)
            ):
                painter.fillRect(
                    QRectF(ox1, oy1, ox2 - ox1, oy2 - oy1), highlight
                )

    def _paint_summary(self, painter: QPainter) -> None:
        """A compact banner with the selected text preview (P3)."""
        preview = self._summary_text[:80]
        label = f"已选择：{preview}"
        font = painter.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() * 0.9))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(label) + 24
        banner = QRectF(
            (self.width() - text_width) / 2.0, 14.0, text_width, 34.0
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(24, 26, 32, 230))
        painter.drawRoundedRect(banner, 8, 8)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(banner, Qt.AlignCenter, label)


__all__ = ["PdfSelectionOverlay", "OverlayState"]