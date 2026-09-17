"""PdfPageView — PyMuPDF-backed page canvas with mouse text selection.

PaperLens 2 Phase 1: Firefly owns PDF rendering and selection.

  - Document bytes parse fully in memory with pymupdf (no temp file).
  - One page renders to a QImage at ``fit_scale * zoom``; zoom re-renders.
  - Mouse drag selects a rectangle; on release the rect maps back to page
    coordinates and ``page.get_textbox()`` returns the text in reading
    order. Emits ``selection_made(text, page_1based)``.

Scope boundary: display + selection only. Chat / consent / model calls stay
in the owning panel (ui/explain_box.py) and core/pdf_qa.py.
"""

from __future__ import annotations

import logging

import pymupdf
from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

log = logging.getLogger("firefly.paperlens2.pdfview")

MIN_SCALE = 0.4
MAX_SCALE = 4.0
ZOOM_STEP = 1.25
DEFAULT_FIT_WIDTH = 640  # used when the owning panel has no width yet

SELECTION_FILL = QColor(86, 156, 214, 70)
SELECTION_BORDER = QColor(86, 156, 214, 210)


class PdfDocument:
    """Tiny in-memory wrapper around one pymupdf document (pages + render)."""

    def __init__(self, data: bytes) -> None:
        self._doc = pymupdf.open(stream=data, filetype="pdf")
        self.page_count = self._doc.page_count
        self.page_rects = [self._doc[index].rect for index in range(self.page_count)]
        self.display_name = ""

    def close(self) -> None:
        self._doc.close()

    def render(self, page_index: int, scale: float) -> QImage:
        """Render one page to an RGB QImage (always a private copy)."""
        page = self._doc[page_index]
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        return QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888
        ).copy()

    def text_in_rect(self, page_index: int, page_rect: pymupdf.Rect) -> str:
        """Reading-order text inside one page-coordinate rectangle."""
        page = self._doc[page_index]
        return (page.get_textbox(page_rect) or "").strip()


