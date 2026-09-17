"""Phase 1-A: optional keyframe visual description for the video pipeline.

Bridges video keyframe image bytes into the existing Screen Vision provider
abstraction:

    keyframe bytes -> ScreenFrame -> VisionProvider.inspect() -> description str

Design constraints:
- The adapter never imports VideoProcessor / KeyframeInfo, so the dependency
  direction is strictly video_pipeline -> video_vision -> screen_vision.models
  (no cycle).
- The VisionProvider is injected by the caller; nothing here constructs a
  provider, reads credentials, or performs network I/O on its own.
- Failures propagate to the caller: VideoProcessor catches them per frame so
  one bad keyframe cannot fail the whole video.

The video instruction is deliberately different from the screen default
(DEFAULT_VISION_INSTRUCTION): keyframe description wants short prose, not the
screen JSON schema.
"""

from __future__ import annotations

import struct
from datetime import datetime

from core.screen_vision.models import ScreenFrame
from core.screen_vision.vision.base import VisionProvider

VIDEO_VISION_INSTRUCTION = """你是视频关键帧视觉描述模块。
只描述这一帧画面中真实可见的内容，不要猜测看不清的部分。
请用 1-3 句中文概括：
- 画面主体（人物、物体、界面、图表、代码、字幕等）
- 场景类型（例如：教学幻灯片、代码编辑器、会议画面、实拍风景、游戏画面）
- 画面中出现的关键文字、数字或图表结论（如果有）
不要逐字复述 OCR 文本，不要给操作建议，只返回一段纯文本描述，不要返回 JSON。"""


def _probe_image(image_bytes: bytes) -> tuple[str, int, int]:
    """Return (mime_type, width, height) from PNG/JPEG magic bytes.

    Width/height are best-effort (0 when unknown): providers only consume
    mime_type + image_bytes, dimensions exist to satisfy ScreenFrame.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        width, height = struct.unpack(">II", image_bytes[16:24])
        return "image/png", width, height
    if image_bytes.startswith(b"\xff\xd8"):
        # JPEG: scan segment markers for the first SOF frame header.
        i = 2
        n = len(image_bytes)
        while i + 4 < n:
            if image_bytes[i] != 0xFF:
                i += 1
                continue
            marker = image_bytes[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 <= n:
                    height = int.from_bytes(image_bytes[i + 5:i + 7], "big")
                    width = int.from_bytes(image_bytes[i + 7:i + 9], "big")
                    return "image/jpeg", width, height
                break
            seg_len = int.from_bytes(image_bytes[i + 2:i + 4], "big")
            if seg_len < 2:
                break
            i += 2 + seg_len
        return "image/jpeg", 0, 0
    return "application/octet-stream", 0, 0


class VideoVisionAdapter:
    """Describe single video keyframes through an injected VisionProvider."""

    def __init__(self, provider: VisionProvider) -> None:
        if provider is None:
            raise ValueError("VideoVisionAdapter requires a VisionProvider instance")
        self._provider = provider

    def describe(
        self,
        *,
        image_bytes: bytes,
        timestamp: float,
        ocr_text: str = "",
    ) -> str:
        """Return a short prose description for one keyframe.

        Raises on provider failure; callers decide how to degrade.
        """
        if not image_bytes:
            raise ValueError("image_bytes is empty")
        mime_type, width, height = _probe_image(image_bytes)
        frame = ScreenFrame(
            width=width,
            height=height,
            mime_type=mime_type,
            image_bytes=image_bytes,
            captured_at=datetime.now(),
        )
        instruction = VIDEO_VISION_INSTRUCTION
        ocr = (ocr_text or "").strip()
        if ocr:
            instruction = (
                f"{VIDEO_VISION_INSTRUCTION}\n\n"
                f"这一帧的 OCR 文本（仅供参考，不要逐字复述）：\n{ocr}"
            )
        observation = self._provider.inspect(frame, instruction)
        summary = (getattr(observation, "scene_summary", "") or "").strip()
        if summary:
            return summary
        return (getattr(observation, "raw_model_text", "") or "").strip()


__all__ = ["VideoVisionAdapter", "VIDEO_VISION_INSTRUCTION"]
