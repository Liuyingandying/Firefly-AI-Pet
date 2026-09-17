"""Scratchpad v1 service — intake policy for dropped / pasted / typed content.

The UI layer converts raw Qt MIME payloads into one of three calls:

- ``add_text``                     plain user text
- ``add_image_data``               decoded image bytes (already validated as
                                   decodable by the UI via QImageReader)
- ``add_image_file``               a local image file path

Everything else (remote URLs, unsupported documents, oversized images)
returns a structured ``AddResult(ok=False, message=...)`` — never an
exception to the widget and never a network access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import ScratchpadItem, ScratchpadSource
from .store import ScratchpadStore, normalize_image_extension

# v1 size guard — a single oversized image must never stall Firefly.
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

REMOTE_IMAGE_HINT = "这个图片链接暂时不能直接收进记事本，可以先保存或截图后拖给我。"
TOO_LARGE_MESSAGE = "这张图片太大了，暂时没有放进记事本。"
UNSUPPORTED_MESSAGE = "这种文件类型暂时收不进记事本。"
DECODE_FAILED_MESSAGE = "这张图片读不出来，暂时没有放进记事本。"
EMPTY_TEXT_MESSAGE = "内容是空的。"


@dataclass(frozen=True, slots=True)
class AddResult:
    ok: bool
    item: ScratchpadItem | None
    message: str = ""

    @staticmethod
    def failure(message: str) -> "AddResult":
        return AddResult(ok=False, item=None, message=message)


def is_remote_url(text: str) -> bool:
    """True when the (stripped) text is a single http(s) URL."""
    candidate = str(text or "").strip()
    if "\n" in candidate or " " in candidate:
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class ScratchpadService:
    """Thin intake policy over the store. Qt-free except image decoding,
    which uses QImageReader for real-format validation (never trust the
    extension alone)."""

    def __init__(
        self,
        store: ScratchpadStore,
        *,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        decoder=None,
    ) -> None:
        self.store = store
        self.max_image_bytes = int(max_image_bytes)
        # ``decoder(path_or_bytes) -> bool``: injectable so tests can fake
        # decode success/failure; defaults to a Qt QImageReader probe.
        self._decoder = decoder if decoder is not None else self._default_decoder

    # ------------------------------------------------------------- text

    def add_text(self, text: str, source: ScratchpadSource) -> AddResult:
        cleaned = str(text or "").strip()
        if not cleaned:
            return AddResult.failure(EMPTY_TEXT_MESSAGE)
        if is_remote_url(cleaned):
            # A single pasted/dropped URL is most likely a dragged image or
            # link — v1 never fetches it, never embeds it as text.
            return AddResult.failure(REMOTE_IMAGE_HINT)
        item = self.store.add_text(cleaned, source)
        return AddResult(ok=True, item=item)

    # ------------------------------------------------------------ images

    def add_image_file(self, path: Path | str, source: ScratchpadSource) -> AddResult:
        file_path = Path(path)
        ext = normalize_image_extension(file_path.suffix)
        if ext is None:
            return AddResult.failure(UNSUPPORTED_MESSAGE)
        try:
            size = file_path.stat().st_size
        except OSError:
            return AddResult.failure(DECODE_FAILED_MESSAGE)
        if size > self.max_image_bytes:
            return AddResult.failure(TOO_LARGE_MESSAGE)
        if size == 0:
            return AddResult.failure(DECODE_FAILED_MESSAGE)
        if not self._decoder(str(file_path)):
            return AddResult.failure(DECODE_FAILED_MESSAGE)
        try:
            item = self.store.add_image_copy(
                file_path, safe_extension=file_path.suffix, source=source
            )
        except OSError:
            return AddResult.failure("图片保存失败，磁盘可能不可写。")
        return AddResult(ok=True, item=item)

    def add_image_data(
        self, data: bytes, *, suggested_extension: str = ".png",
        source: ScratchpadSource = ScratchpadSource.DRAG_DROP,
    ) -> AddResult:
        if not data:
            return AddResult.failure(DECODE_FAILED_MESSAGE)
        if len(data) > self.max_image_bytes:
            return AddResult.failure(TOO_LARGE_MESSAGE)
        ext = normalize_image_extension(suggested_extension) or ".png"
        if not self._decoder(data):
            return AddResult.failure(DECODE_FAILED_MESSAGE)
        try:
            item = self.store.add_image_bytes(data, safe_extension=ext, source=source)
        except OSError:
            return AddResult.failure("图片保存失败，磁盘可能不可写。")
        return AddResult(ok=True, item=item)

    # ----------------------------------------------------------- deletes

    def delete(self, item_id: str) -> bool:
        return self.store.delete(item_id)

    # ----------------------------------------------------------- decoder

    @staticmethod
    def _default_decoder(target: object) -> bool:
        """Qt QImageReader probe: a file/bytes is an image only if Qt can
        actually decode it — the extension alone is never trusted."""
        from PySide6.QtGui import QImage, QImageReader
        from PySide6.QtCore import QBuffer, QIODevice

        if isinstance(target, bytes):
            reader = QImageReader()
            buffer = QBuffer()
            buffer.setData(target)
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader.setDevice(buffer)
        else:
            reader = QImageReader(str(target))
        reader.setAutoTransform(True)
        image = reader.read()
        return (not image.isNull()) and isinstance(image, QImage)
