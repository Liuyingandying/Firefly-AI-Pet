"""Lazy OCR session state for scanned PDFs (Progressive / Lazy OCR v1).

Attaching a scanned PDF only builds a cheap index (metadata + native text +
bookmarks); pages are OCR'd on demand, in small bounded batches, or via a
single-worker background job. All state is session-only (in memory, tied to
one attachment), never written to disk, history or Memory.

Design rules enforced here:
- at most MAX_LAZY_OCR_PAGES_PER_TURN pages OCR'd synchronously per turn
- generic QA runs at most MAX_PROGRESSIVE_OCR_PAGES_PER_TURN progressive pages
- an initial full summary seeds at most MAX_SUMMARY_SEED_OCR_PAGES pages
- "继续扫描" advances exactly one PROGRESSIVE_BATCH_SIZE batch
- an explicit full-OCR request runs ONE background worker with a cancel event
  and a generation id so stale results can never touch a newer attachment
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Sequence

from core.document_attachment import DocumentContext, DocumentSection
from core.pdf_processor import PdfPageStatus

log = logging.getLogger(__name__)

MAX_LAZY_OCR_PAGES_PER_TURN = 8
MAX_PROGRESSIVE_OCR_PAGES_PER_TURN = 6
MAX_SUMMARY_SEED_OCR_PAGES = 8
PROGRESSIVE_BATCH_SIZE = 6
SUMMARY_COVERAGE_THRESHOLD = 0.70

_PAGE_RANGE_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*(?:到|至|~|-)\s*(\d+)\s*页"),
    re.compile(r"\bpages?\s*(\d+)\s*(?:to|-|~)\s*(\d+)\b", re.IGNORECASE),
)
_PAGE_SINGLE_PATTERNS = (
    re.compile(r"第\s*(\d+)\s*页"),
    re.compile(r"\bpage\s*(\d+)\b", re.IGNORECASE),
)

_CONTINUE_SCAN_PHRASES = ("继续扫描", "再扫描一些", "继续识别", "多看几页", "继续扫")
_FULL_OCR_PHRASES = ("全部识别", "完整 OCR", "完整识别", "扫描全部页面", "全部页面都识别", "识别全部页面")

LOW_COVERAGE_REPLY = (
    "这份 PDF 是扫描版，我目前只识别了部分页面，还没有找到足够内容支持这个问题。"
    "你可以告诉我大概页码，或者让我继续扫描更多页面。"
)
PAGE_OCR_FAILED_REPLY = "这一页没有成功识别。"
BATCH_LIMIT_NOTE = "（一次最多识别 8 页，其余页面可以让我继续扫描）"


class LazyPdfOcrState:
    """Thread-safe, session-scoped lazy OCR state for one PDF attachment."""

    def __init__(self, index, generation_id: str) -> None:
        self.generation_id = generation_id
        self.page_count = index.page_count
        self.page_states = dict(index.page_states)
        self.native_text = dict(index.native_text)
        self.outline = list(index.outline)
        self.truncated = index.truncated
        self.display_name = index.display_name
        self.ocr_cache: dict[int, str] = {}
        self.ocr_failures: set[int] = set()
        self.cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._completed = sum(
            1 for state in self.page_states.values()
            if state in (PdfPageStatus.NATIVE, PdfPageStatus.OCR_DONE)
        )

    # ---------------------------------------------------------- read state

    def page_state(self, number: int) -> str:
        with self._lock:
            return self.page_states.get(number, PdfPageStatus.OCR_PENDING)

    def page_text(self, number: int) -> str:
        """Text for a page: native first, then OCR cache."""
        with self._lock:
            return self.native_text.get(number) or self.ocr_cache.get(number) or ""

    def ocr_pages_completed(self) -> int:
        with self._lock:
            return self._completed

    def ocr_pages_pending(self) -> list[int]:
        with self._lock:
            return [
                number for number in range(1, self.page_count + 1)
                if self.page_states.get(number) in (
                    PdfPageStatus.OCR_PENDING, PdfPageStatus.OCR_FAILED,
                )
            ]

    def coverage_ratio(self) -> float:
        with self._lock:
            if self.page_count <= 0:
                return 1.0
            return self._completed / self.page_count

    def requested_pages(self, question: str) -> list[int] | None:
        """Deterministic page-number extraction (single or range), unbounded.

        The caller caps the batch (MAX_LAZY_OCR_PAGES_PER_TURN) and adds a
        note when the user asked for more pages than that.
        """
        for pattern in _PAGE_RANGE_PATTERNS:
            match = pattern.search(question)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                if end < start:
                    start, end = end, start
                return list(range(start, min(end, self.page_count) + 1))
        for pattern in _PAGE_SINGLE_PATTERNS:
            match = pattern.search(question)
            if match:
                number = int(match.group(1))
                if 1 <= number <= self.page_count:
                    return [number]
        return None

    # ---------------------------------------------------------- mutations

    def record_ocr_results(self, results: dict[int, str]) -> int:
        """Merge OCR results; returns how many pages newly completed."""
        added = 0
        with self._lock:
            for number, text in results.items():
                if text:
                    self.ocr_cache[number] = text
                    if self.page_states.get(number) != PdfPageStatus.NATIVE:
                        self.page_states[number] = PdfPageStatus.OCR_DONE
                        if self.page_states.get(number) == PdfPageStatus.OCR_DONE:
                            added += 1
                    self.ocr_failures.discard(number)
                else:
                    self.page_states[number] = PdfPageStatus.OCR_FAILED
                    self.ocr_failures.add(number)
            self._completed = sum(
                1 for state in self.page_states.values()
                if state in (PdfPageStatus.NATIVE, PdfPageStatus.OCR_DONE)
            )
        return added

    def build_context(self) -> DocumentContext:
        """Derived retrieval context: native + OCR_DONE pages, in page order."""
        sections: list[DocumentSection] = []
        total_chars = 0
        with self._lock:
            for number in range(1, self.page_count + 1):
                text = self.native_text.get(number) or self.ocr_cache.get(number)
                if text:
                    sections.append(DocumentSection(
                        index=number, label=f"Page {number}", text=text
                    ))
                    total_chars += len(text)
        return DocumentContext(
            filename=self.display_name,
            kind="pdf",
            sections=sections,
            total_characters=total_chars,
            page_count=self.page_count,
            truncated=self.truncated,
            parse_warnings=[],
        )

    def cancel(self) -> None:
        self.cancel_event.set()

    # ---------------------------------------------------------- planning

    def pick_progressive_pages(self, question: str, max_pages: int = MAX_PROGRESSIVE_OCR_PAGES_PER_TURN) -> list[int]:
        """Pick the next OCR batch by priority: bookmarks -> representative -> next.

        Never uses an LLM intent classifier.
        """
        pending = self.ocr_pages_pending()
        if not pending:
            return []
        selected: list[int] = []
        # 1) bookmark titles that share terms with the question.
        question_terms = _question_terms(question)
        for title, page in self.outline:
            if page in pending and _title_matches(title, question_terms):
                for offset in (-1, 0, 1):
                    near = page + offset
                    if near in pending and near not in selected:
                        selected.append(near)
        # 2) representative pages of what is still pending.
        if len(selected) < max_pages and pending:
            for candidate in _representative_pages(pending):
                if candidate not in selected and len(selected) < max_pages:
                    selected.append(candidate)
        # 3) then the next sequential pending pages.
        if len(selected) < max_pages:
            for page in pending:
                if page not in selected and len(selected) < max_pages:
                    selected.append(page)
        return selected[:max_pages]

    def pick_next_batch(self, batch_size: int = PROGRESSIVE_BATCH_SIZE) -> list[int]:
        return self.ocr_pages_pending()[:batch_size]

    def pick_summary_seed_pages(self, max_pages: int = MAX_SUMMARY_SEED_OCR_PAGES) -> list[int]:
        """Representative spread (first / front / middle / back) for a summary."""
        pending = self.ocr_pages_pending()
        if not pending:
            return []
        return _representative_pages(pending)[:max_pages]


def _question_terms(question: str) -> set[str]:
    import re as _re

    terms: set[str] = set()
    for word in _re.findall(r"[A-Za-z0-9_]+", question.lower()):
        if len(word) >= 2:
            terms.add(word)
    for index in range(len(question) - 1):
        pair = question[index:index + 2]
        if "\u4e00" <= pair[0] <= "\u9fff" and "\u4e00" <= pair[1] <= "\u9fff":
            terms.add(pair)
    return terms


def _title_matches(title: str, terms: set[str]) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in terms)


def _representative_pages(pending: Sequence[int]) -> list[int]:
    ordered = sorted(pending)
    if not ordered:
        return []
    result: list[int] = []
    for index in (0, len(ordered) // 2, len(ordered) - 1):
        if ordered[index] not in result:
            result.append(ordered[index])
    return result


def is_continue_scan_request(question: str) -> bool:
    lowered = (question or "").lower()
    return any(phrase.lower() in lowered for phrase in _CONTINUE_SCAN_PHRASES)


def is_full_ocr_request(question: str) -> bool:
    lowered = (question or "").lower()
    return any(phrase.lower() in lowered for phrase in _FULL_OCR_PHRASES)


def run_ocr_batch(
    state: LazyPdfOcrState,
    pages: list[int],
    ocr_fn: Callable[[bytes, list[int]], dict[int, str]],
    source_bytes: bytes,
) -> int:
    """OCR one bounded batch; returns how many pages newly completed.

    A single page failing never kills the attachment (it is marked
    OCR_FAILED and can be retried explicitly later).
    """
    results: dict[int, str] = {}
    for page in pages:
        if state.cancel_event.is_set():
            break
        try:
            outcome = ocr_fn(source_bytes, [page])
            results.update(outcome)
        except Exception as exc:  # a page failure never kills the attachment
            log.info("lazy OCR page %d failed: %s", page, type(exc).__name__)
            results[page] = ""
    return state.record_ocr_results(results)


__all__ = [
    "LazyPdfOcrState",
    "MAX_LAZY_OCR_PAGES_PER_TURN",
    "MAX_PROGRESSIVE_OCR_PAGES_PER_TURN",
    "MAX_SUMMARY_SEED_OCR_PAGES",
    "PROGRESSIVE_BATCH_SIZE",
    "SUMMARY_COVERAGE_THRESHOLD",
    "LOW_COVERAGE_REPLY",
    "PAGE_OCR_FAILED_REPLY",
    "BATCH_LIMIT_NOTE",
    "is_continue_scan_request",
    "is_full_ocr_request",
    "run_ocr_batch",
]
