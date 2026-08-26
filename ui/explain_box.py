"""PDF Explain Box — terminology, formula, chart explanations.

Architecture:
  - ExplainBox widget provides a scrollable panel for PDF explanations
  - Integrates with existing PageLens concept cards via shared theme
  - Supports: term explanation, formula explanation, chart explanation,
    paragraph summary, concept cards, and recommended questions

Dependencies:
  - core.pdf_qa (this project)
  - core.pdf_processor (this project)
  - ui.theme (this project)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QApplication,
    QSizePolicy,
)

from . import theme
from core.pdf_qa import ExplainEntry, PdfQa, PdfQaResult

log = logging.getLogger("firefly.explain_box")


class ExplainBox(QWidget):
    """PDF Explain Box — terminology, formula, chart explanations.

    Signals:
        term_requested(str): user clicked a term chip to explain
        question_requested(str): user clicked a recommended question
        close_requested(): user clicked the close button
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

    def load_pdf(self, file_path: str | Path) -> None:
        """Load a PDF and extract initial concepts/summary."""
        self._pdf_path = str(file_path)
        self._qa = PdfQa()

        self._content.show_loading()

        # Extract concepts in background (would need threading for production)
        try:
            # Process PDF text extraction (fast, no LLM)
            result = self._qa.process(file_path)
            log.info("[ExplainBox] PDF loaded: %d pages, %d chars", result.total_pages, result.text_length)

            # Extract concepts for top chips
            concepts = self._qa.extract_concepts(file_path)
            self._content.show_concepts(concepts)

            # Generate summary
            qa_result = self._qa.summarize(file_path)
            self._content.show_summary(qa_result.summary, qa_result.recommended_questions)

        except Exception as exc:
            log.warning("[ExplainBox] Failed to load PDF: %s", exc)
            self._content.show_error(f"无法加载 PDF：{exc}")

    def explain_term(self, term: str) -> None:
        """Explain a specific term."""
        if not self._qa or not self._pdf_path:
            return
        try:
            entry = self._qa.explain_term(self._pdf_path, term)
            self._content.show_explain(entry)
        except Exception as exc:
            self._content.show_error(f"无法解释术语：{exc}")

    def ask_question(self, question: str) -> None:
        """Answer a question about the PDF."""
        if not self._qa or not self._pdf_path:
            return
        try:
            result = self._qa.answer(self._pdf_path, question)
            self._content.show_answer(result.answer, result.terms)
        except Exception as exc:
            self._content.show_error(f"无法回答：{exc}")

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
            return True


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
        self._title_label.setText("正在加载 PDF...")
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
            painter.fillPath(path, QColor(theme.GLASS_BACKGROUND_HOVER))
            painter.setPen(QColor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, QColor(theme.GLASS_BACKGROUND))
            painter.setPen(QColor(theme.GLASS_BORDER))

        painter.drawPath(path)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        text_rect = self.rect().adjusted(8, 0, -8, 0)
        text = QFontMetrics(painter.font()).elidedText(
            self._text, Qt.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignCenter, text)
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
