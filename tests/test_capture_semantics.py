"""Capture Semantics v1 tests.

Where a look request captures:
- primary_screen            explicit "my screen / whole screen / desktop"
- last_non_firefly_window   "what I was doing" (default)
- firefly_companion         explicit "this chat box / your window"

Trigger (whether to look) and target (where to look) stay separate. HWND
tracking stores references only — never pixels, never provider calls.
"""

from datetime import datetime

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core.screen_vision import foreground_tracker as ft_module
from core.screen_vision.foreground_tracker import ForegroundContextTracker
from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.service import ScreenVisionService
from core.screen_vision.trigger import (
    CAPTURE_FIREFLY_COMPANION,
    CAPTURE_LAST_NON_FIREFLY_WINDOW,
    CAPTURE_PRIMARY_SCREEN,
    is_explicit_screen_vision_request,
    resolve_capture_target,
)
from ui.companion_chat_window import CompanionChatWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _frame():
    return ScreenFrame(32, 16, "image/jpeg", b"\xff\xd8x", datetime.now())


def _obs(scene="s"):
    return ScreenObservation(scene_summary=scene)


# ------------------------------------------------- A-F phrase -> target


@pytest.mark.parametrize("text,expected", [
    # A
    ("看一下我的屏幕", CAPTURE_PRIMARY_SCREEN),
    ("看看整个屏幕", CAPTURE_PRIMARY_SCREEN),
    ("看看我的桌面", CAPTURE_PRIMARY_SCREEN),
    ("看看整个桌面", CAPTURE_PRIMARY_SCREEN),
    # B
    ("看看我在做什么", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看一下我在干什么", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看看刚才这个窗口", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看看我刚才在看的东西", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看看这个页面", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看看我现在在做什么", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    # C
    ("看看这个聊天框", CAPTURE_FIREFLY_COMPANION),
    ("看看你的窗口", CAPTURE_FIREFLY_COMPANION),
    ("看看流萤窗口", CAPTURE_FIREFLY_COMPANION),
    ("看看 Companion", CAPTURE_FIREFLY_COMPANION),
    # rule 11: negated phrases never win
    ("流萤，看看你这个聊天框，不用看整个桌面", CAPTURE_FIREFLY_COMPANION),
    ("看看整个屏幕，别看聊天框", CAPTURE_PRIMARY_SCREEN),
    # D default
    ("看看屏幕", CAPTURE_LAST_NON_FIREFLY_WINDOW),
    ("看看现在屏幕上是什么", CAPTURE_LAST_NON_FIREFLY_WINDOW),
])
def test_a_f_phrase_to_target(text, expected):
    assert resolve_capture_target(text) == expected


# ------------------------------------------------------- G gate remains


def test_g_ordinary_chat_has_no_capture_target():
    for text in ("今天怎么样", "帮我写个函数", "你好"):
        assert not is_explicit_screen_vision_request(text)


class FakeRunner(QObject):
    agent_event = Signal(object)

    @property
    def history(self):
        return []

    @property
    def running(self):
        return False

    def ask(self, text):
        pass


# ------------------------------------------------- tracker semantics


class FakeForeground:
    def __init__(self, hwnd_sequence):
        self.hwnds = list(hwnd_sequence)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        index = min(self.calls, len(self.hwnds)) - 1
        return self.hwnds[index]


def test_tracker_records_only_external_windows():
    query = FakeForeground([1001, 1002, 1001])
    tracker = ForegroundContextTracker(query_foreground=query)
    # 1001 external -> recorded; then our own process hwnd -> ignored.
    monkey_own = pytest.MonkeyPatch()
    monkey_own.setattr(ft_module, "is_firefly_hwnd", lambda hwnd: hwnd == 1002)
    try:
        tracker.remember_current_external_window()
        assert tracker.last_non_firefly_window == 1001
        tracker.remember_current_external_window()  # 1002 is Firefly: ignored
        assert tracker.last_non_firefly_window == 1001
        tracker.remember_current_external_window()  # 1001 again
        assert tracker.last_non_firefly_window == 1001
    finally:
        monkey_own.undo()


def test_consecutive_asks_do_not_overwrite_with_firefly(monkeypatch):
    """L: repeated Ask clicks while Companion holds focus must not replace
    the stored external window with a Firefly HWND."""
    tracker = ForegroundContextTracker(query_foreground=FakeForeground([2001]))
    monkeypatch.setattr(ft_module, "is_firefly_hwnd", lambda hwnd: hwnd != 2001)
    monkeypatch.setattr(ft_module, "foreground_tracker", tracker, raising=False)
    tracker.remember_current_external_window()  # user in external app
    assert tracker.last_non_firefly_window == 2001
    # foreground is now Firefly's companion (registered hwnd) -> not recorded
    tracker.remember_firefly_window(9999)
    monkeypatch.setattr(
        ft_module, "get_foreground_hwnd", lambda: 9999, raising=False
    )
    tracker.remember_current_external_window()
    assert tracker.last_non_firefly_window == 2001


# ------------------------------------------------- Ask path ordering (H)


def test_h_ask_records_foreground_before_opening_companion(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication

    order = []
    tracker = ForegroundContextTracker(query_foreground=FakeForeground([3001]))
    monkeypatch.setattr(
        tracker, "remember_current_external_window",
        lambda: order.append("remember") or 3001,
    )
    monkeypatch.setattr(ft_module, "foreground_tracker", tracker)

    from ui.companion_chat_window import CompanionChatWindow

    original_show = CompanionChatWindow.show

    def spy_show(self):
        order.append("open")
        return original_show(self)

    monkeypatch.setattr(CompanionChatWindow, "show", spy_show)
    try:
        CompanionChatWindow.open_singleton(runner=FakeRunner())
    finally:
        CompanionChatWindow._instance = None
        QApplication.instance().closeAllWindows()
    assert order[:2] == ["remember", "open"]  # record BEFORE Firefly focuses


# --------------------------------------- service routing + meta (I/J/K/O/P)


class RoutingCapture:
    """Fakes all four capture methods; records which one the service used."""

    def __init__(self):
        self.calls = []
        self.last_capture_info = {}
        self._info_by_method = {
            "capture_primary_screen": ("primary_screen", False),
            "capture_last_non_firefly_window": ("last_non_firefly_window", False),
            "capture_firefly_companion": ("firefly_companion", False),
        }

    def _record(self, name):
        self.calls.append(name)
        target, fallback = self._info_by_method.get(name, (name, False))
        self.last_capture_info = {
            "capture_target": target,
            "capture_fallback_used": fallback,
        }
        return _frame()

    def capture_primary_screen(self, **kwargs):
        return self._record("capture_primary_screen")

    def capture_last_non_firefly_window(self, **kwargs):
        return self._record("capture_last_non_firefly_window")

    def capture_firefly_companion(self, **kwargs):
        return self._record("capture_firefly_companion")

    def capture_active_window(self, **kwargs):
        return self._record("capture_active_window")


class OkVision:
    name = "tju-qwen"
    model = "tju-llm"

    def inspect(self, frame, instruction=None):
        return _obs("scene")


class OkReasoning:
    name = "tju-deepseek"
    model = "deepseek-v4-flash"

    def answer(self, question, observation):
        return "answer"


def _service(capture):
    return ScreenVisionService(OkVision(), OkReasoning(), capture)


def test_i_service_routes_last_non_firefly_window():
    capture = RoutingCapture()
    result = _service(capture).look("看看我在做什么", capture_mode="last_non_firefly_window")
    assert capture.calls == ["capture_last_non_firefly_window"]
    assert result.meta["capture_target"] == "last_non_firefly_window"


def test_k_last_external_preferred_over_firefly_foreground():
    """The recorded external HWND is used even though the Companion window
    currently holds focus."""
    capture = RoutingCapture()
    _service(capture).look("看看我在做什么", capture_mode="last_non_firefly_window")
    assert capture.calls == ["capture_last_non_firefly_window"]
    assert capture.last_capture_info["capture_fallback_used"] is False


def test_n_explicit_look_exactly_one_capture():
    capture = RoutingCapture()
    _service(capture).look("看看我的屏幕", capture_mode="primary_screen")
    assert capture.calls == ["capture_primary_screen"]
    capture.calls.clear()
    _service(capture).look("看看这个聊天框", capture_mode="firefly_companion")
    assert capture.calls == ["capture_firefly_companion"]


def test_o_primary_screen_does_not_capture_companion():
    capture = RoutingCapture()
    _service(capture).look("看看整个屏幕", capture_mode="primary_screen")
    assert "capture_firefly_companion" not in capture.calls
    assert "capture_last_non_firefly_window" not in capture.calls


def test_p_meta_reports_target_and_fallback():
    capture = RoutingCapture()
    result = _service(capture).look("看看我在做什么", capture_mode="last_non_firefly_window")
    assert result.meta["capture_target"] == "last_non_firefly_window"
    assert result.meta["capture_fallback_used"] is False


def test_p2_unknown_mode_rejected():
    with pytest.raises(ValueError):
        _service(RoutingCapture()).look("q", capture_mode="everywhere")


# ------------------------------------- stale HWND fallback chain (J)


def test_j_stale_hwnd_falls_back_gracefully(monkeypatch):
    """A recorded window that no longer exists degrades to the current
    foreground (if external) or the primary screen - never a crash."""
    from core.screen_vision.screen import capture as capture_module

    tracker = ForegroundContextTracker(query_foreground=FakeForeground([4321]))
    tracker.remember_current_external_window()
    monkeypatch.setattr(capture_module, "_fg_tracker", tracker)
    # recorded HWND is gone; current foreground is a Firefly window
    monkeypatch.setattr(capture_module, "_window_hwnd_valid", lambda hwnd: False)
    monkeypatch.setattr(capture_module, "is_firefly_hwnd", lambda hwnd: True)
    screen_stub = type("S", (), {
        "devicePixelRatio": lambda self: 1.0,
        "geometry": lambda self: type("G", (), {
            "x": lambda self: 0, "y": lambda self: 0,
            "width": lambda self: 1000, "height": lambda self: 800,
        })(),
    })()
    monkeypatch.setattr(
        capture_module, "_grab_primary_pixmap", lambda: (screen_stub, object())
    )
    monkeypatch.setattr(
        capture_module, "_encode_pixmap",
        lambda pixmap, crop=None, max_edge=1600, jpeg_quality=85: _frame(),
    )

    service = capture_module.ScreenCaptureService()
    frame = service.capture_last_non_firefly_window()
    assert isinstance(frame, ScreenFrame)
    assert service.last_capture_info["capture_fallback_used"] is True
    assert service.last_capture_info["fallback_source"] == "primary_screen"


def test_j2_stale_hwnd_uses_current_external_foreground(monkeypatch):
    from core.screen_vision.screen import capture as capture_module

    tracker = ForegroundContextTracker(query_foreground=FakeForeground([4321]))
    tracker.remember_current_external_window()
    monkeypatch.setattr(capture_module, "_fg_tracker", tracker)
    monkeypatch.setattr(capture_module, "_window_hwnd_valid", lambda hwnd: hwnd == 555)
    monkeypatch.setattr(capture_module, "is_firefly_hwnd", lambda hwnd: hwnd == 9999)
    monkeypatch.setattr(capture_module, "get_foreground_hwnd", lambda: 555)
    monkeypatch.setattr(
        capture_module, "_window_rect", lambda hwnd: (0, 0, 500, 400)
    )

    screen_stub = type("S", (), {
        "devicePixelRatio": lambda self: 1.0,
        "geometry": lambda self: type("G", (), {
            "x": lambda self: 0, "y": lambda self: 0,
            "width": lambda self: 1000, "height": lambda self: 800,
        })(),
    })()
    pixmap_stub = object()
    monkeypatch.setattr(
        capture_module, "_grab_primary_pixmap", lambda: (screen_stub, pixmap_stub)
    )
    monkeypatch.setattr(
        capture_module, "_encode_pixmap",
        lambda pixmap, crop=None, max_edge=1600, jpeg_quality=85: _frame(),
    )

    service = capture_module.ScreenCaptureService()
    frame = service.capture_last_non_firefly_window()
    assert isinstance(frame, ScreenFrame)
    assert service.last_capture_info["capture_fallback_used"] is True
    assert service.last_capture_info["fallback_source"] == "current_foreground"


# -------------------------------------- firefly_companion fallback (9)


def test_companion_target_falls_back_to_primary_when_no_hwnd(monkeypatch):
    from core.screen_vision.screen import capture as capture_module

    tracker = ForegroundContextTracker(query_foreground=FakeForeground([0]))
    monkeypatch.setattr(capture_module, "_fg_tracker", tracker)  # no companion hwnd
    screen_stub = type("S", (), {
        "devicePixelRatio": lambda self: 1.0,
        "geometry": lambda self: type("G", (), {
            "x": lambda self: 0, "y": lambda self: 0,
            "width": lambda self: 1000, "height": lambda self: 800,
        })(),
    })()
    monkeypatch.setattr(
        capture_module, "_grab_primary_pixmap", lambda: (screen_stub, object())
    )
    monkeypatch.setattr(
        capture_module, "_encode_pixmap",
        lambda pixmap, crop=None, max_edge=1600, jpeg_quality=85: _frame(),
    )
    service = capture_module.ScreenCaptureService()
    frame = service.capture_firefly_companion()
    assert isinstance(frame, ScreenFrame)
    assert service.last_capture_info["capture_target"] == "firefly_companion"
    assert service.last_capture_info["capture_fallback_used"] is True


# ------------------------------------- companion registers its HWND (M-adjacent)


def test_companion_window_registers_and_forgets_hwnd(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication

    tracker = ForegroundContextTracker(query_foreground=FakeForeground([7001]))
    monkeypatch.setattr(ft_module, "foreground_tracker", tracker)

    from ui.companion_chat_window import CompanionChatWindow

    try:
        window = CompanionChatWindow.open_singleton(runner=FakeRunner())
        hwnd = int(window.winId())
        assert tracker.companion_window == hwnd  # registered on open
        window.close()
        assert tracker.companion_window != hwnd  # forgotten on close
    finally:
        CompanionChatWindow._instance = None
        QApplication.instance().closeAllWindows()
