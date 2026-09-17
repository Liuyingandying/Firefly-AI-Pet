"""PdfReaderPanel — PaperLens 2.2 independent paper reading mode.

Layout:
  left : PdfPageView (render / page turn / zoom) — reused verbatim
  right: PageConceptPanel (page / section / keywords / summary)

Selection on the rendered page forwards as ``explain_requested(text, page)``.
This panel NEVER calls a model: AI / consent / async stay in ExplainBox,
which receives the selection through the existing public entry
``explain_box.show_browser_selection`` (wired in app.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .pdf_page_view import DEFAULT_FIT_WIDTH, PdfPageView
from core.page_context import PageConceptContext, build_page_concept
from core.pdf_processor import PdfEncryptedError, build_pdf_lazy_index

log = logging.getLogger("firefly.paperlens2.reader")


class PageConceptPanel(QFrame):
    """Right-hand panel: current page / section / keywords / summary."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("pageConceptPanel")
        self.setStyleSheet(
            f"QFrame#pageConceptPanel {{ background: {theme.css_color(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {theme.RADIUS_ITEM}px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel("本页概览", self)
        title.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 11pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        layout.addWidget(title)

        self._page_value = self._add_row(layout, "页码", "")
        self._section_value = self._add_row(layout, "章节", "")
        self._keywords_value = self._add_row(layout, "关键词", "")
        self._summary_value = self._add_row(layout, "摘要", "")

        layout.addStretch(1)
        self.clear()

    def _add_row(self, layout: QVBoxLayout, name: str, value: str) -> QLabel:
        label = QLabel(name, self)
        label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        layout.addWidget(label)

        value_label = QLabel(value, self)
        value_label.setWordWrap(True)
        value_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 9.5pt;"
        )
        value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(value_label)
        return value_label

    def show_context(self, context: PageConceptContext) -> None:
        self._page_value.setText(str(context.page))
        self._section_value.setText(context.section or "（无章节信息）")
        self._keywords_value.setText(
            " · ".join(context.keywords) if context.keywords else "（本页无可选关键词）"
        )
        self._summary_value.setText(context.summary or "（本页无可选摘要）")

    def clear(self) -> None:
        self.show_context(PageConceptContext(page=0))


