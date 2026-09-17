"""AmbientStatusWidget — lightweight reading status feedback (Ambient Mode).

A compact translucent pill near the pet cluster showing what Firefly is
currently reading (title / page / section / keywords), in the spirit of the
Camera Vision status feedback. No reading window, no chat surface.

Dismiss semantics: the × button records the CURRENT document key (URL
without fragment). Further updates for the same document keep the internal
labels fresh but the pill stays hidden; a different document clears the
dismissal and the pill may appear again.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from . import theme
from core.paper_context import PaperContext
from core.pdf_ambient_context import PdfAmbientContext

_MAX_PILL_WIDTH = 640
_TITLE_MAX_WIDTH = 190
_SECTION_MAX_WIDTH = 210
_KEYWORDS_MAX_WIDTH = 150


def _document_key_from(url: str) -> str:
    """Document identity: URL without its fragment (#page=N is navigation)."""
    return (url or "").split("#", 1)[0].strip().lower()


class AmbientStatusWidget(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = theme.GlassPanel(theme.RADIUS_ITEM, self)
        theme.apply_soft_shadow(card, blur=16, y_offset=2)
        layout.addWidget(card)

        inner = QHBoxLayout(card)
        inner.setContentsMargins(12, 6, 6, 6)
        inner.setSpacing(8)

        self._title_label = QLabel("", card)
        self._title_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 9pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._title_label.setMaximumWidth(_TITLE_MAX_WIDTH)
        inner.addWidget(self._title_label)

        self._section_label = QLabel("", card)
        self._section_label.setStyleSheet(
            f"color: {theme.css_color(theme.PAGELENS_TEXT_ACCENT)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt;"
        )
        self._section_label.setMaximumWidth(_SECTION_MAX_WIDTH)
        inner.addWidget(self._section_label)

        self._keywords_label = QLabel("", card)
        self._keywords_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 8pt;"
        )
        self._keywords_label.setMaximumWidth(_KEYWORDS_MAX_WIDTH)
        inner.addWidget(self._keywords_label)

        self._close_btn = QPushButton("×", card)
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("关闭阅读状态")
        self._close_btn.setStyleSheet(theme.link_button_style("ambientClose"))
        self._close_btn.clicked.connect(self.dismiss)
        inner.addWidget(self._close_btn)

        self._document_key: str | None = None
        self._dismissed_document_key: str | None = None

        self.setMaximumWidth(_MAX_PILL_WIDTH)
        self.hide()

    # ------------------------------------------------------------------
    # updates (data always refreshed; visibility gated by dismiss state)
    # ------------------------------------------------------------------

    def show_context(self, context: PaperContext) -> None:
        """HTML-page ambient status (extension concepts/selection)."""
        self._set_document_key(_document_key_from(context.url))
        self._title_label.setText(f"当前阅读：{context.title[:24]}")
        self._title_label.setToolTip(context.title)
        self._section_label.setText(f"章节：{context.section[:22]}" if context.section else "章节：（未知）")
        self._section_label.setToolTip(context.section)
        self._keywords_label.setText(f"关键词：{context.selection[:20]}" if context.selection else "关键词：（无选区）")
        self._keywords_label.setToolTip(context.selection)
        self._maybe_show()

    def show_pdf(self, context: PdfAmbientContext) -> None:
        """PDF ambient status (opened paper + latest viewport values)."""
        name = context.title or context.file_name or "未知论文"
        self._set_document_key(_document_key_from(context.url or context.file_name))
        self._title_label.setText(f"当前阅读：{name[:24]}")
        self._title_label.setToolTip(name)
        page_text = (
            f"{context.current_page}/{context.total_pages}"
            if context.total_pages
            else "未知"
        )
        section = context.section[:22] if context.section else "（未知）"
        self._section_label.setText(f"页码：{page_text} · 章节：{section}")
        self._section_label.setToolTip(f"页码：{page_text} · 章节：{context.section}")
        keywords = " · ".join(context.keywords[:6]) or "（未知）"
        self._keywords_label.setText(f"关键词：{keywords}")
        self._keywords_label.setToolTip(" · ".join(context.keywords))
        self._maybe_show()

    def _set_document_key(self, key: str) -> None:
        """A different document clears the user dismissal (dismiss is
        per-document, not global)."""
        key = key or ""
        if key != self._document_key:
            self._document_key = key
            self._dismissed_document_key = None

    def _maybe_show(self) -> None:
        if self._document_key and self._dismissed_document_key == self._document_key:
            self.hide()
            return
        self.adjustSize()
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        """User clicked ×: remember the document and hide."""
        self._dismissed_document_key = self._document_key
        self.hide()

    def is_dismissed_for(self, key: str) -> bool:
        return bool(key) and self._dismissed_document_key == key


__all__ = ["AmbientStatusWidget"]