"""PaperContext — Ambient reading context (Context Layer).

Holds the CURRENT page only: URL / title / section / visible text /
selection. In-RAM, single page, no Memory, no database, no persistence —
the store is replaced on every page_context update and never written out.

``source_type`` reserves the future PDF path ("pdf") while HTML pages use
"html"; the browser never reaches Edge's sealed PDF viewer in this phase.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.pdf_ambient_context import PdfAmbientContext

_MAX_VISIBLE_TEXT = 2000


@dataclass
class PaperContext:
    """One ambient snapshot of the page the user is reading."""

    url: str = ""
    title: str = ""
    section: str = ""
    visible_text: str = ""
    selection: str = ""
    selection_page: int = -1
    timestamp: float = 0.0
    source_type: str = "html"  # "html" now; "pdf" reserved for the future


class PaperContextStore:
    """Holds exactly one PaperContext (the current page). Qt-free."""

    def __init__(self) -> None:
        self._context = PaperContext()
        # Ambient PDF v1.0: the PDF the user has open in the browser (in-RAM).
        self._pdf: PdfAmbientContext | None = None

    @property
    def current(self) -> PaperContext:
        return self._context

    @property
    def pdf(self) -> PdfAmbientContext | None:
        """Current opened-PDF ambient context (None when no PDF is open)."""
        return self._pdf

    def snapshot(self) -> PaperContext:
        """Detached copy for consumers."""
        return PaperContext(**self._context.__dict__)

    def update_page(self, payload: dict) -> None:
        """Apply a page_context message (url/title/heading/text…)."""
        if not isinstance(payload, dict):
            return
        url = str(payload.get("url") or "").strip()
        title = str(payload.get("title") or "").strip()
        section = str(payload.get("heading") or payload.get("section") or "").strip()
        visible = str(payload.get("text") or payload.get("visible_text") or "").strip()
        self._context.url = url
        self._context.title = title
        self._context.section = section
        self._context.visible_text = visible[:_MAX_VISIBLE_TEXT]
        self._context.timestamp = time.time()
        if url:
            self._context.source_type = "pdf" if url.lower().endswith(".pdf") else "html"

    def update_selection(self, text: str, page: int = -1) -> None:
        """Apply a selection message; keeps the page context intact."""
        text = (text or "").strip()[:2000]
        self._context.selection = text
        self._context.selection_page = int(page) if int(page) >= 1 else -1
        self._context.timestamp = time.time()

    # ------------------------------------------------------------------
    # Ambient PDF v1.0
    # ------------------------------------------------------------------

    def set_pdf(self, context: PdfAmbientContext | None) -> None:
        """Replace the opened-PDF context (from the pdf_opened event)."""
        self._pdf = context

    def update_pdf_page(self, page: int) -> None:
        """Advance the current PDF page from a page-carrying signal.

        Sources: an explain/selection with a real page, or the reserved
        pdf_view_state interface. No-op when no PDF is open.
        """
        if self._pdf is None:
            return
        try:
            page = int(page)
        except (TypeError, ValueError):
            return
        if page < 1:
            return
        if self._pdf.total_pages and page > self._pdf.total_pages:
            page = self._pdf.total_pages
        self._pdf.current_page = page
        self._pdf.timestamp = time.time()

    def update_pdf_viewport(
        self,
        *,
        page: int | None = None,
        total_pages: int | None = None,
        section: str | None = None,
        keywords: list[str] | None = None,
        visible_text: str | None = None,
    ) -> bool:
        """Merge a PDF-viewport observation into the current opened PDF.

        Overwrites ONLY page/total_pages/section/keywords/visible_text —
        the "which paper" identity from pdf_opened is preserved. No-op when
        no PDF is open. In-RAM only; new observations replace old ones.
        """
        if self._pdf is None:
            return False
        if page is not None and int(page) >= 1:
            self._pdf.current_page = int(page)
        if total_pages is not None and int(total_pages) >= 1:
            self._pdf.total_pages = int(total_pages)
        if section is not None:
            self._pdf.section = str(section)[:120]
        if keywords is not None:
            self._pdf.keywords = [str(k) for k in keywords][:8]
        if visible_text is not None:
            self._pdf.visible_text = str(visible_text)[:2000]
        self._pdf.timestamp = time.time()
        return True

    def clear(self) -> None:
        self._context = PaperContext()
        self._pdf = None


__all__ = ["PaperContext", "PaperContextStore"]