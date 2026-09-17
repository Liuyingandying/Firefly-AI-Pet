"""CameraCapture — one-shot, in-memory camera frame capture (Vision-1A).

Contract (mirrors the screen-capture privacy invariants):
- Capture happens ONLY inside an explicit user-requested turn.
- Exactly one frame is grabbed per call; the camera is started, the first
  frame is encoded, and the camera is stopped before the call returns.
- The frame stays in memory and never touches disk; nothing is recorded.
- No background capture, no periodic sampling, no worker threads.

Thread note: QCamera needs a live Qt event loop on its own thread. Firefly
invokes ``look()`` from a plain ``threading.Thread`` (no event loop), so the
grab is marshalled to the Qt main thread with a queued signal and the caller
waits on a local QEventLoop. From the main thread the grab runs inline.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import (
    QCamera,
    QMediaCaptureSession,
    QMediaDevices,
    QVideoSink,
)

from core.screen_vision.models import ScreenFrame
from core.screen_vision.screen.capture import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MAX_EDGE,
    _encode_pixmap,
    _ensure_qapp,
)

# Single-frame wait ceiling. A camera that produced nothing within this
# window raises instead of blocking the pet's turn forever.
CAMERA_GRAB_TIMEOUT_MS = 8000

# Margin over the grabber's own watchdog so the worker-side waiter never
# beats the main-thread grabber when both timers race.
_OFF_MAIN_THREAD_WAIT_MARGIN_MS = 2000

# ---------------------------------------------------------------------------
# P0.2 diagnostic tracing (env-gated, behavior-neutral)
# FIREFLY_CAMERA_TRACE=1 enables monotonic timestamps + thread identity at
# every pipeline stage. Never changes business behavior.
# ---------------------------------------------------------------------------

_trace_start = time.monotonic()
_trace_log = logging.getLogger("firefly.camera_trace")


def _trace(event: str, extra: str = "") -> None:
    if not os.environ.get("FIREFLY_CAMERA_TRACE"):
        return
    thread = threading.current_thread()
    try:
        current = QThread.currentThread()
        qname = current.objectName() or type(current).__name__
    except Exception:  # noqa: BLE001
        qname = "?"
    _trace_log.info(
        "CAMTRACE %-24s dt=%7.1fms py_tid=%s py_name=%s qt=%s %s",
        event,
        (time.monotonic() - _trace_start) * 1000.0,
        threading.get_ident(),
        thread.name or "?",
        qname,
        extra,
    )


class CameraUnavailableError(RuntimeError):
    """Raised when no camera device is present, the frame never arrives, or
    the captured frame cannot be encoded. Never leaks image bytes."""


def image_to_frame(
    image: QImage,
    max_edge: int = DEFAULT_MAX_EDGE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> ScreenFrame:
    """Encode one QImage into the in-memory JPEG ScreenFrame the screen
    capture and attachment pipelines use (mirrors
    ``ui.companion_attachment.attachment_to_frame``)."""
    if image.isNull():
        raise CameraUnavailableError("摄像头画面为空，无法编码")
    _ensure_qapp()
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        raise CameraUnavailableError("摄像头画面转换为位图失败")
    return _encode_pixmap(pixmap, max_edge=max_edge, jpeg_quality=jpeg_quality)


class _FrameGrabber(QObject):
    """Owns one QCamera stack and returns the first frame as a ScreenFrame.

    This object lives on the Qt main thread; ``requested`` is emitted from
    whichever thread calls ``capture_camera`` and is delivered here queued.
    """

    requested = Signal(int)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.frame: ScreenFrame | None = None
        self.error_message: str = ""
        self._stack: tuple | None = None

    @Slot(int)
    def run_grab(self, timeout_ms: int) -> None:
        """Start the camera, wait for the first frame, stop immediately."""
        self.frame = None
        self.error_message = ""
        _trace("T4_run_grab_enter", f"timeout_ms={timeout_ms}")

        device = QMediaDevices.defaultVideoInput()
        if device is None or device.isNull():
            self.error_message = "未检测到可用摄像头设备"
            _trace("T8_no_device")
            self.finished.emit()
            return

        camera = QCamera(device)
        session = QMediaCaptureSession()
        sink = QVideoSink(None)
        session.setCamera(camera)
        session.setVideoSink(sink)
        self._stack = (camera, session, sink)

        loop = QEventLoop()
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        first_frame_seen: list[bool] = [False]

        def _on_frame(qframe) -> None:
            if self.frame is not None:
                return
            if not first_frame_seen[0]:
                first_frame_seen[0] = True
                _trace("T7_first_frame_received", f"size={qframe.size()}")
            try:
                self.frame = image_to_frame(qframe.toImage())
            except Exception as exc:  # noqa: BLE001 - surfaced as error text
                self.error_message = f"摄像头画面编码失败: {exc}"
            _trace("T8_capture_result_ready")
            loop.quit()

        def _on_error(error, message) -> None:
            _trace("T8_camera_error", f"error={error} message={message!r}")
            if self.frame is None and not self.error_message:
                self.error_message = f"摄像头错误: {message}"
            loop.quit()

        def _on_watchdog() -> None:
            _trace("T8_watchdog_timeout")
            if self.frame is None and not self.error_message:
                self.error_message = f"摄像头在 {timeout_ms / 1000:.0f} 秒内未返回画面"
            loop.quit()

        def _on_active(active: bool) -> None:
            _trace("T6_camera_active" if active else "T6_camera_inactive")

        sink.videoFrameChanged.connect(_on_frame)
        camera.errorOccurred.connect(_on_error)
        camera.activeChanged.connect(_on_active)
        watchdog.timeout.connect(_on_watchdog)
        try:
            _trace("T5_camera_start")
            camera.start()
            watchdog.start(timeout_ms)
            loop.exec()
        finally:
            camera.stop()
            watchdog.stop()
            sink.videoFrameChanged.disconnect(_on_frame)
            camera.errorOccurred.disconnect(_on_error)
            camera.activeChanged.disconnect(_on_active)
            watchdog.timeout.disconnect(_on_watchdog)
            camera.deleteLater()
            session.deleteLater()
            sink.deleteLater()
            self._stack = None
        _trace("T8_run_grab_done", f"frame={'ok' if self.frame else 'none'} err={self.error_message!r}")
        self.finished.emit()


_grabber: _FrameGrabber | None = None


def _get_grabber() -> _FrameGrabber:
    global _grabber
    if _grabber is None:
        _grabber = _FrameGrabber()
        # Wire the worker-thread request to the main-thread grabber ONCE at
        # creation. The connection is queued at emit time because the
        # grabber lives on the Qt main thread (moved below) while the caller
        # runs on Firefly's worker thread; without it the emit is a no-op and
        # every camera request would time out with "摄像头未返回画面".
        _grabber.requested.connect(_grabber.run_grab)
        app = QCoreApplication.instance()
        if app is not None:
            _grabber.moveToThread(app.thread())
    return _grabber


def _is_main_thread() -> bool:
    app = QCoreApplication.instance()
    return app is None or QThread.currentThread() is app.thread()


class CameraCapture:
    """Grab exactly one camera frame as an in-memory ScreenFrame."""

    def __init__(self, timeout_ms: int = CAMERA_GRAB_TIMEOUT_MS) -> None:
        self._timeout_ms = timeout_ms
        self.last_capture_info: dict = {
            "capture_target": "camera",
            "capture_fallback_used": False,
        }

    def capture_camera(
        self,
        max_edge: int = DEFAULT_MAX_EDGE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> ScreenFrame:
        """Single synchronous camera grab; the camera stops before returning.

        Raises CameraUnavailableError when no device exists, the camera
        errors, no frame arrives within the timeout, or encoding fails.
        """
        grabber = _get_grabber()
        _trace("T2_capture_request_created", f"caller_is_main_thread={_is_main_thread()}")
        if _is_main_thread():
            grabber.run_grab(self._timeout_ms)
        else:
            self._wait_off_main_thread(grabber)

        _trace("T8_result_returned_to_worker", f"frame={'ok' if grabber.frame else 'none'}")
        if grabber.error_message:
            raise CameraUnavailableError(grabber.error_message)
        if grabber.frame is None:
            raise CameraUnavailableError("摄像头未返回画面")
        return grabber.frame

    def _wait_off_main_thread(self, grabber: _FrameGrabber) -> None:
        """Block the caller on a local event loop until the grabber (running
        on the Qt main thread) reports a frame, an error, or its timeout."""
        loop = QEventLoop()
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        grabber.finished.connect(loop.quit)
        watchdog.start(self._timeout_ms + _OFF_MAIN_THREAD_WAIT_MARGIN_MS)
        _trace("T3_request_dispatched_to_qt")
        grabber.requested.emit(self._timeout_ms)
        loop.exec()
        watchdog.stop()
        grabber.finished.disconnect(loop.quit)
        watchdog.timeout.disconnect(loop.quit)
        _trace("T3_worker_wait_released")
