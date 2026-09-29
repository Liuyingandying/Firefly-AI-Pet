"""Document Vision v1 — render one PDF page / PPTX slide to an in-memory JPEG ScreenFrame.

Scope boundary: this module only maps a document page/slide to a rendered
image + ScreenFrame. Intent, UI, chat, memory and history live elsewhere.

Privacy:
- PDF pages render fully in memory (``pymupdf.open(stream=...)``); nothing is
  ever written to disk.
- PPTX slides need PowerPoint COM, which can only open a real file, so the
  source bytes are written to a unique temp directory (``firefly-docvision-*``)
  that is always cleaned up in a ``finally`` block — even when the export
  fails. No temp path ever reaches history, logs or meta.
"""

from __future__ import annotations

import io
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from core.document_attachment import _PAGE_CN_RE, _PAGE_EN_RE, _SLIDE_RE
from core.screen_vision.models import ScreenFrame

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- limits

MAX_RENDER_EDGE = 1600        # long-edge cap for the JPEG frame
JPEG_QUALITY = 85
PDF_RENDER_DPI = 150          # nominal DPI; lowered automatically for big pages
PPTX_EXPORT_SCALE_MAX = 2.0   # PowerPoint export scale cap (points -> pixels)


# ---------------------------------------------------------------- errors


class DocumentVisionError(Exception):
    """Base class for Document Vision render failures."""


class PageOutOfRangeError(DocumentVisionError):
    """Requested page/slide index is outside the document."""


class DocumentVisionRenderError(DocumentVisionError):
    """The page/slide could not be rendered."""


# ---------------------------------------------------------------- trigger


_DOCUMENT_VISION_TRIGGERS = (
    "看看这页",
    "这张图",
    "图片",
    "照片",
    "曲线",
    "图表",
    "框图",
    "流程图",
    "示意图",
    "图",
)


def is_document_vision_request(question: str) -> bool:
    """Explicit visual trigger only; never auto-triggers on ordinary QA.

    The caller must also resolve a page/slide location before rendering.
    """
    lowered = (question or "").strip().lower()
    return any(marker in lowered for marker in _DOCUMENT_VISION_TRIGGERS)


# ---------------------------------------------------------------- location


def resolve_document_location(question: str, context: Any) -> int | None:
    """Reuse the existing page/slide regexes (single parser, no duplicate).

    Mirrors ``core.document_attachment.direct_section_lookup``: 第N页 / page N
    resolve for any kind (pptx maps to Slide N), slide N resolves for pptx.
    Returns a 1-based index, or None when no location is mentioned.
    """
    match = _PAGE_CN_RE.search(question) or _PAGE_EN_RE.search(question)
    if match:
        return int(match.group(1))
    if context.kind == "pptx":
        match = _SLIDE_RE.search(question)
        if match:
            return int(match.group(1))
    return None


# ---------------------------------------------------------------- prompt


DOCUMENT_VISION_STYLE_CONTEXT = (
    "The image is one page from an attached document. "
    "Answer using only this page image and provided text. "
    "Do not invent invisible information."
)


