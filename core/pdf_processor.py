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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

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
    extraction_source: str = "text_layer"
    ocr_attempted: bool = False
    ocr_succeeded: bool = False
    ocr_error: str | None = None


@dataclass
class PdfResult:
    """Result of processing a PDF document."""

    file_path: str
    total_pages: int
    pages: list[PdfPage] = field(default_factory=list)
    is_scanned: bool = False
    has_text_layer: bool = True
    ocr_used: bool = False
    ocr_attempted: bool = False
    ocr_backend: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def text_length(self) -> int:
        return sum(len(p.text) for p in self.pages)


class RapidOcrBackend:
    """Small compatibility wrapper around the installed RapidOCR API."""

    def __init__(self) -> None:
        self.engine: Any = None
        self.name: str | None = None
        self.version: str | None = None
        self.error: str | None = None
        try:
            from rapidocr import RapidOCR

            self.engine = RapidOCR()
            self.name = "rapidocr"
            self.version = _package_version("rapidocr")
            return
        except (ImportError, OSError, RuntimeError) as exc:
            self.error = f"rapidocr initialization failed: {type(exc).__name__}: {exc}"

        try:
            from rapidocr_onnxruntime import RapidOCR

            self.engine = RapidOCR()
            self.name = "rapidocr_onnxruntime"
            self.version = _package_version("rapidocr-onnxruntime")
            self.error = None
        except (ImportError, OSError, RuntimeError) as exc:
            self.error = (
                "RapidOCR backend unavailable: "
                f"{type(exc).__name__}: {exc}"
            )

    @property
    def available(self) -> bool:
        return self.engine is not None

    @property
    def label(self) -> str | None:
        if self.name is None:
            return None
        return f"{self.name}=={self.version}" if self.version else self.name

    def extract_text(self, image_bytes: bytes) -> str:
        if self.engine is None:
            raise RuntimeError(self.error or "RapidOCR backend unavailable")
        return parse_rapidocr_text(self.engine(image_bytes))


def parse_rapidocr_text(raw_result: Any) -> str:
    """Extract recognized strings from current and legacy RapidOCR outputs."""
    if raw_result is None:
        return ""

    modern_texts = getattr(raw_result, "txts", None)
    if modern_texts is not None:
        return "\n".join(
            text.strip() for text in modern_texts
            if isinstance(text, str) and text.strip()
        )

    lines = raw_result
    if isinstance(raw_result, tuple) and len(raw_result) == 2:
        lines = raw_result[0]
    if not lines:
        return ""
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
        raise ValueError("RapidOCR returned an unsupported result type")

    texts: list[str] = []
    for line in lines:
        # rapidocr_onnxruntime 1.x: [box, text, score]
        if isinstance(line, Sequence) and not isinstance(line, (str, bytes)):
            if len(line) >= 2 and isinstance(line[1], str) and line[1].strip():
                texts.append(line[1].strip())
    return "\n".join(texts)


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


class PdfEncryptedError(ValueError):
    """The PDF is password-protected and cannot be read without a password."""


