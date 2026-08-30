"""Screen capture service. Screenshots stay in memory and are never saved.

capture_active_window crops the foreground window region out of a fresh
full-screen grab (Windows: GetForegroundWindow + GetWindowRect via ctypes).
Limitations, by design: the crop contains whatever is visibly composited in
that region, and only windows on the primary screen are supported.
"""

import ctypes
from datetime import datetime
from io import BytesIO
from typing import Tuple

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

from core.screen_vision.models import ScreenFrame

DEFAULT_MAX_EDGE = 1600
DEFAULT_JPEG_QUALITY = 85

_qapp = None


def _ensure_qapp() -> QGuiApplication:
    """Create the QGuiApplication exactly once per process."""
    global _qapp
    if _qapp is None:
        _qapp = QGuiApplication.instance() or QGuiApplication([])
    return _qapp


def _grab_primary_pixmap():
    app = _ensure_qapp()
    screen = app.primaryScreen()
    if screen is None:
        raise RuntimeError("No primary screen available for capture.")
    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        raise RuntimeError("QScreen.grabWindow returned an empty pixmap.")
    return screen, pixmap


def _foreground_window_rect() -> Tuple[int, int, int, int]:
    """Physical-pixel rect of the OS foreground window (Windows only)."""

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("No foreground window found.")
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed for the foreground window.")
    return rect.left, rect.top, rect.right, rect.bottom


def _encode_pixmap(pixmap, crop=None, max_edge=DEFAULT_MAX_EDGE,
                   jpeg_quality=DEFAULT_JPEG_QUALITY) -> ScreenFrame:
    """Encode a pixmap (optionally cropped) into an in-memory JPEG frame."""
    png_buffer = QBuffer()
    png_buffer.open(QIODevice.WriteOnly)
    if not pixmap.save(png_buffer, "PNG"):
        raise RuntimeError("Failed to encode grabbed pixmap in memory.")
    png_buffer.close()

    pil_image = Image.open(BytesIO(png_buffer.data()))
    if crop:
        pil_image = pil_image.crop(crop)
    if pil_image.mode == "RGBA":
        background = Image.new("RGB", pil_image.size, (0, 0, 0))
        background.paste(pil_image, mask=pil_image.split()[3])
        pil_image = background
    else:
        pil_image = pil_image.convert("RGB")

    if max_edge and max(pil_image.size) > max_edge:
        scale = max_edge / max(pil_image.size)
        new_size = (round(pil_image.width * scale), round(pil_image.height * scale))
        pil_image = pil_image.resize(new_size, Image.LANCZOS)

    encode_buffer = BytesIO()
    pil_image.save(encode_buffer, format="JPEG", quality=jpeg_quality)
    encoded = encode_buffer.getvalue()

    return ScreenFrame(
        width=pil_image.width,
        height=pil_image.height,
        mime_type="image/jpeg",
        image_bytes=encoded,
        captured_at=datetime.now(),
    )


class ScreenCaptureService:
    """Produces in-memory ScreenFrames on demand. Nothing runs in background."""

    def capture_primary_screen(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Grab the full primary screen into a JPEG ScreenFrame."""
        _screen, pixmap = _grab_primary_pixmap()
        return _encode_pixmap(pixmap, max_edge=max_edge, jpeg_quality=jpeg_quality)

    def capture_active_window(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Grab the current OS foreground window region (primary screen only)."""
        screen, pixmap = _grab_primary_pixmap()
        rect = _foreground_window_rect()
        dpr = screen.devicePixelRatio() or 1.0
        geo = screen.geometry()
        # Convert Qt logical screen geometry to physical pixels to match both
        # the Win32 rect and the pixmap's device-pixel coordinate space.
        screen_phys = (
            round(geo.x() * dpr),
            round(geo.y() * dpr),
            round((geo.x() + geo.width()) * dpr),
            round((geo.y() + geo.height()) * dpr),
        )
        x0 = max(rect[0], screen_phys[0])
        y0 = max(rect[1], screen_phys[1])
        x1 = min(rect[2], screen_phys[2])
        y1 = min(rect[3], screen_phys[3])
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise RuntimeError(
                "Foreground window is not (meaningfully) on the primary screen; "
                "active-window capture currently supports the primary screen only."
            )
        crop = (x0 - screen_phys[0], y0 - screen_phys[1], x1 - screen_phys[0], y1 - screen_phys[1])
        return _encode_pixmap(pixmap, crop=crop, max_edge=max_edge, jpeg_quality=jpeg_quality)


# Module-level convenience wrappers keep the original PoC API available.
_default_service = ScreenCaptureService()


def capture_primary_screen(max_edge=DEFAULT_MAX_EDGE, jpeg_quality=DEFAULT_JPEG_QUALITY) -> ScreenFrame:
    return _default_service.capture_primary_screen(max_edge, jpeg_quality)


def capture_active_window(max_edge=DEFAULT_MAX_EDGE, jpeg_quality=DEFAULT_JPEG_QUALITY) -> ScreenFrame:
    return _default_service.capture_active_window(max_edge, jpeg_quality)
