"""Screen capture service. Screenshots stay in memory and are never saved.

Capture semantics v1 targets:
- primary_screen: the whole primary screen.
- last_non_firefly_window: the window the user was in before a Firefly
  window took focus (HWND recorded on demand by ForegroundContextTracker).
- firefly_companion: the Companion chat window itself (explicit requests
  like "看看这个聊天框" only).
- Legacy aliases: primary (-> primary_screen), active_window (current OS
  foreground).

Stale-HWND policy: a recorded window that no longer exists/visible falls
back to the current foreground (if external) and then to the primary screen,
with ``last_capture_info["capture_fallback_used"] = True``. No provider is
re-run by a fallback; nothing runs in background; nothing is saved.
"""

import ctypes
from datetime import datetime
from io import BytesIO
from typing import Tuple

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

from core.screen_vision.foreground_tracker import foreground_tracker as _fg_tracker
from core.screen_vision.foreground_tracker import (
    get_foreground_hwnd,
    is_firefly_hwnd,
)
from core.screen_vision.models import ScreenFrame

DEFAULT_MAX_EDGE = 1600
DEFAULT_JPEG_QUALITY = 85

_qapp = None

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


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


def _window_hwnd_valid(hwnd: int) -> bool:
    """The recorded HWND still refers to a visible, non-minimized window."""
    if not hwnd or _user32 is None:
        return False
    if not _user32.IsWindow(hwnd) or not _user32.IsWindowVisible(hwnd):
        return False
    return not _user32.IsIconic(hwnd)


def _window_rect(hwnd: int) -> Tuple[int, int, int, int] | None:
    if _user32 is None or not hwnd:
        return None
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _foreground_window_rect() -> Tuple[int, int, int, int]:
    """Physical-pixel rect of the OS foreground window (Windows only)."""
    hwnd = get_foreground_hwnd()
    if not hwnd:
        raise RuntimeError("No foreground window found.")
    rect = _window_rect(hwnd)
    if rect is None:
        raise RuntimeError("GetWindowRect failed for the foreground window.")
    return rect


def _primary_screen_phys(screen) -> Tuple[int, int, int, int]:
    dpr = screen.devicePixelRatio() or 1.0
    geo = screen.geometry()
    # Convert Qt logical screen geometry to physical pixels to match both
    # the Win32 rect and the pixmap's device-pixel coordinate space.
    return (
        round(geo.x() * dpr),
        round(geo.y() * dpr),
        round((geo.x() + geo.width()) * dpr),
        round((geo.y() + geo.height()) * dpr),
    )


MIN_CROP_DIMENSION = 40  # narrower/taller than this is a degenerate sliver


def _crop_rect_for_rect(rect, screen, pixmap) -> Tuple[int, int, int, int] | None:
    """Intersect a physical-pixel window rect with the grabbed primary
    pixmap; None when the window is not (meaningfully) on this screen
    (off-screen, or only a degenerate sliver is visible)."""
    screen_phys = _primary_screen_phys(screen)
    x0 = max(rect[0], screen_phys[0])
    y0 = max(rect[1], screen_phys[1])
    x1 = min(rect[2], screen_phys[2])
    y1 = min(rect[3], screen_phys[3])
    if x1 - x0 < MIN_CROP_DIMENSION or y1 - y0 < MIN_CROP_DIMENSION:
        return None
    return (x0 - screen_phys[0], y0 - screen_phys[1], x1 - screen_phys[0], y1 - screen_phys[1])


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

    def __init__(self):
        # Diagnostics for the last capture: capture_target /
        # capture_fallback_used / fallback_source. Never contains pixels,
        # HWND values are fine but are not logged by callers.
        self.last_capture_info: dict = {}

    def _set_info(self, target: str, fallback_used: bool, fallback_source: str = "") -> None:
        self.last_capture_info = {
            "capture_target": target,
            "capture_fallback_used": fallback_used,
            "fallback_source": fallback_source,
        }

    def capture_primary_screen(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Grab the full primary screen into a JPEG ScreenFrame."""
        _screen, pixmap = _grab_primary_pixmap()
        self._set_info("primary_screen", False)
        return _encode_pixmap(pixmap, max_edge=max_edge, jpeg_quality=jpeg_quality)

    def capture_active_window(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Legacy alias: grab the current OS foreground window region."""
        screen, pixmap = _grab_primary_pixmap()
        rect = _foreground_window_rect()
        crop = _crop_rect_for_rect(rect, screen, pixmap)
        if crop is None:
            raise RuntimeError(
                "Foreground window is not (meaningfully) on the primary screen; "
                "active-window capture currently supports the primary screen only."
            )
        self._set_info("active_window", False)
        return _encode_pixmap(pixmap, crop=crop, max_edge=max_edge, jpeg_quality=jpeg_quality)

    def capture_last_non_firefly_window(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Capture the window the user was in before Firefly took focus.

        Stale-HWND fallback chain: recorded HWND -> current foreground (only
        if it is an external window) -> primary screen. The fallback only
        re-crops; it never re-runs providers."""
        screen, pixmap = _grab_primary_pixmap()
        recorded = _fg_tracker.last_non_firefly_window
        if _window_hwnd_valid(recorded):
            crop = _crop_rect_for_rect(_window_rect(recorded), screen, pixmap)
            if crop is not None:
                self._set_info("last_non_firefly_window", False)
                return _encode_pixmap(pixmap, crop=crop, max_edge=max_edge, jpeg_quality=jpeg_quality)

        current = get_foreground_hwnd()
        if current and not is_firefly_hwnd(current) and _window_hwnd_valid(current):
            crop = _crop_rect_for_rect(_window_rect(current), screen, pixmap)
            if crop is not None:
                self._set_info("last_non_firefly_window", True, "current_foreground")
                return _encode_pixmap(pixmap, crop=crop, max_edge=max_edge, jpeg_quality=jpeg_quality)

        self._set_info("last_non_firefly_window", True, "primary_screen")
        return _encode_pixmap(pixmap, max_edge=max_edge, jpeg_quality=jpeg_quality)

    def capture_firefly_companion(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Capture the Companion chat window itself; falls back to the
        primary screen when no live companion HWND is registered."""
        screen, pixmap = _grab_primary_pixmap()
        companion = _fg_tracker.companion_window
        if _window_hwnd_valid(companion):
            crop = _crop_rect_for_rect(_window_rect(companion), screen, pixmap)
            if crop is not None:
                self._set_info("firefly_companion", False)
                return _encode_pixmap(pixmap, crop=crop, max_edge=max_edge, jpeg_quality=jpeg_quality)

        self._set_info("firefly_companion", True, "primary_screen")
        return _encode_pixmap(pixmap, max_edge=max_edge, jpeg_quality=jpeg_quality)


# Module-level convenience wrappers keep the original PoC API available.
_default_service = ScreenCaptureService()


def capture_primary_screen(max_edge=DEFAULT_MAX_EDGE, jpeg_quality=DEFAULT_JPEG_QUALITY) -> ScreenFrame:
    return _default_service.capture_primary_screen(max_edge, jpeg_quality)


def capture_active_window(max_edge=DEFAULT_MAX_EDGE, jpeg_quality=DEFAULT_JPEG_QUALITY) -> ScreenFrame:
    return _default_service.capture_active_window(max_edge, jpeg_quality)
