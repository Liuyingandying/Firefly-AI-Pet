"""OCR text hit-testing — map a selection rectangle to recognized text.

PDF OCR Overlay Phase 1 (Firefly_PDF_OCR_Overlay_Audit.md §4): pure logic
only — no UI, no capture, no ExplainBox wiring. The caller supplies OCR
lines (text + RapidOCR bbox) and a selection rectangle; this module decides
which lines the rectangle covers and joins them in reading order.

Coordinate contract
    OCR line bboxes live in the OCR image's pixel space. The selection
    rectangle normally arrives in a larger space (screen physical pixels);
    the caller subtracts the window-crop offset and passes ``scale`` so the
    rectangle is divided by it before matching
    (``image_rect = screen_rect / scale``).

Matching rule (line granularity — RapidOCR emits line boxes, not word boxes)
    A line is hit when the rectangle covers at least
    ``min_vertical_overlap`` of the line's height (default 0.5) AND has a
    positive horizontal intersection. A hit always yields the whole line's
    text — partial-line selections return the full line.

Output
    Reading order: hit lines are clustered into visual rows (vertical
    overlap against the row's union rect), rows run top-to-bottom, lines
    inside a row left-to-right. Rows join with "``\\n``", lines within a
    row with a single space.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

Rect = tuple[float, float, float, float]  # (x1, y1, x2, y2)

DEFAULT_MIN_VERTICAL_OVERLAP = 0.5


@dataclass
class OCRLine:
    """One OCR line: recognized text + its raw geometry.

    ``bbox`` accepts what RapidOCR emits — a 4-point polygon
    (``[[x, y], [x, y], [x, y], [x, y]]``, in any corner order) — or an
    already-normalized ``(x1, y1, x2, y2)`` sequence. Lines without a
    usable bbox can never be hit.
    """

    text: str
    bbox: Any = None


def line_rect(bbox: Any) -> Rect:
    """Normalize a RapidOCR polygon or rect into an axis-aligned rect."""
    points = _flatten_points(bbox)
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _flatten_points(bbox: Any) -> list[tuple[float, float]]:
    """Extract (x, y) pairs from a polygon or a flat/paired rect."""
    if bbox is None:
        return []
    if isinstance(bbox, (int, float, str, bytes)):
        return []
    items = list(bbox)
    if not items:
        return []
    first = items[0]
    if isinstance(first, (int, float)):
        # Flat rect (x1, y1, x2, y2) — pad to corner pairs when 4 values.
        if len(items) < 4:
            return []
        values = [float(v) for v in items[:4]]
        return list(zip(values[0::2], values[1::2]))
    points: list[tuple[float, float]] = []
    for item in items:
        try:
            if isinstance(item, (int, float)) or len(item) < 2:
                continue
            points.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError):
            continue
    return points


def _as_ocr_line(line: Any) -> OCRLine:
    """Accept OCRLine instances or plain dicts with text/bbox keys."""
    if isinstance(line, OCRLine):
        return line
    if isinstance(line, dict):
        return OCRLine(text=line.get("text") or "", bbox=line.get("bbox"))
    text = getattr(line, "text", "")
    bbox = getattr(line, "bbox", None)
    return OCRLine(text=text, bbox=bbox)


def _normalize_rect(rect: Sequence[float], scale: float) -> Rect:
    x1, y1, x2, y2 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    if scale and scale != 1.0:
        x1, y1, x2, y2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _vertical_overlap_ratio(rect: Rect, other: Rect) -> float:
    """Overlap height of ``rect`` over ``other`` relative to rect's height."""
    height = rect[3] - rect[1]
    if height <= 1e-9:
        return 0.0
    overlap = min(rect[3], other[3]) - max(rect[1], other[1])
    return max(0.0, overlap) / height


def _is_hit(line_rect_values: Rect, selection: Rect, min_vertical_overlap: float) -> bool:
    horizontal = min(line_rect_values[2], selection[2]) - max(line_rect_values[0], selection[0])
    if horizontal <= 1e-9:
        return False
    return _vertical_overlap_ratio(line_rect_values, selection) >= min_vertical_overlap


