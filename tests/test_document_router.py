"""Document Agent Router v1 tests (A–J + runner integration).

All routing is pure rules — no model calls, no file bytes, no side effects.
Runner integration tests mock the vision provider / renderer / chat handler.
"""

from __future__ import annotations

import inspect
import os
from datetime import datetime
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType
from core.document_attachment import DocumentContext, DocumentSection
from core.document_router import (
    DocumentAction,
    route_attachment_question,
    route_document_question,
)
from core.lazy_pdf_ocr import LazyPdfOcrState
from core.pdf_processor import LazyPdfIndex, PdfPageStatus
from core.screen_vision.models import ScreenFrame
from ui.character_conversation_runner import CharacterConversationRunner
from ui.companion_attachment import DocumentAttachment


# ---------------------------------------------------------------- helpers


def _ctx(kind: str = "pdf", pages: int = 5) -> DocumentContext:
    """A plain in-memory DocumentContext; no bytes anywhere."""
    labels = [f"Slide {i}" if kind == "pptx" else f"Page {i}" for i in range(1, pages + 1)]
    sections = [
        DocumentSection(index=i, label=labels[i - 1], text=f"Content {i}")
        for i in range(1, pages + 1)
    ]
    return DocumentContext(
        filename=f"doc.{kind}",
        kind=kind,
        sections=sections,
        page_count=None if kind == "pptx" else pages,
        slide_count=pages if kind == "pptx" else None,
    )


def _lazy_state(
    page_states: dict[int, str] | None = None, page_count: int = 3
) -> LazyPdfOcrState:
    states = page_states or {
        1: PdfPageStatus.NATIVE,
        2: PdfPageStatus.OCR_PENDING,
        3: PdfPageStatus.OCR_PENDING,
    }
    native = {
        p: f"Native {p}"
        for p, s in states.items()
        if s == PdfPageStatus.NATIVE
    }
    index = LazyPdfIndex(
        display_name="scan.pdf",
        page_count=page_count,
        page_states=states,
        native_text=native,
        outline=[],
        truncated=False,
    )
    return LazyPdfOcrState(index, generation_id="g-test")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _native_attachment() -> DocumentAttachment:
    """A ready native-PDF attachment (bytes retained, context parsed)."""
    import pymupdf

    from core.document_attachment import parse_document_bytes

    doc = pymupdf.open()
    for i in range(1, 4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"PDF page {i} content", fontsize=16)
    data = doc.tobytes()
    doc.close()
    context = parse_document_bytes(data, "pdf", "paper.pdf")
    attachment = DocumentAttachment(
        display_name="paper.pdf",
        kind="pdf",
        original_size=len(data),
        source_bytes=data,
    )
    attachment.retain_source_bytes()
    attachment.mark_ready(context)
    return attachment


def _scanned_attachment() -> DocumentAttachment:
    """A ready lazy scanned-PDF attachment with page 2 pending OCR."""
    states = {
        1: PdfPageStatus.NATIVE,
        2: PdfPageStatus.OCR_PENDING,
        3: PdfPageStatus.OCR_PENDING,
    }
    index = LazyPdfIndex(
        display_name="scan.pdf",
        page_count=3,
        page_states=states,
        native_text={1: "Native page one"},
        outline=[],
        truncated=False,
    )
    state = LazyPdfOcrState(index, generation_id="g-test")
    attachment = DocumentAttachment(
        display_name="scan.pdf",
        kind="pdf",
        original_size=1024,
        source_bytes=b"%PDF-1.4 fake bytes",
    )
    attachment.attach_lazy_state(state)
    attachment.mark_lazy_ready(state.build_context())

    def fake_ocr(source: bytes, pages: list[int]) -> dict[int, str]:
        return {p: f"OCR text for page {p}" for p in pages}

    attachment.set_lazy_ocr_fn(fake_ocr)
    return attachment


class FakeVisionProvider:
    name = "fake-vision"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[tuple[ScreenFrame, str, str | None]] = []

    def answer_direct(self, frame, question, style_context=None) -> str:
        self.calls.append((frame, question, style_context))
        return "这是一张架构流程图，描述了系统处理请求的完整路径。"


