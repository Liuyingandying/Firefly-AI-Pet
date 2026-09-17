"""PdfViewportContext — what the user is currently SEEING in their PDF viewer.

Ambient PDF Vision v1: understands the visible region of the PDF the user is
reading in Edge/Chrome by capturing that window and running local OCR.
Composition only — reuses ScreenCaptureService (capture), RapidOcrBackend
(OCR) and the page-context heuristics. No LLM by default, no Memory, no
database, no disk writes; the ScreenFrame never leaves memory.

Page number comes from the viewer toolbar pattern ``N / M`` (digits only,
toolbar region preferred when OCR boxes are available, formula lines like
``1 / sqrt(d_k)`` rejected), clamped by the known total-pages hint.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from core.page_context import _tokenize

_MAX_VISIBLE_TEXT = 2000
_MAX_KEYWORDS = 8
_MAX_TOTAL_PAGES = 4999

# N / M — digits on both sides; the surrounding-line letter guard rejects
# formula fragments like "1 / sqrt(d_k)".
PAGE_NUM_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*/\s*(\d{1,4})(?![\d.])")
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,2})\s+([A-Za-z\u4e00-\u9fff].{0,60})$")
_TOOLBAR_Y_RATIO = 0.18


@dataclass
class PdfViewportContext:
    """What is currently visible in the user's PDF viewer window."""

    page: int | None = None
    total_pages: int | None = None
    section: str = ""
    visible_text: str = ""
    keywords: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "ocr"  # "ocr" | "ocr-unavailable" | "vlm"
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# OCR raw result → ordered lines (text + vertical center when available)
# ---------------------------------------------------------------------------


def ocr_lines_from_raw(raw) -> list[dict]:
    """Normalize modern (txts/boxes) and legacy ([box, text], …) results.

    Each line dict carries ``text``, ``y`` (vertical center, legacy consumer
    contract) and ``bbox`` — the original RapidOCR geometry preserved for
    the OCR Overlay hit-testing (PDF OCR Overlay Phase 1). ``bbox`` is the
    raw 4-point polygon ``[[x, y], …]`` when available, else ``None``.
    """
    if raw is None:
        return []
    lines: list[dict] = []
    txts = getattr(raw, "txts", None)
    if txts:
        boxes = getattr(raw, "boxes", None)
        for i, text in enumerate(txts):
            y = None
            box = None
            if boxes is not None and i < len(boxes) and boxes[i] is not None:
                box = [[float(pt[0]), float(pt[1])] for pt in boxes[i]]
                ys = [pt[1] for pt in box]
                y = sum(ys) / len(ys) if ys else None
            text = (text or "").strip()
            if text:
                lines.append({"text": text, "y": y, "bbox": box})
        return lines
    try:
        items = raw[0]
    except (IndexError, KeyError, TypeError):
        return []
    for item in items or []:
        try:
            box, text = item[0], item[1]
        except (IndexError, TypeError):
            continue
        y = None
        norm_box = None
        if box:
            norm_box = [[float(pt[0]), float(pt[1])] for pt in box if pt]
            ys = [pt[1] for pt in norm_box]
            y = sum(ys) / len(ys) if ys else None
        text = str(text or "").strip()
        if text:
            lines.append({"text": text, "y": y, "bbox": norm_box})
    return lines


