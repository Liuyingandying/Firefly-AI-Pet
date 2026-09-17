"""Scratchpad v1 drop controller — Qt MIME classification → core service.

Lives between PetOverlay's drop events and core.scratchpad.  Pure policy:

- hasImage()        → decode and store as PNG (C in the phase spec)
- local file URLs   → copy the first supported image file (B)
- plain text        → store as a text item (A)
- remote image URL  → polite hint, NEVER downloaded (§7)
- anything else     → unsupported hint

The controller never touches Memory / Conversation / Learning stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QMimeData
from PySide6.QtGui import QImage, QImageReader

from core.scratchpad.models import ScratchpadSource
from core.scratchpad.service import (
    REMOTE_IMAGE_HINT,
    UNSUPPORTED_MESSAGE,
    ScratchpadService,
)

SUPPORTED_DROP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass(frozen=True, slots=True)
class DropOutcome:
    ok: bool
    message: str


class ScratchpadDropController:
    """Stateless policy object installed on the pet via set_drop_handler."""

    def __init__(self, service: ScratchpadService) -> None:
        self._service = service

    # ------------------------------------------------------------- probe

    def can_accept(self, mime: QMimeData) -> bool:
        """Hover/drop admission policy.  Qt reports hasText()==True for
        URL-only payloads, so the URL branch is authoritative and never
        falls through to the text branch."""
        if mime is None:
            return False
        if mime.hasImage():
            return True
        if mime.hasUrls():
            for url in mime.urls():
                if not url.isLocalFile():
                    # remote URL: accept on hover so the drop can deliver
                    # the polite no-download hint (v1 never downloads)
                    return True
                if Path(url.toLocalFile()).suffix.lower() in SUPPORTED_DROP_SUFFIXES:
                    return True
            return False  # only unsupported local files -> silent deny
        if mime.hasText():
            return bool(mime.text().strip())
        return False

    # ------------------------------------------------------------- handle

    def handle(self, mime: QMimeData) -> DropOutcome:
        if mime is None:
            return DropOutcome(ok=False, message=UNSUPPORTED_MESSAGE)

        # C — in-memory image data wins (some apps hand raw bitmaps over)
        if mime.hasImage():
            image = self._image_from_mime(mime)
            if image is None or image.isNull():
                return DropOutcome(ok=False, message="这张图片读不出来，暂时没有放进记事本。")
            result = self._service.add_image_data(
                self._png_bytes(image), source=ScratchpadSource.DRAG_DROP
            )
            return self._outcome(result)

        # B — local image files (first supported one)
        if mime.hasUrls():
            remote_seen = False
            for url in mime.urls():
                if not url.isLocalFile():
                    remote_seen = True
                    continue
                path = Path(url.toLocalFile())
                if path.suffix.lower() not in SUPPORTED_DROP_SUFFIXES:
                    continue
                result = self._service.add_image_file(
                    path, source=ScratchpadSource.DRAG_DROP
                )
                if result.ok:
                    return DropOutcome(ok=True, message="记下啦")
                return DropOutcome(ok=False, message=result.message)
            if remote_seen:
                return DropOutcome(ok=False, message=REMOTE_IMAGE_HINT)
            return DropOutcome(ok=False, message=UNSUPPORTED_MESSAGE)

        # A — plain text (a single http(s) URL is redirected to the hint by
        # the service: v1 never downloads remote content)
        if mime.hasText():
            result = self._service.add_text(
                mime.text(), source=ScratchpadSource.DRAG_DROP
            )
            return self._outcome(result)

        return DropOutcome(ok=False, message=UNSUPPORTED_MESSAGE)

    # ------------------------------------------------------------ helpers

    def ingest_text(self, text: str) -> DropOutcome:
        return self._outcome(
            self._service.add_text(text, source=ScratchpadSource.MANUAL)
        )

    def ingest_image_file(self, path: Path | str) -> DropOutcome:
        return self._outcome(
            self._service.add_image_file(path, source=ScratchpadSource.MANUAL)
        )

    def _outcome(self, result) -> DropOutcome:
        return DropOutcome(ok=bool(result.ok), message=result.message or (
            "记下啦" if result.ok else UNSUPPORTED_MESSAGE
        ))

    @staticmethod
    def _image_from_mime(mime: QMimeData) -> QImage | None:
        image = mime.imageData()
        if isinstance(image, QImage):
            return image
        reader = QImageReader()
        buffer = QBuffer()
        buffer.setData(mime.data("image/png"))
        reader.setDevice(buffer)
        return reader.read()

    @staticmethod
    def _png_bytes(image: QImage) -> bytes:
        from PySide6.QtCore import QIODevice

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())