class FakeRenderer:
    def __init__(self) -> None:
        self.pdf_pages: list[int] = []
        self.pptx_slides: list[int] = []

    def render_pdf_page(self, source: bytes, page_index: int) -> ScreenFrame:
        self.pdf_pages.append(page_index)
        return ScreenFrame(
            width=100, height=100, mime_type="image/jpeg",
            image_bytes=b"\xff\xd8\xff\xe0fake", captured_at=datetime.now(),
        )

    def render_pptx_slide(self, source: bytes, slide_index: int) -> ScreenFrame:
        self.pptx_slides.append(slide_index)
        return ScreenFrame(
            width=100, height=100, mime_type="image/jpeg",
            image_bytes=b"\xff\xd8\xff\xe0fake", captured_at=datetime.now(),
        )


class ChatSpy:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, temperature: float = 0.2) -> dict[str, Any]:
        self.calls.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "文档文本回答"}}]}


def _runner():
    _app()
    provider = FakeVisionProvider()
    renderer = FakeRenderer()
    chat = ChatSpy()
    runner = CharacterConversationRunner(
        attachment_vision_provider=provider,
        document_chat_handler=chat.chat,
        document_vision_renderer=renderer,
    )
    return runner, provider, renderer, chat


# ---------------------------------------------------------------- A–J router rules


def test_a_summary_question_routes_to_summary() -> None:
    for question in ("总结一下论文", "概括这篇文档", "主要内容是什么", "帮我摘要一下"):
        route = route_document_question(question, _ctx())
        assert route.action == DocumentAction.SUMMARY, question


def test_b_page_question_routes_to_page_lookup() -> None:
    assert route_document_question("第5页讲什么", _ctx()).action == DocumentAction.PAGE_LOOKUP
    assert route_document_question("page 5 讲了什么", _ctx()).action == DocumentAction.PAGE_LOOKUP
    assert route_document_question("第2页讲了什么", _ctx(kind="pptx")).action == (
        DocumentAction.PAGE_LOOKUP
    )


def test_c_visual_page_question_routes_to_document_vision() -> None:
    for question in ("第5页这张图什么意思", "看看第5页的曲线", "第2页的流程图解释一下"):
        route = route_document_question(question, _ctx())
        assert route.action == DocumentAction.DOCUMENT_VISION, question


def test_d_apparatus_question_routes_to_document_vision() -> None:
    for question in ("实验装置怎么工作", "这个装置的结构是什么", "第3页的设备布局怎么样"):
        route = route_document_question(question, _ctx())
        assert route.action == DocumentAction.DOCUMENT_VISION, question


def test_e_scanned_pending_page_routes_to_ocr_page() -> None:
    lazy = _lazy_state()  # page 2 OCR_PENDING
    route = route_document_question("第2页讲了什么", _ctx(), lazy_state=lazy)
    assert route.action == DocumentAction.OCR_PAGE

    route = route_document_question("继续扫描", _ctx(), lazy_state=lazy)
    assert route.action == DocumentAction.OCR_PAGE

    route = route_document_question("全部识别", _ctx(), lazy_state=lazy)
    assert route.action == DocumentAction.OCR_PAGE


def test_f_ordinary_question_routes_to_text_qa() -> None:
    for question in ("这篇文档的背景是什么", "文档提到了哪些方法", "作者的结论是什么"):
        route = route_document_question(question, _ctx())
        assert route.action == DocumentAction.TEXT_QA, question


def test_g_conflict_summary_plus_visual_wins_vision() -> None:
    route = route_document_question("总结第五页这张图", _ctx())
    assert route.action == DocumentAction.DOCUMENT_VISION


def test_h_router_makes_no_model_calls() -> None:
    source = inspect.getsource(__import__("core.document_router", fromlist=["x"]))
    # No provider / HTTP surface is even reachable from the router module.
    for forbidden in ("requests", "ai_router", "deepseek_vision", "http"):
        assert forbidden not in source, forbidden
    # And routing runs to completion with every input above (conftest blocks
    # any accidental external HTTP, so a model call would fail loudly).
    for question in ("总结一下论文", "第5页讲什么", "实验装置怎么工作", "继续扫描"):
        route_document_question(question, _ctx(), lazy_state=_lazy_state())


