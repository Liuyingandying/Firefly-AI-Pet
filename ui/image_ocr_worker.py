"""Reusable async image OCR worker — shared by ExplainBox and PageLens.

Wraps ``core.pdf_processor.RapidOcrBackend`` in a ``QThread``-compatible
``QObject`` so callers can start OCR without blocking the Qt main thread.

Signals
    finished(int, str): (generation, OCR text result)
    error(int, str): (generation, OCR / runtime error message)

Usage
    worker = ImageOcrWorker(image_path, generation=1)
    worker.finished.connect(on_done)
    worker.error.connect(on_err)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

log = logging.getLogger("firefly.image_ocr")


class ImageOcrWorker(QObject):
    """Runs RapidOCR on an image file in a background thread.

    Reuses the same ``RapidOcrBackend`` path as the P8 PDF pipeline — no
    duplicate OCR logic.

    Parameters
        image_path: path to image file
        generation: request id (must match caller's _ocr_generation)
    """

    finished = Signal(int, str)
    error = Signal(int, str)

    def __init__(self, image_path: str, generation: int = 0) -> None:
        super().__init__()
        self._image_path = image_path
        self._generation = generation

    @Slot()
    def run(self) -> None:
        """Execute OCR on ``_image_path`` and emit ``finished`` / ``error``."""
        try:
            from core.pdf_processor import RapidOcrBackend

            image_bytes = Path(self._image_path).read_bytes()
            if QImage.fromData(image_bytes).isNull():
                self.error.emit(
                    self._generation,
                    "OCR 失败：图片文件损坏或格式不支持",
                )
                return
            backend = RapidOcrBackend()
            if not backend.available:
                self.error.emit(self._generation, backend.error or "RapidOCR 后端不可用")
                return
            ocr_text = backend.extract_text(image_bytes)
            self.finished.emit(self._generation, ocr_text.strip())
        except FileNotFoundError:
            self.error.emit(self._generation, "图片文件不存在")
        except Exception as exc:
            self.error.emit(self._generation, f"OCR 失败：{exc}")


class ImageOcrSignalRelay(QObject):
    """GUI-affine relay that turns worker emissions into GUI-thread signals."""

    finished = Signal(int, str)
    error = Signal(int, str)

    @Slot(int, str)
    def forward_finished(self, generation: int, text: str) -> None:
        self.finished.emit(generation, text)

    @Slot(int, str)
    def forward_error(self, generation: int, message: str) -> None:
        self.error.emit(generation, message)
