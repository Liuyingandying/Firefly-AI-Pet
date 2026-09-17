"""Async image vision worker — shared by PageLens image understanding.

Mirrors ``ui.image_ocr_worker.ImageOcrWorker``: runs the existing Screen
Vision provider (``attachment_to_frame`` + ``answer_direct``) on an image
file in a background QThread. This is the "understand the picture / formula /
table" half that RapidOCR alone cannot provide; it reuses the shared FAST
TJU-Qwen vision provider — no second vision system.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

log = logging.getLogger("firefly.image_vision")

VISION_QUESTION = (
    "请描述这张图片/文档页面：1) 整体布局与结构（有哪些区块）；"
    "2) 关键文字内容要点；3) 图表、公式或表格的关键信息（如有，请尽量转述）。"
    "用中文简洁回答；看不清或没有的内容不要猜测。"
)

VISION_STYLE_CONTEXT = (
    "The image is an attached picture or document page. "
    "Answer using only this image. Do not invent invisible information."
)


class ImageVisionWorker(QObject):
    """Runs the shared vision provider on an image in a background thread.

    Parameters
        image_path: path to image file
        generation: request id (must match caller's generation counter)
        provider: optional VisionProvider (injectable for tests); defaults to
            the shared FAST TJU-Qwen provider.
    """

    finished = Signal(int, str)
    error = Signal(int, str)

    def __init__(self, image_path: str, generation: int = 0, *, provider=None) -> None:
        super().__init__()
        self._image_path = image_path
        self._generation = generation
        self._provider = provider

    @Slot()
    def run(self) -> None:
        """Describe the image through the vision provider and emit the result."""
        try:
            image_bytes = Path(self._image_path).read_bytes()
            image = QImage.fromData(image_bytes)
            if image.isNull():
                self.error.emit(
                    self._generation,
                    "视觉理解失败：图片文件损坏或格式不支持",
                )
                return
            from ui.companion_attachment import (
                AttachmentError,
                attachment_to_frame,
                qimage_to_attachment,
            )

            attachment = qimage_to_attachment(image, Path(self._image_path).name)
            frame = attachment_to_frame(attachment)

            provider = self._provider or self._default_provider()
            answer = provider.answer_direct(
                frame,
                VISION_QUESTION,
                style_context=VISION_STYLE_CONTEXT,
            )
            if not isinstance(answer, str) or not answer.strip():
                self.error.emit(self._generation, "视觉理解失败：模型返回为空")
                return
            self.finished.emit(self._generation, answer.strip())
        except FileNotFoundError:
            self.error.emit(self._generation, "图片文件不存在")
        except AttachmentError as exc:
            self.error.emit(self._generation, f"图片读取失败：{exc}")
        except Exception as exc:  # provider failures degrade to OCR-only
            log.warning("image vision failed: %s", type(exc).__name__)
            self.error.emit(self._generation, f"视觉理解失败：{type(exc).__name__}")

    @staticmethod
    def _default_provider():
        from core.screen_vision.config import get_fast_direct_provider

        return get_fast_direct_provider()


class ImageVisionSignalRelay(QObject):
    """GUI-affine relay turning worker emissions into GUI-thread signals."""

    finished = Signal(int, str)
    error = Signal(int, str)

    @Slot(int, str)
    def forward_finished(self, gen: int, text: str) -> None:
        self.finished.emit(gen, text)

    @Slot(int, str)
    def forward_error(self, gen: int, message: str) -> None:
        self.error.emit(gen, message)


__all__ = ["ImageVisionWorker", "ImageVisionSignalRelay", "VISION_QUESTION"]
