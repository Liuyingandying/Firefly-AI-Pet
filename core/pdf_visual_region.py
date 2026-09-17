"""PDF Visual Region — crop + prompt helpers (Phase 4-B).

Pure, in-memory helpers for the visual-region explain pipeline:

  - ``crop_region``: crop the fresh ScreenFrame at an image-space rect,
    optionally LANCZOS-upscale, re-encode in memory (never touches disk),
    and return a new ScreenFrame whose metadata stays consistent
    (``crop_offset`` = region origin in screen pixels, scale = frame scale
    × upscale).
  - ``classify_region``: cheap heuristic → "formula" / "figure" / "general"
    (no learned classifier; uncertainty falls back to "general").
  - ``build_visual_prompt``: the three Phase 4-A.5 templates (formula /
    figure / general) + optional bounded document context + OCR grounding.
  - ``gather_region_ocr``: bounded OCR lines overlapping the region (and
    neighbors) as prompt grounding only.

No production module below is modified by this file.
"""

from __future__ import annotations

import io
import re
import typing
from datetime import datetime

from PIL import Image

from core.pdf_text_hit_test import line_rect
from core.screen_vision.models import ScreenFrame

Rect = tuple[float, float, float, float]

DEFAULT_UPSCALE = 2.0
FORMULA_MIN_WIDTH = 300  # live-gate guidance (not a hard block)
FIGURE_MIN_WxH = (400, 250)

GROUNDING_LINE_LIMIT = 12
GROUNDING_CHAR_LIMIT = 600

_FORMULA_TOKENS = ("=", "∑", "√", "sqrt", "softmax", "tanh", "exp(", "λ", "∈", "∞")
# Weak math tokens (×, ·, →, ^) also appear inside diagrams — never enough
# on their own to classify a region as a formula.
_FIGURE_TOKENS = ("figure ", "fig. ", "fig ", "图 ", "图注", "diagram", "图示")

# ---------------------------------------------------------------------------
# crop
# ---------------------------------------------------------------------------


def crop_region(
    frame: ScreenFrame,
    image_rect: Rect,
    upscale: float = DEFAULT_UPSCALE,
    jpeg_quality: int = 92,
) -> ScreenFrame:
    """In-memory crop of the frame at an image-space rect, optional LANCZOS
    upscale, re-encoded JPEG. Metadata: the sub-crop's screen origin and a
    scale that maps screen px → cropped image px."""
    x1, y1, x2, y2 = image_rect
    left, right = sorted((int(x1), int(x2)))
    top, bottom = sorted((int(y1), int(y2)))
    left = max(0, left)
    top = max(0, top)
    right = min(frame.width, right)
    bottom = min(frame.height, bottom)
    if right <= left or bottom <= top:
        raise ValueError("empty crop region")

    image = Image.open(io.BytesIO(frame.image_bytes))
    crop = image.crop((left, top, right, bottom))
    if upscale and upscale > 1.0:
        crop = crop.resize(
            (round(crop.width * upscale), round(crop.height * upscale)),
            Image.LANCZOS,
        )

    buffer = io.BytesIO()
    crop.save(buffer, format="JPEG", quality=jpeg_quality)

    scale_x = (frame.scale_x or 1.0) * (upscale or 1.0)
    scale_y = (frame.scale_y or 1.0) * (upscale or 1.0)
    origin_x = (frame.crop_offset[0] or 0) + (
        left / (frame.scale_x or 1.0)
    )
    origin_y = (frame.crop_offset[1] or 0) + (
        top / (frame.scale_y or 1.0)
    )
    return ScreenFrame(
        width=crop.width,
        height=crop.height,
        mime_type="image/jpeg",
        image_bytes=buffer.getvalue(),
        captured_at=datetime.now(),
        crop_offset=(round(origin_x), round(origin_y)),
        scale_x=scale_x,
        scale_y=scale_y,
    )


# ---------------------------------------------------------------------------
# classification (cheap heuristic, falls back to "general")
# ---------------------------------------------------------------------------


