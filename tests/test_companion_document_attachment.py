"""Companion Document Attachment v1 tests.

Documents (PDF / DOCX / PPTX / TXT / MD / XLSX / CSV) are parsed LOCALLY into
a :class:`DocumentContext`; only question-relevant excerpts reach the text
provider. Documents are never uploaded as raw files, never enter Memory /
Bond / suggestion, never trigger screen capture or vision, and never persist
bytes / base64 / absolute paths in history. After a successful turn the
document attachment stays for follow-up questions.

Coverage map (A-AO from the task):
  A-E drag acceptance   F legacy rejection   G picker filter   H chip ready
  I document retained after success          J x clears context
  K/L PDF native + page markers   M scanned->OCR route   N encrypted/corrupt
  O/P/Q DOCX paragraphs/order/tables   R/S/T PPTX slides/text/tables
  U/V XLSX/CSV extraction + truncation
  W/Z retrieval   X/Y page & slide direct lookup
  AA-AF privacy isolation   AG-AK interaction   AL-AM summaries   AN/AO grounding
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from core.document_attachment import (
    CHUNK_SIZE,
    MAX_CSV_ROWS,
    MAX_PDF_PAGES,
    MAX_XLSX_SHEET_ROWS,
    SUMMARY_DIRECT_CHAR_BUDGET,
    DocumentChunk,
    DocumentContext,
    DocumentParseError,
    DocumentSection,
    EncryptedPdfError,
    build_qa_messages,
    chunk_document,
    direct_section_lookup,
    format_excerpts,
    is_summary_request,
    parse_document_bytes,
    retrieve_chunks,
)
from core.screen_vision.models import ScreenObservation, ScreenVisionResult
from core.screen_vision.provider_errors import EmptyProviderResponse
from ui import companion_chat_window as window_mod
from ui.companion_attachment import (
    LEGACY_DOCUMENT_MESSAGE,
    AttachmentImage,
    DocumentAttachment,
    load_document_file,
)
from ui.companion_chat_window import CompanionChatWindow
from ui.character_conversation_runner import CharacterConversationRunner


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- fakes


class FakeTextChat:
    """Records text-provider calls; no network involved."""

    def __init__(self, answer="根据文档内容，答案是……", error=None):
        self.calls = []
        self.answer = answer
        self.error = error

    def __call__(self, messages, temperature=0.2):
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}


class FakeVision:
    """Records answer_direct calls (must stay untouched by document turns)."""

    def __init__(self):
        self.calls = []

    def answer_direct(self, frame, question, style_context=None):
        self.calls.append(question)
        return "图片回答"


class FakeRuntime:
    def __init__(self):
        self.chat_calls = []

    def chat(self, prompt, history=None, turn_context=None):
        self.chat_calls.append(prompt)
        return {"choices": [{"message": {"role": "assistant", "content": "普通回复"}}]}


class FakeScreenVisionService:
    def __init__(self):
        self.look_calls = []

    def look(self, user_question, capture_mode="last_non_firefly_window"):
        self.look_calls.append(capture_mode)
        return ScreenVisionResult(
            observation=ScreenObservation(),
            answer="屏幕内容",
            timings={"total_ms": 1.0},
            meta={"direct_one_shot": True, "remote_calls": 1, "reasoning_calls": 0},
        )


# ---------------------------------------------------------------- document builders


def _pdf_bytes(pages: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    for index in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {index}: neural network training details.")
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_pdf_bytes() -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 100), False)
    pix.clear_with(180)
    page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _encrypted_pdf_bytes() -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "secret")
    data = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user"
    )
    doc.close()
    return data


def _docx_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("Introduction", level=1)
    doc.add_paragraph("This is the introduction paragraph.")
    doc.add_heading("Methods", level=2)
    doc.add_paragraph("We used a novel approach.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    table.cell(1, 0).text = "1"
    table.cell(1, 1).text = "2"
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _pptx_bytes(slides: int = 3) -> bytes:
    from pptx import Presentation

    prs = Presentation()
    for index in range(1, slides + 1):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = f"Slide {index} title"
        slide.placeholders[1].text = f"Body text for slide {index}"
    slide2 = prs.slides[1]
    table_shape = slide2.shapes.add_table(2, 2, 72, 72, 200, 80)
    table_shape.table.cell(0, 0).text = "X"
    table_shape.table.cell(0, 1).text = "Y"
    table_shape.table.cell(1, 0).text = "1"
    table_shape.table.cell(1, 1).text = "2"
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Name", "Value"])
    ws.append(["alpha", 10])
    ws.append(["beta", 20])
    ws2 = wb.create_sheet("Summary")
    ws2.append(["total", 30])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _raw_attachment(kind: str, data: bytes, name: str) -> DocumentAttachment:
    return DocumentAttachment(
        display_name=name, kind=kind, original_size=len(data), source_bytes=data
    )


def _ready_attachment(kind: str, data: bytes, name: str) -> DocumentAttachment:
    attachment = _raw_attachment(kind, data, name)
    attachment.mark_ready(parse_document_bytes(data, kind, name))
    return attachment


def _runner(chat, *, screen=None, runtime=None, vision=None):
    return CharacterConversationRunner(
        runtime=runtime or FakeRuntime(),
        screen_vision_service=screen,
        attachment_vision_provider=vision,
        document_chat_handler=chat,
    )


def _window(runner=None) -> CompanionChatWindow:
    return CompanionChatWindow(
        runner=runner or CharacterConversationRunner(runtime=FakeRuntime())
    )


def _drag_enter(window, mime):
    event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.dragEnterEvent(event)
    return event.isAccepted()


def _drop(window, mime):
    event = QDropEvent(
        QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier,
        QEvent.Type.Drop,
    )
    window.dropEvent(event)
    return event.isAccepted()


def _wait_parse(attachment, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and attachment.parse_state not in ("ready", "error"):
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()
    return attachment.parse_state


def _wait_turn(window, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and window.runner.running:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()


# ================================================== A-E drag acceptance


@pytest.mark.parametrize(
    "kind,name,data",
    [
        ("pdf", "paper.pdf", _pdf_bytes()),
        ("docx", "report.docx", _docx_bytes()),
        ("pptx", "deck.pptx", _pptx_bytes()),
        ("txt", "notes.txt", "hello txt".encode("utf-8")),
        ("md", "readme.md", "# Title\ncontent".encode("utf-8")),
        ("xlsx", "data.xlsx", _xlsx_bytes()),
        ("csv", "table.csv", "a,b\n1,2\n".encode("utf-8")),
    ],
    ids=["pdf", "docx", "pptx", "txt", "md", "xlsx", "csv"],
)
def test_drag_document_accepted(tmp_path, qapp, kind, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    window = _window()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    assert _drag_enter(window, mime)
    assert _drop(window, mime)
    assert isinstance(window._pending_attachment, DocumentAttachment)
    assert window._pending_attachment.kind == kind
    assert window._pending_attachment.display_name == name


def test_f_legacy_doc_ppt_xls_rejected(tmp_path, qapp):
    for name, content in (
        ("old.doc", b"doc bytes"),
        ("old.ppt", b"ppt bytes"),
        ("old.xls", b"xls bytes"),
    ):
        path = tmp_path / name
        path.write_bytes(content)
        window = _window()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        assert _drag_enter(window, mime)                 # drop is consumed...
        _drop(window, mime)
        assert window._pending_attachment is None        # ...but no attachment
        assert LEGACY_DOCUMENT_MESSAGE in window.hint_label.text()


def test_g_file_picker_filters_and_adds(tmp_path, qapp, monkeypatch):
    path = tmp_path / "pick.pdf"
    path.write_bytes(_pdf_bytes())
    filters: list = []

    def fake_picker(*args, **kwargs):
        filters.append(args[3])   # getOpenFileName(parent, caption, dir, filter)
        return (str(path), "")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_picker))
    window = _window()
    window._pick_image()
    assert filters and "*.pdf" in filters[0]
    for ext in (".png", ".docx", ".pptx", ".txt", ".md", ".xlsx", ".csv"):
        assert ext in filters[0]
    assert isinstance(window._pending_attachment, DocumentAttachment)


# ================================================== H/I/J chip lifecycle


def test_h_document_chip_parsing_to_ready(tmp_path, qapp):
    path = tmp_path / "paper.pdf"
    path.write_bytes(_pdf_bytes(3))
    window = _window()
    window._set_attachment(load_document_file(path))
    assert window._chip.status_label.text() == "Parsing…"
    state = _wait_parse(window._pending_attachment)
    assert state == "ready"
    # The chip updates via a queued signal from the parse thread; poll until
    # the UI has caught up (avoids cross-thread delivery timing flakes).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "Ready" not in window._chip.status_label.text():
        QApplication.processEvents()
        time.sleep(0.01)
    assert "Ready" in window._chip.status_label.text()
    assert "3 pages" in window._chip.status_label.text()


def test_i_document_retained_after_success(tmp_path, qapp):
    chat = FakeTextChat()
    runner = _runner(chat)
    window = _window(runner)
    path = tmp_path / "notes.txt"
    path.write_bytes("重点内容一\n重点内容二".encode("utf-8"))
    window._set_attachment(load_document_file(path))
    _wait_parse(window._pending_attachment)
    window.input.setText("总结这篇文档")
    window._send()
    _wait_turn(window)
    assert window._pending_attachment is not None   # documents are NOT cleared
    assert window.runner.last_document_meta["source"] == "document_attachment"


def test_j_remove_button_clears_document(tmp_path, qapp):
    path = tmp_path / "notes.txt"
    path.write_bytes("hello".encode("utf-8"))
    window = _window()
    window._set_attachment(load_document_file(path))
    _wait_parse(window._pending_attachment)
    close = window._chip.findChild(QPushButton)
    close.click()
    assert window._pending_attachment is None
    assert window.attachment_row.isHidden()


# ================================================== K-N PDF


def test_k_pdf_native_text_extraction():
    context = parse_document_bytes(_pdf_bytes(3), "pdf", "paper.pdf")
    assert context.page_count == 3
    assert any("neural network" in section.text for section in context.sections)


def test_l_pdf_page_markers():
    context = parse_document_bytes(_pdf_bytes(4), "pdf", "paper.pdf")
    assert [section.label for section in context.sections] == [
        "Page 1", "Page 2", "Page 3", "Page 4",
    ]


def test_m_scanned_pdf_routes_existing_ocr():
    # An image-only page has no text layer; the reused PdfProcessor pipeline
    # must mark it and route it through the OCR path.
    context = parse_document_bytes(_scanned_pdf_bytes(), "pdf", "scan.pdf")
    assert context.sections[0].text == "" or context.parse_warnings
    assert any("OCR" in warning for warning in context.parse_warnings)


def test_n_encrypted_and_corrupt_pdf_safe_errors():
    with pytest.raises(EncryptedPdfError):
        parse_document_bytes(_encrypted_pdf_bytes(), "pdf", "locked.pdf")
    with pytest.raises(DocumentParseError):
        parse_document_bytes(b"%PDF-1.4 this is not a real pdf", "pdf", "broken.pdf")


# ================================================== O-Q DOCX


def test_o_docx_paragraph_extraction():
    context = parse_document_bytes(_docx_bytes(), "docx", "report.docx")
    all_text = "\n".join(section.text for section in context.sections)
    assert "This is the introduction paragraph." in all_text
    assert "We used a novel approach." in all_text


def test_p_docx_heading_order():
    context = parse_document_bytes(_docx_bytes(), "docx", "report.docx")
    texts = [section.text for section in context.sections]
    intro_index = next(i for i, t in enumerate(texts) if "Introduction" in t)
    methods_index = next(i for i, t in enumerate(texts) if "Methods" in t)
    assert intro_index < methods_index                 # headings keep order
    paragraph_labels = [
        section.label for section in context.sections
        if section.label.startswith("Section")
    ]
    assert paragraph_labels == [f"Section {i}" for i in range(1, len(paragraph_labels) + 1)]
    assert any(section.label.startswith("Table") for section in context.sections)


def test_q_docx_tables():
    context = parse_document_bytes(_docx_bytes(), "docx", "report.docx")
    tables = [s for s in context.sections if s.label.startswith("Table")]
    assert tables
    assert "A | B" in tables[0].text and "1 | 2" in tables[0].text


# ================================================== R-T PPTX


def test_r_pptx_slide_markers():
    context = parse_document_bytes(_pptx_bytes(3), "pptx", "deck.pptx")
    assert [section.label for section in context.sections] == ["Slide 1", "Slide 2", "Slide 3"]


def test_s_pptx_slide_text():
    context = parse_document_bytes(_pptx_bytes(3), "pptx", "deck.pptx")
    assert "Slide 1 title" in context.sections[0].text
    assert "Body text for slide 3" in context.sections[2].text


def test_t_pptx_tables():
    context = parse_document_bytes(_pptx_bytes(3), "pptx", "deck.pptx")
    slide2 = next(s for s in context.sections if s.label == "Slide 2")
    assert "X | Y" in slide2.text


# ================================================== U/V XLSX & CSV


def test_u_xlsx_sheet_and_header_extraction():
    context = parse_document_bytes(_xlsx_bytes(), "xlsx", "data.xlsx")
    labels = [section.label for section in context.sections]
    assert "Sheet: Data" in labels and "Sheet: Summary" in labels
    sheet = next(s for s in context.sections if s.label == "Sheet: Data")
    assert "Name | Value" in sheet.text and "alpha | 10" in sheet.text


def test_v_csv_and_large_truncation():
    small = parse_document_bytes(b"h1,h2\n1,2\n", "csv", "t.csv")
    assert "h1 | h2" in small.sections[0].text
    big_csv = ("a,b\n" + "x,y\n" * (MAX_CSV_ROWS + 50)).encode("utf-8")
    big = parse_document_bytes(big_csv, "csv", "big.csv")
    assert big.truncated
    assert big.parse_warnings


def test_v2_xlsx_large_truncation():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Big"
    for index in range(MAX_XLSX_SHEET_ROWS + 50):
        ws.append([f"r{index}", index])
    buffer = io.BytesIO()
    wb.save(buffer)
    context = parse_document_bytes(buffer.getvalue(), "xlsx", "big.xlsx")
    assert context.truncated


# ================================================== W/Z retrieval, X/Y direct lookup


def _topic_doc() -> DocumentContext:
    sections = [
        DocumentSection(index=1, label="Page 1", text="Photosynthesis converts light into chemical energy."),
        DocumentSection(index=2, label="Page 2", text="Quantum computing uses qubits for superposition."),
        DocumentSection(index=3, label="Page 3", text="Photosynthesis happens in chloroplasts."),
    ]
    return DocumentContext(filename="mix.pdf", kind="pdf", sections=sections,
                           total_characters=sum(len(s.text) for s in sections))


def test_w_question_retrieves_relevant_chunk():
    chunks = chunk_document(_topic_doc())
    top = retrieve_chunks("photosynthesis energy chloroplast", chunks, top_k=3)
    labels = [chunk.label for chunk in top]
    assert "Page 1" in labels or "Page 3" in labels
    assert "Page 2" not in labels            # irrelevant chunk excluded


def test_z_unrelated_query_returns_bounded_minimal_result():
    doc = DocumentContext(
        filename="a.pdf", kind="pdf",
        sections=[
            DocumentSection(index=1, label="Page 1", text="cold fusion reactor design"),
            DocumentSection(index=2, label="Page 2", text="quantum error correction"),
        ],
        total_characters=60,
    )
    top = retrieve_chunks("热带水果种植技术", chunk_document(doc), top_k=4)
    # No term overlap: the result stays bounded (fallback is at most 1 chunk).
    assert len(top) <= 1


def test_x_page_12_direct_lookup():
    sections = [DocumentSection(index=i, label=f"Page {i}", text=f"content {i}") for i in range(1, 15)]
    context = DocumentContext(filename="p.pdf", kind="pdf", sections=sections, total_characters=10)
    hit = direct_section_lookup("第 12 页讲了什么", context)
    assert [s.label for s in hit] == ["Page 12"]
    hit_en = direct_section_lookup("what does page 12 say?", context)
    assert [s.label for s in hit_en] == ["Page 12"]


def test_y_slide_8_direct_lookup():
    sections = [DocumentSection(index=i, label=f"Slide {i}", text=f"content {i}") for i in range(1, 10)]
    context = DocumentContext(filename="d.pptx", kind="pptx", sections=sections, total_characters=10)
    hit = direct_section_lookup("第 8 页主要讲什么", context)
    assert [s.label for s in hit] == ["Slide 8"]
    hit_en = direct_section_lookup("Slide 8 details", context)
    assert [s.label for s in hit_en] == ["Slide 8"]


# ================================================== AA-AF privacy isolation


def test_aa_file_bytes_not_sent_to_provider():
    chat = FakeTextChat()
    runner = _runner(chat)
    runner.perform_with_document("方法是什么", _ready_attachment("txt", b"method A", "m.txt"))
    blob = json.dumps(chat.calls[0]["messages"], ensure_ascii=False)
    assert "data:" not in blob
    assert "base64" not in blob
    assert len(chat.calls[0]["messages"][1]["content"]) < 2000  # excerpts only


def test_ab_absolute_path_not_sent(tmp_path):
    chat = FakeTextChat()
    runner = _runner(chat)
    data = "方法内容".encode("utf-8")
    attachment = _ready_attachment("txt", data, "doc.txt")
    runner.perform_with_document("方法是什么", attachment)
    blob = json.dumps(chat.calls[0]["messages"], ensure_ascii=False)
    assert "doc.txt" in blob
    assert str(tmp_path) not in blob


def test_ac_full_document_not_sent_for_ordinary_qa():
    chat = FakeTextChat()
    runner = _runner(chat)
    large = ("sentence " * 50 + "\n") * 30  # ~7000 chars, many chunks
    runner.perform_with_document("问题", _ready_attachment("txt", large.encode(), "big.txt"))
    user = chat.calls[0]["messages"][1]["content"]
    assert len(user) < 4000
    assert runner.last_document_meta["chunks_sent"] <= 6


def test_ad_ae_memory_and_suggestion_not_called():
    chat = FakeTextChat()
    runtime = FakeRuntime()
    runner = _runner(chat, runtime=runtime)
    runner.perform_with_document("总结", _ready_attachment("txt", b"content", "d.txt"))
    assert runtime.chat_calls == []            # runtime (memory/suggestion) untouched


def test_af_history_safe_placeholder_only():
    chat = FakeTextChat()
    runner = _runner(chat)
    runner.perform_with_document("方法", _ready_attachment("txt", b"method", "secret.txt"))
    history = runner.history
    assert "[Document attachment: secret.txt]" in history[0]["content"]
    blob = json.dumps(history, ensure_ascii=False)
    assert "base64" not in blob
    assert "data:" not in blob


# ================================================== AG-AK interaction


def test_ag_document_question_uses_text_provider():
    chat = FakeTextChat()
    runner = _runner(chat)
    events, answer = runner.perform_with_document(
        "方法是什么", _ready_attachment("pdf", _pdf_bytes(), "paper.pdf")
    )
    assert len(chat.calls) == 1
    assert answer == "根据文档内容，答案是……"
    assert events[0].type is not None


def test_ah_no_screen_capture_for_document():
    chat = FakeTextChat()
    screen = FakeScreenVisionService()
    runner = _runner(chat, screen=screen)
    runner.perform_with_document("总结", _ready_attachment("txt", b"content", "d.txt"))
    assert screen.look_calls == []
    assert runner.last_document_meta["screen_capture_calls"] == 0


def test_ai_no_vision_call_for_textual_document():
    chat = FakeTextChat()
    vision = FakeVision()
    runner = _runner(chat, vision=vision)
    runner.perform_with_document("总结", _ready_attachment("txt", b"content", "d.txt"))
    assert vision.calls == []
    assert runner.last_document_meta["vision_calls"] == 0
    assert runner.last_document_meta["reasoning_calls"] == 0


def test_aj_normal_no_attachment_chat_unchanged():
    chat = FakeTextChat()
    runtime = FakeRuntime()
    runner = _runner(chat, runtime=runtime)
    runner.perform("你好")
    assert runtime.chat_calls == ["你好"]
    assert chat.calls == []


def test_ak_image_attachment_behavior_unchanged(qapp):
    from PySide6.QtGui import QImage

    from ui.companion_attachment import qimage_to_attachment

    chat = FakeTextChat()
    screen = FakeScreenVisionService()
    vision = FakeVision()
    runner = CharacterConversationRunner(
        runtime=FakeRuntime(), screen_vision_service=screen,
        attachment_vision_provider=vision, document_chat_handler=chat,
    )
    image = QImage(20, 10, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    runner.perform_with_image("看看这张图", qimage_to_attachment(image, "x.png"))
    assert len(vision.calls) == 1              # image still routes to vision
    assert screen.look_calls == []             # no screen capture
    assert runner.last_attachment_meta["screen_capture_calls"] == 0


def test_document_attachment_prioritized_over_plain_chat():
    chat = FakeTextChat()
    runner = _runner(chat)
    runner.perform_with_document("它有什么问题", _ready_attachment("txt", b"method has limits", "d.txt"))
    assert len(chat.calls) == 1
    assert "[Document attachment: d.txt]" in runner.history[0]["content"]


def test_document_negation_falls_through_to_normal():
    chat = FakeTextChat()
    runtime = FakeRuntime()
    runner = _runner(chat, runtime=runtime)
    runner.perform_with_document("先不看这个文件，随便聊聊", _ready_attachment("txt", b"x", "d.txt"))
    assert chat.calls == []
    assert runtime.chat_calls == ["先不看这个文件，随便聊聊"]


def test_document_empty_text_uses_default_question():
    chat = FakeTextChat()
    runner = _runner(chat)
    runner.perform_with_document("", _ready_attachment("txt", b"content", "d.txt"))
    user = chat.calls[0]["messages"][1]["content"]
    # Empty text maps to the default summary request, which now runs the
    # five-point summary prompt.
    assert "研究问题/目的" in user
    assert "局限/意义" in user


# ================================================== AL-AM summaries


def test_al_small_document_summary_minimal_calls():
    chat = FakeTextChat()
    runner = _runner(chat)
    small = ("段落内容 " * 100).encode("utf-8")  # ~500 chars
    runner.perform_with_document("总结全文", _ready_attachment("txt", small, "small.txt"))
    assert len(chat.calls) == 1
    assert runner.last_document_meta["remote_calls"] == 1


def test_am_large_document_hierarchical_summary():
    chat = FakeTextChat()
    runner = _runner(chat)
    # Many distinct, sentence-separated sections keep the compressed outline
    # above the direct budget, so the bounded map/reduce path runs.
    sections = [
        DocumentSection(
            index=i, label=f"Section {i}",
            text=f"第 {i} 段独特方法内容。\n" + ("方法细节描述与实验结果。" * 300),
        )
        for i in range(1, 26)
    ]
    context = DocumentContext(
        filename="big.txt", kind="txt", sections=sections,
        total_characters=sum(len(s.text) for s in sections),
    )
    attachment = DocumentAttachment(
        display_name="big.txt", kind="txt", original_size=1, source_bytes=b""
    )
    attachment.mark_ready(context)
    runner.perform_with_document("总结全文", attachment)
    assert runner.last_document_meta["remote_calls"] == len(chat.calls)
    assert runner.last_document_meta["remote_calls"] >= 2
    assert runner.last_document_meta["remote_calls"] <= 6


def test_an_summary_grounded_in_source():
    chat = FakeTextChat()
    runner = _runner(chat)
    data = ("source material about the topic. " * 30).encode("utf-8")
    runner.perform_with_document("总结全文", _ready_attachment("txt", data, "s.txt"))
    # The provider saw excerpts/summary input derived from the document text.
    blob = json.dumps(chat.calls[0]["messages"], ensure_ascii=False)
    assert "source material about the topic" in blob


def test_ao_insufficient_evidence_instruction_present():
    messages = build_qa_messages("p.pdf", "[Page 1]\ntext", "问题")
    system = messages[0]["content"]
    assert "based only on the supplied document excerpts" in system
    assert "Do not invent content" in system
    assert messages[1]["content"].startswith("DOCUMENT:\np.pdf")


# ================================================== chunking & format helpers


def test_chunking_keeps_source_labels():
    sections = [DocumentSection(index=i, label=f"Page {i}", text="x" * (CHUNK_SIZE * 2)) for i in (1, 2)]
    context = DocumentContext(filename="p.pdf", kind="pdf", sections=sections, total_characters=1)
    chunks = chunk_document(context)
    assert all(chunk.label in ("Page 1", "Page 2") for chunk in chunks)
    assert len(chunks) >= 4


def test_summary_intent_detection():
    assert is_summary_request("总结这篇论文")
    assert is_summary_request("这份 PPT 的整体逻辑是什么")
    assert not is_summary_request("第 6 页讲了什么")
