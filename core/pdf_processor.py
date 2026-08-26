"""PDF processing pipeline — text layer + OCR fallback.

Architecture:
  1. Try text layer extraction via PyMuPDF (fitz)
  2. If text layer is empty/insufficient, fall back to OCR via RapidOCR
  3. For mixed PDFs (some pages with text, some scanned), per-page detection

Dependencies:
  - pymupdf (PyMuPDF) — always available
  - rapidocr_onnxruntime — optional, installed via requirements.txt

Usage:
  >>> processor = PdfProcessor()
  >>> result = await processor.process("report.pdf")
  >>> print(result.pages[0].text)
  >>> print(result.pages[0].image)  # rendered image for OCR
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

log = logging.getLogger("firefly.pdf_processor")


@dataclass(frozen=True)
class PdfPage:
    """A single PDF page with extracted text and optional rendered image."""

    page_index: int
    text: str
    image: bytes | None = None  # rendered PNG for OCR
    width: float = 0.0
    height: float = 0.0
    has_text_layer: bool = True


@dataclass
class PdfResult:
    """Result of processing a PDF document."""

    file_path: str
    total_pages: int
    pages: list[PdfPage] = field(default_factory=list)
    is_scanned: bool = False
    has_text_layer: bool = True
    ocr_used: bool = False

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def text_length(self) -> int:
        return sum(len(p.text) for p in self.pages)


class PdfProcessor:
    """PDF processing with text layer + OCR fallback.

    Uses PyMuPDF for text extraction and rendering.
    Uses RapidOCR as OCR fallback for scanned/image PDFs.
    """

    # Minimum text length to consider a page as having a text layer
    MIN_TEXT_LENGTH = 50

    def __init__(self, use_ocr: bool = True) -> None:
        self._use_ocr = use_ocr
        self._ocr: Any = None
        if use_ocr:
            self._ocr = self._init_ocr()

    def _init_ocr(self) -> Any:
        """Lazy-initialize OCR engine. Returns None if not available."""
        try:
            from rapidocr_onnxruntime import RapidOCR
            return RapidOCR()
        except ImportError:
            log.warning("[PdfProcessor] rapidocr_onnxruntime not available, OCR disabled")
            return None

    def process(self, file_path: str | Path) -> PdfResult:
        """Process a PDF file, extracting text and optionally running OCR.

        Args:
            file_path: Path to the PDF file.

        Returns:
            PdfResult with extracted text per page.
        """
        import fitz  # PyMuPDF

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"PDF not found: {file_path}")

        doc = fitz.open(str(file_path))
        pages: list[PdfPage] = []
        has_text_layer = False
        ocr_used = False

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            text = page.get_text().strip()

            # Check if this page has a usable text layer
            page_has_text = len(text) >= self.MIN_TEXT_LENGTH

            if page_has_text:
                has_text_layer = True
            else:
                # Try OCR fallback
                if self._ocr is not None:
                    # Render page to image
                    mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better OCR
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")

                    # Run OCR
                    try:
                        result, elapse = self._ocr(img_bytes)
                        if result:
                            ocr_text = "\n".join(line[0] for line in result if line)
                            if ocr_text.strip():
                                text = ocr_text
                                ocr_used = True
                    except Exception as exc:
                        log.debug("[PdfProcessor] OCR failed on page %d: %s", page_idx, exc)

            pages.append(PdfPage(
                page_index=page_idx,
                text=text,
                image=img_bytes if not page_has_text and self._ocr else None,
                width=float(page.rect.width),
                height=float(page.rect.height),
                has_text_layer=page_has_text,
            ))

        doc.close()

        is_scanned = not has_text_layer and ocr_used

        return PdfResult(
            file_path=str(file_path),
            total_pages=len(doc) if 'doc' not in locals() else len(pages),
            pages=pages,
            is_scanned=is_scanned,
            has_text_layer=has_text_layer,
            ocr_used=ocr_used,
        )

    def extract_images(self, file_path: str | Path, *, max_count: int = 10) -> list[dict]:
        """Extract images from a PDF page for chart/diagram analysis.

        Returns list of dicts with page_index, image_bytes, and bounding box.
        """
        import fitz

        file_path = Path(file_path)
        doc = fitz.open(str(file_path))
        images: list[dict] = []

        for page_idx in range(min(5, len(doc))):  # First 5 pages only
            page = doc[page_idx]
            image_list = page.get_images(full=True)

            for img_idx, img in enumerate(image_list[:max_count]):
                xref = img[0]
                try:
                    base_image = doc.extract_image(xref)
                    if base_image:
                        images.append({
                            "page_index": page_idx,
                            "image_bytes": base_image["image"],
                            "width": base_image["width"],
                            "height": base_image["height"],
                            "format": base_image["ext"],
                        })
                except Exception:
                    continue

        doc.close()
        return images


__all__ = [
    "PdfProcessor",
    "PdfResult",
    "PdfPage",
]
