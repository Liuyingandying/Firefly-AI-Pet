"""Vision-1B camera.observed session-event tests (offline).

No real camera, no full Firefly startup. Verifies:
1. a successful camera look ("看看我") publishes kind="camera.observed" with
   the answer text and saves last_camera_observation;
2. a non-camera look publishes nothing and leaves the field None;
3. the companion console updates its task card to "观察完成" on the event.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal

from core.runtime_bus import CameraObservedEvent, RuntimeEvent
from core.screen_vision.models import ScreenObservation, ScreenVisionResult
from ui.character_conversation_runner import CharacterConversationRunner
from ui.v2.console import CompanionConsole


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


class FakeRuntimeBus(QObject):
    """Records publish_event calls and delivers them synchronously to any
    subscribers — mirrors test_ui_v2_console's fake, plus publish_event."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[RuntimeEvent] = []
        self.callbacks: list = []

    def subscribe_event(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)

    def publish_event(self, event: RuntimeEvent) -> None:
        self.published.append(event)
        for callback in list(self.callbacks):
            callback(event)


class _StubRuntime:
    """Minimal runtime for the runner: no disk, no store, no chat path."""

    conversation_store = None


class FakeVisionService:
    def __init__(self, capture_mode: str) -> None:
        self._capture_mode = capture_mode

    def look(self, question, capture_mode=None) -> ScreenVisionResult:
        return ScreenVisionResult(
            observation=ScreenObservation(),
            answer="检测到用户画面",
            timings={},
            meta={
                "capture_mode": self._capture_mode,
                "direct_one_shot": True,
            },
        )


def _make_runner(bus: FakeRuntimeBus, capture_mode: str) -> CharacterConversationRunner:
    return CharacterConversationRunner(
        runtime=_StubRuntime(),
        screen_vision_service=FakeVisionService(capture_mode),
        runtime_bus=bus,
    )


def test_camera_look_publishes_event():
    bus = FakeRuntimeBus()
    runner = _make_runner(bus, "camera")

    events, _ = runner.perform("看看我", threading.Event())

    assert events  # the turn returned at least the final reply
    assert runner.last_camera_observation == "检测到用户画面"
    observed = [e for e in bus.published if e.kind == "camera.observed"]
    assert len(observed) == 1
    event = observed[0]
    assert event.source == "character_conversation_runner"
    assert isinstance(event.payload, CameraObservedEvent)
    assert event.payload.text == "检测到用户画面"


def test_non_camera_look_no_event():
    bus = FakeRuntimeBus()
    runner = _make_runner(bus, "primary_screen")

    runner.perform("看看我的屏幕", threading.Event())

    assert runner.last_camera_observation is None
    assert all(e.kind != "camera.observed" for e in bus.published)


def test_console_camera_event_updates_panel():
    class FakeRunner(QObject):
        agent_event = Signal(object)

        def __init__(self) -> None:
            super().__init__()
            self.session_video = None
            self.video_study = None

    bus = FakeRuntimeBus()
    console = CompanionConsole(FakeRunner(), runtime_bus=bus)
    try:
        console.companion.set_task("等待指令")  # reset baseline
        bus.publish_event(
            RuntimeEvent(
                kind="camera.observed",
                source="character_conversation_runner",
                payload=CameraObservedEvent(text="检测到用户画面"),
            )
        )
        assert console.companion.task_label.text() == "最近观察：刚刚"
    finally:
        console.close()


def test_task_label_untouched_by_non_camera_events():
    class FakeRunner(QObject):
        agent_event = Signal(object)

        def __init__(self) -> None:
            super().__init__()
            self.session_video = None
            self.video_study = None

    bus = FakeRuntimeBus()
    console = CompanionConsole(FakeRunner(), runtime_bus=bus)
    try:
        console.companion.set_task("等待指令")
        # unrelated event kinds must never touch the task card
        bus.publish_event(
            RuntimeEvent(kind="plugin.status", source="test", payload="x")
        )
        assert console.companion.task_label.text() == "等待指令"
    finally:
        console.close()


def test_runtime_activity_still_updates_state_label():
    from core.runtime_state_aggregator import RuntimeActivityState

    class FakeRunner(QObject):
        agent_event = Signal(object)

        def __init__(self) -> None:
            super().__init__()
            self.session_video = None
            self.video_study = None

    bus = FakeRuntimeBus()
    console = CompanionConsole(FakeRunner(), runtime_bus=bus)
    try:
        # scenario: a camera observation just happened on the task card
        console.companion.set_task("最近观察：刚刚")
        bus.publish_event(
            RuntimeEvent(
                kind="runtime.activity",
                source="test",
                payload=RuntimeActivityState.WORKING,
            )
        )
        assert console.companion.companion_state_label.text() == "正在工作"
        assert console.companion.task_label.text() == "最近观察：刚刚"
    finally:
        console.close()