"""Screen Vision x Companion integration tests (Stage 1).

Coverage map (per Stage 1 acceptance):
A. ordinary chat  -> zero capture
B. explicit look  -> exactly one active_window capture
C. whole screen   -> primary capture
D. vision context only enters the current turn (not persisted)
E. no memory writes from the vision round
F. DeepSeek payload contains no image data
G. no disk writes during a look turn
H. vision failure -> friendly reply, companion does not crash
I. look runs on a worker thread, GUI thread not blocked
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from core.screen_vision.models import (
    ScreenFrame,
    ScreenObservation,
    ScreenVisionResult,
)
from core.screen_vision.trigger import (
    format_screen_vision_context,
    is_explicit_screen_vision_request,
    is_look_command,
    resolve_capture_mode,
    screen_vision_question,
)
from ui.character_conversation_runner import CharacterConversationRunner


# ----------------------------------------------------------------- fakes


def _fake_frame() -> ScreenFrame:
    return ScreenFrame(64, 32, "image/jpeg", b"\xff\xd8fake", datetime.now())


class RecordingCapture:
    def __init__(self):
        self.calls: list[str] = []

    def capture_primary_screen(self, **kwargs):
        self.calls.append("primary")
        return _fake_frame()

    def capture_active_window(self, **kwargs):
        self.calls.append("active_window")
        return _fake_frame()


class FakeVision:
    model = "fake-vision"

    def __init__(self):
        self.frames: list[ScreenFrame] = []

    def inspect(self, frame, instruction=None):
        self.frames.append(frame)
        return ScreenObservation(
            scene_summary="假场景：代码编辑器",
            active_application="FakeApp",
            window_title="fake.py - Editor",
            visible_text=["def main()"],
        )


class FakeReasoning:
    model = "fake-reasoning"

    def __init__(self):
        self.payloads: list[dict] = []

    def answer(self, question, payload):
        self.payloads.append(dict(payload))
        return "视觉推理结论：屏幕上是代码编辑器。"


class FakeRuntime:
    """Duck-typed ConversationRuntime recording what it receives."""

    def __init__(self):
        self.chats: list[dict] = []
        self.conversation_store = None

    def chat(self, user_message, *, history=None, model=None, temperature=0.2,
             turn_context=None):
        self.chats.append({
            "user_message": user_message,
            "history": [dict(m) for m in (history or [])],
            "turn_context": turn_context,
        })
        return {
            "choices": [{"message": {"role": "assistant", "content": "流萤的回复"}}]
        }


def _make_runner(capture, service=None) -> tuple[CharacterConversationRunner, FakeRuntime]:
    runtime = FakeRuntime()
    vision = FakeVision()
    reasoning = FakeReasoning()
    from core.screen_vision.service import ScreenVisionService

    svc = service or ScreenVisionService(vision, reasoning, capture)
    runner = CharacterConversationRunner(runtime=runtime, screen_vision_service=svc)
    return runner, runtime


# ------------------------------------------------------- trigger rules


def test_trigger_explicit_requests():
    for text in (
        "流萤，看一下我的屏幕",
        "看看我的屏幕",
        "帮我看看当前窗口",
        "看看我现在在干什么",
        "你看看这是怎么回事",
        "/look",
        "/look 现在在播放什么",
    ):
        assert is_explicit_screen_vision_request(text) or is_look_command(text), text


def test_trigger_ordinary_chat_never_matches():
    for text in ("今天怎么样", "你好", "帮我写个函数", "讲个笑话", "看看书", ""):
        assert not is_explicit_screen_vision_request(text), text
        assert not is_look_command(text), text


def test_capture_mode_default_active_window():
    assert resolve_capture_mode("流萤，看一下我的屏幕") == "active_window"
    assert resolve_capture_mode("看看当前窗口") == "active_window"
    assert resolve_capture_mode("看看整个屏幕") == "primary"
    assert resolve_capture_mode("看看我的整个桌面") == "primary"


def test_look_command_maps_to_canonical_question():
    assert screen_vision_question("/look") != "/look"
    assert screen_vision_question("/look 在播什么视频") == "在播什么视频"


# ------------------------------------------------------- A/B/C runtime flow


def test_a_ordinary_chat_does_not_capture():
    capture = RecordingCapture()
    runner, runtime = _make_runner(capture)
    events, answer = runner.perform("今天怎么样")
    assert capture.calls == []
    assert runtime.chats[0]["turn_context"] is None
    assert answer == "流萤的回复"


def test_b_explicit_look_captures_active_window_once():
    capture = RecordingCapture()
    runner, runtime = _make_runner(capture)
    events, answer = runner.perform("流萤，看一下我的屏幕")
    assert capture.calls == ["active_window"]
    assert answer == "流萤的回复"
    context = runtime.chats[0]["turn_context"]
    assert "[Screen Vision Context]" in context
    assert "[End Screen Vision Context]" in context
    assert "Capture mode: active_window" in context


def test_b_look_command_captures_once():
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)
    runner.perform("/look")
    assert capture.calls == ["active_window"]


def test_c_whole_screen_uses_primary_capture():
    capture = RecordingCapture()
    runner, runtime = _make_runner(capture)
    runner.perform("看看整个屏幕，现在发生了什么")
    assert capture.calls == ["primary"]
    assert "Capture mode: primary" in runtime.chats[0]["turn_context"]


# ------------------------------------------------------- D/E context scope


def test_d_vision_context_not_persisted_into_history():
    capture = RecordingCapture()
    runner, runtime = _make_runner(capture)
    runner.perform("流萤，看一下我的屏幕")
    # provider received the context this turn...
    assert "Screen Vision Context" in runtime.chats[0]["turn_context"]
    # ...but the runner's persisted history stays clean
    history = runner.history
    assert all("Screen Vision Context" not in m.get("content", "") for m in history)
    # and the next ordinary turn receives no vision context
    runner.perform("今天怎么样")
    assert runtime.chats[1]["turn_context"] is None
    assert all("Screen Vision Context" not in m.get("content", "")
               for m in runtime.chats[1]["history"])


def test_e_vision_round_does_not_write_memory(monkeypatch):
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)
    # Any memory repository write during the turn fails the test.
    from memory import repository as memory_repository

    def forbid(self, *args, **kwargs):
        raise AssertionError("Memory write attempted during screen vision turn")

    for name in ("save", "add", "append", "upsert", "write"):
        if hasattr(memory_repository.JsonMemoryRepository, name):
            monkeypatch.setattr(
                memory_repository.JsonMemoryRepository, name, forbid, raising=False
            )
    events, answer = runner.perform("看看我的屏幕")
    assert answer == "流萤的回复"


# ------------------------------------------------------- F image safety


def test_f_reasoning_payload_has_no_image_data():
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)
    svc = runner._screen_vision_service
    runner.perform("看看我的屏幕")
    reasoning = svc._reasoning
    for payload in reasoning.payloads:
        serialized = str(payload).lower()
        for marker in ("base64", "image_url", "data:image", "image_bytes"):
            assert marker not in serialized
        assert "raw_model_text" not in payload


def test_f_format_context_is_text_only():
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)
    result = runner._screen_vision_service.look("看看我的屏幕")
    context = format_screen_vision_context(result)
    lowered = context.lower()
    for marker in ("base64", "image_bytes", "data:image", "authorization", "bearer "):
        assert marker not in lowered


# ------------------------------------------------------- G disk safety


def test_g_look_turn_never_writes_to_disk(monkeypatch):
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)

    import pathlib

    def forbid(self, *args, **kwargs):
        raise AssertionError("Disk write attempted during a look turn")

    monkeypatch.setattr(pathlib.Path, "write_bytes", forbid)
    monkeypatch.setattr(pathlib.Path, "write_text", forbid)
    monkeypatch.setattr(pathlib.Path, "open", forbid)

    events, answer = runner.perform("看看我的屏幕")
    assert answer == "流萤的回复"


# ------------------------------------------------------- H failure mode


def test_h_vision_failure_returns_friendly_reply():
    class BrokenService:
        def look(self, question, capture_mode="active_window"):
            raise RuntimeError("vision endpoint unavailable")

    runner, runtime = _make_runner(RecordingCapture(), service=BrokenService())
    events, answer = runner.perform("流萤，看一下我的屏幕")
    assert answer and "没能看成屏幕" in answer
    assert events[0].type.value == "final"
    # The provider was never called with a half-built context.
    assert runtime.chats == []


# ------------------------------------------------------- I threading


def test_i_ask_path_runs_look_on_worker_thread():
    """Through the public ask() path, look() must run off the caller thread
    (the runner's existing daemon worker), never on the calling/GUI thread."""
    capture = RecordingCapture()
    runner, _ = _make_runner(capture)
    caller_thread = threading.current_thread()
    seen = []
    real_capture_active = capture.capture_active_window

    def spy_capture_active(**kwargs):
        seen.append(threading.current_thread())
        return real_capture_active(**kwargs)

    capture.capture_active_window = spy_capture_active
    assert runner.ask("流萤，看一下我的屏幕") is True
    # Wait until the worker thread has FULLY finished (not just captured):
    # returning earlier would let the runner emit Qt signals from a daemon
    # thread after this QObject-based test tore down -> native crash.
    deadline = threading.Event()
    for _ in range(1000):
        if capture.calls == ["active_window"] and not runner.running:
            break
        deadline.wait(0.02)
    deadline.wait(0.2)  # let the worker's final agent_event.emit() drain
    assert capture.calls == ["active_window"]
    assert seen and all(t is not caller_thread for t in seen)
