"""Camera Vision managed-plugin and Companion routing integration."""

from __future__ import annotations

from datetime import datetime
from threading import Event, get_ident
from time import monotonic, sleep
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType
from core.plugin_loader import MANAGED_PLUGIN_IDS, PluginLoader
from core.quick_tools import QuickToolsRegistry
from core.screen_vision.models import ScreenFrame, ScreenObservation, ScreenVisionResult
from core.screen_vision.provider_errors import ProviderHTTPError
from core.screen_vision.screen.camera import CameraUnavailableError
from core.screen_vision.trigger import (
    CAPTURE_CAMERA,
    CAPTURE_PRIMARY_SCREEN,
    is_camera_vision_request,
    resolve_capture_target,
)
from ui.character_conversation_runner import (
    CAMERA_VISION_DISABLED_REPLY,
    CAMERA_VISION_FAILURE_REPLY,
    CAMERA_VISION_UNAVAILABLE_REPLY,
    CharacterConversationRunner,
)
from ui.quick_tools_popover import QuickToolsPopover


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _RecordingVisionService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error

    def look(self, question: str, capture_mode: str):
        self.calls.append((question, capture_mode))
        if self.error is not None:
            raise self.error
        return ScreenVisionResult(
            observation=ScreenObservation(scene_summary="看到用户坐在桌前"),
            answer="我看到你正坐在桌前。",
            timings={},
            meta={"capture_mode": capture_mode, "direct_one_shot": True},
        )


@pytest.mark.parametrize(
    "text",
    [
        "看看我",
        "萤宝看看我",
        "你看看我现在在干嘛",
        "看我现在在干嘛",
        "用摄像头看看",
        "看一下摄像头",
        "你能看到我吗",
    ],
)
def test_camera_intents_route_to_camera(text: str) -> None:
    assert is_camera_vision_request(text)
    assert resolve_capture_target(text) == CAPTURE_CAMERA


def test_screen_intent_stays_screen() -> None:
    assert not is_camera_vision_request("看看我的屏幕")
    assert resolve_capture_target("看看我的屏幕") == CAPTURE_PRIMARY_SCREEN
    assert not is_camera_vision_request("看看我电脑上是什么")


def test_camera_on_calls_camera_once_and_returns_companion_answer() -> None:
    service = _RecordingVisionService()
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    events, answer = runner.perform("萤宝看看我")
    assert service.calls == [("萤宝看看我", "camera")]
    assert answer == "我看到你正坐在桌前。"
    assert events[-1].type == AgentEventType.FINAL
    assert runner.last_camera_observation == answer


def test_camera_off_never_calls_capture_route_and_explains_toggle() -> None:
    service = _RecordingVisionService()
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: False,
    )
    _events, answer = runner.perform("看看我")
    assert service.calls == []
    assert answer == CAMERA_VISION_DISABLED_REPLY


def test_camera_unavailable_is_identified_as_camera_vision() -> None:
    service = _RecordingVisionService(error=CameraUnavailableError("none"))
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    _events, answer = runner.perform("看看我")
    assert service.calls == [("看看我", "camera")]
    assert answer == CAMERA_VISION_UNAVAILABLE_REPLY
    assert "屏幕" not in answer


def test_camera_provider_error_is_user_friendly() -> None:
    """A camera provider failure surfaces a friendly reply; the internal
    exception type/HTTP/billing details never reach the user."""
    service = _RecordingVisionService(error=ProviderHTTPError(402, "Insufficient Balance"))
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    _events, answer = runner.perform("看看我")
    assert answer == CAMERA_VISION_FAILURE_REPLY
    assert "ProviderHTTPError" not in answer
    assert "HTTP" not in answer
    assert "402" not in answer
    assert "Balance" not in answer
    assert "屏幕" not in answer
    assert not answer.startswith("Traceback")


def test_camera_http_error_does_not_crash() -> None:
    service = _RecordingVisionService(error=ProviderHTTPError(500, "upstream"))
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    events, answer = runner.perform("看看我")
    assert answer == CAMERA_VISION_FAILURE_REPLY
    assert events[-1].type == AgentEventType.FINAL


