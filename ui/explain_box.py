"""PDF Explain Box — terminology, formula, chart explanations.

Architecture:
  - ExplainBox widget provides a scrollable panel for PDF explanations
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
)

from . import theme
from .image_ocr_worker import ImageOcrSignalRelay, ImageOcrWorker
from core.pdf_qa import ExplainEntry, PdfQa, PdfQaResult

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

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Explain Box")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        # State
        self._pdf_path: str | None = None
        self._qa: PdfQa | None = None
        self._visible = False
        self._external_provider_consent = False

        # Async worker (QThread + QObject)
        self._worker_thread: QThread | None = None
        self._worker: _ExplainWorker | None = None
        self._running = False

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

        self.resize(320, 400)

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

    def load_pdf(self, file_path: str | Path) -> None:
        """Load and parse a PDF locally without contacting any provider."""
        self._pdf_path = str(file_path)
        self._qa = PdfQa()
        self._external_provider_consent = False

        self._content.show_loading()

        # Extract concepts in background (would need threading for production)
        try:
            # Process PDF text extraction (fast, no LLM)
            result = self._qa.process(file_path)
            log.info("[ExplainBox] PDF loaded: %d pages, %d chars", result.total_pages, result.text_length)

            source_counts: dict[str, int] = {}
            for page in result.pages:
                source_counts[page.extraction_source] = (
                    source_counts.get(page.extraction_source, 0) + 1
                )
            details = "，".join(
                f"{source} {count} 页" for source, count in sorted(source_counts.items())
            )
            self._content.show_summary(
                f"已在本地解析 {result.total_pages} 页（{details}）。"
                "选择需要解释的文本后，再明确授权发送该段文本。",
                [],
            )

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
            self._content.show_explain(entry)
        except Exception as exc:
            self._content.show_error(f"无法解释术语：{exc}")

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
            self._content.show_answer(result.answer, result.terms)
        except Exception as exc:
            self._content.show_error(f"无法回答：{exc}")

    def set_external_provider_consent(self, granted: bool) -> None:
        """Set consent only from an explicit user-facing confirmation action."""
        self._external_provider_consent = granted is True

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
        """Cooperatively stop the explain provider thread and wait for it to finish."""
        self._running = False
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
        # Duplicate-click protection: reject if a request is already in flight.
        if self._running:
            log.debug("[ExplainBox] explain_selection already running, ignoring")
            return

        self._running = True
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

        # When thread finishes, clean up worker and reset running flag.
        thread.finished.connect(self._stop_explain_thread)
        thread.finished.connect(thread.deleteLater)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)

        # Start the worker.
        thread.started.connect(worker.run)
        thread.start()

        # Store references so we can guard against reuse.
        self._worker_thread = thread
        self._worker = worker

    def _on_explain_finished(self, entry: ExplainEntry) -> None:
        """Handle successful explanation result (runs in Qt main thread)."""
        self._running = False
        self._worker_thread = None
        self._worker = None
        self._content.show_explain(entry)

    def _on_explain_error(self, message: str) -> None:
        """Handle provider/runtime error (runs in Qt main thread)."""
        self._running = False
        self._worker_thread = None
        self._worker = None
        self._content.show_error(f"无法解释选中文本：{message}")

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
            return False
        else:
            self.show_panel()
            return False


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