class PdfProcessor:
    """PDF processing with text layer + OCR fallback.

    Uses PyMuPDF for text extraction and rendering.
    Uses RapidOCR as OCR fallback for scanned/image PDFs.
    """

    def __init__(
        self,
        use_ocr: bool = True,
        *,
        ocr_backend: RapidOcrBackend | None = None,
    ) -> None:
        self._use_ocr = use_ocr
        self._ocr_backend: RapidOcrBackend | None = None
        self._ocr: Any = None
        self._ocr_init_error: str | None = None
        if use_ocr:
            self._ocr_backend = ocr_backend or RapidOcrBackend()
            if self._ocr_backend.available:
                self._ocr = self._ocr_backend
            else:
                self._ocr_init_error = self._ocr_backend.error
                log.warning("[PdfProcessor] %s", self._ocr_init_error)

    def process(self, file_path: str | Path) -> PdfResult:
        """Process a PDF file, extracting text and optionally running OCR.

        Args:
            file_path: Path to the PDF file.

        Returns:
            PdfResult with extracted text per page.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"PDF not found: {file_path}")

        import fitz  # PyMuPDF

        doc = fitz.open(str(file_path))
        return self._process_open_doc(doc, str(file_path))

    def process_stream(
        self,
        data: bytes,
        display_name: str = "document.pdf",
        *,
        max_pages: int | None = None,
        max_ocr_pages: int | None = None,
        keep_page_images: bool = True,
    ) -> PdfResult:
        """Process PDF bytes in memory (no temp file, original untouched).

        Shares the exact native-text + OCR fallback pipeline with
        :meth:`process`; the document bytes never touch disk. ``max_pages``
        bounds native-text pages; ``max_ocr_pages`` bounds how many scanned
        pages get OCR (the rest are skipped with a recorded warning).
        ``keep_page_images=False`` drops each OCR page's PNG after recognition
        so fully-scanned documents do not accumulate hundreds of page bitmaps.
        """
        if not data:
            raise ValueError("PDF stream is empty")
        import fitz  # PyMuPDF

        doc = fitz.open(stream=data, filetype="pdf")
        return self._process_open_doc(
            doc, str(display_name), max_pages=max_pages, max_ocr_pages=max_ocr_pages,
            keep_page_images=keep_page_images,
        )

    def _process_open_doc(
        self, doc, label: str, *, max_pages: int | None = None,
        max_ocr_pages: int | None = None, keep_page_images: bool = True,
    ) -> PdfResult:
        import fitz  # PyMuPDF (used for OCR page rendering)

        pages: list[PdfPage] = []
        has_text_layer = False
        ocr_used = False
        ocr_attempted = False
        errors: list[str] = []
        truncated = False
        ocr_pages_used = 0

        try:
            if doc.needs_pass:
                raise PdfEncryptedError("PDF requires a password")
            total = len(doc)
            if max_pages is not None and total > max_pages:
                total = max_pages
                truncated = True
            for page_idx in range(total):
                page = doc[page_idx]
                text = page.get_text().strip()
                page_has_text = bool(text)
                img_bytes: bytes | None = None
                page_ocr_attempted = False
                page_ocr_succeeded = False
                page_ocr_error: str | None = None
                extraction_source = "text_layer" if page_has_text else "empty"

                if page_has_text:
                    has_text_layer = True
                elif self._use_ocr:
                    if max_ocr_pages is not None and ocr_pages_used >= max_ocr_pages:
                        page_ocr_error = "OCR skipped (page budget)"
                    elif self._ocr is None:
                        page_ocr_error = self._ocr_init_error or "RapidOCR backend unavailable"
                    else:
                        mat = fitz.Matrix(2.0, 2.0)
                        pix = page.get_pixmap(matrix=mat, alpha=False)
                        img_bytes = pix.tobytes("png")
                        page_ocr_attempted = True
                        ocr_attempted = True
                        ocr_pages_used += 1
                        try:
                            ocr_text = self._ocr.extract_text(img_bytes)
                        except Exception as exc:
                            page_ocr_error = f"{type(exc).__name__}: {exc}"
                            log.warning(
                                "[PdfProcessor] OCR failed on page %d: %s",
                                page_idx + 1,
                                page_ocr_error,
                            )
                        else:
                            if ocr_text.strip():
                                text = ocr_text.strip()
                                extraction_source = "ocr"
                                page_ocr_succeeded = True
                                ocr_used = True

                if page_ocr_error:
                    errors.append(f"page {page_idx + 1}: {page_ocr_error}")

                if not keep_page_images:
                    # The PNG was only needed for OCR; drop it so scanned
                    # documents do not accumulate every page bitmap in memory.
                    img_bytes = None

                pages.append(PdfPage(
                    page_index=page_idx,
                    text=text,
                    image=img_bytes,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    has_text_layer=page_has_text,
                    extraction_source=extraction_source,
                    ocr_attempted=page_ocr_attempted,
                    ocr_succeeded=page_ocr_succeeded,
                    ocr_error=page_ocr_error,
                ))
        finally:
            doc.close()

        is_scanned = bool(pages) and all(not page.has_text_layer for page in pages)

        result = PdfResult(
            file_path=label,
            total_pages=len(pages),
            pages=pages,
            is_scanned=is_scanned,
            has_text_layer=has_text_layer,
            ocr_used=ocr_used,
            ocr_attempted=ocr_attempted,
            ocr_backend=self._ocr_backend.label if self._ocr_backend else None,
            errors=errors,
        )
        if truncated:
            result.errors.append("truncated: page count exceeded the processing limit")
        return result

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
    "PdfEncryptedError",
    "RapidOcrBackend",
    "parse_rapidocr_text",
]