def test_screen_vision_error_does_not_affect_camera_vision() -> None:
    service = _RecordingVisionService(error=ProviderHTTPError(500, "screen down"))
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    _events, answer = runner.perform("看看我的屏幕")
    # The screen failure path took its own reply; the camera-friendly copy
    # is not used for a screen turn.
    assert "屏幕" in answer
    assert "我的视觉分析服务" not in answer
    # Same service, camera turn still routed to the camera and handled.
    service.error = None
    _events, answer2 = runner.perform("看看我")
    assert answer2 == "我看到你正坐在桌前。"
    assert service.calls[-1] == ("看看我", "camera")


def test_camera_vision_error_does_not_affect_normal_chat() -> None:
    service = _RecordingVisionService(error=ProviderHTTPError(402, "no balance"))
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: True,
    )
    _events, answer = runner.perform("看看我")
    assert answer == CAMERA_VISION_FAILURE_REPLY
    # A later camera turn recovers cleanly (no stuck state).
    service.error = None
    _events, answer2 = runner.perform("萤宝看看我")
    assert answer2 == "我看到你正坐在桌前。"
    assert runner.last_camera_observation == answer2


def test_screen_request_bypasses_camera_plugin_gate() -> None:
    service = _RecordingVisionService()
    runner = CharacterConversationRunner(
        screen_vision_service=service,
        camera_vision_enabled=lambda: False,
    )
    _events, answer = runner.perform("看看我的屏幕")
    assert service.calls == [("看看我的屏幕", "primary_screen")]
    assert answer == "我看到你正坐在桌前。"


def test_fixture_frame_is_in_memory_only() -> None:
    frame = ScreenFrame(4, 4, "image/jpeg", b"frame", datetime.now())
    assert frame.image_bytes == b"frame"


def test_real_managed_catalog_renders_only_managed_product_tools(qapp, monkeypatch) -> None:
    from pathlib import Path
    from core.path_config import PLUGIN_ROOT_ENV
    monkeypatch.setenv(PLUGIN_ROOT_ENV, str(Path(__file__).resolve().parents[1] / "extensions"))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    try:
        manifests = loader.load()
        assert [manifest.id for manifest in manifests] == [
            plugin_id for plugin_id in MANAGED_PLUGIN_IDS if plugin_id != "tju-info-retrieval"
        ]  # The private external retrieval adapter is not bundled.
        assert [manifest.name for manifest in manifests] == [
            "Video Analysis",
            "Camera Vision",
            "Learning Enhancements",
            "Voice",
        ]
        popover = QuickToolsPopover(
            registry,
            plugin_is_enabled=loader.is_plugin_enabled,
            plugin_set_enabled=loader.set_plugin_enabled,
        )
        assert list(popover._cards) == [manifest.id for manifest in manifests]
        assert "firefly-vision-demo" not in popover._cards
        popover.close()
    finally:
        loader.shutdown()


@pytest.mark.skip(reason="External runtime-plugin asynchronous-probe revision is not bundled in the main repo")
def test_camera_plugin_probe_does_not_block_gui(qapp, monkeypatch) -> None:
    from pathlib import Path
    from core.path_config import PLUGIN_ROOT_ENV
    monkeypatch.setenv(PLUGIN_ROOT_ENV, str(Path(__file__).resolve().parents[1] / "extensions"))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    release = Event()
    probe_threads: list[int] = []
    try:
        loader.load()
        plugin = loader._plugins_by_id["firefly-camera-vision"]
        def delayed_probe(results):
            probe_threads.append(get_ident())
            release.wait(2)
            results.put("UNAVAILABLE")

        monkeypatch.setitem(
            plugin._ensure_probe.__func__.__globals__,
            "_probe_video_inputs",
            delayed_probe,
        )
        started = monotonic()
        loader.start_all()
        assert monotonic() - started < 0.2
        assert plugin.capability_exposed is True
        assert not hasattr(plugin, "camera")
        assert not hasattr(plugin, "worker")
        assert plugin.status() == "CHECKING"
        assert probe_threads and probe_threads[0] != get_ident()
        release.set()
        deadline = monotonic() + 2
        while plugin.status() == "CHECKING" and monotonic() < deadline:
            qapp.processEvents()
            sleep(0.01)
        assert plugin.status() == "UNAVAILABLE"
        loader.set_plugin_enabled("firefly-camera-vision", False)
        assert plugin.capability_exposed is False
        assert plugin.status() == "UNAVAILABLE"
    finally:
        release.set()
        loader.shutdown()