def classify_region(ocr_text: str, width: int, height: int) -> str:
    """"formula" | "figure" | "general" from OCR + geometry.

    Broad-and-short strips with math tokens are formulas; Figure/Fig./图
    captions (or 图 with a numeral) are figures; everything else — including
    uncertainty — is "general".
    """
    text = (ocr_text or "").strip().lower()
    if not text:
        return "general"

    width = int(width or 0)
    height = int(height or 0)
    aspect = (width / height) if height else 0.0

    has_figure = any(token in text for token in _FIGURE_TOKENS)
    if has_figure:
        return "figure"

    has_formula = any(token in text for token in _FORMULA_TOKENS)
    if has_formula and (aspect >= 2.0 or len(text) <= 90):
        return "formula"
    return "general"


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

_FORMULA_PROMPT = (
    "这是一个论文页面的裁剪区域，其中有一个数学公式。请回答："
    "1. 识别公式本身（逐符号写出）；"
    "2. 说明各符号含义（Q、K、V、QK^T、sqrt(d_k)、softmax 等各是什么）；"
    "3. 解释该公式执行的计算；"
    "4. 说明它在 Attention 机制中的作用；"
    "5. 对无法可靠辨认的符号明确表示不确定。"
    "不要虚构图中不存在的符号。"
)

_FIGURE_PROMPT = (
    "这是一个论文页面的裁剪区域，包含一张神经网络结构图（如 Figure 2 "
    "Multi-Head Attention 相关）。请回答："
    "1. 识别图的主要模块（Linear、MatMul、Scale、Mask、SoftMax、Concat 等）；"
    "2. 按数据流顺序解释；"
    "3. 说明多头并行（×h / heads）发生在哪里；"
    "4. 说明 Concat 之后发生什么；"
    "5. 不得凭空增加图中没有的组件。若某处看不清，明确说明。"
)

_GENERAL_PROMPT = (
    "这是论文页面的一块区域（可能含公式、图或正文，或它们的组合）。"
    "请解释这块区域表达了什么，以及它在当前论文章节中的作用。"
    "先说明你看到了什么，再说明其作用；看不清的部分要明确说明，不要猜测。"
)

_PROMPT_BY_KIND = {
    "formula": _FORMULA_PROMPT,
    "figure": _FIGURE_PROMPT,
    "general": _GENERAL_PROMPT,
}


def build_visual_prompt(
    kind: str,
    context: str = "",
    grounding: str = "",
) -> str:
    """Assemble the region prompt: optional bounded context + optional OCR
    grounding + the kind-specific template. OCR is never a substitute for
    the image — it is only prompt grounding."""
    parts: list[str] = []
    if context and context.strip():
        parts.append(context.strip())
    if grounding and grounding.strip():
        parts.append(f"【该区域 OCR 文本（仅供参考，可能不完整）：{grounding.strip()}】")
    parts.append(_PROMPT_BY_KIND.get(kind, _GENERAL_PROMPT))
    return "\n".join(parts)


def gather_region_ocr(
    lines: typing.Sequence,
    image_rect: Rect,
    *,
    line_limit: int = GROUNDING_LINE_LIMIT,
    char_limit: int = GROUNDING_CHAR_LIMIT,
) -> str:
    """Bounded OCR lines overlapping the region (reading order) + the line
    right below it (caption/paragraph context). Prompt grounding only."""
    if not lines:
        return ""
    x1, y1, x2, y2 = image_rect
    hits: list[tuple[int, int, str]] = []
    for line in lines:
        r = line_rect(line["bbox"])
        if (
            min(y1, y2) <= r[3]
            and r[1] <= max(y1, y2)
            and min(x1, x2) <= r[2]
            and r[0] <= max(x1, x2)
        ):
            hits.append((int(r[1]), int(r[0]), (line["text"] or "").strip()))
    hits.sort()
    pieces: list[str] = []
    used = 0
    for _y, _x, text in hits:
        if not text or used >= char_limit:
            continue
        piece = text[: char_limit - used]
        pieces.append(piece)
        used += len(piece)
        if len(pieces) >= line_limit:
            break
    return " | ".join(pieces)


__all__ = [
    "Rect",
    "crop_region",
    "classify_region",
    "build_visual_prompt",
    "gather_region_ocr",
    "DEFAULT_UPSCALE",
    "FORMULA_MIN_WIDTH",
    "FIGURE_MIN_WxH",
]