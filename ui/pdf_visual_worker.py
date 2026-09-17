"""Async visual-region explain worker — PDF Visual Region Explain (Phase 4-B).

Runs the existing Screen Vision provider on an IN-MEMORY cropped ScreenFrame
in a background QThread — mirrors ``ui.image_vision_worker.ImageVisionWorker``
(file-path based) and ``ui.image_ocr_worker.ImageOcrWorker``, but never
touches the disk. Reuses the shared FAST TJU-Qwen vision client
(``get_fast_direct_provider``) — no second vision system, no copied
``answer_direct`` logic.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from core.screen_vision.models import ScreenFrame

log = logging.getLogger("firefly.pdf_visual_region")


class PdfVisualRegionWorker(QObject):
    """Answers one visual-region question from an in-memory crop.

    Parameters
        frame: cropped ScreenFrame (already upscaled by the caller)
        question: full prompt (kind template + context + grounding)
        generation: request id (must match the caller's generation counter)
        provider: optional VisionProvider (injectable for tests); defaults to
            the shared FAST provider factory.
    """

    finished = Signal(int, str)
    error = Signal(int, str)

    def __init__(
        self,
        frame: ScreenFrame,
        question: str,
        generation: int = 0,
        *,
        provider=None,
    ) -> None:
        super().__init__()
        self._frame = frame
        self._question = question
        self._generation = generation
        self._provider = provider

    @Slot()
    def run(self) -> None:
        try:
            if self._frame is None or not self._frame.image_bytes:
                self.error.emit(self._generation, "视觉区域无效")
                return
            provider = self._provider or self._default_provider()
            answer = provider.answer_direct(self._frame, self._question)
            if not isinstance(answer, str) or not answer.strip():
                self.error.emit(self._generation, "视觉解释失败：模型返回为空")
                return
            self.finished.emit(self._generation, answer.strip())
        except Exception as exc:  # provider failures degrade to an error state
            log.warning("visual region explain failed: %s", type(exc).__name__)
            self.error.emit(self._generation, f"视觉解释失败：{type(exc).__name__}")

    @staticmethod
    def _default_provider():
        from core.screen_vision.config import get_fast_direct_provider

        return get_fast_direct_provider()


class PdfVisualRegionRelay(QObject):
    """GUI-affine relay turning worker emissions into GUI-thread signals."""

    finished = Signal(int, str)
    error = Signal(int, str)

    @Slot(int, str)
    def forward_finished(self, gen: int, text: str) -> None:
        self.finished.emit(gen, text)

    @Slot(int, str)
    def forward_error(self, gen: int, message: str) -> None:
        self.error.emit(gen, message)


__all__ = ["PdfVisualRegionWorker", "PdfVisualRegionRelay"]