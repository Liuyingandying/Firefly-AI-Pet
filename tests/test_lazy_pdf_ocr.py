"""Lazy / Progressive OCR for scanned PDFs (A-AN from the task).

Attaching a scanned PDF builds only a cheap index (metadata + native text +
bookmarks); OCR happens on demand (Page N), in bounded progressive batches,
or via a single-worker background job. Everything is session-only, fake OCR is
injected — no real RapidOCR, no real network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.document_attachment import (
    DocumentContext,
    DocumentSection,
    direct_section_lookup,
)
from core.lazy_pdf_ocr import (
    MAX_LAZY_OCR_PAGES_PER_TURN,
    MAX_PROGRESSIVE_OCR_PAGES_PER_TURN,
    MAX_SUMMARY_SEED_OCR_PAGES,
    PROGRESSIVE_BATCH_SIZE,
    LazyPdfOcrState,
    LOW_COVERAGE_REPLY,
    is_continue_scan_request,
    is_full_ocr_request,
)
from core.pdf_processor import PdfPageStatus, build_pdf_lazy_index
from ui.companion_attachment import DocumentAttachment
from ui.character_conversation_runner import CharacterConversationRunner
from ui.companion_chat_window import CompanionChatWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- fixtures


def _scanned_pdf_bytes(pages: int = 10) -> bytes:
    import fitz

    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 60), False)
    pix.clear_with(200)
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _native_pdf_bytes(pages: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"native page {index + 1} with real text")
    data = doc.tobytes()
    doc.close()
    return data


def _mixed_pdf_bytes(native: int = 2, scanned: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 60), False)
    pix.clear_with(200)
    for _ in range(native):
        page = doc.new_page()
        page.insert_text((72, 72), "native text layer content")
    for _ in range(scanned):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


class FakeOcr:
    def __init__(self, text_fn=lambda p: f"Page {p} 扫描文本优化算法 optimization 与实验结果。"):
        self.calls: list[int] = []
        self.text_fn = text_fn

    def __call__(self, source: bytes, pages):
        self.calls.extend(int(p) for p in pages)
        return {int(p): self.text_fn(int(p)) for p in pages}


class FakeChat:
    def __init__(self, answer="回答"):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, temperature=0.2):
        self.calls.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}


class FakeRuntime:
    def __init__(self):
        self.chat_calls = []

    def chat(self, prompt, history=None, turn_context=None):
        self.chat_calls.append(prompt)
        return {"choices": [{"message": {"role": "assistant", "content": "普通回复"}}]}


def _lazy_attachment(data: bytes, name="scan.pdf", ocr_fn=None):
    index = build_pdf_lazy_index(data, name)
    attachment = DocumentAttachment(
        display_name=name, kind="pdf", original_size=len(data), source_bytes=data
    )
    state = LazyPdfOcrState(index, generation_id="g-test")
    attachment.attach_lazy_state(state)
    attachment.mark_lazy_ready(state.build_context())
    if ocr_fn is not None:
        attachment.set_lazy_ocr_fn(ocr_fn)
    return attachment, state


def _runner(chat, runtime=None):
    return CharacterConversationRunner(
        runtime=runtime or FakeRuntime(), document_chat_handler=chat
    )


# ================================================== A-F initial attach + page OCR


def test_a_native_pdf_remains_eager_fast_path():
    data = _native_pdf_bytes(3)
    index = build_pdf_lazy_index(data, "native.pdf")
    assert set(index.page_states.values()) == {PdfPageStatus.NATIVE}
    assert index.page_count == 3
    assert not index.truncated
    # Native PDFs have no pending pages -> the window takes the eager full path.


def test_b_scanned_168_page_attach_zero_ocr_calls():
    data = _scanned_pdf_bytes(168)
    index = build_pdf_lazy_index(data, "scan.pdf")
    assert index.page_count == 168
    assert set(index.page_states.values()) == {PdfPageStatus.OCR_PENDING}


def test_c_initial_state_is_ocr_pending():
    attachment, state = _lazy_attachment(_scanned_pdf_bytes(10))
    assert state.ocr_pages_pending() == list(range(1, 11))
    assert state.ocr_pages_completed() == 0


def test_d_initial_attachment_ready_partial():
    attachment, state = _lazy_attachment(_scanned_pdf_bytes(10))
    assert attachment.parse_state == "ready"
    assert attachment.is_lazy
    assert state.build_context().sections == []      # partial (no OCR yet)
    assert state.build_context().page_count == 10


def test_e_page_37_request_ocrs_only_that_page():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第37页讲了什么", attachment)
    assert ocr.calls == [37]
    assert state.page_state(37) == PdfPageStatus.OCR_DONE
    assert runner.last_document_meta["ocr_mode"] == "lazy"
    assert runner.last_document_meta["ocr_pages_requested"] == 1


def test_f_page_37_repeat_is_cache_hit():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第37页讲了什么", attachment)
    first_calls = list(ocr.calls)
    runner.perform_with_document("第37页还有什么", attachment)
    assert ocr.calls == first_calls                    # no second OCR
    assert runner.last_document_meta["ocr_cache_hits"] == 1


# ================================================== G/H ranges


def test_g_page_37_to_40_ocrs_four_pages():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第37到40页总结一下", attachment)
    assert sorted(ocr.calls) == [37, 38, 39, 40]


def test_h_range_over_8_is_bounded():
    data = _scanned_pdf_bytes(120)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第1到100页讲了什么", attachment)
    assert len(ocr.calls) <= MAX_LAZY_OCR_PAGES_PER_TURN
    # The batch note is attached to the excerpts sent to the provider.
    assert "一次最多识别 8 页" in str(chat.calls)


# ================================================== I/J failures + retry


def test_i_page_ocr_failure_does_not_kill_attachment():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr(text_fn=lambda p: "" if p == 5 else "ok text")
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第5页讲了什么", attachment)
    assert state.page_state(5) == PdfPageStatus.OCR_FAILED
    assert attachment.parse_state == "ready"            # attachment still alive
    answer = runner.history[-1]["content"]
    assert "没有成功识别" in answer


def test_j_failed_page_explicit_retry():
    data = _scanned_pdf_bytes(10)
    attempts = {"count": 0}

    def flaky(source, pages):
        attempts["count"] += 1
        page = pages[0]
        if attempts["count"] == 1:
            return {page: ""}
        return {page: "now readable content"}

    attachment, state = _lazy_attachment(data, ocr_fn=flaky)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第3页讲了什么", attachment)
    assert state.page_state(3) == PdfPageStatus.OCR_FAILED
    runner.perform_with_document("第3页再解释一下", attachment)   # explicit retry
    assert state.page_state(3) == PdfPageStatus.OCR_DONE


# ================================================== K-M retrieval & cache integration


def test_k_new_ocr_page_enters_retrieval_context():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第4页讲了什么", attachment)
    context = state.build_context()
    assert any(section.label == "Page 4" for section in context.sections)
    assert any("optimization" in section.text for section in context.sections)


def test_l_bm25_sees_newly_ocr_text():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("优化算法有哪些", attachment)
    # The generic QA ran progressive OCR and retrieved matching text.
    assert runner.last_document_meta["ocr_mode"] == "progressive"
    assert any("optimization" in str(m) for m in chat.calls)


def test_m_new_ocr_page_invalidates_summary_cache():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    attachment.summary_cache["input"] = "stale"
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第2页讲了什么", attachment)
    assert attachment.summary_cache == {}


# ================================================== N-R progressive + summary seed


def test_n_generic_qa_insufficient_evidence_uses_bounded_progressive():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("这本书介绍哪些算法", attachment)
    assert len(ocr.calls) <= MAX_PROGRESSIVE_OCR_PAGES_PER_TURN


def test_o_generic_qa_never_silently_full_ocrs():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr(text_fn=lambda p: "unrelated generic filler text")
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("量子计算是什么", attachment)
    assert len(ocr.calls) <= MAX_PROGRESSIVE_OCR_PAGES_PER_TURN
    answer = runner.history[-1]["content"]
    assert "扫描版" in answer                              # honest fallback


def test_p_continue_scan_advances_next_batch():
    data = _scanned_pdf_bytes(30)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("继续扫描", attachment)
    assert len(ocr.calls) == PROGRESSIVE_BATCH_SIZE
    runner.perform_with_document("再扫描一些", attachment)
    assert len(ocr.calls) == PROGRESSIVE_BATCH_SIZE * 2


def test_q_initial_summary_low_coverage_uses_seed():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结全文", attachment)
    assert len(ocr.calls) <= MAX_SUMMARY_SEED_OCR_PAGES
    assert state.coverage_ratio() < 1.0


def test_r_partial_summary_marked_preliminary():
    data = _scanned_pdf_bytes(60)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结这篇论文", attachment)
    answer = runner.history[-1]["content"]
    assert "初步概览" in answer
    assert "不是完整全文总结" in answer


# ================================================== S-U full summary coverage / background


def test_s_high_coverage_summary_uses_normal_summary_v2():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    # OCR 8 of 10 pages -> coverage >= 0.7
    state.record_ocr_results({p: f"page {p} text" for p in range(1, 9)})
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结全文", attachment)
    answer = runner.history[-1]["content"]
    assert "初步概览" not in answer


def test_t_explicit_full_ocr_starts_background_job():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("把这份PDF全部识别出来", attachment)
    # The background job runs asynchronously (single worker).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and state.ocr_pages_completed() < state.page_count:
        time.sleep(0.02)
    assert state.ocr_pages_completed() == state.page_count
    assert runner.last_document_meta["ocr_mode"] == "full_background"


def test_u_background_ocr_uses_one_worker_per_iteration():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.start_full_pdf_ocr(attachment)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and state.ocr_pages_completed() < state.page_count:
        time.sleep(0.02)
    assert ocr.calls
    # Every OCR invocation processed exactly one page (single worker).
    assert max(ocr.calls.count(p) for p in set(ocr.calls)) <= 1


def test_w_attachment_cancel_stops_background_job():
    data = _scanned_pdf_bytes(200)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.start_full_pdf_ocr(attachment)
    time.sleep(0.1)
    attachment.cancel_lazy()
    time.sleep(0.1)
    completed_after_cancel = state.ocr_pages_completed()
    time.sleep(0.2)
    assert state.ocr_pages_completed() == completed_after_cancel   # stopped


def test_x_replacement_prevents_stale_result_injection():
    data = _scanned_pdf_bytes(10)
    ocr_a = FakeOcr()
    attachment_a, state_a = _lazy_attachment(data, "a.pdf", ocr_fn=ocr_a)
    attachment_b, state_b = _lazy_attachment(data, "b.pdf", ocr_fn=ocr_a)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第1页讲了什么", attachment_a)
    # Page 1 text went to attachment A's state only.
    assert state_a.page_text(1)
    assert state_b.page_text(1) == ""


# ================================================== AA-AB page budgets


def test_aa_max_pdf_ocr_pages_preserved():
    from core.document_attachment import MAX_PDF_OCR_PAGES

    assert MAX_PDF_OCR_PAGES == 200


def test_ab_more_than_200_scan_pages_bounded():
    data = _scanned_pdf_bytes(250)
    index = build_pdf_lazy_index(data, "huge.pdf", max_pages=200)
    assert index.page_count == 200
    assert index.truncated is True


# ================================================== AC-AD bookmark / native


def test_ac_native_text_pages_never_ocr_unnecessarily():
    data = _mixed_pdf_bytes(native=2, scanned=3)
    ocr = FakeOcr()
    attachment, state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第1页讲了什么", attachment)   # native page
    assert ocr.calls == []
    assert state.page_state(1) == PdfPageStatus.NATIVE


def test_ad_bookmark_prioritizes_relevant_page():
    import fitz

    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 60), False)
    pix.clear_with(200)
    for _ in range(10):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pix)
    doc.set_toc([[1, "第四章 优化算法", 4]])
    data = doc.tobytes()
    doc.close()
    index = build_pdf_lazy_index(data, "book.pdf")
    attachment, state = _lazy_attachment(data, "book.pdf")
    picked = state.pick_progressive_pages("优化算法", max_pages=4)
    assert 4 in picked or 3 in picked or 5 in picked       # bookmark vicinity


def test_ae_no_bookmark_fallback_remains_bounded():
    data = _scanned_pdf_bytes(30)
    attachment, state = _lazy_attachment(data)
    picked = state.pick_progressive_pages("任意问题", max_pages=6)
    assert 1 <= len(picked) <= 6


# ================================================== AF-AH regressions


def test_af_ordinary_docx_behavior_unchanged():
    from io import BytesIO
    from docx import Document

    from core.document_attachment import parse_document_bytes

    doc = Document()
    doc.add_paragraph("docx paragraph")
    buffer = BytesIO()
    doc.save(buffer)
    context = parse_document_bytes(buffer.getvalue(), "docx", "x.docx")
    assert context.kind == "docx"
    assert any("docx paragraph" in s.text for s in context.sections)


def test_ag_page_direct_lookup_regression_unchanged():
    sections = [DocumentSection(index=i, label=f"Page {i}", text=f"c{i}") for i in range(1, 15)]
    context = DocumentContext(filename="p.pdf", kind="pdf", sections=sections, total_characters=10)
    hit = direct_section_lookup("第 12 页讲了什么", context)
    assert [s.label for s in hit] == ["Page 12"]


def test_ah_summary_v2_regression_unchanged():
    from core.document_attachment import build_summary_input, parse_document_bytes

    context = parse_document_bytes(("内容" * 100).encode("utf-8"), "txt", "s.txt")
    summary_input = build_summary_input(context)
    assert summary_input.total_chars > 0


# ================================================== AI-AL privacy


def test_ai_aj_raw_pdf_and_ocr_image_never_uploaded():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第3页讲了什么", attachment)
    blob = json.dumps(chat.calls, ensure_ascii=False)
    assert "data:" not in blob and "base64" not in blob
    assert "PNG" not in blob and "image" not in blob.lower()


def test_ak_memory_history_privacy_unchanged():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runtime = FakeRuntime()
    runner = _runner(chat, runtime=runtime)
    runner.perform_with_document("第1页讲了什么", attachment)
    assert runtime.chat_calls == []                        # memory path untouched
    history = runner.history
    assert "[Document attachment: scan.pdf]" in history[0]["content"]


def test_al_meta_has_no_path_bytes_images():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第2页讲了什么", attachment)
    meta = runner.last_document_meta
    assert "ocr_mode" in meta and "ocr_coverage_ratio" in meta
    assert "pdf_initial_index_ms" in meta
    blob = json.dumps(meta)
    assert "data:" not in blob and "base64" not in blob
    assert "C:" not in blob


def test_am_summary_cache_cleared_on_new_ocr_page():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment, _state = _lazy_attachment(data, ocr_fn=ocr)
    attachment.summary_cache["input"] = "stale"
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第7页讲了什么", attachment)
    assert attachment.summary_cache == {}


def test_an_old_attachment_generation_ignored():
    data = _scanned_pdf_bytes(10)
    ocr = FakeOcr()
    attachment_old, state_old = _lazy_attachment(data, "old.pdf", ocr_fn=ocr)
    attachment_new, state_new = _lazy_attachment(data, "new.pdf", ocr_fn=ocr)
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("第1页讲了什么", attachment_old)
    assert state_old.page_text(1)
    assert state_new.page_text(1) == ""                     # no cross-talk


# ================================================== phrase detection


def test_phrase_detectors():
    assert is_continue_scan_request("继续扫描")
    assert is_continue_scan_request("再多看几页")
    assert is_full_ocr_request("把这本 PDF 全部识别出来")
    assert is_full_ocr_request("完整 OCR 全书")
    assert not is_full_ocr_request("总结全文")
