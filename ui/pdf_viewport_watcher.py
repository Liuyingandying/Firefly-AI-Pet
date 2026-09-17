"""PdfViewportWatcher — low-frequency viewport change detector (Ambient PDF).

Every ~2 s (only while an opened PDF is the active context): capture the
current external foreground window (the Edge PDF viewer), fingerprint the
frame, and run local OCR only when the picture actually changed. The built
PdfViewportContext is delivered via ``viewport_context`` on the GUI thread.

Hard guards: no PDF open -> idle; Firefly itself is foreground -> skip
(never reads Firefly's own windows); OCR busy -> skip; no disk writes; no
VLM here.
"""

from __future__ import annotations

import ctypes
import logging
import threading

from PySide6.QtCore import Qt, QObject, QTimer, Signal

from core.pdf_viewport_context import PdfViewportContextBuilder, frame_fingerprint
from core.screen_vision.foreground_tracker import (
    get_foreground_hwnd,
    is_firefly_hwnd,
)

log = logging.getLogger("firefly.paperlens2.viewport")

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def _foreground_window_title() -> str:
    if _user32 is None:
        return ""
    hwnd = get_foreground_hwnd()
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value or ""


def _foreground_process_name() -> str:
    """Image file name of the foreground window's process ("" when unknown)."""
    if _user32 is None:
        return ""
    hwnd = get_foreground_hwnd()
    pid = 0
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(ctypes.c_uint32(pid)))
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        size = ctypes.c_uint32(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return (buffer.value or "").rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


_BROWSER_PROCESSES = ("msedge.exe", "chrome.exe")


def _foreground_is_browser() -> bool:
    """True when the foreground process is a browser (fail-open on unknown)."""
    name = _foreground_process_name()
    if not name:
        return True
    return name in _BROWSER_PROCESSES


class _SignalRelay(QObject):
    finished = Signal(object)


class PdfViewportWatcher(QObject):
    viewport_context = Signal(object)  # PdfViewportContext

    def __init__(
        self,
        store,
        builder: PdfViewportContextBuilder | None = None,
        capture_fn=None,
        interval_ms: int = 2000,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._store = store
        self._builder = builder or PdfViewportContextBuilder()
        self._capture_fn = capture_fn or self._default_capture
        self._interval_ms = interval_ms
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)
        self._last_fingerprint: str | None = None
        self._busy = False
        self._relay = _SignalRelay()
        self._relay.finished.connect(self.viewport_context, Qt.QueuedConnection)

    @staticmethod
    def _default_capture():
        from core.screen_vision.screen.capture import ScreenCaptureService

        return ScreenCaptureService().capture_last_non_firefly_window()

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def is_active(self) -> bool:
        return self._timer.isActive()

    def capture_now(self) -> None:
        """User-initiated refresh: ignore the fingerprint dedupe once."""
        self._last_fingerprint = None
        self.check()

    # ------------------------------------------------------------------

    def check(self) -> None:
        """One observation tick (GUI thread): fingerprint gate, then OCR
        off-thread; the built context arrives via viewport_context."""
        if self._busy:
            return
        pdf = getattr(self._store, "pdf", None)
        if pdf is None:
            return
        foreground = get_foreground_hwnd()
        if not foreground or is_firefly_hwnd(foreground):
            return  # Firefly itself is foreground — never read our windows
        if not _foreground_is_browser():
            return  # an unrelated app is foreground — stop observing
        # The foreground window must still be THIS PDF (title match), so an
        # unrelated tab in front never pollutes the ambient page context.
        # Edge titles the viewer window with the URL when the PDF has no
        # metadata title, so URL fragments (host/last segment) are needles.
        # An empty window title is treated as unknown and allowed.
        title = _foreground_window_title().lower()
        needles = [
            needle.lower()
            for needle in (pdf.title or "", pdf.file_name or "") if needle
        ]
        url = pdf.url or ""
        if url:
            from urllib.parse import unquote, urlparse

            try:
                parsed = urlparse(url)
                if parsed.netloc:
                    needles.append(parsed.netloc.lower())
                last_segment = unquote(parsed.path.rsplit("/", 1)[-1]).lower()
                if last_segment:
                    needles.append(last_segment)
            except ValueError:
                pass
        needles = [needle for needle in needles if needle]
        if title and needles and not any(needle in title for needle in needles):
            return  # user switched to another app/window
        try:
            frame = self._capture_fn()
        except Exception as exc:  # noqa: BLE001 - capture failure is silent
            log.warning("viewport capture failed: %s", type(exc).__name__)
            return
        if frame is None or not frame.image_bytes:
            return
        fingerprint = frame_fingerprint(frame)
        if fingerprint == self._last_fingerprint:
            return  # nothing visibly changed — no OCR
        self._last_fingerprint = fingerprint
        self._busy = True

        total_hint = pdf.total_pages or None

        def work() -> None:
            try:
                context = self._builder.build_from_frame(
                    frame, total_pages_hint=total_hint
                )
                self._relay.finished.emit(context)
            except Exception as exc:  # noqa: BLE001
                log.warning("viewport OCR failed: %s", type(exc).__name__)
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True, name="PdfViewportOcr").start()


__all__ = ["PdfViewportWatcher"]