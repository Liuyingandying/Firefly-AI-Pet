"""Real local OCR dogfood tests using generated image-only PDFs."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

from core.pdf_processor import PdfProcessor
from core.pdf_qa import ExternalProviderConsentRequired, PdfQa


EXPECTED_ENGLISH = "FIREFLY OCR TEST"
EXPECTED_ENGLISH_TOKENS = {"FIREFLY", "OCR", "TEST"}
EXPECTED_CHINESE = "中文识别测试"


def _font(size: int = 64) -> ImageFont.FreeTypeFont:
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("Microsoft YaHei is required for the Windows OCR dogfood")
    return ImageFont.truetype(str(font_path), size)


def _scan_png() -> bytes:
    # Wider image to prevent OCR line-segmentation artifacts where spaces
    # between words are misread as letters (e.g. "TEST" → "RTEST").
    image = Image.new("RGB", (1600, 420), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 70), EXPECTED_ENGLISH, font=font, fill="black")
    draw.text((60, 210), EXPECTED_CHINESE, font=font, fill="black")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _make_scanned_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=800, height=250)
    page.insert_image(page.rect, stream=_scan_png())
    doc.save(str(path), garbage=4)
    doc.close()
    return path


def _make_text_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "TEXT LAYER ONLY - OCR MUST NOT RUN")
    doc.save(str(path), garbage=4)
    doc.close()
    return path


def _make_mixed_pdf(path: Path) -> Path:
    doc = fitz.open()
    text_page = doc.new_page()
    text_page.insert_text((72, 72), "MIXED PDF TEXT PAGE")
    scan_page = doc.new_page(width=800, height=250)
    scan_page.insert_image(scan_page.rect, stream=_scan_png())
    doc.save(str(path), garbage=4)
    doc.close()
    return path


def test_real_scanned_pdf_uses_rapidocr(tmp_path: Path) -> None:
    pdf_path = _make_scanned_pdf(tmp_path / "scanned.pdf")
    with fitz.open(str(pdf_path)) as raw:
        assert raw[0].get_text().strip() == ""

    result = PdfProcessor().process(pdf_path)

    assert result.total_pages == 1
    assert result.is_scanned is True
    assert result.has_text_layer is False
    assert result.ocr_attempted is True
    assert result.ocr_used is True
    assert result.ocr_backend is not None
    assert result.ocr_backend.startswith("rapidocr")
    assert result.errors == []
    assert result.pages[0].page_index == 0
    assert result.pages[0].extraction_source == "ocr"
    # Token-level check: OCR may split on layout/line-segmentation boundaries.
    ocr_upper = result.pages[0].text.upper()
    for token in EXPECTED_ENGLISH_TOKENS:
        assert token in ocr_upper, (
            f"OCR output {ocr_upper!r} missing English token {token!r}"
        )
    assert EXPECTED_CHINESE in result.pages[0].text


def test_text_pdf_does_not_call_ocr(tmp_path: Path) -> None:
    result = PdfProcessor().process(_make_text_pdf(tmp_path / "text.pdf"))

    assert result.has_text_layer is True
    assert result.ocr_attempted is False
    assert result.ocr_used is False
    assert result.pages[0].extraction_source == "text_layer"


def test_mixed_pdf_uses_text_then_real_ocr(tmp_path: Path) -> None:
    result = PdfProcessor().process(_make_mixed_pdf(tmp_path / "mixed.pdf"))

    assert result.total_pages == 2
    assert result.has_text_layer is True
    assert result.is_scanned is False
    assert result.ocr_attempted is True
    assert result.ocr_used is True
    assert [page.extraction_source for page in result.pages] == ["text_layer", "ocr"]
    assert "MIXED PDF TEXT PAGE" in result.pages[0].text
    # Token-level check for scanned page (same rationale as scanned test).
    ocr_upper = result.pages[1].text.upper()
    for token in EXPECTED_ENGLISH_TOKENS:
        assert token in ocr_upper, (
            f"OCR output {ocr_upper!r} missing English token {token!r}"
        )
    assert EXPECTED_CHINESE in result.pages[1].text


def test_selected_text_requires_explicit_external_consent() -> None:
    chat = MagicMock()
    qa = PdfQa(chat_handler=chat)

    with pytest.raises(ExternalProviderConsentRequired):
        qa.explain_selection(EXPECTED_CHINESE, source_page=1)

    chat.assert_not_called()


def test_selected_text_sends_only_the_selection_after_consent() -> None:
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "这是一次真实 OCR 选中文本解释。"}}],
    })
    qa = PdfQa(chat_handler=chat)

    entry = qa.explain_selection(
        EXPECTED_CHINESE,
        source_page=1,
        consent=True,
    )

    prompt = chat.call_args.args[0][0]["content"]
    assert EXPECTED_CHINESE in prompt
    assert EXPECTED_ENGLISH not in prompt
    assert entry.source_page == 1
    assert entry.source_text == EXPECTED_CHINESE
    assert entry.explanation