def hit_test(
    lines: Sequence[Any],
    rect: Sequence[float],
    *,
    scale: float = 1.0,
    min_vertical_overlap: float = DEFAULT_MIN_VERTICAL_OVERLAP,
) -> str:
    """Return the text of the OCR lines covered by the selection rectangle.

    Args:
        lines: OCRLine instances (or ``{"text", "bbox"}`` dicts).
        rect: Selection rectangle ``(x1, y1, x2, y2)`` in the caller's
            coordinate space; corner order is normalized.
        scale: Divisor mapping the rectangle into OCR image coordinates
            (``1.0`` when the rectangle already lives in image space).
        min_vertical_overlap: Fraction of a line's height the rectangle
            must cover vertically for the line to count as hit.

    Returns:
        Hit text in reading order (rows top-to-bottom joined by newline,
        lines within a row joined by space). Empty string when nothing
        matches — including an empty/degenerate rectangle.
    """
    selection = _normalize_rect(rect, scale)
    if selection[2] - selection[0] <= 1e-9 or selection[3] - selection[1] <= 1e-9:
        return ""

    hits: list[tuple[Rect, str]] = []
    for entry in lines:
        line = _as_ocr_line(entry)
        text = (line.text or "").strip()
        if not text or line.bbox is None:
            continue
        values = line_rect(line.bbox)
        if values[2] - values[0] <= 1e-9 or values[3] - values[1] <= 1e-9:
            continue  # degenerate geometry — never hit
        if _is_hit(values, selection, min_vertical_overlap):
            hits.append((values, text))
    if not hits:
        return ""

    # Reading order: rows by vertical clustering, lines left-to-right.
    hits.sort(key=lambda item: ((item[0][1] + item[0][3]) / 2.0, item[0][0]))
    rows: list[list[tuple[Rect, str]]] = []
    for values, text in hits:
        placed = False
        for row in rows:
            union = _union_rect(row)
            if _vertical_overlap_ratio(values, union) >= min_vertical_overlap:
                row.append((values, text))
                placed = True
                break
        if not placed:
            rows.append([(values, text)])
    rows.sort(key=lambda row: _union_rect(row)[1])
    rendered = [
        " ".join(text for _values, text in sorted(row, key=lambda item: item[0][0]))
        for row in rows
    ]
    return "\n".join(rendered)


def _union_rect(row: list[tuple[Rect, str]]) -> Rect:
    x1 = min(values[0] for values, _text in row)
    y1 = min(values[1] for values, _text in row)
    x2 = max(values[2] for values, _text in row)
    y2 = max(values[3] for values, _text in row)
    return (x1, y1, x2, y2)


# ---------------------------------------------------------------------------
# Screen ↔ OCR image coordinate mapping (PDF OCR Overlay Phase 2-A)
# ---------------------------------------------------------------------------


def screen_to_image_rect(
    rect: Sequence[float],
    crop_offset: Sequence[float],
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> Rect:
    """Map a selection rectangle from absolute screen pixels into OCR image
    coordinates: ``image = (screen - crop_offset) * scale``.

    ``crop_offset`` is the absolute physical-screen coordinate of the
    captured image's top-left (``ScreenFrame.crop_offset``); ``scale_x`` /
    ``scale_y`` are ``ScreenFrame.scale_x`` / ``scale_y``. Corner order is
    normalized.
    """
    x1, y1, x2, y2 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    ox, oy = float(crop_offset[0]), float(crop_offset[1])
    sx, sy = float(scale_x), float(scale_y)
    ix1, iy1, ix2, iy2 = (x1 - ox) * sx, (y1 - oy) * sy, (x2 - ox) * sx, (y2 - oy) * sy
    return (min(ix1, ix2), min(iy1, iy2), max(ix1, ix2), max(iy1, iy2))


def image_to_screen_rect(
    rect: Sequence[float],
    crop_offset: Sequence[float],
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> Rect:
    """Inverse of :func:`screen_to_image_rect`
    (``screen = image / scale + crop_offset``) — used to paint OCR line
    boxes back on screen. Raises ``ValueError`` for a non-positive scale.
    """
    if float(scale_x) <= 0.0 or float(scale_y) <= 0.0:
        raise ValueError("scale_x/scale_y must be positive")
    x1, y1, x2, y2 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    ox, oy = float(crop_offset[0]), float(crop_offset[1])
    sx1, sy1 = x1 / float(scale_x) + ox, y1 / float(scale_y) + oy
    sx2, sy2 = x2 / float(scale_x) + ox, y2 / float(scale_y) + oy
    return (min(sx1, sx2), min(sy1, sy2), max(sx1, sx2), max(sy1, sy2))


__all__ = [
    "OCRLine",
    "Rect",
    "line_rect",
    "hit_test",
    "screen_to_image_rect",
    "image_to_screen_rect",
    "DEFAULT_MIN_VERTICAL_OVERLAP",
]
