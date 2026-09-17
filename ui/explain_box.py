"""PDF Explain Box — terminology, formula, chart explanations.

Architecture:
  - The object retains the proven explanation worker/consent lifecycle
  - Production single-surface mode publishes results to PageLens and keeps
    the legacy scrollable top-level hidden
  - Integrates with existing PageLens concept cards via shared theme
  - Supports: term explanation, formula explanation, chart explanation,
    paragraph summary, concept cards, and recommended questions
  - Async: explain_selection runs in a background thread to avoid blocking
    the Qt main thread.  Uses QThread + worker signal pattern (same family
    as ``ui.character_conversation_runner``).

Dependencies:
  - core.pdf_qa (this project)
  - core.pdf_processor (this project)
  - ui.theme (this project)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QApplication,
    QSizePolicy,
    QFileDialog,
    QPushButton,
)

from . import theme
from .image_ocr_worker import ImageOcrSignalRelay, ImageOcrWorker
from .pdf_page_view import DEFAULT_FIT_WIDTH, PdfPageView
from core.pdf_qa import ExplainEntry, PdfQa, PdfQaResult
from core.pdf_processor import PdfEncryptedError, build_pdf_lazy_index

log = logging.getLogger("firefly.explain_box")


# ---------------------------------------------------------------------------
# Async worker: runs PdfQa.explain_selection outside the Qt main thread
# ---------------------------------------------------------------------------

class _ExplainWorker(QObject):
    """Runs PdfQa.explain_selection in a background thread.

    Signals
        finished(ExplainEntry): successful explanation
        error(str): provider / runtime error message
    """

    finished = Signal(object)  # ExplainEntry
    error = Signal(str)

    def __init__(
        self,
        qa: PdfQa,
        selected_text: str,
        source_page: int,
        consent: bool,
        image_ocr_text: str = "",
    ) -> None:
        super().__init__()
        self._qa = qa
        self._selected_text = selected_text
        self._source_page = source_page
        self._consent = consent
        self._image_ocr_text = image_ocr_text

    def run(self) -> None:
        try:
            entry = self._qa.explain_selection(
                self._selected_text,
                source_page=self._source_page,
                consent=self._consent,
                image_ocr_text=self._image_ocr_text or None,
            )
            self.finished.emit(entry)
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Async worker: runs RapidOCR on an image file outside the Qt main thread
# ---------------------------------------------------------------------------

# Public, shared worker — also used by PageLensPanel.
# ExplainBox keeps a backwards-compatible alias for existing imports in tests.
_ImageOcrWorker = ImageOcrWorker


# ---------------------------------------------------------------------------
# Explain Box widget
# ---------------------------------------------------------------------------

class ExplainBox(QWidget):
    """PDF Explain Box — terminology, formula, chart explanations.

    Signals:
        term_requested(str): user clicked a term chip to explain
        question_requested(str): user clicked a recommended question
        close_requested(): user clicked the close button

    Async behaviour:
        explain_selection() spawns a background worker.  The UI returns
        immediately and updates when the worker emits ``finished`` / ``error``.
    """

    term_requested = Signal(str)
    question_requested = Signal(str)
    close_requested = Signal()
    # P0.3: controller output for the single visible reading surface.
    # The stable worker/consent lifecycle remains here; PageLens renders it.
    surface_loading = Signal(dict)
    surface_consent_required = Signal(dict)
    surface_card = Signal(dict)
    surface_error = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Explain Box")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        # State
        self._pdf_path: str | None = None
        self._qa: PdfQa | None = None
        self._visible = False
        self._single_surface_mode = False
        self._external_provider_consent = False
        # PaperLens P0: local PDF reading + word-selection explanation.
        self._page_texts: list[str] = []
        self._current_page: int = 0  # 0-based; page_number sent out = +1
        self._explain_consent_granted = False
        self._pending_selection: tuple[str, int] | None = None
        self._last_selection: tuple[str, int] | None = None

        # Async worker (QThread + QObject)
        # Lifecycle: _running covers "worker.run in flight, result not yet
        # delivered"; _finishing covers "result delivered, QThread event loop
        # not yet finished".  _worker/_worker_thread are released only by
        # _on_explain_thread_finished (QThread.finished), never earlier.
        self._worker_thread: QThread | None = None
        self._worker: _ExplainWorker | None = None
        self._running = False
        self._finishing = False

        # Image attachment state
        self._image_path: str | None = None
        self._ocr_text: str = ""
        self._ocr_status: str = "未识别"  # 未识别/识别中/已识别/OCR失败
        self._ocr_generation: int = 0  # generation counter for race prevention
        self._ocr_thread: QThread | None = None
        self._ocr_worker: _ImageOcrWorker | None = None
        self._ocr_relay = ImageOcrSignalRelay(self)
        self._ocr_relay.finished.connect(
            self._on_ocr_finished, Qt.QueuedConnection,
        )
        self._ocr_relay.error.connect(
            self._on_ocr_error, Qt.QueuedConnection,
        )

        # Root layout
        root = QVBoxLayout(self)
        shadow = theme.SHADOW_MARGIN - 2
        root.setContentsMargins(shadow, shadow, shadow, shadow)
        root.setSpacing(0)

        self._glass = theme.GlassPanel(theme.RADIUS_CARD, self)
        theme.apply_soft_shadow(self._glass)
        root.addWidget(self._glass)

        inner = QVBoxLayout(self._glass)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # Header
        self._header = self._build_header()
        inner.addWidget(self._header)

        # Separator
        sep = QFrame(self._glass)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        inner.addWidget(sep)

        # PDF reading toolbar (open PDF + consent for selection explain)
        self._pdf_toolbar = self._build_pdf_toolbar()
        inner.addWidget(self._pdf_toolbar)

        # Page text view: the selection source for word-selection explanation
        self._page_area = self._build_page_area()
        inner.addWidget(self._page_area)

        # Separator
        sep2 = QFrame(self._glass)
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        inner.addWidget(sep2)

        # Image attachment toolbar
        self._image_toolbar = self._build_image_toolbar()
        inner.addWidget(self._image_toolbar)

        # Scroll area
        self._scroll = QScrollArea(self._glass)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; }"
            "QScrollBar::handle:vertical { background: %s; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            % theme.css_color(theme.GLASS_BORDER)
        )
        inner.addWidget(self._scroll)

        self._content = _ExplainContent(self._glass)
        self._scroll.setWidget(self._content)

        self.resize(460, 640)

    def _build_header(self) -> QFrame:
        header = QFrame(self._glass)
        header.setFixedHeight(36)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)

        title = QLabel("Explain Box", header)
        title.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 11pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        layout.addWidget(title, 1)

        # Close button
        close_btn = _CloseButton(header)
        close_btn.clicked.connect(lambda: self.close_requested.emit())
        layout.addWidget(close_btn)

        return header

    def _build_image_toolbar(self) -> QFrame:
        """Build the image attachment toolbar below the header."""
        toolbar = QFrame(self._glass)
        toolbar.setFixedHeight(44)
        toolbar.setObjectName("imageToolbar")

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # Add image button
        add_btn = _IconButton("📷", "添加图片", self._glass)
        add_btn.clicked.connect(self._on_add_image)
        layout.addWidget(add_btn)

        # Image preview label (hidden until an image is attached)
        self._image_preview = QLabel(self._glass)
        self._image_preview.setFixedSize(32, 32)
        self._image_preview.setVisible(False)
        layout.addWidget(self._image_preview)

        # Remove image button (hidden until an image is attached)
        self._remove_btn = _IconButton("✕", "移除", self._glass)
        self._remove_btn.setVisible(False)
        self._remove_btn.clicked.connect(self._on_remove_image)
        layout.addWidget(self._remove_btn)

        # Spacer
        layout.addStretch(1)

        # OCR status label
        self._ocr_status_label = QLabel("未识别", toolbar)
        self._ocr_status_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 8pt;"
        )
        self._ocr_status_label.setVisible(False)
        layout.addWidget(self._ocr_status_label)

        # Collapsible OCR text panel (hidden by default)
        self._ocr_text_panel = QFrame(self._glass)
        self._ocr_text_panel.setVisible(False)
        self._ocr_text_panel.setFixedHeight(0)
        self._ocr_text_panel.setStyleSheet(
            f"background: {theme.css_color(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: 6px;"
        )
        ocr_inner = QVBoxLayout(self._ocr_text_panel)
        ocr_inner.setContentsMargins(6, 4, 6, 4)
        ocr_inner.setSpacing(2)

        self._ocr_text_label = QLabel("", self._ocr_text_panel)
        self._ocr_text_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 8pt;"
        )
        self._ocr_text_label.setWordWrap(True)
        self._ocr_text_label.setMaximumHeight(60)
        ocr_inner.addWidget(self._ocr_text_label)

        layout.addWidget(self._ocr_text_panel)

        return toolbar

    # ------------------------------------------------------------------
    # PaperLens P0: PDF reading + word-selection explanation
    # ------------------------------------------------------------------

    def _build_pdf_toolbar(self) -> QFrame:
        """PDF toolbar: open a local PDF (fast text-layer index, no OCR)."""
        toolbar = QFrame(self._glass)
        toolbar.setObjectName("pdfToolbar")
        toolbar.setFixedHeight(40)

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        self._open_pdf_btn = _IconButton("📄", "打开 PDF", self._glass)
        self._open_pdf_btn.clicked.connect(self._on_open_pdf)
        layout.addWidget(self._open_pdf_btn)

        self._pdf_filename = QLabel("未打开 PDF", toolbar)
        self._pdf_filename.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt;"
        )
        self._pdf_filename.setWordWrap(False)
        self._pdf_filename.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        layout.addWidget(self._pdf_filename, 1)

        # Explain + consent live here so browser selections (no local PDF
        # loaded) can still be explained.
        self._consent_btn = QPushButton("授权并解释", toolbar)
        self._consent_btn.setVisible(False)
        self._consent_btn.setCursor(Qt.PointingHandCursor)
        self._consent_btn.setStyleSheet(theme.popover_button_style("pdfConsent"))
        self._consent_btn.clicked.connect(self._on_consent_granted)
        layout.addWidget(self._consent_btn)

        self._explain_btn = QPushButton("解释", toolbar)
        self._explain_btn.setCursor(Qt.PointingHandCursor)
        self._explain_btn.setStyleSheet(theme.link_button_style("pdfExplain"))
        self._explain_btn.setEnabled(False)
        self._explain_btn.clicked.connect(self._on_explain_clicked)
        layout.addWidget(self._explain_btn)

        self._pdf_status = QLabel("", toolbar)
        self._pdf_status.setStyleSheet(
            f"color: {theme.css_color(theme.CLAUDE_ORANGE)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt;"
        )
        self._pdf_status.setVisible(False)
        layout.addWidget(self._pdf_status)

        return toolbar

    def _build_page_area(self) -> QFrame:
        """Rendered page canvas: mouse drag-select feeds explain_selection."""
        area = QFrame(self._glass)
        area.setObjectName("pageArea")
        area.setFixedHeight(300)
        area.setVisible(False)

        layout = QVBoxLayout(area)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        self._page_scroll = QScrollArea(area)
        self._page_scroll.setWidgetResizable(False)
        self._page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._page_scroll.setStyleSheet(
            "QScrollArea { background: white; border: 1px solid "
            + theme.css_color(theme.GLASS_BORDER)
            + "; border-radius: 6px; }"
        )
        self._page_view = PdfPageView(self._page_scroll)
        self._page_view.selection_made.connect(self._on_page_selection)
        self._page_scroll.setWidget(self._page_view)
        layout.addWidget(self._page_scroll, 1)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self._prev_btn = QPushButton("‹", area)
        self._prev_btn.setFixedSize(30, 24)
        self._prev_btn.setCursor(Qt.PointingHandCursor)
        self._prev_btn.setStyleSheet(theme.link_button_style("pdfNav"))
        self._prev_btn.setEnabled(False)
        self._prev_btn.clicked.connect(self._on_page_prev)
        nav.addWidget(self._prev_btn)

        self._page_label = QLabel("第 0 / 0 页", area)
        self._page_label.setStyleSheet(theme.secondary_label_style(size=8))
        self._page_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(self._page_label)

        self._next_btn = QPushButton("›", area)
        self._next_btn.setFixedSize(30, 24)
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.setStyleSheet(theme.link_button_style("pdfNav"))
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._on_page_next)
        nav.addWidget(self._next_btn)

        self._zoom_out_btn = QPushButton("−", area)
        self._zoom_out_btn.setFixedSize(30, 24)
        self._zoom_out_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_out_btn.setToolTip("缩小")
        self._zoom_out_btn.setStyleSheet(theme.link_button_style("pdfZoom"))
        self._zoom_out_btn.clicked.connect(self._on_zoom_out)
        nav.addWidget(self._zoom_out_btn)

        self._zoom_in_btn = QPushButton("＋", area)
        self._zoom_in_btn.setFixedSize(30, 24)
        self._zoom_in_btn.setCursor(Qt.PointingHandCursor)
        self._zoom_in_btn.setToolTip("放大")
        self._zoom_in_btn.setStyleSheet(theme.link_button_style("pdfZoom"))
        self._zoom_in_btn.clicked.connect(self._on_zoom_in)
        nav.addWidget(self._zoom_in_btn)

        nav.addStretch(1)

        layout.addLayout(nav)
        return area

    def _on_open_pdf(self) -> None:
        """Open a PDF file dialog and load it for word-selection explain."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 PDF 论文",
            "",
            "PDF 文件 (*.pdf)",
        )
        if not path:
            return
        self.load_pdf(str(Path(path).resolve()))

    def _show_page(self, index: int) -> None:
        """Navigate the rendered page view (0-based) and reset selection."""
        if self._page_view.page_count == 0:
            return
        index = max(0, min(index, self._page_view.page_count - 1))
        self._current_page = index
        self._page_view.set_page(index)
        self._page_label.setText(
            f"第 {index + 1} / {self._page_view.page_count} 页"
        )
        self._prev_btn.setEnabled(index > 0)
        self._next_btn.setEnabled(index < self._page_view.page_count - 1)
        self._last_selection = None
        self._consent_btn.setVisible(False)
        if self._pending_selection is not None:
            self._pending_selection = None
        self._explain_btn.setEnabled(False)

    def _on_page_prev(self) -> None:
        self._show_page(self._current_page - 1)

    def _on_page_next(self) -> None:
        self._show_page(self._current_page + 1)

    def _on_zoom_in(self) -> None:
        self._page_view.zoom_in()

    def _on_zoom_out(self) -> None:
        self._page_view.zoom_out()

    def _on_page_selection(self, text: str, page: int) -> None:
        """The rendered page reported a drag-selection (text, 1-based page)."""
        self._last_selection = (text, page)
        self._pdf_status.setVisible(False)
        self._explain_btn.setEnabled(True)

    def _on_explain_clicked(self) -> None:
        """Route the last selection to PdfQa.explain_selection.

        The first explanation of a session requires an explicit consent click
        (the PdfQa external-provider gate); the selection is stored pending
        until then and never sent anywhere without it.
        """
        if self._last_selection is None:
            self._pdf_status.setVisible(True)
            self._pdf_status.setText("请先选择一段文字（浏览器划词或本地 PDF 划词）")
            return
        selected, page = self._last_selection
        if page < 1:
            self._pdf_status.setVisible(True)
            self._pdf_status.setText("缺少页码信息，无法带页码解释；请确认扩展已同步当前 PDF 页码")
            return
        log.info("[ExplainBox] selection explain page=%s text=%r", page, selected[:200])
        self._pdf_status.setVisible(False)
        if not self._explain_consent_granted:
            self._pending_selection = (selected, page)
            self._consent_btn.setVisible(True)
            self._pdf_status.setVisible(True)
            self._pdf_status.setText("首次解释需授权：将把选中文本发送到 AI 提供商")
            return
        self.explain_selection(selected, source_page=page)

    def _on_consent_granted(self) -> None:
        """User explicitly authorized sending the selected text."""
        self._external_provider_consent = True
        self._explain_consent_granted = True
        self._consent_btn.setVisible(False)
        self._pdf_status.setVisible(False)
        if self._pending_selection is not None:
            selected, page = self._pending_selection
            self._pending_selection = None
            self.explain_selection(selected, source_page=page)
        elif self._last_selection is not None:
            selected, page = self._last_selection
            if page >= 1:
                self.explain_selection(selected, source_page=page)

    def enable_single_surface_mode(self, enabled: bool = True) -> None:
        """Keep this object as the explanation controller, never as a window."""
        self._single_surface_mode = enabled is True
        if self._single_surface_mode:
            self.hide_panel()

    def grant_pending_selection_consent(self) -> None:
        """Public consent action used by the PageLens authorization button."""
        self._on_consent_granted()

    def show_browser_selection(self, text: str, page: int) -> None:
        """A browser selection arrived through the PageLens bridge.

        Reuses the exact ExplainBox explain path (consent gate + async
        worker + PdfQa.explain_selection). ``page`` is 1-based, or -1 when
        the browser could not determine it.
        """
        text = (text or "").strip()[:2_000]
        if not text:
            return
        if self._qa is None:
            self._qa = PdfQa()
        self._last_selection = (text, page)
        self._consent_btn.setVisible(not self._explain_consent_granted)
        self._explain_btn.setEnabled(page >= 1)
        preview = text if len(text) <= 40 else text[:40] + "…"
        if page < 1:
            hint = f"浏览器选区：{preview}（缺少页码，无法带页码解释）"
        elif not self._explain_consent_granted:
            hint = f"浏览器选区：{preview}（首次解释需授权）"
        else:
            hint = f"浏览器选区：{preview}"
        self._pdf_status.setVisible(True)
        self._pdf_status.setText(hint)
        if self._single_surface_mode:
            self.hide_panel()
            if page < 1:
                self.surface_error.emit("缺少页码信息，无法带页码解释")
            elif not self._explain_consent_granted:
                self._pending_selection = (text, page)
                self.surface_consent_required.emit({"title": preview, "page": page})
            else:
                self.explain_selection(text, source_page=page)
            return
        self.show_panel()

    def load_pdf(self, file_path: str | Path) -> None:
        """Load a PDF locally and show its rendered pages for selection.

        PaperLens 2: Firefly owns rendering + selection. Page text comes from
        the fast text-layer lazy index (never OCR on the main thread); the
        panel renders pages through PdfPageView (PyMuPDF pixmap). ``PdfQa``
        stays the single backend for ``explain_selection``.
        """
        self._pdf_path = str(file_path)
        self._qa = PdfQa()
        self._external_provider_consent = False
        self._explain_consent_granted = False
        self._pending_selection = None
        self._last_selection = None

        self._content.show_loading()

        try:
            data = Path(file_path).read_bytes()
            index = build_pdf_lazy_index(data, display_name=Path(file_path).name)
            self._page_texts = [
                (index.native_text.get(number, "") or "")
                for number in range(1, index.page_count + 1)
            ]
            log.info(
                "[ExplainBox] PDF indexed: %s pages, %d text pages",
                index.page_count,
                sum(1 for text in self._page_texts if text.strip()),
            )

            native_count = sum(1 for text in self._page_texts if text.strip())
            scanned_count = index.page_count - native_count
            details = f"文本层 {native_count} 页"
            if scanned_count:
                details += f"，扫描页 {scanned_count} 页"
            self._content.show_summary(
                f"已在本地解析 {index.page_count} 页（{details}）。"
                "在页面中用鼠标拖选文本，再明确授权发送该段文本。",
                [],
            )

            # Render the document into the page view (Firefly-owned display).
            self._page_view.load_bytes(data)
            viewport_width = self._page_scroll.viewport().width()
            if viewport_width <= 0:
                viewport_width = DEFAULT_FIT_WIDTH
            self._page_view.set_fit_width(viewport_width)
            log.info(
                "[ExplainBox] PDF rendered: %d pages (fit width %d)",
                self._page_view.page_count,
                viewport_width,
            )

            if self._page_view.page_count:
                self._page_area.setVisible(True)
                self._show_page(0)
                metrics = QFontMetrics(self.font())
                display = index.display_name or "PDF"
                if metrics.horizontalAdvance(display) > 260:
                    display = metrics.elidedText(display, Qt.ElideMiddle, 260)
                self._pdf_filename.setText(display)
            else:
                self._page_area.setVisible(False)
                self._pdf_filename.setText(Path(file_path).name)
        except PdfEncryptedError as exc:
            log.warning("[ExplainBox] PDF encrypted: %s", exc)
            self._content.show_error(f"无法加载 PDF：{exc}")
        except Exception as exc:
            log.warning("[ExplainBox] Failed to load PDF: %s", exc)
            self._content.show_error(f"无法加载 PDF：{exc}")

    def explain_term(self, term: str) -> None:
        """Explain a specific term."""
        if not self._qa or not self._pdf_path:
            return
        try:
            entry = self._qa.explain_term(
                self._pdf_path,
                term,
                consent=self._external_provider_consent,
            )
            if self._single_surface_mode:
                self.surface_card.emit(self._entry_surface_card(entry))
            else:
                self._content.show_explain(entry)
        except Exception as exc:
            message = f"无法解释术语：{exc}"
            if self._single_surface_mode:
                self.surface_error.emit(message)
            else:
                self._content.show_error(message)

    def ask_question(self, question: str) -> None:
        """Answer a question about the PDF."""
        if not self._qa or not self._pdf_path:
            return
        try:
            result = self._qa.answer(
                self._pdf_path,
                question,
                consent=self._external_provider_consent,
            )
            if self._single_surface_mode:
                self.surface_card.emit({
                    "term": "回答",
                    "summary": result.answer,
                    "related": list(result.terms or []),
                })
            else:
                self._content.show_answer(result.answer, result.terms)
        except Exception as exc:
            message = f"无法回答：{exc}"
            if self._single_surface_mode:
                self.surface_error.emit(message)
            else:
                self._content.show_error(message)

    def set_external_provider_consent(self, granted: bool) -> None:
        """Set consent only from an explicit user-facing confirmation action."""
        self._external_provider_consent = granted is True

    # ------------------------------------------------------------------
    # Phase 4-B: visual region explanation (delegates to the content view;
    # no thread changes, no image persistence)
    # ------------------------------------------------------------------

    def show_visual_explanation_loading(
        self, *, kind_label: str = "区域", page: int | None = None
    ) -> None:
        """Loading state while the visual region is being explained."""
        if self._single_surface_mode:
            self.surface_loading.emit({
                "title": kind_label,
                "kind": "visual",
                "page": page,
            })
            return
        self._content.show_visual_loading()

    def show_visual_explanation(
        self,
        text: str,
        *,
        kind_label: str = "区域",
        page: int | None = None,
    ) -> None:
        """Show a visual-region explanation result."""
        if self._single_surface_mode:
            icon = "📐" if kind_label == "公式" else "🖼" if kind_label == "图表" else "📷"
            title = f"{icon} {kind_label}"
            if page and page >= 1:
                title += f" · 第 {page} 页"
            self.surface_card.emit({
                "term": title,
                "summary": text,
                "_surface_kind": "visual",
            })
            return
        self._content.show_visual_explanation(
            text, kind_label=kind_label, page=page
        )

    def show_visual_error(self, message: str) -> None:
        """Brief error state for a failed visual-region request."""
        if self._single_surface_mode:
            self.surface_error.emit(f"区域解释失败：{message}")
            return
        self._content.show_error(f"区域解释失败：{message}")

    # ------------------------------------------------------------------
    # Image attachment
    # ------------------------------------------------------------------

    def _on_add_image(self) -> None:
        """Open file dialog to attach an image for OCR context."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        self._attach_image(str(Path(path).resolve()))

    def _attach_image(self, image_path: str) -> None:
        """Attach an image and start OCR."""
        # Cancel any in-flight OCR
        self._cancel_ocr()
        self._image_path = image_path
        self._ocr_text = ""
        self._ocr_status = "识别中"
        self._ocr_generation += 1
        gen = self._ocr_generation

        # Show preview
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self._image_preview.setPixmap(scaled)
            self._image_preview.setVisible(True)
            self._remove_btn.setVisible(True)

        # Show status
        self._ocr_status_label.setVisible(True)
        self._update_ocr_status()

        # Hide OCR text panel
        self._ocr_text_panel.setVisible(False)
        self._ocr_text_panel.setFixedHeight(0)

        # Run OCR in background
        thread = QThread(self)
        worker = _ImageOcrWorker(image_path, generation=gen)
        worker.moveToThread(thread)

        worker.finished.connect(
            self._ocr_relay.forward_finished, Qt.QueuedConnection,
        )
        worker.error.connect(
            self._ocr_relay.forward_error, Qt.QueuedConnection,
        )
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)

        thread.started.connect(worker.run)
        thread.start()

        self._ocr_thread = thread
        self._ocr_worker = worker

    @Slot(int, str)
    def _on_ocr_finished(self, gen: int, ocr_text: str) -> None:
        """Handle OCR completion (runs in Qt main thread via QueuedConnection)."""
        if gen != self._ocr_generation:
            return
        self._ocr_worker = None
        self._ocr_text = ocr_text
        self._ocr_status = "已识别" if ocr_text else "OCR失败"
        self._update_ocr_status()

        if ocr_text:
            self._ocr_text_label.setText(ocr_text)
            self._ocr_text_panel.setVisible(True)
            self._ocr_text_panel.setFixedHeight(70)

    @Slot(int, str)
    def _on_ocr_error(self, gen: int, message: str) -> None:
        """Handle OCR error (runs in Qt main thread via QueuedConnection)."""
        if gen != self._ocr_generation:
            return
        self._ocr_worker = None
        self._ocr_status = "OCR失败"
        self._update_ocr_status()
        log.warning("[ExplainBox] OCR error gen=%d: %s", gen, message)

    def _update_ocr_status(self) -> None:
        status_colors = {
            "未识别": theme.TEXT_SECONDARY,
            "识别中": theme.CLAUDE_ORANGE,
            "已识别": theme.CHATGPT_GREEN,
            "OCR失败": theme.ERROR_STATUS,
        }
        color = status_colors.get(self._ocr_status, theme.TEXT_SECONDARY)
        self._ocr_status_label.setStyleSheet(
            f"color: {theme.css_color(color)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 8pt;"
        )
        self._ocr_status_label.setText(self._ocr_status)

    def _on_remove_image(self) -> None:
        """Remove the attached image and clear OCR state."""
        self._ocr_generation += 1
        self._stop_ocr_thread()
        self._image_path = None
        self._ocr_text = ""
        self._ocr_status = "未识别"
        self._image_preview.setVisible(False)
        self._remove_btn.setVisible(False)
        self._ocr_status_label.setVisible(False)
        self._ocr_text_panel.setVisible(False)
        self._ocr_text_panel.setFixedHeight(0)

    def _cancel_ocr(self) -> None:
        """Stop accepting the current job and ask its event loop to exit."""
        self._ocr_worker = None
        if self._ocr_thread is not None:
            thread = self._ocr_thread
            self._ocr_thread = None
            try:
                if thread.isRunning():
                    thread.quit()
            except RuntimeError:
                pass

    def _stop_ocr_thread(self) -> None:
        """Cooperatively stop the OCR thread and wait for it to finish."""
        self._cancel_ocr()
        if self._ocr_thread is not None:
            thread = self._ocr_thread
            self._ocr_thread = None
            try:
                if thread.isRunning():
                    thread.wait(5000)
            except RuntimeError:
                pass
            self._ocr_worker = None

    def _stop_explain_thread(self) -> None:
        """Cooperatively stop the explain provider thread and wait for it to finish.

        Shutdown path only (widget close / app exit): normal request
        completion is handled by worker signals → thread.quit →
        _on_explain_thread_finished.  Waits with a finite timeout and never
        blocks forever.
        """
        self._running = False
        self._finishing = False
        if self._worker_thread is not None:
            thread = self._worker_thread
            self._worker_thread = None
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(5000)
            except RuntimeError:
                pass
            self._worker = None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Gracefully shut down background threads before closing."""
        self._stop_ocr_thread()
        self._stop_explain_thread()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # explain_selection (extended with image OCR context)
    # ------------------------------------------------------------------

    def explain_selection(
        self, selected_text: str, *, source_page: int
    ) -> None:
        """Explain one explicitly selected passage; never sends the full PDF.

        Runs asynchronously in a background thread.  The UI returns
        immediately and updates when the worker finishes or fails.
        """
        if not self._qa:
            return
        # Duplicate-click / lifecycle protection: a new request may only start
        # after the previous QThread.finished released the old references.
        # Rejecting here never queues requests and never overwrites the
        # _worker/_worker_thread of a thread that has not finished yet.
        if (
            self._running
            or self._finishing
            or self._worker is not None
            or self._worker_thread is not None
        ):
            log.info(
                "[ExplainBox] explain request rejected: previous lifecycle "
                "still active (running=%s finishing=%s)",
                self._running,
                self._finishing,
            )
            return

        self._running = True
        if self._single_surface_mode:
            preview = selected_text if len(selected_text) <= 60 else selected_text[:60] + "…"
            self.surface_loading.emit({"title": preview, "page": source_page})
        else:
            self._content.show_loading()

        # Create worker + thread (QThread ensures signals fire in main thread).
        thread = QThread(self)
        worker = _ExplainWorker(
            qa=self._qa,
            selected_text=selected_text,
            source_page=source_page,
            consent=self._external_provider_consent,
            image_ocr_text=self._ocr_text,
        )
        worker.moveToThread(thread)

        # Wire signals: worker finished → UI update; worker error → UI update.
        worker.finished.connect(
            self._on_explain_finished, Qt.QueuedConnection,
        )
        worker.error.connect(
            self._on_explain_error, Qt.QueuedConnection,
        )

        # Worker cleans itself up inside its own thread event loop (never in
        # the GUI thread), then asks the event loop to exit normally.
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        # QThread.finished is the ONLY point that releases the worker/thread
        # references.  The identity-checked cleanup runs before the thread
        # object's own deleteLater (connection order = delivery order).
        thread.finished.connect(self._on_explain_thread_finished)
        thread.finished.connect(thread.deleteLater)

        # Start the worker.
        thread.started.connect(worker.run)
        thread.start()

        # Store references so we can guard against reuse.
        self._worker_thread = thread
        self._worker = worker
        log.info(
            "[ExplainBox] explain thread started worker=%s thread=%s page=%s",
            id(worker),
            id(thread),
            source_page,
        )

    def _on_explain_finished(self, entry: ExplainEntry) -> None:
        """Handle successful explanation result (runs in Qt main thread).

        Duty of this slot: flip the request state and render the result.  The
        thread may still be running here, so the worker/thread references are
        deliberately kept until QThread.finished does the identity-checked
        cleanup.
        """
        self._running = False
        self._finishing = True
        if self._single_surface_mode:
            self.surface_card.emit(self._entry_surface_card(entry))
        else:
            self._content.show_explain(entry)
        log.info(
            "[ExplainBox] explain result shown; waiting for thread exit "
            "(thread=%s)",
            id(self._worker_thread),
        )

    def _on_explain_error(self, message: str) -> None:
        """Handle provider/runtime error (runs in Qt main thread).

        Same contract as the success slot: state + UI only; the references
        survive until QThread.finished.
        """
        self._running = False
        self._finishing = True
        error = f"无法解释选中文本：{message}"
        if self._single_surface_mode:
            self.surface_error.emit(error)
        else:
            self._content.show_error(error)
        log.info(
            "[ExplainBox] explain error shown; waiting for thread exit "
            "(thread=%s)",
            id(self._worker_thread),
        )

    def _on_explain_thread_finished(self) -> None:
        """Release worker/thread references for exactly one finished QThread.

        Runs in the GUI thread (queued).  The cleanup only fires when the
        finished thread is the one currently recorded, so a late ``finished``
        from a previous request (or from shutdown) can never clear the
        references of a newer request.
        """
        thread = self.sender()
        if thread is None or self._worker_thread is not thread:
            return
        self._worker_thread = None
        self._worker = None
        self._finishing = False
        log.info("[ExplainBox] explain thread finished; references released")

    def show_panel(self) -> None:
        if self._single_surface_mode:
            self._visible = False
            self.hide()
            return
        self._visible = True
        self.show()
        self.raise_()

    def hide_panel(self) -> None:
        self._visible = False
        self.hide()

    def toggle(self) -> bool:
        if self._single_surface_mode:
            self.hide_panel()
            return False
        if self._visible:
            self.hide_panel()
            return False
        else:
            self.show_panel()
            return False

    @staticmethod
    def _entry_surface_card(entry: ExplainEntry) -> dict:
        """Mechanical ExplainEntry → PageLens card mapping; no AI changes."""
        source = (entry.source_text or "").strip()
        title = source if source else entry.label
        if len(title) > 80:
            title = title[:80] + "…"
        if entry.source_page >= 1:
            title = f"{title} · 第 {entry.source_page} 页"
        return {
            "term": title,
            "summary": entry.explanation,
            "_surface_kind": (
                "visual" if entry.kind in {"formula", "chart"} else "explain"
            ),
        }