def lines_from_plain_text(text: str) -> list[dict]:
    return [
        {"text": line.strip(), "y": None}
        for line in (text or "").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# page number: toolbar "N / M"
# ---------------------------------------------------------------------------


def parse_page_number(
    lines: list[dict],
    image_height: int | None = None,
    total_pages_hint: int | None = None,
) -> tuple[int | None, int | None, float]:
    """Pick the toolbar page indicator from OCR lines.

    Preference: a candidate inside the toolbar band (top of the window when
    y-coordinates are available), else the first candidate in reading order.
    Candidates are rejected when the surrounding line carries letters
    (formula fragments like ``1 / sqrt(d_k)``) or when M is out of range.
    A known total-pages hint overrides a mismatching OCR total (lower
    confidence).
    """
    candidates: list[tuple[int, int, int, float, bool, int]] = []
    for order, line in enumerate(lines):
        text = line.get("text") or ""
        match = PAGE_NUM_RE.search(text)
        if not match:
            continue
        current, total = int(match.group(1)), int(match.group(2))
        if not (1 <= current <= total <= _MAX_TOTAL_PAGES):
            continue
        remainder = text.replace(match.group(0), " ").strip()
        if re.search(r"[A-Za-z]", remainder):
            continue  # formula line (e.g. "1 / sqrt(d_k) attention")
        y = line.get("y")
        toolbar = bool(
            image_height and y is not None and y <= image_height * _TOOLBAR_Y_RATIO
        )
        confidence = 0.85 if toolbar else 0.55
        candidates.append((order, current, total, confidence, toolbar, len(candidates)))

    if not candidates:
        return None, None, 0.0

    candidates.sort(key=lambda c: (0 if c[4] else 1, c[0]))
    _order, page, total, confidence, _toolbar, _n = candidates[0]
    if total_pages_hint and total != int(total_pages_hint):
        total = int(total_pages_hint)
        confidence = max(0.1, confidence * 0.6)
    return page, total, confidence


# ---------------------------------------------------------------------------
# section / keywords
# ---------------------------------------------------------------------------


def parse_section(lines: list[dict]) -> str:
    for line in lines:
        text = (line.get("text") or "").strip()
        if _NUMBERED_HEADING_RE.match(text):
            return text[:80]
    for line in lines:
        text = (line.get("text") or "").strip()
        if 3 <= len(text) <= 60 and not PAGE_NUM_RE.search(text) \
                and not text.endswith((".", "。", ",", ";", ":", "：")):
            return text
    return ""


def _singular(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def extract_keywords(lines: list[dict], section: str) -> list[str]:
    phrase = None
    if section:
        match = _NUMBERED_HEADING_RE.match(section)
        phrase = (match.group(2) if match else section).strip().lower() or None

    counts: dict[str, int] = {}
    for line in lines:
        for token in _tokenize(line.get("text") or ""):
            token = _singular(token)
            counts[token] = counts.get(token, 0) + 1
    ranked = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    keywords: list[str] = []
    if phrase:
        keywords.append(phrase)
    for token in ranked:
        if len(keywords) >= _MAX_KEYWORDS:
            break
        # Exact-duplicate only: "attention" still gets its own chip even when
        # the section phrase ("multi-head attention") contains it — the task
        # acceptance expects both.
        if token in keywords:
            continue
        keywords.append(token)
    return keywords


# ---------------------------------------------------------------------------
# fingerprint (viewport change detection — no OCR when nothing changed)
# ---------------------------------------------------------------------------


def frame_fingerprint(frame) -> str:
    """Stable hash of a captured frame (identical pixels -> identical hash)."""
    if frame is None or not getattr(frame, "image_bytes", None):
        return ""
    return hashlib.sha1(frame.image_bytes).hexdigest()


def should_ocr(last_fingerprint: str | None, frame) -> bool:
    return frame_fingerprint(frame) != (last_fingerprint or "")


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


class PdfViewportContextBuilder:
    """ScreenFrame/OCR text -> PdfViewportContext. Composition only."""

    def __init__(self, ocr_backend=None):
        self._backend = ocr_backend

    def _backend_ready(self):
        if self._backend is None:
            from core.pdf_processor import _get_shared_ocr_backend

            self._backend = _get_shared_ocr_backend()
        return self._backend

    def build_from_ocr_text(
        self,
        ocr_text: str,
        *,
        image_height: int | None = None,
        total_pages_hint: int | None = None,
    ) -> PdfViewportContext:
        """Deterministic path: parse from given OCR text (no screen work)."""
        return self.build_from_lines(
            lines_from_plain_text(ocr_text),
            image_height=image_height,
            total_pages_hint=total_pages_hint,
        )

    def build_from_frame(
        self,
        frame,
        *,
        total_pages_hint: int | None = None,
    ) -> PdfViewportContext:
        """Capture-composed path: OCR the frame then parse. No disk writes."""
        backend = self._backend_ready()
        if backend is None or frame is None or not frame.image_bytes:
            return PdfViewportContext(timestamp=time.time(), source="ocr-unavailable")
        try:
            raw = backend.engine(frame.image_bytes)
        except Exception:  # noqa: BLE001 - OCR failure degrades to empty context
            return PdfViewportContext(timestamp=time.time(), source="ocr-unavailable")
        lines = ocr_lines_from_raw(raw)
        if not lines:
            lines = lines_from_plain_text(
                backend.extract_text(frame.image_bytes)
            )
        context = self.build_from_lines(
            lines, image_height=frame.height, total_pages_hint=total_pages_hint
        )
        # Second pass: the viewer toolbar renders its page indicator as a
        # small input field the full-page OCR often misses — re-OCR an
        # upscaled top band only when the page is still unknown. Band text
        # is also joined ("4" "/" "11" -> "4 / 11") before parsing.
        if context.page is None and frame.image_bytes:
            band_lines = self._toolbar_band_ocr(backend, frame)
            if band_lines:
                joined = " ".join(line["text"] for line in band_lines)
                band_page, band_total, band_conf = parse_page_number(
                    lines_from_plain_text(joined),
                    total_pages_hint=total_pages_hint,
                )
                if band_page is None:
                    band_page, band_total, band_conf = parse_page_number(
                        band_lines, total_pages_hint=total_pages_hint
                    )
                if band_page is not None:
                    context.page = band_page
                    context.total_pages = band_total
                    context.confidence = band_conf
        return context

    @staticmethod
    def _toolbar_band_ocr(backend, frame) -> list[dict]:
        """OCR an upscaled top band (the viewer toolbar region)."""
        try:
            from io import BytesIO

            from PIL import Image

            image = Image.open(BytesIO(frame.image_bytes))
            band_height = max(60, int(image.height * 0.20))
            band = image.crop((0, 0, image.width, band_height))
            band = band.resize(
                (band.width * 2, band_height * 2), Image.LANCZOS
            )
            buffer = BytesIO()
            band.save(buffer, format="JPEG", quality=92)
            raw = backend.engine(buffer.getvalue())
            return ocr_lines_from_raw(raw)
        except Exception:  # noqa: BLE001 - band OCR is best-effort
            return []

    def build_from_lines(
        self,
        lines: list[dict],
        *,
        image_height: int | None = None,
        total_pages_hint: int | None = None,
    ) -> PdfViewportContext:
        page, total, confidence = parse_page_number(
            lines, image_height=image_height, total_pages_hint=total_pages_hint
        )
        section = parse_section(lines)
        keywords = extract_keywords(lines, section)
        visible = "\n".join(line.get("text") or "" for line in lines)
        return PdfViewportContext(
            page=page,
            total_pages=total,
            section=section,
            visible_text=visible[:_MAX_VISIBLE_TEXT],
            keywords=keywords,
            confidence=confidence,
            source="ocr",
            timestamp=time.time(),
        )

    def build_with_vlm(
        self,
        frame,
        vlm_provider,
        *,
        total_pages_hint: int | None = None,
    ) -> PdfViewportContext:
        """Fallback path (explicit use only): one multimodal call.

        The answer is parsed back into the same PdfViewportContext shape;
        nothing is stored anywhere by this module.
        """
        if vlm_provider is None or frame is None:
            return PdfViewportContext(timestamp=time.time(), source="vlm-unavailable")
        question = (
            "这是用户正在阅读的 PDF 当前页截图。请严格逐行回答：\n"
            "页码：<若浏览器工具栏可见页码，格式如 4 / 11；否则写 未知>\n"
            "章节：<当前可见的章节标题；没有写 未知>\n"
            "关键词：<空格分隔的 5 个本页关键词>"
        )
        try:
            answer = vlm_provider.answer_direct(frame, question)
        except Exception:  # noqa: BLE001
            return PdfViewportContext(timestamp=time.time(), source="vlm-unavailable")
        answer = (answer or "").strip()
        page = total = None
        match = PAGE_NUM_RE.search(answer)
        if match:
            page, total = int(match.group(1)), int(match.group(2))
            if not (1 <= page <= total <= _MAX_TOTAL_PAGES):
                page = total = None
        section = ""
        keywords: list[str] = []
        for line in answer.splitlines():
            text = line.strip()
            if text.startswith("章节：") or text.startswith("章节:"):
                section = text.split("：", 1)[-1].split(":", 1)[-1].strip()[:80]
            if text.startswith("关键词：") or text.startswith("关键词:"):
                keywords = [
                    _singular(t) for t in re.split(r"[、,，;；\s]+", text.split("：", 1)[-1]) if t
                ][:_MAX_KEYWORDS]
        if total_pages_hint and total is None:
            total = int(total_pages_hint)
        return PdfViewportContext(
            page=page,
            total_pages=total,
            section=section,
            keywords=keywords,
            confidence=0.7,
            source="vlm",
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# PageLens 本页概念 (viewport concepts -> existing concept chips)
# ---------------------------------------------------------------------------


def viewport_concepts(context: PdfViewportContext) -> list[str]:
    """Concept chips for the current viewport: section phrase + keywords."""
    concepts: list[str] = []
    if context.section:
        match = _NUMBERED_HEADING_RE.match(context.section)
        phrase = (match.group(2) if match else context.section).strip()
        if phrase:
            concepts.append(phrase)
    for keyword in context.keywords:
        title = keyword.title()
        if title.lower() not in (c.lower() for c in concepts):
            concepts.append(title)
    return concepts[:_MAX_KEYWORDS]


__all__ = [
    "PdfViewportContext",
    "PdfViewportContextBuilder",
    "viewport_concepts",
    "parse_page_number",
    "parse_section",
    "extract_keywords",
    "frame_fingerprint",
    "should_ocr",
]