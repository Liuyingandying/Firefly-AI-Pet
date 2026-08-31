"""Companion attachment model — one in-memory image OR one document per turn.

Image attachments keep the v1 behaviour: the image lives only in memory, is
preprocessed like FAST Screen Vision (longest edge 1600px, JPEG quality 85),
and is answered through DeepSeek Vision one-shot. Document attachments are
parsed locally into a :class:`~core.document_attachment.DocumentContext`;
only question-relevant excerpts go to the text provider. Nothing is ever
copied into the project or persisted; history stores only a safe placeholder.

Privacy: the absolute path is never stored; only ``display_name`` (the bare
filename) is kept for the chip. Conversation history stores only the
``[Image attachment]`` / ``[Document attachment: <name>]`` placeholders plus
the user's text — never base64, raw bytes, extracted text, or the local path.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap

from core.document_attachment import (
    DOCUMENT_KIND_LIMITS,
    DocumentContext,
    DocumentParseError,
    parse_document_bytes,
)
from core.screen_vision.models import ScreenFrame

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
ALLOWED_DOCUMENT_EXTENSIONS = (
    ".pdf", ".docx", ".pptx", ".txt", ".md", ".xlsx", ".csv",
)
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS + ALLOWED_DOCUMENT_EXTENSIONS
THUMBNAIL_SIZE = 56  # px (target 56-72)
THUMBNAIL_RADIUS = 8

DEFAULT_ATTACHMENT_QUESTION = "看看这张图片，告诉我你注意到了什么。"
HISTORY_IMAGE_PLACEHOLDER = "[Image attachment]"
HISTORY_DOCUMENT_PLACEHOLDER = "[Document attachment: {name}]"
DEFAULT_DOCUMENT_QUESTION = "总结这个文档的主要内容。"
UNREADABLE_MESSAGE = "这张图片好像无法读取。"
TOO_LARGE_MESSAGE = "图片超过 20MB，无法添加。"
DOCUMENT_TOO_LARGE_MESSAGE = "文件太大，无法添加。"
LEGACY_DOCUMENT_MESSAGE = "暂时支持 DOCX / PPTX / XLSX，新版格式可以直接读取。"

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


class DocumentAttachment:
    """In-memory single document attachment with thread-safe parse state.

    ``source_bytes`` is held only until background parsing finishes and is
    then released; the parsed :class:`DocumentContext` stays in memory for
    follow-up questions.
    """

    def __init__(
        self,
        *,
        display_name: str,
        kind: str,
        original_size: int,
        source_bytes: bytes,
    ) -> None:
        self.display_name = display_name
        self.kind = kind
        self.original_size = original_size
        self._source_bytes = source_bytes
        self._context: DocumentContext | None = None
        self._parse_state = "parsing"
        self._parse_error: str | None = None
        # In-memory, session-scoped summary cache (outline + group summaries).
        # Lives only on this attachment object: cleared when the attachment is
        # removed or replaced. Never written to disk / memory / history.
        self._summary_cache: dict = {}
        self._lock = threading.RLock()

    @property
    def summary_cache(self) -> dict:
        with self._lock:
            return self._summary_cache

    def clear_summary_cache(self) -> None:
        with self._lock:
            self._summary_cache = {}

    @property
    def context(self) -> DocumentContext | None:
        with self._lock:
            return self._context

    @property
    def parse_state(self) -> str:
        with self._lock:
            return self._parse_state

    @property
    def parse_error(self) -> str | None:
        with self._lock:
            return self._parse_error

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._parse_state == "ready" and self._context is not None

    def take_source_bytes(self) -> bytes:
        with self._lock:
            return self._source_bytes

    def release_source_bytes(self) -> None:
        with self._lock:
            self._source_bytes = b""

    def mark_ready(self, context: DocumentContext) -> None:
        with self._lock:
            self._context = context
            self._parse_state = "ready"
            self._parse_error = None
            self._source_bytes = b""

    def mark_error(self, message: str) -> None:
        with self._lock:
            self._parse_state = "error"
            self._parse_error = message
            self._context = None
            self._source_bytes = b""


# ---------------------------------------------------------------- kind detection


def detect_kind_from_path(path: Path) -> str | None:
    """Return attachment kind for a local file path, or None if unsupported."""
    return detect_kind_from_name(path.name)


def detect_kind_from_name(name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    if suffix in ALLOWED_IMAGE_EXTENSIONS:
        return "image"
    return {
        ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx",
        ".txt": "txt", ".md": "md", ".xlsx": "xlsx", ".csv": "csv",
    }.get(suffix)


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


def load_document_file(path: Path) -> DocumentAttachment:
    """Load one local document file into a pending DocumentAttachment.

    The file is read (bounded) and parsed in a later background step; the
    original file is never modified or copied into the project.
    """
    path = Path(path)
    kind = detect_kind_from_path(path)
    if kind is None or kind == "image":
        raise AttachmentError(LEGACY_DOCUMENT_MESSAGE)
    limit = DOCUMENT_KIND_LIMITS[kind]
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AttachmentError(UNREADABLE_MESSAGE) from exc
    if size > limit:
        raise AttachmentError(DOCUMENT_TOO_LARGE_MESSAGE)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(UNREADABLE_MESSAGE) from exc
    return DocumentAttachment(
        display_name=path.name,
        kind=kind,
        original_size=size,
        source_bytes=data,
    )


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
