"""Companion Document Attachment v1 — parsing, chunking and retrieval.

Documents are parsed LOCALLY and only relevant excerpts ever leave the
machine. No raw document bytes, no absolute path and no full extracted text
are persisted anywhere: the conversation history keeps only a safe
``[Document attachment: <name>]`` placeholder plus the user's question.

Pipeline for one turn:
    user file -> local parser -> DocumentContext (sections) -> chunks
    -> direct page/slide lookup OR BM25 retrieval -> text excerpts
    -> text provider (core.ai_router) -> Companion answer

PDF parsing reuses the existing REAL PASS pipeline
(``core.pdf_processor.PdfProcessor``: PyMuPDF native text layer + RapidOCR
fallback for scanned pages). DOCX / PPTX / XLSX use python-docx /
python-pptx / openpyxl; TXT / MD / CSV use the standard library.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- models


@dataclass
class DocumentSection:
    """One addressable slice of a document (page / slide / sheet / section)."""

    index: int
    label: str  # "Page 12" / "Slide 8" / "Sheet: Data" / "Section 3"
    text: str


@dataclass
class DocumentContext:
    """Fully parsed, in-memory document content. Never persisted."""

    filename: str
    kind: str
    sections: list[DocumentSection] = field(default_factory=list)
    total_characters: int = 0
    page_count: int | None = None
    slide_count: int | None = None
    sheet_count: int | None = None
    truncated: bool = False
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class DocumentChunk:
    """A retrieval unit tagged with its source label."""

    index: int
    label: str
    text: str
    section_index: int


# ---------------------------------------------------------------- limits

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_XLSX_BYTES = 30 * 1024 * 1024

DOCUMENT_KIND_LIMITS: dict[str, int] = {
    "pdf": MAX_DOCUMENT_BYTES,
    "docx": MAX_DOCUMENT_BYTES,
    "pptx": MAX_DOCUMENT_BYTES,
    "txt": MAX_TEXT_BYTES,
    "md": MAX_TEXT_BYTES,
    "csv": MAX_TEXT_BYTES,
    "xlsx": MAX_XLSX_BYTES,
}

MAX_PDF_PAGES = 500
CHUNK_SIZE = 1200          # ~chars per chunk (task: 1000-2000)
CHUNK_OVERLAP = 100
SUMMARY_DIRECT_BUDGET = 12000   # single-call summary below this size
SUMMARY_GROUP_SIZE = 6000       # map-reduce group size
RETRIEVAL_TOP_K = 6

MAX_XLSX_SHEET_ROWS = 2000
MAX_XLSX_CELLS = 100_000
MAX_CSV_ROWS = 5000

KIND_LABELS = {
    "pdf": "PDF", "docx": "DOCX", "pptx": "PPTX",
    "txt": "TXT", "md": "Markdown", "xlsx": "XLSX", "csv": "CSV",
}

# ---------------------------------------------------------------- errors


class DocumentParseError(Exception):
    """User-facing document parse failure (corrupt / encrypted / unsupported)."""


class EncryptedPdfError(DocumentParseError):
    """The PDF requires a password."""


# ---------------------------------------------------------------- dispatch


def parse_document_bytes(data: bytes, kind: str, filename: str) -> DocumentContext:
    """Parse one in-memory document into a :class:`DocumentContext`."""
    if kind == "pdf":
        return _parse_pdf(data, filename)
    if kind == "docx":
        return _parse_docx(data, filename)
    if kind == "pptx":
        return _parse_pptx(data, filename)
    if kind == "txt" or kind == "md":
        return _parse_text(data, filename, kind)
    if kind == "csv":
        return _parse_csv(data, filename)
    if kind == "xlsx":
        return _parse_xlsx(data, filename)
    raise DocumentParseError(f"unsupported document kind: {kind}")


# ---------------------------------------------------------------- PDF


def _parse_pdf(data: bytes, filename: str) -> DocumentContext:
    from core.pdf_processor import PdfEncryptedError, PdfProcessor

    try:
        result = PdfProcessor().process_stream(data, filename, max_pages=MAX_PDF_PAGES)
    except PdfEncryptedError as exc:
        raise EncryptedPdfError("PDF 需要密码") from exc
    except Exception as exc:
        raise DocumentParseError("无法读取 PDF") from exc

    sections = [
        DocumentSection(index=page.page_index + 1, label=f"Page {page.page_index + 1}", text=page.text)
        for page in result.pages
    ]
    ocr_errors = [err for err in result.errors if "OCR" in err or "ocr" in err.lower()]
    warnings = list(result.errors)
    if result.ocr_used:
        warnings.insert(0, "部分页面通过 OCR 识别")
    elif result.ocr_attempted:
        warnings.insert(0, "部分页面尝试 OCR 识别但未获得文本")
    context = DocumentContext(
        filename=filename,
        kind="pdf",
        sections=sections,
        total_characters=sum(len(s.text) for s in sections),
        page_count=len(sections),
        truncated=bool(result.errors and "truncated" in result.errors[-1]),
        parse_warnings=warnings,
    )
    return context


# ---------------------------------------------------------------- DOCX


def _parse_docx(data: bytes, filename: str) -> DocumentContext:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - python-docx is a dependency
        raise DocumentParseError("DOCX 解析库不可用") from exc

    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        raise DocumentParseError("无法读取 DOCX") from exc

    sections: list[DocumentSection] = []
    para_index = 0
    table_index = 0
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = (para.text or "").strip()
            if not text:
                continue
            para_index += 1
            label = f"Section {para_index}"
            sections.append(DocumentSection(index=para_index, label=label, text=text))
        elif child.tag == qn("w:tbl"):
            try:
                table = Table(child, doc)
            except Exception:
                continue
            table_index += 1
            rows: list[str] = []
            for row in table.rows:
                cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
                rows.append(" | ".join(cells))
            label = f"Table {table_index}"
            sections.append(DocumentSection(
                index=len(sections) + 1, label=label, text=f"[{label}]\n" + "\n".join(rows)
            ))

    return DocumentContext(
        filename=filename,
        kind="docx",
        sections=sections,
        total_characters=sum(len(s.text) for s in sections),
    )


# ---------------------------------------------------------------- PPTX


def _parse_pptx(data: bytes, filename: str) -> DocumentContext:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover - python-pptx is a dependency
        raise DocumentParseError("PPTX 解析库不可用") from exc

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise DocumentParseError("无法读取 PPTX") from exc

    sections: list[DocumentSection] = []
    for slide_index, slide in enumerate(prs.slides, 1):
        parts: list[str] = []
        title_shape = slide.shapes.title
        title_text = ""
        if title_shape is not None:
            title_text = (title_shape.text or "").strip()
            if title_text:
                parts.append(title_text)
        for shape in slide.shapes:
            if shape is title_shape:
                continue
            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    parts.append(text)
            elif getattr(shape, "has_table", False):
                rows: list[str] = []
                for row in shape.table.rows:
                    cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
                    rows.append(" | ".join(cells))
                if rows:
                    parts.append("[Table]\n" + "\n".join(rows))
        try:
            notes_text = slide.notes_slide.notes_text_frame.text.strip()
            if notes_text:
                parts.append(f"[Notes]\n{notes_text}")
        except Exception:
            pass
        text = "\n".join(p for p in parts if p)
        sections.append(DocumentSection(index=slide_index, label=f"Slide {slide_index}", text=text))

    return DocumentContext(
        filename=filename,
        kind="pptx",
        sections=sections,
        total_characters=sum(len(s.text) for s in sections),
        slide_count=len(sections),
    )


# ---------------------------------------------------------------- TXT / MD


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _parse_text(data: bytes, filename: str, kind: str) -> DocumentContext:
    text = _decode_text(data)
    sections = [DocumentSection(index=1, label="Document", text=text)] if text.strip() else []
    return DocumentContext(
        filename=filename,
        kind=kind,
        sections=sections,
        total_characters=len(text),
    )


# ---------------------------------------------------------------- CSV


def _parse_csv(data: bytes, filename: str) -> DocumentContext:
    text = _decode_text(data)
    rows: list[str] = []
    truncated = False
    try:
        reader = csv.reader(io.StringIO(text))
        for index, row in enumerate(reader):
            if index >= MAX_CSV_ROWS:
                truncated = True
                break
            rows.append(" | ".join((cell or "").strip() for cell in row))
    except Exception as exc:
        raise DocumentParseError("无法读取 CSV") from exc
    sections = [DocumentSection(index=1, label="CSV", text="\n".join(rows))]
    return DocumentContext(
        filename=filename,
        kind="csv",
        sections=sections,
        total_characters=sum(len(r) for r in rows),
        truncated=truncated,
        parse_warnings=["内容超过上限已截断"] if truncated else [],
    )


# ---------------------------------------------------------------- XLSX


def _parse_xlsx(data: bytes, filename: str) -> DocumentContext:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is a dependency
        raise DocumentParseError("XLSX 解析库不可用") from exc

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise DocumentParseError("无法读取 XLSX") from exc

    sections: list[DocumentSection] = []
    total_cells = 0
    truncated = False
    try:
        for worksheet in workbook.worksheets:
            rows: list[str] = []
            row_count = 0
            for row in worksheet.iter_rows(values_only=True):
                cells = ["" if value is None else str(value) for value in row]
                rows.append(" | ".join(cells))
                row_count += 1
                total_cells += len(cells)
                if row_count >= MAX_XLSX_SHEET_ROWS or total_cells >= MAX_XLSX_CELLS:
                    truncated = True
                    break
            label = f"Sheet: {worksheet.title}"
            sections.append(DocumentSection(
                index=len(sections) + 1, label=label, text="\n".join(rows)
            ))
    finally:
        workbook.close()

    return DocumentContext(
        filename=filename,
        kind="xlsx",
        sections=sections,
        total_characters=sum(len(s.text) for s in sections),
        sheet_count=len(sections),
        truncated=truncated,
        parse_warnings=["表格内容超过上限已截断"] if truncated else [],
    )


# ---------------------------------------------------------------- chunking


def chunk_document(
    context: DocumentContext,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[DocumentChunk]:
    """Split every section into source-labelled chunks of ~``chunk_size``."""
    chunks: list[DocumentChunk] = []
    for section in context.sections:
        text = (section.text or "").strip()
        if not text:
            continue
        start = 0
        guard = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(DocumentChunk(
                index=len(chunks),
                label=section.label,
                text=text[start:end],
                section_index=section.index,
            ))
            if end == len(text):
                break
            start = max(start + chunk_size - overlap, start + 1)
            guard += 1
            if guard > 2000:  # hard safety net for degenerate inputs
                break
    return chunks


# ---------------------------------------------------------------- retrieval


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_STOP_WORDS = {
    "the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "is",
    "are", "was", "were", "this", "that", "it", "with", "as", "at", "by",
    "from", "what", "how", "why", "do", "does", "did", "page", "slide",
}


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if word not in _STOP_WORDS:
            tokens.append(word)
    # CJK unigrams carry the meaning for Chinese questions.
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            tokens.append(char)
    return tokens


def bm25_scores(
    query_terms: Sequence[str],
    chunk_terms: Sequence[Sequence[str]],
) -> list[float]:
    """Lightweight BM25 (k1=1.5, b=0.75) over in-memory chunks. No index."""
    n_chunks = len(chunk_terms)
    if n_chunks == 0 or not query_terms:
        return [0.0] * n_chunks
    doc_lengths = [max(len(terms), 1) for terms in chunk_terms]
    average_length = sum(doc_lengths) / n_chunks

    document_frequency: dict[str, int] = {}
    for terms in chunk_terms:
        for term in set(terms):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    k1, b = 1.5, 0.75
    scores: list[float] = []
    idf_cache: dict[str, float] = {}
    for index, terms in enumerate(chunk_terms):
        term_counts: dict[str, int] = {}
        for term in terms:
            term_counts[term] = term_counts.get(term, 0) + 1
        score = 0.0
        for term in query_terms:
            if term not in term_counts:
                continue
            if term not in idf_cache:
                df = document_frequency.get(term, 0)
                idf_cache[term] = math.log(
                    1 + (n_chunks - df + 0.5) / (df + 0.5)
                ) if df else 0.0
            tf = term_counts[term]
            denom = tf + k1 * (1 - b + b * doc_lengths[index] / average_length)
            score += idf_cache[term] * (tf * (k1 + 1)) / max(denom, 1e-9)
        scores.append(score)
    return scores


def retrieve_chunks(question: str, chunks: list[DocumentChunk], top_k: int = RETRIEVAL_TOP_K) -> list[DocumentChunk]:
    """Return the most relevant chunks for a question (BM25 over chunks)."""
    if not chunks:
        return []
    query_terms = _tokenize(question)
    if not query_terms:
        return chunks[:top_k]
    scored = sorted(
        zip(chunks, bm25_scores(query_terms, [_tokenize(c.text) for c in chunks])),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return [chunk for chunk, _score in scored[:top_k] if _score > 0] or chunks[:1]


# ---------------------------------------------------------------- direct lookup

_PAGE_CN_RE = re.compile(r"第\s*(\d+)\s*页")
_PAGE_EN_RE = re.compile(r"\bpage\s*(\d+)\b", re.IGNORECASE)
_SLIDE_RE = re.compile(r"\bslide\s*(\d+)\b", re.IGNORECASE)
_SHEET_RE = re.compile(r"sheet\s*[::：]?\s*[\"']?([^\"'\s，。]+)", re.IGNORECASE)


def direct_section_lookup(question: str, context: DocumentContext) -> list[DocumentSection]:
    """Deterministic page / slide / sheet lookup, no LLM intent classifier."""
    normalized = question.strip().lower()
    targets: list[str] = []

    match = _PAGE_CN_RE.search(question) or _PAGE_EN_RE.search(question)
    if match:
        number = int(match.group(1))
        if context.kind == "pptx":
            targets.append(f"Slide {number}")
        else:
            targets.append(f"Page {number}")

    match = _SLIDE_RE.search(normalized)
    if match and context.kind == "pptx":
        targets.append(f"Slide {int(match.group(1))}")

    match = _SHEET_RE.search(normalized)
    if match and context.kind == "xlsx":
        targets.append(f"Sheet: {match.group(1)}")

    if not targets:
        return []
    return [section for section in context.sections if section.label in targets]


def format_excerpts(sections_or_chunks: Sequence[Any], *, label_attr: str = "label", text_attr: str = "text") -> str:
    """Render retrieval units as the bounded SOURCE EXCERPTS block."""
    blocks = []
    for unit in sections_or_chunks:
        text = (getattr(unit, text_attr) or "").strip()
        if not text:
            continue
        blocks.append(f"[{getattr(unit, label_attr)}]\n{text}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- summary intent


_SUMMARY_KEYWORDS = (
    "总结", "概述", "摘要", "概括", "要点", "主要内容", "整体逻辑",
    "讲了什么", "讲了些什么", "写论文笔记", "逐页讲", "详细总结",
    "summary", "summarize", "overview", "main points", "主要内容是什么",
)


_PAGE_OR_SLIDE_HINT_RE = re.compile(
    r"第\s*\d+\s*页|\bpage\s*\d+\b|\bslide\s*\d+\b", re.IGNORECASE
)


def is_summary_request(question: str) -> bool:
    lowered = (question or "").strip().lower()
    if not lowered:
        return False
    # "第 6 页讲了什么" is a page-specific question, never a full summary.
    if _PAGE_OR_SLIDE_HINT_RE.search(lowered):
        return False
    return any(keyword in lowered for keyword in _SUMMARY_KEYWORDS)


# ---------------------------------------------------------------- provider messages


DOCUMENT_PERSONA = """You are Firefly, the user's desktop companion.
Answer based only on the supplied document excerpts.
If the excerpts do not support the answer, say so.
Do not invent content.
Be warm and concise, normally 2-5 sentences.
Refer to pages/slides naturally when the content comes from a clear source."""

SUMMARY_MAP_PROMPT = (
    "你是 Firefly，用户的桌面伙伴。请阅读以下文档片段，用简洁要点概括其内容。"
    "保留重要的页/幻灯片来源信息，不要编造片段中没有的内容。"
)


def build_qa_messages(
    filename: str,
    excerpts: str,
    question: str,
    *,
    persona: str = DOCUMENT_PERSONA,
) -> list[dict[str, str]]:
    """Bounded model payload: document name + relevant excerpts + question.

    Never contains raw file bytes, the absolute path, memory, or history.
    """
    user = (
        f"DOCUMENT:\n{filename}\n\n"
        f"SOURCE EXCERPTS:\n\n{excerpts}\n\n"
        f"USER QUESTION:\n{question}"
    )
    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": user},
    ]


def _group_chunks(chunks: list[DocumentChunk], group_size: int = SUMMARY_GROUP_SIZE) -> list[list[DocumentChunk]]:
    groups: list[list[DocumentChunk]] = []
    current: list[DocumentChunk] = []
    current_chars = 0
    for chunk in chunks:
        if current and current_chars + len(chunk.text) > group_size:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(chunk)
        current_chars += len(chunk.text)
    if current:
        groups.append(current)
    return groups


def hierarchical_summary(
    context: DocumentContext,
    chat: Any,
    *,
    direct_budget: int = SUMMARY_DIRECT_BUDGET,
    max_groups: int = 8,
) -> tuple[str, int]:
    """Summarize a whole document; remote calls stay bounded.

    Small documents (<= ``direct_budget`` chars) use exactly one call. Larger
    documents run a map-reduce: at most ``max_groups`` local summaries, then
    one combining summary.
    """
    if context.total_characters <= direct_budget:
        excerpts = format_excerpts(context.sections)
        messages = build_qa_messages(
            context.filename, excerpts, "请总结这个文档的主要内容。"
        )
        answer = _chat_content(chat(messages, temperature=0.3))
        return answer, 1

    chunks = chunk_document(context)
    groups = _group_chunks(chunks)
    if len(groups) > max_groups:
        merged: list[list[DocumentChunk]] = []
        for group in groups:
            if merged and len(merged[-1]) + len(group) <= max_groups + 1:
                merged[-1].extend(group)
            else:
                merged.append(list(group))
        groups = merged

    partial_summaries: list[str] = []
    for group in groups:
        excerpts = format_excerpts(group)
        messages = [
            {"role": "system", "content": DOCUMENT_PERSONA},
            {"role": "user", "content": f"{SUMMARY_MAP_PROMPT}\n\n{excerpts}"},
        ]
        partial_summaries.append(_chat_content(chat(messages, temperature=0.3)))
    combined = "\n\n".join(partial_summaries)
    final_messages = [
        {"role": "system", "content": DOCUMENT_PERSONA},
        {"role": "user", "content": (
            "以下是同一文档各部分的分段摘要，请合并为一份完整、连贯的中文总结，"
            "突出整体逻辑与要点。\n\n" + combined
        )},
    ]
    final = _chat_content(chat(final_messages, temperature=0.3))
    return final, 1 + len(groups)


def _chat_content(response: Any) -> str:
    """Extract the assistant text from an OpenAI-compatible completion dict."""
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content.strip() if isinstance(content, str) else ""


__all__ = [
    "DocumentSection",
    "DocumentContext",
    "DocumentChunk",
    "DocumentParseError",
    "EncryptedPdfError",
    "parse_document_bytes",
    "chunk_document",
    "retrieve_chunks",
    "bm25_scores",
    "direct_section_lookup",
    "format_excerpts",
    "is_summary_request",
    "build_qa_messages",
    "hierarchical_summary",
    "DOCUMENT_PERSONA",
    "DOCUMENT_KIND_LIMITS",
    "MAX_PDF_PAGES",
]
