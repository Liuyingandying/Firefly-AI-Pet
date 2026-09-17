"""Video frame visual analysis through the existing Screen Vision stack.

Chain: BiliInsight ``frame`` action -> JPEG bytes -> ``ScreenFrame`` ->
existing ``VisionProvider.inspect()`` -> user-readable answer.

This is an adapter, not a new vision system: it reuses the frozen Screen
Vision abstractions (``core.screen_vision.models``), the Phase 1-A video
instruction conventions from ``core.video_vision``, and the shared provider
cache (``core.screen_vision.config.get_fast_direct_provider``). Providers are
injectable so tests can stay offline.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from core.bili_insight_client import BiliInsightClient, BiliServiceError
from core.bili_video_reader import extract_bilibili_video_id
from core.screen_vision.models import ScreenFrame, ScreenObservation

# Reuse the frozen Phase 1-A keyframe instruction for the generic
# "describe this frame" case instead of redefining it.
from core.video_vision import VIDEO_VISION_INSTRUCTION

FRAME_QUESTION_INSTRUCTION = """你是视频画面分析模块。
只基于这一帧画面中真实可见的内容回答用户问题，不要猜测看不清的部分。
你只看到了这一帧画面：涉及动作、前后变化或画面之外的内容时，
明确说明"我只能根据这一帧判断"；如果画面中没有足够证据，
请直接说"画面中无法确定"。
可以引用画面中可见的文字、物体或界面作为依据。
用中文简洁回答，不要返回 JSON。

用户问题：{question}"""


@dataclass(frozen=True)
class VideoFrameAnalysis:
    """One frame reading: facts from the service plus the vision answer."""

    bvid: str
    url: str
    timestamp_s: float
    frame_path: str
    frame_bytes: int
    description: str


def build_frame_instruction(question: str | None) -> str:
    """Generic description reuses the frozen instruction; questions override it."""
    if question and question.strip():
        return FRAME_QUESTION_INSTRUCTION.format(question=question.strip())
    return VIDEO_VISION_INSTRUCTION


def analyze_video_frame(
    url: str,
    timestamp: str | float,
    question: str | None = None,
    *,
    client: BiliInsightClient | None = None,
    vision_provider=None,
) -> VideoFrameAnalysis:
    """Capture one video frame via BiliInsight and analyze it with the
    existing Screen Vision provider.

    ``vision_provider`` is injectable for offline tests; by default the shared
    FAST TJU-Qwen vision provider (existing cache, no new system) is used.
    Raises BiliServiceError on service/argument failures.
    """
    bvid = extract_bilibili_video_id(url or "")
    if not bvid:
        raise BiliServiceError("invalid_args", f"no BV id found in: {url!r}", "frame")
    service = client or BiliInsightClient()

    frame_data = service.frame(bvid, str(timestamp), include_base64=True)
    encoded = frame_data.get("base64") or ""
    image_bytes = base64.b64decode(encoded) if encoded else b""
    if not image_bytes:
        raise BiliServiceError("internal", "service returned no frame image", "frame")

    frame = ScreenFrame(
        width=0,  # dimensions are best-effort in this stack; providers do not need them
        height=0,
        mime_type=str(frame_data.get("media_type") or "image/jpeg"),
        image_bytes=image_bytes,
        captured_at=datetime.now(),
    )
    provider = vision_provider if vision_provider is not None else _default_provider()
    observation: ScreenObservation = provider.inspect(frame, build_frame_instruction(question))
    description = (
        (getattr(observation, "scene_summary", "") or "").strip()
        or (getattr(observation, "raw_model_text", "") or "").strip()
    )
    return VideoFrameAnalysis(
        bvid=bvid,
        url=f"https://www.bilibili.com/video/{bvid}",
        timestamp_s=float(frame_data.get("timestamp_s") or 0),
        frame_path=str(frame_data.get("frame_path") or ""),
        frame_bytes=len(image_bytes),
        description=description,
    )


def _default_provider():
    """Resolve the existing shared vision provider (deferred, cached upstream)."""
    from core.screen_vision.config import get_fast_direct_provider

    return get_fast_direct_provider()


__all__ = ["VideoFrameAnalysis", "analyze_video_frame", "build_frame_instruction"]
