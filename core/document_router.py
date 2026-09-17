"""Document Agent Router v1 — rule-based action selection for document turns.

Picks one of the five existing document handlers for a question without any
model call: TEXT_QA / PAGE_LOOKUP / DOCUMENT_VISION / OCR_PAGE / SUMMARY.

Pure-function module: inputs are plain data (question text, the parsed
``DocumentContext``, an optional ``LazyPdfOcrState``). The router never
receives a DocumentAttachment, never touches file bytes, never mutates state
and never performs I/O — it only reads public, side-effect-free accessors.

Conflict resolution is fixed by the cascade order below (visual intent beats
summary, explicit no-text claims beat page lookup, scan state decides between
OCR and lookup). No LLM judgement is involved anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.document_attachment import (
    DocumentContext,
    direct_section_lookup,
    is_summary_request,
)
from core.document_vision import (
    is_document_vision_request,
    resolve_document_location,
)
from core.lazy_pdf_ocr import is_continue_scan_request, is_full_ocr_request
from core.pdf_processor import PdfPageStatus

# Extended visual markers beyond core.document_vision's frozen trigger list:
# apparatus / structure words that imply a figure question even without 图.
_EXTENDED_VISUAL_MARKERS = ("装置", "结构", "布局", "怎么工作")

# Formula / math words: a rendered page image is the only reliable source for
# these. They are gated on a resolved page (see rule 1b below) so a page-free
# "这个公式在讲什么" still falls through to ordinary text QA instead of
# erroring on an unresolved render target.
_FORMULA_MARKERS = ("公式", "方程", "推导", "算式", "怎么解", "如何解", "证明过程", "这个式子")

# The user says the page has no readable text -> only the rendered image helps.
_NO_TEXT_CLAIM_MARKERS = (
    "没有文字", "没文字", "没有文本", "没有字", "识别不出", "扫不出",
)

# Kinds the Document Vision renderer can actually turn into a page image.
_RENDERABLE_KINDS = frozenset({"pdf", "pptx"})

# Page states that still need OCR before their text can answer anything.
_TEXT_AVAILABLE_STATES = (PdfPageStatus.NATIVE, PdfPageStatus.OCR_DONE)


class DocumentAction(str, Enum):
    TEXT_QA = "text_qa"
    PAGE_LOOKUP = "page_lookup"
    DOCUMENT_VISION = "document_vision"
    OCR_PAGE = "ocr_page"
    SUMMARY = "summary"


@dataclass(frozen=True)
class DocumentRoute:
    """One routing decision plus the rule that produced it (debug only)."""

    action: DocumentAction
    reason: str


def _is_visual_question(question: str) -> bool:
    lowered = (question or "").strip().lower()
    return is_document_vision_request(lowered) or any(
        marker in lowered for marker in _EXTENDED_VISUAL_MARKERS
    )


def _is_formula_question(question: str) -> bool:
    lowered = (question or "").strip().lower()
    return any(marker in lowered for marker in _FORMULA_MARKERS)


def _claims_no_text(question: str) -> bool:
    lowered = (question or "").strip().lower()
    return any(marker in lowered for marker in _NO_TEXT_CLAIM_MARKERS)


def _page_section_text(context: Any, kind: str, number: int) -> str:
    """The parsed text of one page/slide section (empty when missing)."""
    label = f"Slide {number}" if kind == "pptx" else f"Page {number}"
    for section in getattr(context, "sections", ()) or ():
        if getattr(section, "label", None) == label:
            return (getattr(section, "text", "") or "").strip()
    return ""


def _is_renderable(context: Any) -> bool:
    return (getattr(context, "kind", "") or "") in _RENDERABLE_KINDS


def route_document_question(
    question: str,
    context: Any = None,
    lazy_state: Any = None,
) -> DocumentRoute:
    """Route one document question to an action. Rules only, no model call.

    Priority cascade (first match wins):
      1. renderable + visual keyword           -> DOCUMENT_VISION
      2. renderable + explicit no-text claim   -> DOCUMENT_VISION
      3. scanned  + full-OCR / continue-scan   -> OCR_PAGE
      4. renderable + page + empty page text   -> DOCUMENT_VISION
      5. scanned  + page + target pending      -> OCR_PAGE
      6. direct section/sheet lookup hit       -> PAGE_LOOKUP
      7. summary intent (page-free)            -> SUMMARY
      8. page mentioned (even a lookup miss)   -> PAGE_LOOKUP
      9. otherwise                             -> TEXT_QA
    """
    text = (question or "").strip()
    kind = (getattr(context, "kind", "") or "") if context is not None else ""
    renderable = kind in _RENDERABLE_KINDS

    # 1. Explicit visual intent wins over everything (总结第五页这张图 -> vision).
    if renderable and _is_visual_question(text):
        return DocumentRoute(
            DocumentAction.DOCUMENT_VISION,
            "visual_trigger" if resolve_document_location(text, context) is None
            else "visual_trigger+page",
        )

    # 1b. Formula / equation questions anchor to the page image when the user
    #     pointed at a page (公式页的文字抽取经常失真，图像才可靠)。Page-free
    #     formula questions fall through to ordinary text QA below.
    if renderable and _is_formula_question(text):
        formula_location = (
            resolve_document_location(text, context) if context is not None else None
        )
        if formula_location is not None:
            return DocumentRoute(
                DocumentAction.DOCUMENT_VISION, "formula_trigger+page"
            )

    # 2. "第五页没有文字": text lookup is declared useless, show the page image.
    if renderable and _claims_no_text(text):
        return DocumentRoute(DocumentAction.DOCUMENT_VISION, "no_text_claim")

    # 3. Scan-session verbs stay with the lazy OCR handler.
    if lazy_state is not None and (
        is_full_ocr_request(text) or is_continue_scan_request(text)
    ):
        return DocumentRoute(DocumentAction.OCR_PAGE, "scan_session_request")

    location = (
        resolve_document_location(text, context) if context is not None else None
    )

    if location is not None:
        total = (
            getattr(context, "slide_count" if kind == "pptx" else "page_count", None)
            if context is not None else None
        )
        in_bounds = total is None or 1 <= location <= total
        # 4. A renderable page whose parsed text is empty: only the image helps.
        if (
            renderable
            and lazy_state is None
            and in_bounds
            and not _page_section_text(context, kind, location)
        ):
            return DocumentRoute(DocumentAction.DOCUMENT_VISION, "empty_page_text")
        # 5. Scanned PDF with pending target page: OCR it through the lazy path.
        if (
            lazy_state is not None
            and in_bounds
            and lazy_state.page_state(location) not in (_TEXT_AVAILABLE_STATES)
        ):
            return DocumentRoute(DocumentAction.OCR_PAGE, "scan_page_pending")

    # 6. Existing deterministic page/slide/sheet lookup (incl. xlsx sheets).
    if context is not None and direct_section_lookup(text, context):
        return DocumentRoute(DocumentAction.PAGE_LOOKUP, "section_lookup")

    # 7. Explicit summary intent (is_summary_request already excludes pages).
    if is_summary_request(text):
        return DocumentRoute(DocumentAction.SUMMARY, "summary_request")

    # 8. A page was mentioned but nothing matched above (e.g. out of range):
    #    still a lookup question; the QA path degrades to retrieval safely.
    if location is not None:
        return DocumentRoute(DocumentAction.PAGE_LOOKUP, "page_mentioned")

    # 9. Everything else: ordinary text QA (lazy handler may still OCR).
    return DocumentRoute(DocumentAction.TEXT_QA, "default")


def route_attachment_question(attachment: Any, question: str) -> DocumentRoute:
    """Convenience wrapper: read-only snapshot of an attachment's state.

    Only the parsed context and the lazy OCR state are read; source bytes are
    never touched.
    """
    return route_document_question(
        question,
        getattr(attachment, "context", None),
        lazy_state=getattr(attachment, "lazy_state", None),
    )


__all__ = [
    "DocumentAction",
    "DocumentRoute",
    "route_attachment_question",
    "route_document_question",
]