def build_document_vision_question(
    user_question: str,
    location_label: str,
    page_text: str,
) -> str:
    """The bounded vision question: current question + this page's text.

    Never includes the full document, other pages, Memory, History or Bond.
    """
    parts = [(user_question or "").strip()]
    if page_text:
        parts.append(f"[{location_label} extracted text]\n{page_text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- renderer


class DocumentVisionRenderer:
    """Renders one page/slide of an in-memory document to a JPEG ScreenFrame."""

    def render_pdf_page(self, document_source: bytes, page_index: int) -> ScreenFrame:
        """Render PDF page ``page_index`` (1-based) fully in memory."""
        import pymupdf

        page_index = int(page_index)
        try:
            doc = pymupdf.open(stream=document_source, filetype="pdf")
        except Exception as exc:
            raise DocumentVisionRenderError(
                f"PDF 无法打开: {type(exc).__name__}"
            ) from exc
        try:
            if page_index < 1 or page_index > doc.page_count:
                raise PageOutOfRangeError(
                    f"page {page_index} out of range (1..{doc.page_count})"
                )
            page = doc[page_index - 1]
            # Cap the DPI so the rendered pixmap itself never exceeds the long
            # edge limit (avoids huge intermediate buffers on large pages).
            # PyMuPDF's set_dpi needs an int; floor to stay under the cap.
            long_edge_pt = max(page.rect.width, page.rect.height)
            dpi = int(min(PDF_RENDER_DPI, MAX_RENDER_EDGE * 72.0 / long_edge_pt))
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        return self._to_screen_frame(image)

    def render_pptx_slide(self, document_source: bytes, slide_index: int) -> ScreenFrame:
        """Render one PPTX slide via PowerPoint COM.

        Only the target slide is exported. The unique temp directory is always
        cleaned up (try/finally), including when the export fails.
        """
        import pythoncom
        import win32com.client

        slide_index = int(slide_index)
        pythoncom.CoInitialize()
        tmp = tempfile.TemporaryDirectory(prefix="firefly-docvision-")
        try:
            tmp_path = Path(tmp.name)
            pptx_path = tmp_path / "source.pptx"
            pptx_path.write_bytes(document_source)
            png_path = tmp_path / f"slide-{slide_index}.png"

            app = None
            presentation = None
            try:
                app = win32com.client.DispatchEx("PowerPoint.Application")
                presentation = app.Presentations.Open(
                    str(pptx_path), ReadOnly=True, WithWindow=False
                )
                if slide_index < 1 or slide_index > presentation.Slides.Count:
                    raise PageOutOfRangeError(
                        f"slide {slide_index} out of range (1..{presentation.Slides.Count})"
                    )
                width, height = self._export_size(presentation)
                presentation.Slides.Item(slide_index).Export(
                    str(png_path), "PNG", width, height
                )
            finally:
                if presentation is not None:
                    try:
                        presentation.Close()
                    except Exception:
                        pass
                if app is not None:
                    try:
                        app.Quit()
                    except Exception:
                        pass
            image = Image.open(io.BytesIO(png_path.read_bytes()))
            return self._to_screen_frame(image)
        except DocumentVisionError:
            raise
        except Exception as exc:
            raise DocumentVisionRenderError(
                f"PPTX 渲染失败: {type(exc).__name__}"
            ) from exc
        finally:
            tmp.cleanup()
            pythoncom.CoUninitialize()

    def _export_size(self, presentation: Any) -> tuple[int, int]:
        """Pixel size for the exported PNG, long edge capped at MAX_RENDER_EDGE."""
        slide_width = float(presentation.PageSetup.SlideWidth)
        slide_height = float(presentation.PageSetup.SlideHeight)
        scale = min(
            PPTX_EXPORT_SCALE_MAX,
            MAX_RENDER_EDGE / max(slide_width, slide_height),
        )
        return max(int(slide_width * scale), 1), max(int(slide_height * scale), 1)

    def _to_screen_frame(self, image: Any) -> ScreenFrame:
        """Encode a PIL image to an in-memory JPEG ScreenFrame (max 1600px, q85)."""
        if image.mode != "RGB":
            image = image.convert("RGB")
        if max(image.size) > MAX_RENDER_EDGE:
            scale = MAX_RENDER_EDGE / max(image.size)
            new_size = (round(image.width * scale), round(image.height * scale))
            image = image.resize(new_size, Image.LANCZOS)
        encode_buffer = io.BytesIO()
        image.save(encode_buffer, format="JPEG", quality=JPEG_QUALITY)
        encoded = encode_buffer.getvalue()
        return ScreenFrame(
            width=image.width,
            height=image.height,
            mime_type="image/jpeg",
            image_bytes=encoded,
            captured_at=datetime.now(),
        )
