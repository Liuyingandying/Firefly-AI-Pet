"""PdfAmbientContext — PDF current-page awareness (Ambient PDF v1.0).

Feeds the Ambient status with "which paper is the user reading" without
touching the browser's PDF viewer:

  - ``pdf_opened`` (extension, tabs.url/title) → file:// path → lazy index
  - current page: default 1; updated by explain/selection page signals or the
    reserved ``pdf_view_state`` interface (page:null until a real source).

Reuses ``build_pdf_lazy_index`` (no re-parsing) and ``build_page_concept``
(no OCR, no LLM). In-RAM only: tiny per-path index cache, no database, no
Memory, no long-term storage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from core.page_context import build_page_concept
from core.pdf_processor import build_pdf_lazy_index

_MAX_VISIBLE_TEXT = 2000
_MAX_CACHE = 3


@dataclass
class PdfAmbientContext:
    """One ambient snapshot of the PDF the user is reading."""

    url: str = ""
    title: str = ""
    file_name: str = ""
    total_pages: int = 0
    current_page: int = 1
    section: str = ""
    keywords: list[str] = field(default_factory=list)
    visible_text: str = ""
    timestamp: float = 0.0


class PdfAmbientEngine:
    """file:// PDF → LazyPdfIndex (cached) → page context. Qt-free."""

    def __init__(self) -> None:
        self._cache: dict[str, object] = {}  # resolved path -> LazyPdfIndex

    # ------------------------------------------------------------------
    # URL handling
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_local_path(url: str) -> Path | None:
        """file:///*.pdf → local Path; anything else (http…/missing) → None."""
        if not url:
            return None
        parsed = urlparse(url)
        if parsed.scheme != "file":
            return None
        try:
            path = Path(url2pathname(unquote(parsed.path)))
        except ValueError:
            return None
        return path if path.is_file() else None

    @staticmethod
    def file_name_from_url(url: str) -> str:
        if not url:
            return ""
        last = (url.rsplit("/", 1)[-1] or "").split("?", 1)[0].split("#", 1)[0]
        return unquote(last)

    @staticmethod
    def estimate_page(ratio, total_pages: int) -> int:
        """D3 formula: round(ratio * total_pages), clamped to [1, total]."""
        total = int(total_pages)
        if total <= 0:
            return 0
        try:
            r = float(ratio)
        except (TypeError, ValueError):
            return 1
        r = max(0.0, min(1.0, r))
        return max(1, min(total, round(r * total)))

    # ------------------------------------------------------------------
    # index cache
    # ------------------------------------------------------------------

    def _load_index(self, path: Path):
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        try:
            index = build_pdf_lazy_index(path.read_bytes(), display_name=path.name)
        except Exception:  # noqa: BLE001 - unreadable/encrypted PDF -> unknown
            return None
        self._cache[key] = index
        if len(self._cache) > _MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        return index

    # ------------------------------------------------------------------
    # context
    # ------------------------------------------------------------------

    def context_for(
        self, url: str, title: str = "", page=None
    ) -> PdfAmbientContext:
        """Build the ambient context for an opened PDF.

        Local file:// paths get a full page context (pages/section/keywords/
        visible text); other URLs fall back to url/title only (total_pages=0,
        keywords empty) — no download, no OCR, no viewer access. A URL
        fragment ``#page=N`` (the browser's own navigation hint) sets the
        current page.
        """
        ctx = PdfAmbientContext(
            url=url or "",
            title=title or "",
            file_name=self.file_name_from_url(url or ""),
            timestamp=time.time(),
        )
        # The URL fragment (#page=N) is a page hint from the browser itself —
        # it applies to remote PDFs too (no index needed), so parse it before
        # the local-index early return.
        fragment_page = self._page_from_fragment(url or "")
        if fragment_page:
            ctx.current_page = fragment_page
        path = self.resolve_local_path(url or "")
        index = self._load_index(path) if path is not None else None
        if index is None:
            return ctx
        ctx.total_pages = index.page_count
        if page in (None, 0):
            page = fragment_page
        try:
            page = 1 if page in (None, 0) else int(page)
        except (TypeError, ValueError):
            page = 1
        ctx.current_page = max(1, min(page, index.page_count))
        concept = build_page_concept(index, ctx.current_page)
        ctx.section = concept.section
        ctx.keywords = list(concept.keywords)
        text = (index.native_text.get(ctx.current_page, "") or "").strip()
        ctx.visible_text = text[:_MAX_VISIBLE_TEXT]
        return ctx

    @staticmethod
    def _page_from_fragment(url: str) -> int | None:
        """``…pdf#page=4`` → 4 (the browser's own page navigation hint)."""
        if not url or "#" not in url:
            return None
        fragment = url.rsplit("#", 1)[1]
        for part in fragment.split("&"):
            key, _, value = part.partition("=")
            if key.strip().lower() == "page":
                try:
                    page = int(value)
                except ValueError:
                    return None
                return page if page >= 1 else None
        return None


__all__ = ["PdfAmbientContext", "PdfAmbientEngine"]