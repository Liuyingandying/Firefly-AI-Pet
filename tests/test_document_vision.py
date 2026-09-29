"""Document Vision v1 tests (A–O). All remote calls mocked; never hits the API.

Covers:
- PDF page render -> ScreenFrame (A), only requested page (B)
- PPTX render exports only the target slide (C), temp cleanup success (D) / failure (E)
- Vision receives image bytes (F) and page text (G)
- No PDF bytes to the provider (H), no screen capture (I), no Memory/history (J)
- remote_calls == 1 (K), reasoning_calls == 0 (L)
- wrong page rejected safely (M), lazy-PDF visual request does not trigger OCR (N)
- normal document QA unchanged (O)
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType, ErrorCategory
from core.document_attachment import parse_document_bytes
from core.document_vision import (
    DOCUMENT_VISION_STYLE_CONTEXT,
    DocumentVisionRenderError,
    DocumentVisionRenderer,
    PageOutOfRangeError,
    build_document_vision_question,
    is_document_vision_request,
    resolve_document_location,
)
from core.lazy_pdf_ocr import LazyPdfOcrState
from core.pdf_processor import build_pdf_lazy_index
from core.screen_vision.models import ScreenFrame
from ui.character_conversation_runner import CharacterConversationRunner
from ui.companion_attachment import DocumentAttachment

# ---------------------------------------------------------------- helpers


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _fake_frame() -> ScreenFrame:
    return ScreenFrame(
        width=100,
        height=100,
        mime_type="image/jpeg",
        image_bytes=b"\xff\xd8\xff\xe0fake-jpeg",
        captured_at=datetime.now(),
    )


def _tiny_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _build_pdf_bytes(pages: int = 3) -> bytes:
    doc = pymupdf.open()
    for i in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"PDF page {i} content", fontsize=16)
    data = doc.tobytes()
    doc.close()
    return data


def _build_pptx_bytes(slides: int = 3) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for i in range(1, slides + 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = f"PPTX slide {i} content"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _pdf_attachment() -> tuple[DocumentAttachment, bytes]:
    data = _build_pdf_bytes()
    context = parse_document_bytes(data, "pdf", "paper.pdf")
    attachment = DocumentAttachment(
        display_name="paper.pdf",
        kind="pdf",
        original_size=len(data),
        source_bytes=data,
    )
    attachment.retain_source_bytes()
    attachment.mark_ready(context)
    return attachment, data


def _pptx_attachment() -> tuple[DocumentAttachment, bytes]:
    data = _build_pptx_bytes()
    context = parse_document_bytes(data, "pptx", "deck.pptx")
    attachment = DocumentAttachment(
        display_name="deck.pptx",
        kind="pptx",
        original_size=len(data),
        source_bytes=data,
    )
    attachment.retain_source_bytes()
    attachment.mark_ready(context)
    return attachment, data


class FakeVisionProvider:
    """Records answer_direct calls; returns a canned visual answer."""

    name = "fake-vision"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[tuple[ScreenFrame, str, str | None]] = []

    def answer_direct(self, frame, question, style_context=None) -> str:
        self.calls.append((frame, question, style_context))
        return "这是第 2 页的实验结果图，横轴是训练轮数，纵轴是准确率。"


class FakeRenderer:
    """Records which page/slide indices were requested."""

    def __init__(self) -> None:
        self.pdf_pages: list[int] = []
        self.pptx_slides: list[int] = []

    def render_pdf_page(self, source: bytes, page_index: int) -> ScreenFrame:
        self.pdf_pages.append(page_index)
        return _fake_frame()

    def render_pptx_slide(self, source: bytes, slide_index: int) -> ScreenFrame:
        self.pptx_slides.append(slide_index)
        return _fake_frame()


class ChatSpy:
    """Text document-QA handler; raises if the vision path touches it."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, temperature: float = 0.2) -> dict[str, Any]:
        self.calls.append(messages)
        return {
            "choices": [{"message": {"role": "assistant", "content": "文档文本回答"}}],
        }


class ScreenSpy:
    """Fails loudly if any screen-vision capture is attempted."""

    def __init__(self) -> None:
        self.look_calls = 0

    def look(self, *args, **kwargs) -> Any:
        self.look_calls += 1
        raise AssertionError("screen vision must not be called in a document vision turn")


