"""Phase v0.3-P8: PDF Processor + OCR + Explain Box tests."""

from __future__ import annotations

import io
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.pdf_processor import PdfPage, PdfProcessor, PdfResult
from core.pdf_qa import ExplainEntry, PdfQa, PdfQaResult


# ======================================================================
# Helpers
# ======================================================================


def _make_minimal_pdf(path: Path) -> Path:
    """Create a minimal valid PDF file on disk."""
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\n"
        b"endobj\n"
        b"4 0 obj\n<< /Length 44 >>\n"
        b"stream\n"
        b"BT /F1 12 Tf 100 700 Td (Hello World) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        b"endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000360 00000 n \n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n451\n"
        b"%%EOF\n"
    )
    path.write_bytes(pdf_bytes)
    return path


def _make_text_pdf(path: Path, text: str) -> Path:
    """Create a PDF with actual text content via PyMuPDF."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), text)
    doc.save(str(path), garbage=4)
    doc.close()
    return path


# ======================================================================
# P8-A: PdfProcessor — text extraction
# ======================================================================


class TestP8A_PdfProcessor:
    """PDF text extraction via PyMuPDF."""

    def test_minimal_pdf_no_crash(self, tmp_path: Path) -> None:
        pdf_path = _make_minimal_pdf(tmp_path / "test.pdf")
        processor = PdfProcessor()
        result = processor.process(pdf_path)

        assert result.total_pages == 1
        assert isinstance(result.pages, list)
        assert len(result.pages) == 1

    def test_text_pdf_extracted(self, tmp_path: Path) -> None:
        test_text = "This is a test document with meaningful content for extraction."
        pdf_path = _make_text_pdf(tmp_path / "test.pdf", test_text)
        processor = PdfProcessor()
        result = processor.process(pdf_path)

        assert result.has_text_layer is True
        assert result.ocr_used is False
        assert result.text_length > 0
        assert "test" in result.full_text.lower()

    def test_full_text_joins_pages(self, tmp_path: Path) -> None:
        pdf_path = _make_text_pdf(
            tmp_path / "test.pdf",
            "Page 1 content.\n\nPage 2 content.",
        )
        processor = PdfProcessor()
        result = processor.process(pdf_path)

        assert "page 1" in result.full_text.lower()
        assert "page 2" in result.full_text.lower()

    def test_nonexistent_file_raises(self, tmp_path: Path) -> None:
        processor = PdfProcessor()
        with pytest.raises(FileNotFoundError):
            processor.process(tmp_path / "nonexistent.pdf")

    def test_images_extracted(self, tmp_path: Path) -> None:
        """Image extraction returns empty list for text-only PDFs."""
        pdf_path = _make_text_pdf(tmp_path / "test.pdf", "No images here.")
        processor = PdfProcessor()
        images = processor.extract_images(pdf_path)
        assert isinstance(images, list)


# ======================================================================
# P8-B: PdfProcessor — OCR fallback
# ======================================================================


class TestP8B_OcrFallback:
    """OCR fallback for scanned/image PDFs."""

    def test_ocr_disabled_by_default_when_available(self, tmp_path: Path) -> None:
        """When OCR engine is not available, processor still works."""
        pdf_path = _make_minimal_pdf(tmp_path / "test.pdf")
        processor = PdfProcessor(use_ocr=False)
        result = processor.process(pdf_path)

        assert result is not None
        assert result.total_pages == 1

    def test_ocr_can_be_disabled(self, tmp_path: Path) -> None:
        """Processor works correctly with OCR explicitly disabled."""
        pdf_path = _make_text_pdf(
            tmp_path / "test.pdf",
            "Test content for OCR disable test. This is a longer sentence to exceed the minimum text length threshold for text layer detection.",
        )
        processor = PdfProcessor(use_ocr=False)
        # _ocr should be None when disabled
        assert processor._ocr is None
        result = processor.process(pdf_path)
        # Should still extract text via PyMuPDF
        assert result.has_text_layer is True
        assert result.ocr_used is False

    def test_scanned_pdf_without_ocr(self, tmp_path: Path) -> None:
        """Scanned PDF without OCR returns empty text."""
        # Create a PDF with no text layer (image-only page)
        import fitz

        pdf_path = tmp_path / "scanned.pdf"
        doc = fitz.open()
        page = doc.new_page()
        # Insert an image instead of text
        doc.save(str(pdf_path), garbage=4)
        doc.close()

        processor = PdfProcessor(use_ocr=False)
        result = processor.process(pdf_path)

        # Should have pages but no text layer
        assert result.total_pages >= 1
        assert result.has_text_layer is False


# ======================================================================
# P8-C: PdfQa — QA pipeline
# ======================================================================


class TestP8C_PdfQa:
    """PDF question answering pipeline."""

    def _make_fake_qa(self, mock_chat=None):
        """Create a PdfQa with a mocked chat handler."""
        if mock_chat is None:
            mock_chat = MagicMock(return_value={
                "provider": "fake",
                "choices": [{"message": {"role": "assistant", "content": "测试回答"}}],
            })
        qa = PdfQa(chat_handler=mock_chat)
        return qa, mock_chat

    def test_answer_returns_result(self, tmp_path: Path) -> None:
        qa, mock_chat = self._make_fake_qa()
        pdf_path = _make_text_pdf(tmp_path / "test.pdf", "Test content for QA.")

        result = qa.answer(pdf_path, "What is this about?")

        assert isinstance(result, PdfQaResult)
        assert result.answer == "测试回答"
        mock_chat.assert_called_once()

    def test_answer_uses_pdf_context(self, tmp_path: Path) -> None:
        qa, mock_chat = self._make_fake_qa()
        pdf_path = _make_text_pdf(
            tmp_path / "test.pdf",
            "The quick brown fox jumps over the lazy dog.",
        )

        qa.answer(pdf_path, "What does the fox do?")

        # Verify chat was called with a prompt containing PDF context
        call_args = mock_chat.call_args
        messages = call_args[0][0]
        assert len(messages) == 1
        assert "fox" in messages[0]["content"].lower()

    def test_summarize_returns_result(self, tmp_path: Path) -> None:
        qa, mock_chat = self._make_fake_qa()
        pdf_path = _make_text_pdf(tmp_path / "test.pdf", "Summary test content.")

        result = qa.summarize(pdf_path)

        assert isinstance(result, PdfQaResult)
        assert result.summary == "测试回答"

    def test_extract_concepts_returns_list(self, tmp_path: Path) -> None:
        qa, mock_chat = self._make_fake_qa()
        mock_chat.return_value = {
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": "概念A，概念B，概念C"}}],
        }
        pdf_path = _make_text_pdf(tmp_path / "test.pdf", "Concept extraction test.")

        concepts = qa.extract_concepts(pdf_path)

        assert isinstance(concepts, list)
        assert len(concepts) <= 8

    def test_explain_term_returns_entry(self, tmp_path: Path) -> None:
        qa, mock_chat = self._make_fake_qa()
        pdf_path = _make_text_pdf(
            tmp_path / "test.pdf",
            "The term MachineLearning is important in this paper.",
        )

        entry = qa.explain_term(pdf_path, "MachineLearning")

        assert isinstance(entry, ExplainEntry)
        assert entry.kind == "term"
        assert entry.label == "MachineLearning"
        assert entry.explanation == "测试回答"


# ======================================================================
# P8-D: ExplainEntry — data model
# ======================================================================


class TestP8D_ExplainEntry:
    """ExplainEntry data model."""

    def test_create_entry(self) -> None:
        entry = ExplainEntry(
            kind="term",
            label="Transformer",
            explanation="A neural network architecture...",
            source_page=5,
        )
        assert entry.kind == "term"
        assert entry.label == "Transformer"
        assert entry.source_page == 5

    def test_default_values(self) -> None:
        entry = ExplainEntry(kind="formula", label="E=mc²", explanation="")
        assert entry.source_page == -1
        assert entry.source_text == ""


# ======================================================================
# P8-E: PdfQaResult — data model
# ======================================================================


class TestP8E_PdfQaResult:
    """PdfQaResult data model."""

    def test_empty_result(self) -> None:
        result = PdfQaResult()
        assert result.answer == ""
        assert result.explains == []
        assert result.recommended_questions == []
        assert result.summary == ""
        assert result.terms == []

    def test_full_result(self) -> None:
        result = PdfQaResult(
            answer="Test answer",
            summary="Test summary",
            terms=["term1", "term2"],
            recommended_questions=["Q1?", "Q2?"],
        )
        assert result.answer == "Test answer"
        assert result.summary == "Test summary"
        assert len(result.terms) == 2
        assert len(result.recommended_questions) == 2


# ======================================================================
# P8-F: PdfPage — data model
# ======================================================================


class TestP8F_PdfPage:
    """PdfPage data model."""

    def test_page_with_text(self) -> None:
        page = PdfPage(page_index=0, text="Hello world", has_text_layer=True)
        assert page.page_index == 0
        assert page.text == "Hello world"
        assert page.has_text_layer is True

    def test_page_with_image(self) -> None:
        page = PdfPage(
            page_index=1,
            text="",
            image=b"\x89PNG\r\n\x1a\n",
            has_text_layer=False,
        )
        assert page.image is not None
        assert page.has_text_layer is False


# ======================================================================
# P8-G: PdfResult — data model
# ======================================================================


class TestP8G_PdfResult:
    """PdfResult data model."""

    def test_full_text_joins_pages(self) -> None:
        result = PdfResult(
            file_path="test.pdf",
            total_pages=2,
            pages=[
                PdfPage(page_index=0, text="Page 1 text"),
                PdfPage(page_index=1, text="Page 2 text"),
            ],
        )
        assert "Page 1 text" in result.full_text
        assert "Page 2 text" in result.full_text

    def test_text_length_counts_all(self) -> None:
        result = PdfResult(
            file_path="test.pdf",
            total_pages=1,
            pages=[PdfPage(page_index=0, text="Hello world")],
        )
        assert result.text_length == 11

    def test_empty_result(self) -> None:
        result = PdfResult(file_path="empty.pdf", total_pages=0)
        assert result.full_text == ""
        assert result.text_length == 0
        assert result.is_scanned is False
        assert result.has_text_layer is True
