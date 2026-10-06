"""Offline Vision-1A camera tests — no real camera is ever opened.

Covered:
- the QImage -> ScreenFrame encoding path (synthetic image; shares the
  `_encode_pixmap` pipeline used by screen capture and attachments);
- the no-device failure path (stubbed QMediaDevices);
- the camera trigger words and their mapping to the "camera" capture target;
- the "camera" capture-mode registration and ScreenVisionService wiring with
  an injected fake capture service.
"""

from datetime import datetime

import pytest
from PySide6.QtGui import QGuiApplication, QImage

from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.screen import camera as camera_module
from core.screen_vision.screen.camera import (
    CameraCapture,
    CameraUnavailableError,
    image_to_frame,
)
from core.screen_vision.service import (
    CAPTURE_MODES,
    _CAPTURE_METHOD_BY_MODE,
    ScreenVisionService,
)
from core.screen_vision.trigger import (
    CAPTURE_CAMERA,
    is_camera_vision_request,
    resolve_capture_target,
)


@pytest.fixture(scope="module")
def qapp():
    """QPixmap encoding needs a QGuiApplication; reuse any existing one."""
    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


# ------------------------------------------------------- QImage -> ScreenFrame


def test_image_to_frame_produces_jpeg_screen_frame(qapp):
    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(0xFF6060)

    frame = image_to_frame(image)

    assert isinstance(frame, ScreenFrame)
    assert frame.mime_type == "image/jpeg"
    assert frame.width == 64 and frame.height == 48
    assert frame.image_bytes.startswith(b"\xff\xd8")  # JPEG magic
    assert frame.captured_at is not None


def test_image_to_frame_scales_down_overlarge_frames(qapp):
    image = QImage(3200, 2000, QImage.Format_RGB32)
    image.fill(0x6060FF)

    frame = image_to_frame(image)

    assert max(frame.width, frame.height) == 1600  # DEFAULT_MAX_EDGE
    assert frame.image_bytes.startswith(b"\xff\xd8")


def test_image_to_frame_rejects_null_image():
    with pytest.raises(CameraUnavailableError):
        image_to_frame(QImage())


# ------------------------------------------- no device -> clear exception


class _NoDevices:
    @staticmethod
    def defaultVideoInput():
        return None

    @staticmethod
    def videoInputs():
        return []


def test_capture_camera_without_device_raises(qapp, monkeypatch):
    monkeypatch.setattr(camera_module, "QMediaDevices", _NoDevices)

    with pytest.raises(CameraUnavailableError, match="摄像头"):
        CameraCapture().capture_camera()


# -------------------------------------------------------- trigger words


@pytest.mark.parametrize("text", [
    "看看我",
    "看一下我",
    "摄像头",
    "我的样子",
    "看看我的样子",
    "用摄像头看看我",
])
def test_camera_trigger_words(text):
    assert is_camera_vision_request(text) is True, text
    assert resolve_capture_target(text) == CAPTURE_CAMERA, text


def test_camera_gate_does_not_steal_screen_requests():
    # Screen/companion routing stays exactly as before.
    assert is_camera_vision_request("看看我的屏幕") is False
    assert resolve_capture_target("看看我的屏幕") == "primary_screen"
    assert is_camera_vision_request("看看我在做什么") is True
    assert resolve_capture_target("看看我在做什么") == "camera"
    assert is_camera_vision_request("看看我现在在做什么") is True
    assert resolve_capture_target("看看我现在在做什么") == "camera"
    assert is_camera_vision_request("看看这个聊天框") is False
    assert resolve_capture_target("看看这个聊天框") == "firefly_companion"
    assert is_camera_vision_request("今天怎么样") is False


# ------------------------------------------- capture-mode mapping + wiring


def test_camera_mode_is_registered():
    assert CAPTURE_CAMERA == "camera"
    assert "camera" in CAPTURE_MODES
    assert _CAPTURE_METHOD_BY_MODE["camera"] == "capture_camera"


class _FakeCapture:
    def __init__(self):
        self.calls = []
        self.last_capture_info = {
            "capture_target": "camera",
            "capture_fallback_used": False,
        }

    def capture_camera(self):
        self.calls.append("camera")
        return ScreenFrame(32, 16, "image/jpeg", b"\xff\xd8camera", datetime.now())


class _FakeVision:
    def inspect(self, frame, instruction=""):
        assert isinstance(frame, ScreenFrame)
        return ScreenObservation(scene_summary="看到一个身影")


class _FakeReasoning:
    def answer(self, question, payload):
        return "我看到你了"


def test_look_with_camera_mode_uses_camera_capture():
    capture = _FakeCapture()
    service = ScreenVisionService(
        vision_provider=_FakeVision(),
        reasoning_provider=_FakeReasoning(),
        capture_service=capture,
    )

    result = service.look("看看我", capture_mode="camera")

    assert capture.calls == ["camera"]
    assert result.answer == "我看到你了"
    assert result.meta["capture_target"] == "camera"
    assert result.meta["capture_mode"] == "camera"