class PdfPageView(QWidget):
    """One scrollable page canvas. Sized to the rendered pixmap."""

    selection_made = Signal(str, int)  # (text, page 1-based)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._doc: PdfDocument | None = None
        self._page_index = 0
        self._fit_scale = 1.0
        self._zoom = 1.0
        self._image: QImage | None = None
        self._selection: QRect | None = None
        self._selection_origin: QPoint | None = None
        self._dragging = False
        self._last_text = ""
        self._last_page = 0
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setMinimumSize(160, 200)

    # ------------------------------------------------------------------
    # document lifecycle
    # ------------------------------------------------------------------

    def load_bytes(self, data: bytes) -> None:
        """Open a PDF from bytes and render its first page."""
        self._close_doc()
        self._doc = PdfDocument(data)
        self._page_index = 0
        self._zoom = 1.0
        self._clear_selection()
        self._render()

    def close_document(self) -> None:
        self._close_doc()
        self._page_index = 0
        self._image = None
        self._clear_selection()
        self.update()

    def _close_doc(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    # ------------------------------------------------------------------
    # public page state
    # ------------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self._doc.page_count if self._doc is not None else 0

    @property
    def page_index(self) -> int:
        """0-based current page."""
        return self._page_index

    @property
    def current_scale(self) -> float:
        """Widget pixels per page point (fit_scale * zoom)."""
        return self._fit_scale * self._zoom

    @property
    def last_selection(self) -> tuple[str, int] | None:
        if not self._last_text:
            return None
        return (self._last_text, self._last_page)

    def set_fit_width(self, width: int) -> None:
        """Anchor the fit scale so the rendered page width ≈ ``width`` px."""
        if self._doc is None:
            return
        page_width = self._doc.page_rects[self._page_index].width
        if page_width > 0:
            self._fit_scale = max(1.0, width) / page_width
        self._render()

    def set_page(self, index: int) -> None:
        """Navigate 0-based; renders only when the page actually changes."""
        if self._doc is None:
            return
        index = max(0, min(index, self.page_count - 1))
        if index != self._page_index:
            self._page_index = index
            self._clear_selection()
            self._render()

    def zoom_in(self) -> None:
        self._zoom = min(MAX_SCALE / self._fit_scale, self._zoom * ZOOM_STEP) \
            if self._fit_scale > 0 else self._zoom * ZOOM_STEP
        self._render()

    def zoom_out(self) -> None:
        self._zoom = max(MIN_SCALE / self._fit_scale, self._zoom / ZOOM_STEP) \
            if self._fit_scale > 0 else self._zoom / ZOOM_STEP
        self._render()

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._render()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        if self._doc is None:
            return
        self._image = self._doc.render(self._page_index, self.current_scale)
        size = QSize(self._image.size())
        if self.size() != size:
            self.setFixedSize(size)
        self.update()

    def sizeHint(self):  # noqa: D102
        if self._image is not None:
            return QSize(self._image.size())
        return QSize(DEFAULT_FIT_WIDTH, DEFAULT_FIT_WIDTH * 1.3)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        if self._image is None:
            painter.fillRect(self.rect(), QColor(250, 250, 252))
            painter.setPen(QPen(QColor(120, 120, 128), 1))
            painter.drawText(self.rect(), Qt.AlignCenter, "打开 PDF 后在此显示页面")
            return
        painter.drawImage(0, 0, self._image)
        if self._selection is not None:
            painter.fillRect(self._selection, SELECTION_FILL)
            painter.setPen(QPen(SELECTION_BORDER, 1.2))
            painter.drawRect(self._selection)

    # ------------------------------------------------------------------
    # selection: mouse drag
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._image is not None:
            point = self._clamp_point(event.position().toPoint())
            self._selection_origin = point
            self._selection = QRect(point, point)
            self._dragging = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging and self._selection_origin is not None:
            self._selection = QRect(
                self._selection_origin, self._clamp_point(event.position().toPoint())
            ).normalized()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._finish_selection()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def simulate_select(self, widget_rect: QRect) -> tuple[str, int]:
        """Test/accessibility hook: the mouse-release path for a rectangle.

        Returns ``(text, page_1based)``; emits ``selection_made`` like a
        real drag release.
        """
        self._selection = QRect(widget_rect).intersected(self.rect()).normalized()
        self._finish_selection()
        return (self._last_text, self._last_page)

    def _finish_selection(self) -> None:
        rect = self._selection
        self._selection = None
        if rect is None or rect.width() < 2 or rect.height() < 2:
            self._clear_selection()
            self.update()
            return
        text = self._text_in_widget_rect(rect)
        if not text:
            self._clear_selection()
            self.update()
            return
        self._last_text = text
        self._last_page = self._page_index + 1
        self.selection_made.emit(text, self._last_page)
        self.update()

    def _clear_selection(self) -> None:
        self._selection = None
        self._selection_origin = None
        self._dragging = False
        self._last_text = ""
        self._last_page = 0
        self.update()

    # ------------------------------------------------------------------
    # coordinate mapping
    # ------------------------------------------------------------------

    def _clamp_point(self, point: QPoint) -> QPoint:
        return QPoint(
            max(0, min(point.x(), max(self.width() - 1, 0))),
            max(0, min(point.y(), max(self.height() - 1, 0))),
        )

    def _text_in_widget_rect(self, rect: QRect) -> str:
        if self._doc is None:
            return ""
        page_rect = self._doc.page_rects[self._page_index]
        scale = self.current_scale
        page_coords = pymupdf.Rect(
            page_rect.x0 + rect.left() / scale,
            page_rect.y0 + rect.top() / scale,
            page_rect.x0 + rect.right() / scale,
            page_rect.y0 + rect.bottom() / scale,
        )
        return self._doc.text_in_rect(self._page_index, page_coords)


__all__ = ["PdfDocument", "PdfPageView"]