def test_i_router_never_touches_file_bytes() -> None:
    params = inspect.signature(route_document_question).parameters
    assert set(params) == {"question", "context", "lazy_state"}

    class HostileAttachment:
        def take_source_bytes(self) -> bytes:
            raise AssertionError("router must never access file bytes")

        context = _ctx()
        lazy_state = None

    route = route_attachment_question(HostileAttachment(), "第5页这张图什么意思")
    assert route.action == DocumentAction.DOCUMENT_VISION


def test_j_router_has_no_side_effects() -> None:
    lazy = _lazy_state()
    before_states = dict(lazy.page_states)
    before_cache = dict(lazy.ocr_cache)
    first = route_document_question("第2页讲了什么", _ctx(), lazy_state=lazy)
    second = route_document_question("第2页讲了什么", _ctx(), lazy_state=lazy)
    assert first == second
    assert dict(lazy.page_states) == before_states
    assert dict(lazy.ocr_cache) == before_cache


# ---------------------------------------------------------------- conflict & edge rules


def test_text_intent_beats_weak_visual_words() -> None:
    assert route_document_question("第5页文字讲什么", _ctx()).action == (
        DocumentAction.PAGE_LOOKUP
    )


def test_no_text_claim_routes_to_vision() -> None:
    assert route_document_question("第五页没有文字", _ctx()).action == (
        DocumentAction.DOCUMENT_VISION
    )


def test_scanned_done_page_routes_to_page_lookup() -> None:
    states = {1: PdfPageStatus.OCR_DONE, 2: PdfPageStatus.OCR_DONE}
    lazy = _lazy_state(states)
    route = route_document_question("第2页讲了什么", _ctx(), lazy_state=lazy)
    assert route.action == DocumentAction.PAGE_LOOKUP


def test_non_renderable_kind_never_routes_vision() -> None:
    # docx: visual words exist but the kind cannot render -> text paths only.
    route = route_document_question("第2页的图什么意思", _ctx(kind="docx"))
    assert route.action != DocumentAction.DOCUMENT_VISION
    assert route.action in (DocumentAction.PAGE_LOOKUP, DocumentAction.TEXT_QA)


def test_out_of_range_page_still_routes_page_lookup() -> None:
    route = route_document_question("第99页讲了什么", _ctx(pages=5))
    assert route.action == DocumentAction.PAGE_LOOKUP


def test_pptx_slide_flowchart_routes_vision() -> None:
    route = route_document_question("slide 3 的流程图解释一下", _ctx(kind="pptx"))
    assert route.action == DocumentAction.DOCUMENT_VISION


# ---------------------------------------------------------------- runner integration


def test_runner_perform_with_document_routes_to_vision() -> None:
    attachment = _native_attachment()
    runner, provider, renderer, chat = _runner()

    events, text = runner.perform_with_document("第2页这张图什么意思", attachment)

    assert events[-1].type == AgentEventType.FINAL
    assert renderer.pdf_pages == [2]
    assert len(provider.calls) == 1
    assert chat.calls == []
    assert runner.last_document_vision_meta["document_action"] == "document_vision"


def test_runner_vision_entry_routes_non_visual_to_qa() -> None:
    attachment = _native_attachment()
    runner, provider, renderer, chat = _runner()

    events, text = runner.perform_with_document_vision("第2页讲了什么", attachment)

    assert events[-1].type == AgentEventType.FINAL
    assert len(chat.calls) == 1
    assert provider.calls == []
    assert renderer.pdf_pages == []
    assert runner.last_document_meta["document_action"] == "page_lookup"


def test_runner_normal_qa_meta_reports_text_qa() -> None:
    attachment = _native_attachment()
    runner, provider, renderer, chat = _runner()

    events, text = runner.perform_with_document("文档的背景是什么", attachment)

    assert events[-1].type == AgentEventType.FINAL
    assert len(chat.calls) == 1
    assert provider.calls == []
    assert runner.last_document_meta["document_action"] == "text_qa"


def test_runner_scanned_continue_scan_meta_reports_ocr_page() -> None:
    attachment = _scanned_attachment()
    runner, provider, renderer, chat = _runner()

    events, text = runner.perform_with_document("继续扫描", attachment)

    assert events[-1].type == AgentEventType.FINAL
    assert runner.last_document_meta["document_action"] == "ocr_page"
    assert provider.calls == []
    assert renderer.pdf_pages == []
