"""In-memory image attachments for the Companion chat window.

v1 scope: exactly ONE image per turn. The image lives only in memory — no
file is copied into the project, nothing is persisted, and the original file
is never modified. The only bytes that leave the machine are the JPEG data URI
sent to the current vision provider (DeepSeek Vision by default).

Privacy: the absolute path is never stored; only ``display_name`` (the bare
filename) is kept for the chip. Conversation history stores only the
``[Image attachment]`` placeholder plus the user's text — never base64, raw
bytes, or the local path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap

from core.screen_vision.models import ScreenFrame

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
THUMBNAIL_SIZE = 56  # px (target 56-72)
THUMBNAIL_RADIUS = 8

DEFAULT_ATTACHMENT_QUESTION = "看看这张图片，告诉我你注意到了什么。"
HISTORY_IMAGE_PLACEHOLDER = "[Image attachment]"
UNREADABLE_MESSAGE = "这张图片好像无法读取。"
TOO_LARGE_MESSAGE = "图片超过 20MB，无法添加。"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_WEBP_RIFF = b"RIFF"
_WEBP_IDENTIFIER = b"WEBP"


class AttachmentError(Exception):
    """A user-facing attachment problem (decode / size / unsupported)."""


@dataclass
class AttachmentImage:
    """In-memory single image attachment. Never persisted anywhere."""

    image: QImage
    mime_type: str
    display_name: str
    size_bytes: int


def _guess_image_mime(data: bytes) -> str | None:
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    if data[:3] == _JPEG_MAGIC:
        return "image/jpeg"
    if data[:4] == _WEBP_RIFF and data[8:12] == _WEBP_IDENTIFIER:
        return "image/webp"
    return None


def decode_attachment_bytes(data: bytes, display_name: str) -> AttachmentImage:
    """Decode PNG/JPEG/WebP bytes into an in-memory attachment."""
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(TOO_LARGE_MESSAGE)
    mime = _guess_image_mime(data)
    if mime is None:
        raise AttachmentError(UNREADABLE_MESSAGE)
    image = QImage.fromData(data)
    if image.isNull():
        raise AttachmentError(UNREADABLE_MESSAGE)
    return AttachmentImage(
        image=image,
        mime_type=mime,
        display_name=display_name,
        size_bytes=len(data),
    )


def load_attachment_file(path: Path) -> AttachmentImage:
    """Load one local image file (bounded read; original file untouched)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AttachmentError(UNREADABLE_MESSAGE) from exc
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(TOO_LARGE_MESSAGE)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(UNREADABLE_MESSAGE) from exc
    return decode_attachment_bytes(data, path.name)


def qimage_to_attachment(image: QImage, display_name: str) -> AttachmentImage:
    """Wrap an already-decoded in-memory QImage (clipboard / drag) as an attachment."""
    if image.isNull():
        raise AttachmentError(UNREADABLE_MESSAGE)
    if image.sizeInBytes() > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(TOO_LARGE_MESSAGE)
    return AttachmentImage(
        image=image,
        mime_type="image/png",
        display_name=display_name,
        size_bytes=image.sizeInBytes(),
    )


def attachment_to_frame(attachment: AttachmentImage) -> ScreenFrame:
    """Preprocess into the same in-memory JPEG ScreenFrame FAST vision uses.

    Reuses the REAL PASS Fast preprocessing (``_encode_pixmap``): RGBA
    composite, resize only when the longest edge exceeds 1600px (LANCZOS),
    JPEG quality 85 — all in memory, never upscaling small images, never
    touching disk.
    """
    from core.screen_vision.screen.capture import _encode_pixmap

    pixmap = QPixmap.fromImage(attachment.image)
    if pixmap.isNull():
        raise AttachmentError(UNREADABLE_MESSAGE)
    return _encode_pixmap(pixmap)


def rounded_pixmap(source: QPixmap, radius: int = THUMBNAIL_RADIUS) -> QPixmap:
    """Return a copy of ``source`` clipped to a rounded rectangle."""
    result = QPixmap(source.size())
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, source.width(), source.height(), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return result