class PdfReaderPanel(QWidget):
    """Standalone paper reading panel (left render / right concept)."""

    explain_requested = Signal(str, int)  # (text, page 1-based)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly PaperLens 阅读")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        self._lazy = None
        self._pdf_path: str | None = None
        self._current_page = 0
        self._visible = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._glass = theme.GlassPanel(theme.RADIUS_CARD, self)
        theme.apply_soft_shadow(self._glass)
        root.addWidget(self._glass)

        inner = QVBoxLayout(self._glass)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        header = self._build_header()
        inner.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(12)
        body.addLayout(self._build_left(), 3)
        body.addLayout(self._build_right(), 2)
        inner.addLayout(body, 1)

        self.resize(1000, 720)

    # ------------------------------------------------------------------
    # chrome
    # ------------------------------------------------------------------

    def _build_header(self) -> QFrame:
        header = QFrame(self._glass)
        header.setFixedHeight(44)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 8, 10, 8)
        layout.setSpacing(10)

        title = QLabel("Firefly PaperLens 阅读", header)
        title.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 11pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        layout.addWidget(title, 1)

        self._pdf_filename = QLabel("未打开 PDF", header)
        self._pdf_filename.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt;"
        )
        layout.addWidget(self._pdf_filename)

        close_btn = QPushButton("×", header)
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(theme.link_button_style("readerClose"))
        close_btn.clicked.connect(self.hide_panel)
        layout.addWidget(close_btn)

        return header

    def _build_left(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._open_btn = QPushButton("📄 打开 PDF", self._glass)
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.setStyleSheet(theme.popover_button_style("readerOpen"))
        self._open_btn.clicked.connect(self._on_open_pdf)
        bar.addWidget(self._open_btn)

        self._prev_btn = QPushButton("‹", self._glass)
        self._prev_btn.setFixedSize(30, 26)
        self._prev_btn.setCursor(Qt.PointingHandCursor)
        self._prev_btn.setStyleSheet(theme.link_button_style("readerNav"))
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(lambda: self._show_page(self._current_page - 1))
        bar.addWidget(self._prev_btn)

        self._page_label = QLabel("第 0 / 0 页", self._glass)
        self._page_label.setStyleSheet(theme.secondary_label_style(size=8))
        bar.addWidget(self._page_label)

        self._next_btn = QPushButton("›", self._glass)
        self._next_btn.setFixedSize(30, 26)
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.setStyleSheet(theme.link_button_style("readerNav"))
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(lambda: self._show_page(self._current_page + 1))
        bar.addWidget(self._next_btn)

        self._zoom_out_btn = QPushButton("−", self._glass)
        self._zoom_out_btn.setFixedSize(30, 26)
        self._zoom_out_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_out_btn.setToolTip("缩小")
        self._zoom_out_btn.setStyleSheet(theme.link_button_style("readerZoom"))
        self._zoom_out_btn.clicked.connect(self._page_view_zoom_out)
        bar.addWidget(self._zoom_out_btn)

        self._zoom_in_btn = QPushButton("＋", self._glass)
        self._zoom_in_btn.setFixedSize(30, 26)
        self._zoom_in_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_in_btn.setToolTip("放大")
        self._zoom_in_btn.setStyleSheet(theme.link_button_style("readerZoom"))
        self._zoom_in_btn.clicked.connect(self._page_view_zoom_in)
        bar.addWidget(self._zoom_in_btn)

        bar.addStretch(1)
        layout.addLayout(bar)

        self._page_scroll = QScrollArea(self._glass)
        self._page_scroll.setWidgetResizable(False)
        self._page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._page_scroll.setStyleSheet(
            "QScrollArea { background: white; border: 1px solid "
            + theme.css_color(theme.GLASS_BORDER)
            + "; border-radius: 6px; }"
        )
        self._page_view = PdfPageView(self._page_scroll)
        self._page_view.selection_made.connect(self._on_selection)
        self._page_scroll.setWidget(self._page_view)
        layout.addWidget(self._page_scroll, 1)

        return layout

    def _build_right(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        self._page_concept = PageConceptPanel(self._glass)
        layout.addWidget(self._page_concept, 1)
        return layout

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self._page_view.page_count

    @property
    def current_page(self) -> int:
        """0-based current page."""
        return self._current_page

    def load_pdf(self, file_path: str | Path) -> None:
        """Open a PDF: lazy index (text/bookmarks) + rendered first page."""
        self._pdf_path = str(file_path)
        data = Path(file_path).read_bytes()
        self._lazy = build_pdf_lazy_index(data, display_name=Path(file_path).name)
        self._page_view.load_bytes(data)
        viewport = self._page_scroll.viewport().width()
        if viewport <= 0:
            viewport = DEFAULT_FIT_WIDTH
        self._page_view.set_fit_width(viewport)
        self._pdf_filename.setText(Path(file_path).name)
        log.info(
            "[PdfReaderPanel] loaded %s (%d pages)",
            Path(file_path).name, self._page_view.page_count,
        )
        self._show_page(0)

    def _on_open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 PDF 论文", "", "PDF 文件 (*.pdf)"
        )
        if path:
            self.load_pdf(str(Path(path).resolve()))

    def _show_page(self, index: int) -> None:
        if self._lazy is None or self._page_view.page_count == 0:
            return
        index = max(0, min(index, self._page_view.page_count - 1))
        self._current_page = index
        self._page_view.set_page(index)
        self._page_label.setText(f"第 {index + 1} / {self._page_view.page_count} 页")
        self._prev_btn.setEnabled(index > 0)
        self._next_btn.setEnabled(index < self._page_view.page_count - 1)
        self._refresh_context()

    def _refresh_context(self) -> None:
        if self._lazy is None:
            return
        context = build_page_concept(self._lazy, self._current_page + 1)
        self._page_concept.show_context(context)
        log.info(
            "[PdfReaderPanel] page=%d section=%r keywords=%s",
            context.page, context.section, context.keywords[:5],
        )

    def _on_selection(self, text: str, page: int) -> None:
        log.info("[PdfReaderPanel] selection page=%s text=%r", page, text[:120])
        self.explain_requested.emit(text, page)

    def _page_view_zoom_in(self) -> None:
        self._page_view.zoom_in()

    def _page_view_zoom_out(self) -> None:
        self._page_view.zoom_out()

    # ------------------------------------------------------------------
    # panel lifecycle (mirrors ExplainBox)
    # ------------------------------------------------------------------

    def show_panel(self) -> None:
        self._visible = True
        self.show()
        self.raise_()

    def hide_panel(self) -> None:
        self._visible = False
        self.hide()

    def toggle(self) -> bool:
        if self._visible:
            self.hide_panel()
        else:
            self.show_panel()
        return self._visible

    def apply_scale(self) -> None:
        pass  # fixed reading scale, like PageLens


__all__ = ["PdfReaderPanel", "PageConceptPanel"]