def _vision_runner(
    attachment: DocumentAttachment | None = None,
) -> tuple[CharacterConversationRunner, FakeVisionProvider, FakeRenderer, ChatSpy]:
    _app()
    provider = FakeVisionProvider()
    renderer = FakeRenderer()
    chat = ChatSpy()
    runner = CharacterConversationRunner(
        attachment_vision_provider=provider,
        document_chat_handler=chat.chat,
        document_vision_renderer=renderer,
        screen_vision_service=ScreenSpy(),
    )
    return runner, provider, renderer, chat


# ---------------------------------------------------------------- A–E: renderer


def test_a_pdf_page_render_returns_screenframe() -> None:
    data = _build_pdf_bytes()
    frame = DocumentVisionRenderer().render_pdf_page(data, 2)

    assert isinstance(frame, ScreenFrame)
    assert frame.mime_type == "image/jpeg"
    assert frame.image_bytes[:2] == b"\xff\xd8"  # valid JPEG
    assert frame.width > 0 and frame.height > 0
    assert max(frame.width, frame.height) <= 1600


def test_b_pdf_renders_only_requested_page(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[int] = []

    class FakePage:
        def __init__(self, index: int) -> None:
            self.index = index
            self.rect = pymupdf.Rect(0, 0, 595, 842)

        def get_pixmap(self, dpi=None, colorspace=None) -> SimpleNamespace:
            rendered.append(self.index)
            return SimpleNamespace(
                width=100, height=100, samples=b"\x00" * (100 * 100 * 3)
            )

    class FakeDoc:
        page_count = 3

        def __init__(self) -> None:
            self._pages = [FakePage(i) for i in range(3)]

        def __getitem__(self, index: int) -> FakePage:
            return self._pages[index]

        def close(self) -> None:
            pass

    monkeypatch.setattr(pymupdf, "open", lambda stream=None, filetype=None: FakeDoc())

    renderer = DocumentVisionRenderer()
    monkeypatch.setattr(renderer, "_to_screen_frame", lambda image: _fake_frame())
    renderer.render_pdf_page(b"%PDF-1.4", 2)

    # Only the requested page is rendered: 0-based index 1 == 1-based page 2.
    assert rendered == [1]


def _fake_powerpoint(monkeypatch: pytest.MonkeyPatch, exported: list[int],
                     closed: list[str], fail_export: bool = False) -> None:
    """Install a fake PowerPoint COM app; only the target slide is exported."""

    class FakeSlide:
        def __init__(self, index: int) -> None:
            self.index = index

        def Export(self, path: str, fmt: str, w: int, h: int) -> None:
            if fail_export:
                raise RuntimeError("export exploded")
            exported.append(self.index)
            Path(path).write_bytes(_tiny_png_bytes())

    class FakeSlides:
        Count = 3

        def __init__(self) -> None:
            self._slides = [FakeSlide(i) for i in range(1, 4)]

        def Item(self, index: int) -> FakeSlide:
            return self._slides[index - 1]

    class FakePresentation:
        Slides = FakeSlides()

        class PageSetup:
            SlideWidth = 960.0
            SlideHeight = 540.0

        def Close(self) -> None:
            closed.append("presentation")

    class FakeApp:
        Presentations = SimpleNamespace(
            Open=lambda *a, **k: FakePresentation()
        )

        def Quit(self) -> None:
            closed.append("app")

    import win32com.client

    monkeypatch.setattr(win32com.client, "DispatchEx", lambda *a, **k: FakeApp())


def test_c_pptx_exports_only_target_slide(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: list[int] = []
    closed: list[str] = []
    _fake_powerpoint(monkeypatch, exported, closed)

    renderer = DocumentVisionRenderer()
    monkeypatch.setattr(renderer, "_to_screen_frame", lambda image: _fake_frame())
    frame = renderer.render_pptx_slide(b"pptx-bytes", 2)

    assert exported == [2]  # only the target slide
    assert frame.mime_type == "image/jpeg"
    assert closed == ["presentation", "app"]


def test_d_pptx_temp_cleanup_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    exported: list[int] = []
    closed: list[str] = []
    created: list[str] = []
    cleaned: list[str] = []
    real_td = tempfile.TemporaryDirectory

    class SpyTD:
        def __init__(self, prefix: str | None = None) -> None:
            self._real = real_td(prefix=prefix)
            created.append(self._real.name)

        @property
        def name(self) -> str:
            return self._real.name

        def cleanup(self) -> None:
            cleaned.append(self._real.name)
            self._real.cleanup()

    monkeypatch.setattr(tempfile, "TemporaryDirectory", SpyTD)
    _fake_powerpoint(monkeypatch, exported, closed)

    renderer = DocumentVisionRenderer()
    monkeypatch.setattr(renderer, "_to_screen_frame", lambda image: _fake_frame())
    renderer.render_pptx_slide(b"pptx-bytes", 1)

    assert created and cleaned == created
    assert not Path(created[0]).exists()  # temp dir really removed


def test_e_pptx_temp_cleanup_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    exported: list[int] = []
    closed: list[str] = []
    created: list[str] = []
    cleaned: list[str] = []
    real_td = tempfile.TemporaryDirectory

    class SpyTD:
        def __init__(self, prefix: str | None = None) -> None:
            self._real = real_td(prefix=prefix)
            created.append(self._real.name)

        @property
        def name(self) -> str:
            return self._real.name

        def cleanup(self) -> None:
            cleaned.append(self._real.name)
            self._real.cleanup()

    monkeypatch.setattr(tempfile, "TemporaryDirectory", SpyTD)
    _fake_powerpoint(monkeypatch, exported, closed, fail_export=True)

    renderer = DocumentVisionRenderer()
    monkeypatch.setattr(renderer, "_to_screen_frame", lambda image: _fake_frame())
    with pytest.raises(DocumentVisionRenderError):
        renderer.render_pptx_slide(b"pptx-bytes", 1)

    assert created and cleaned == created  # cleanup ran despite the failure
    assert not Path(created[0]).exists()


# ---------------------------------------------------------------- F–O: runner


def test_f_vision_receives_image_bytes() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    events, text = runner.perform_with_document_vision(
        "看看第2页这张图是什么意思？", attachment
    )

    assert events[-1].type == AgentEventType.FINAL
    assert text
    assert len(provider.calls) == 1
    frame = provider.calls[0][0]
    assert isinstance(frame, ScreenFrame)
    assert frame.image_bytes
    assert frame.mime_type == "image/jpeg"


def test_g_vision_receives_page_text() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    _frame, question, style = provider.calls[0]
    assert "[Page 2 extracted text]" in question
    assert "PDF page 2 content" in question  # the page's own text
    assert "PDF page 1 content" not in question  # never other pages
    assert style == DOCUMENT_VISION_STYLE_CONTEXT


def test_h_no_pdf_bytes_sent_to_provider() -> None:
    attachment, pdf_bytes = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    frame, question, _ = provider.calls[0]
    assert frame.image_bytes[:2] == b"\xff\xd8"
    assert b"%PDF" not in frame.image_bytes
    assert b"%PDF" not in question.encode()
    assert pdf_bytes not in frame.image_bytes


def test_i_no_screen_vision_capture() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    assert runner._screen_vision_service.look_calls == 0
    assert provider.calls


def test_j_no_memory_or_history_payload() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, chat = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    assert chat.calls == []  # text QA / Memory path never touched
    history = runner._history
    assert any("paper.pdf" in item["content"] for item in history)
    assert not any("PDF page 2 content" in item["content"] for item in history)
    assert not any(b"%PDF" in item["content"].encode() for item in history)


def test_k_remote_calls_is_one() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    assert len(provider.calls) == 1
    assert runner.last_document_vision_meta["remote_calls"] == 1
    assert runner.last_document_vision_meta["vision_calls"] == 1


def test_l_reasoning_calls_is_zero() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, _, _ = _vision_runner()

    runner.perform_with_document_vision("看看第2页这张图是什么意思？", attachment)

    assert runner.last_document_vision_meta["reasoning_calls"] == 0
    assert runner.last_document_vision_meta["screen_capture_calls"] == 0


def test_m_wrong_page_request_rejected_safely() -> None:
    attachment, _ = _pdf_attachment()  # 3 pages
    runner, provider, renderer, _ = _vision_runner()

    events, text = runner.perform_with_document_vision(
        "看看第99页这张图是什么意思？", attachment
    )
    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error_code == ErrorCategory.PROTOCOL
    assert provider.calls == []
    assert renderer.pdf_pages == []

    events, _ = runner.perform_with_document_vision(
        "看看第0页这张图是什么意思？", attachment
    )
    assert events[-1].type == AgentEventType.ERROR
    assert provider.calls == []


def test_n_lazy_pdf_visual_request_does_not_trigger_ocr() -> None:
    data = _build_pdf_bytes()
    index = build_pdf_lazy_index(data, "scan.pdf")
    attachment = DocumentAttachment(
        display_name="scan.pdf", kind="pdf", original_size=len(data), source_bytes=data
    )
    state = LazyPdfOcrState(index, generation_id="g-test")
    attachment.attach_lazy_state(state)
    attachment.mark_lazy_ready(state.build_context())

    ocr_calls: list[Any] = []

    def ocr_fn(*args, **kwargs) -> Any:
        ocr_calls.append((args, kwargs))
        return []

    attachment.set_lazy_ocr_fn(ocr_fn)

    runner, provider, renderer, _ = _vision_runner()
    events, text = runner.perform_with_document_vision(
        "看看第2页这张图是什么意思？", attachment
    )

    assert events[-1].type == AgentEventType.FINAL
    assert ocr_calls == []  # visual request renders directly, never OCRs
    assert renderer.pdf_pages == [2]
    assert provider.calls


def test_o_normal_document_qa_unchanged() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, renderer, chat = _vision_runner()

    # Non-visual question through the vision entry falls through to text QA.
    events, text = runner.perform_with_document_vision("第2页讲了什么？", attachment)
    assert events[-1].type == AgentEventType.FINAL
    assert len(chat.calls) == 1
    assert provider.calls == []
    assert renderer.pdf_pages == []

    # The original document QA entry still works untouched.
    events, text = runner.perform_with_document("第2页讲了什么？", attachment)
    assert events[-1].type == AgentEventType.FINAL
    assert len(chat.calls) == 2
    assert provider.calls == []


# ---------------------------------------------------------------- trigger semantics


def test_trigger_semantics() -> None:
    assert is_document_vision_request("看看第6页这张图是什么意思？")
    assert is_document_vision_request("第3页的曲线图说明什么？")
    assert is_document_vision_request("第4页的流程图解释一下")
    assert not is_document_vision_request("第6页讲了什么？")
    assert not is_document_vision_request("总结一下这个文档")
    assert not is_document_vision_request("")


def test_location_resolution_reuses_existing_regexes() -> None:
    pdf = SimpleNamespace(kind="pdf")
    pptx = SimpleNamespace(kind="pptx")

    assert resolve_document_location("看看第6页这张图", pdf) == 6
    assert resolve_document_location("what does page 3 show", pdf) == 3
    assert resolve_document_location("slide 2 的图", pptx) == 2
    assert resolve_document_location("第2页的图", pptx) == 2  # 第N页 maps to Slide N
    assert resolve_document_location("这张图是什么意思", pdf) is None
    assert resolve_document_location("", pdf) is None


def test_unresolved_location_returns_friendly_reply() -> None:
    attachment, _ = _pdf_attachment()
    runner, provider, renderer, _ = _vision_runner()

    events, text = runner.perform_with_document_vision("这张图是什么意思？", attachment)

    assert events[-1].type == AgentEventType.ERROR
    assert "页码" in events[-1].text
    assert provider.calls == []
    assert renderer.pdf_pages == []


def test_pptx_vision_turn_renders_slide_and_answers() -> None:
    attachment, _ = _pptx_attachment()
    runner, provider, renderer, _ = _vision_runner()

    events, text = runner.perform_with_document_vision(
        "看看第2页这个图表达什么？", attachment
    )

    assert events[-1].type == AgentEventType.FINAL
    assert renderer.pptx_slides == [2]
    assert provider.calls
    _frame, question, _ = provider.calls[0]
    assert "[Slide 2 extracted text]" in question
    assert "PPTX slide 2 content" in question
    assert runner.last_document_vision_meta["kind"] == "pptx"