# ---------------------------------------------------------------------------
# Explain Box content widget
# ---------------------------------------------------------------------------

class _ExplainContent(QWidget):
    """Scrollable content area for explain box entries."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 16)
        self._layout.setSpacing(10)
        self._layout.setAlignment(Qt.AlignTop)

        # Labels
        self._title_label = QLabel(self)
        self._title_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 13pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._title_label.setWordWrap(True)
        self._layout.addWidget(self._title_label)

        self._body_label = QLabel(self)
        self._body_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 10pt;"
        )
        self._body_label.setWordWrap(True)
        self._body_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self._layout.addWidget(self._body_label)

        # Concept chips area
        self._chips_frame = QFrame(self)
        self._chips_frame.setStyleSheet(
            f"background-color: {theme.css_color(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: 8px;"
        )
        self._chips_layout = QHBoxLayout(self._chips_frame)
        self._chips_layout.setContentsMargins(8, 6, 8, 6)
        self._chips_layout.setSpacing(6)
        self._chips_layout.setAlignment(Qt.AlignTop)
        self._layout.addWidget(self._chips_frame)

        # Separator
        sep = QFrame(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        self._layout.addWidget(sep)

        # Recommended questions
        self._questions_label = QLabel("推荐问题", self)
        self._questions_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: 9pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._layout.addWidget(self._questions_label)

        self._questions_frame = QFrame(self)
        self._questions_frame.setStyleSheet(
            f"background-color: {theme.css_color(theme.GLASS_BACKGROUND)}; "
            f"border-radius: 8px;"
        )
        self._questions_layout = QVBoxLayout(self._questions_frame)
        self._questions_layout.setContentsMargins(8, 6, 8, 6)
        self._questions_layout.setSpacing(4)
        self._layout.addWidget(self._questions_frame)

    def show_loading(self) -> None:
        self._title_label.setText("解释中…")
        self._body_label.setText("")
        self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()

    def show_error(self, message: str) -> None:
        self._title_label.setText("错误")
        self._body_label.setText(message)
        self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()

    def show_concepts(self, concepts: list[str]) -> None:
        """Show top concept chips."""
        self._chips_frame.show()
        # Clear old chips
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for concept in concepts[:8]:
            chip = _ExplainChip(concept)
            self._chips_layout.addWidget(chip)

    def show_summary(self, summary: str, questions: list[str]) -> None:
        """Show PDF summary and recommended questions."""
        self._title_label.setText("PDF 摘要")
        self._body_label.setText(summary)
        self._chips_frame.hide()

        if questions:
            self._questions_label.show()
            self._questions_frame.show()
            while self._questions_layout.count():
                item = self._questions_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for q in questions[:5]:
                q_label = QLabel(f"• {q}", self._questions_frame)
                q_label.setStyleSheet(
                    f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
                    f"font-family: '{theme.FONT_FAMILY}'; "
                    f"font-size: 9pt;"
                )
                q_label.setWordWrap(True)
                q_label.setCursor(Qt.PointingHandCursor)
                q_label.setToolTip(q)
                self._questions_layout.addWidget(q_label)
        else:
            self._questions_label.hide()
            self._questions_frame.hide()

    def show_explain(self, entry: ExplainEntry) -> None:
        """Show a term/formula/chart explanation."""
        kind_icons = {
            "term": "📖",
            "formula": "∑",
            "chart": "📊",
            "paragraph": "📝",
            "section": "📑",
        }
        icon = kind_icons.get(entry.kind, "📖")
        self._title_label.setText(f"{icon} {entry.label}")
        self._body_label.setText(entry.explanation)
        self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()

    def show_answer(self, answer: str, terms: list[str]) -> None:
        """Show a QA answer."""
        self._title_label.setText("回答")
        self._body_label.setText(answer)
        if terms:
            self._chips_frame.show()
            while self._chips_layout.count():
                item = self._chips_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for term in terms[:6]:
                chip = _ExplainChip(term)
                self._chips_layout.addWidget(chip)
        else:
            self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()

    # ------------------------------------------------------------------
    # Phase 4-B: visual region explanation (loading + result)
    # ------------------------------------------------------------------

    def show_visual_loading(self) -> None:
        """Loading state for a visual-region explanation."""
        self._title_label.setText("视觉解释中…")
        self._body_label.setText("正在分析框选的论文区域…")
        self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()

    def show_visual_explanation(
        self, text: str, *, kind_label: str = "区域", page: int | None = None
    ) -> None:
        """Show a visual-region explanation result (in-memory; no image
        paths, no images)."""
        title = f"📷 {kind_label}"
        if page and page >= 1:
            title += f" · 第 {page} 页"
        self._title_label.setText(title)
        self._body_label.setText(text)
        self._chips_frame.hide()
        self._questions_label.hide()
        self._questions_frame.hide()


# ---------------------------------------------------------------------------
# Explain chip widget
# ---------------------------------------------------------------------------

class _ExplainChip(QFrame):
    """Compact clickable chip for terms/concepts in Explain Box."""

    clicked = Signal(str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        self.setFixedHeight(22)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False
        self.setObjectName("explainChip")

    def sizeHint(self) -> QSize:
        text_width = QFontMetrics(self.font()).horizontalAdvance(self._text)
        width = max(48, min(text_width + 20, 180))
        return QSize(width, 22)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._text)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter, QPainterPath, QColor
        from PySide6.QtCore import QRectF

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 11, 11)

        if self._hovered:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND_HOVER))
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND))
            painter.setPen(theme.qcolor(theme.GLASS_BORDER))

        painter.drawPath(path)
        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        text_rect = self.rect().adjusted(8, 0, -8, 0)
        text = QFontMetrics(painter.font()).elidedText(
            self._text, Qt.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignCenter, text)
        painter.end()


class _IconButton(QFrame):
    """Compact icon button for the image toolbar."""

    clicked = Signal()

    def __init__(self, icon: str, tooltip: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon = icon
        self._tooltip = tooltip
        self.setFixedSize(36, 32)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._hovered:
            painter.fillRect(
                self.rect(), theme.qcolor(theme.GLASS_BACKGROUND_HOVER)
            )
        else:
            painter.fillRect(self.rect(), Qt.transparent)

        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        painter.drawText(
            self.rect(), Qt.AlignCenter, self._icon
        )
        painter.end()


class _CloseButton(QFrame):
    """Simple X close button."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter, QColor
        from PySide6.QtCore import QRect

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._hovered:
            painter.setBrush(QColor(200, 80, 80, 40))
            painter.setPen(QColor(200, 80, 80))
        else:
            painter.setBrush(QColor(128, 128, 136, 20))
            painter.setPen(QColor(128, 128, 136))

        painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)

        # Draw X
        painter.setPen(QColor(128, 128, 136) if not self._hovered else QColor(200, 80, 80))
        painter.drawLine(6, 6, 14, 14)
        painter.drawLine(14, 6, 6, 14)
        painter.end()


__all__ = [
    "ExplainBox",
    "ExplainEntry",
]
