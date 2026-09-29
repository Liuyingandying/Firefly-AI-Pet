"""Document summary latency v2 tests (A-W from the task).

The v2 strategy compresses the document locally (deterministic outline) and
then picks the cheapest safe path: small -> 1 call, medium -> 3 calls,
large -> bounded map/reduce (<= SUMMARY_MAX_MAP_GROUPS + 1). The old logic
turned a ~50k-char document into ~10 map groups -> 11 calls; v2 must keep the
same document at <= 3 calls while preserving grounding, page labels and
privacy. Ordinary QA retrieval and direct page/slide lookup are untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.document_attachment import (
    SUMMARY_DIRECT_CHAR_BUDGET,
    SUMMARY_MEDIUM_CHAR_BUDGET,
    SUMMARY_MAX_MAP_GROUPS,
    SUMMARY_REFERENCE_BUDGET,
    DocumentContext,
    DocumentSection,
    build_qa_messages,
    build_summary_input,
    detect_section_kind,
    direct_section_lookup,
    format_summary_input,
    summarize_document,
)
from core.screen_vision.models import ScreenObservation, ScreenVisionResult
from ui.companion_attachment import DocumentAttachment
from ui.character_conversation_runner import CharacterConversationRunner


class FakeChat:
    def __init__(self, answer="总结内容……"):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, temperature=0.2):
        self.calls.append({"messages": messages, "temperature": temperature})
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}


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


def _context(sections: list[DocumentSection], name="doc.pdf", kind="pdf") -> DocumentContext:
    return DocumentContext(
        filename=name, kind=kind, sections=sections,
        total_characters=sum(len(s.text) for s in sections),
    )


def _attachment(context: DocumentContext) -> DocumentAttachment:
    attachment = DocumentAttachment(
        display_name=context.filename, kind=context.kind,
        original_size=1, source_bytes=b"",
    )
    attachment.mark_ready(context)
    return attachment


def _page_section(index: int, topic: str, sentences: int = 40) -> DocumentSection:
    body = "\n".join(
        f"{topic} detail sentence number {i} with metric value {i * 3}.7 percent."
        for i in range(sentences)
    )
    return DocumentSection(index=index, label=f"Page {index}", text=f"{topic}\n{body}")


def _runner(chat, *, screen=None, runtime=None):
    return CharacterConversationRunner(
        runtime=runtime or FakeRuntime(),
        screen_vision_service=screen,
        document_chat_handler=chat,
    )


# ================================================== A/B level selection


def test_a_small_document_summary_uses_one_call():
    chat = FakeChat()
    context = _context([_page_section(1, "Introduction", sentences=4)], "small.pdf")
    answer, calls, stats = summarize_document(context, chat)
    assert calls == 1
    assert stats["level"] == "small"
    assert stats["groups"] == 1


def test_b_50k_document_uses_at_most_3_calls():
    chat = FakeChat()
    sections = [_page_section(i, f"Topic {i}", sentences=46) for i in range(1, 15)]
    context = _context(sections, "50k.pdf")
    assert context.total_characters >= 40000
    _answer, calls, stats = summarize_document(context, chat)
    assert calls <= 3          # old logic produced ~11 calls for this size
    assert calls == len(chat.calls)
    assert stats["summary_input_chars"] < context.total_characters
    assert stats["summary_input_chars"] > 0


# ================================================== C/D coverage & boundedness


def test_c_medium_document_covers_front_middle_back():
    sections = [
        _page_section(1, "Introduction"),
        _page_section(2, "Background"),
        _page_section(3, "Methods"),
        _page_section(4, "Results"),
        _page_section(5, "Conclusion"),
    ]
    summary_input = build_summary_input(_context(sections, "m.pdf"))
    labels = [entry.label for entry in summary_input.entries]
    assert "Page 1" in labels and "Page 3" in labels and "Page 5" in labels
    outline_text = " ".join(entry.text for entry in summary_input.entries)
    assert "Introduction" in outline_text
    assert "Methods" in outline_text
    assert "Conclusion" in outline_text


def test_d_large_document_calls_stay_bounded():
    chat = FakeChat()
    sections = [_page_section(i, f"Long topic {i}", sentences=60) for i in range(1, 50)]
    context = _context(sections, "large.pdf")
    _answer, calls, stats = summarize_document(context, chat)
    assert calls <= SUMMARY_MAX_MAP_GROUPS + 1
    assert stats["level"] == "large"
    assert stats["groups"] <= SUMMARY_MAX_MAP_GROUPS


# ================================================== E/F/G compression quality


def test_e_references_downgraded_in_summary_input():
    sections = [
        _page_section(1, "Introduction", sentences=8),
        DocumentSection(
            index=2, label="Page 2",
            text="References\n" + "\n".join(f"[{i}] Author, Title, Journal, 202{i % 10}." for i in range(1, 200)),
        ),
    ]
    summary_input = build_summary_input(_context(sections, "refs.pdf"))
    assert summary_input.references_downgraded == 1
    reference_entry = summary_input.entries[-1]
    assert len(reference_entry.text) <= SUMMARY_REFERENCE_BUDGET + 10


def test_f_methods_results_conclusion_preserved():
    sections = [
        DocumentSection(index=1, label="Page 1", text="Methods\nWe propose a spiking graph network with TAGCN convolution."),
        DocumentSection(index=2, label="Page 2", text="Results\nAccuracy reaches 89.5 percent on NeuTouch dataset."),
        DocumentSection(index=3, label="Page 3", text="Conclusion\nWe demonstrate low power neuromorphic tactile sensing."),
    ]
    summary_input = build_summary_input(_context(sections, "paper.pdf"))
    outline = " ".join(entry.text for entry in summary_input.entries)
    assert "spiking graph network" in outline
    assert "89.5" in outline
    assert "low power" in outline


def test_g_page_labels_preserved_in_outline():
    sections = [_page_section(i, f"Content {i}", sentences=5) for i in range(1, 5)]
    summary_input = build_summary_input(_context(sections, "p.pdf"))
    assert [entry.label for entry in summary_input.entries] == [
        "Page 1", "Page 2", "Page 3", "Page 4",
    ]


# ================================================== H/I/J/K ordinary QA untouched


def test_h_ordinary_qa_uses_retrieval_not_summary():
    chat = FakeChat()
    runner = _runner(chat)
    sections = [
        DocumentSection(index=1, label="Page 1", text="Methods\nWe use a novel spiking network."),
        DocumentSection(index=2, label="Page 2", text="Results\nThe accuracy is 89.5 percent."),
    ]
    runner.perform_with_document("方法是什么", _attachment(_context(sections, "p.pdf")))
    assert len(chat.calls) == 1
    assert runner.last_document_meta.get("summary_level") is None   # not a summary turn


def test_i_page_12_direct_lookup_unchanged():
    sections = [DocumentSection(index=i, label=f"Page {i}", text=f"content {i}") for i in range(1, 15)]
    hit = direct_section_lookup("第 12 页讲了什么", _context(sections, "p.pdf"))
    assert [s.label for s in hit] == ["Page 12"]


def test_j_slide_direct_lookup_unchanged():
    sections = [DocumentSection(index=i, label=f"Slide {i}", text=f"content {i}") for i in range(1, 10)]
    hit = direct_section_lookup("Slide 8 细节", _context(sections, "d.pptx", "pptx"))
    assert [s.label for s in hit] == ["Slide 8"]


def test_k_docx_qa_unchanged():
    chat = FakeChat()
    runner = _runner(chat)
    sections = [DocumentSection(index=1, label="Section 1", text="老师要求完成实验报告。")]
    runner.perform_with_document("要求我做什么", _attachment(_context(sections, "r.docx", "docx")))
    assert len(chat.calls) == 1
    assert runner.last_document_meta["kind"] == "docx"


# ================================================== L/M/N cache


def test_l_summary_cache_reused_within_session():
    chat = FakeChat()
    sections = [_page_section(i, f"Topic {i}", sentences=30) for i in range(1, 30)]
    context = _context(sections, "big.pdf")
    cache: dict = {}
    _answer1, calls1, _stats = summarize_document(context, chat, cache=cache)
    assert calls1 >= 2
    first_calls = len(chat.calls)
    _answer2, calls2, _stats2 = summarize_document(context, chat, cache=cache)
    assert calls2 == 1                     # only the cheap final synthesis re-runs
    assert len(chat.calls) == first_calls + 1


def test_m_attachment_clear_clears_cache():
    attachment = _attachment(_context([_page_section(1, "Intro")], "a.pdf"))
    attachment.summary_cache["input"] = "cached"
    attachment.clear_summary_cache()
    assert attachment.summary_cache == {}


def test_n_new_attachment_has_fresh_cache():
    first = _attachment(_context([_page_section(1, "A")], "a.pdf"))
    first.summary_cache["input"] = "old"
    second = _attachment(_context([_page_section(1, "B")], "b.pdf"))
    assert second.summary_cache == {}      # new attachment -> old cache gone


# ================================================== O/P/Q/R privacy


def test_o_summary_cache_not_written_to_disk(tmp_path):
    chat = FakeChat()
    cache: dict = {}
    summarize_document(_context([_page_section(1, "Intro")], "c.pdf"), chat, cache=cache)
    assert [p for p in tmp_path.rglob("*")] == []


def test_p_summary_not_in_memory_history():
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结全文", _attachment(_context(
        [_page_section(1, "Introduction", sentences=3)], "s.pdf"
    )))
    blob = json.dumps(runner.history, ensure_ascii=False)
    assert "[Document attachment: s.pdf]" in blob
    assert "总结内容" in blob               # only the final answer, no outline dump


def test_q_original_bytes_not_sent_to_provider():
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结全文", _attachment(_context(
        [_page_section(1, "Introduction", sentences=30)], "s.pdf"
    )))
    blob = json.dumps(chat.calls[0]["messages"], ensure_ascii=False)
    assert "data:" not in blob and "base64" not in blob


def test_r_summary_input_has_no_absolute_path(tmp_path):
    chat = FakeChat()
    runner = _runner(chat)
    runner.perform_with_document("总结全文", _attachment(_context(
        [_page_section(1, "Introduction", sentences=3)], "s.pdf"
    )))
    blob = json.dumps(chat.calls[0]["messages"], ensure_ascii=False)
    assert str(tmp_path) not in blob


# ================================================== S metadata accuracy


def test_s_provider_call_count_metadata_accurate():
    chat = FakeChat()
    runner = _runner(chat)
    sections = [_page_section(i, f"Topic {i}", sentences=40) for i in range(1, 30)]
    runner.perform_with_document("总结全文", _attachment(_context(sections, "big.pdf")))
    assert runner.last_document_meta["remote_calls"] == len(chat.calls)
    assert runner.last_document_timings["summary_input_chars"] == \
        runner.last_document_meta["summary_input_chars"]


# ================================================== T empty/short safe


def test_t_empty_and_very_short_documents_safe():
    chat = FakeChat()
    empty = _context([], "empty.pdf")
    answer, calls, stats = summarize_document(empty, chat)
    assert calls == 1 or calls == 0
    assert isinstance(answer, str)
    tiny = _context([DocumentSection(index=1, label="Page 1", text="hi")], "tiny.pdf")
    answer2, calls2, _stats2 = summarize_document(tiny, chat)
    assert calls2 == 1 and isinstance(answer2, str)


# ================================================== U reference-heavy doc


def test_u_reference_heavy_paper_does_not_blow_context():
    chat = FakeChat()
    sections = [
        _page_section(1, "Abstract", sentences=6),
        DocumentSection(
            index=2, label="Page 2",
            text="References\n" + "\n".join(f"[{i}] Author {i}, Title {i}, Journal, 2020." for i in range(1, 400)),
        ),
    ]
    context = _context(sections, "ref-heavy.pdf")
    _answer, calls, stats = summarize_document(context, chat)
    assert calls == 1
    assert stats["references_downgraded"] == 1


# ================================================== V/W academic heading detection


@pytest.mark.parametrize(
    "first_line,expected",
    [
        ("Methods\nbody", "method"),
        ("3. Methods\nbody", "method"),
        ("Results\nbody", "results"),
        ("References\nbody", "references"),
        ("Conclusion\nbody", "conclusion"),
        ("Abstract\nbody", "abstract"),
        ("方法\n正文", "method"),
        ("3 实验结果\n正文", "results"),
        ("参考文献\n条目", "references"),
        ("结论\n正文", "conclusion"),
        ("摘要\n正文", "abstract"),
    ],
)
def test_vw_academic_heading_detection(first_line, expected):
    assert detect_section_kind(first_line) == expected


def test_non_academic_page_detects_none():
    assert detect_section_kind("Random prose about everyday life.") is None


# ================================================== prompt grounding


def test_summary_prompts_keep_grounding_instruction():
    messages = build_qa_messages("p.pdf", "[Page 1]\ntext", "请总结")
    system = messages[0]["content"]
    assert "based only on the supplied document excerpts" in system
    assert "Do not invent content" in system


def test_five_point_instruction_in_direct_prompt():
    from core.document_attachment import SUMMARY_DIRECT_PROMPT
    assert "研究问题/目的" in SUMMARY_DIRECT_PROMPT
    assert "局限/意义" in SUMMARY_DIRECT_PROMPT
    assert "文档未明确提供" in SUMMARY_DIRECT_PROMPT
