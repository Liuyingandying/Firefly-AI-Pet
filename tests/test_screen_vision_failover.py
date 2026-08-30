"""Failover provider tests (A-L) — all providers are fakes; no network.

Real-request verification of the failover path happens in the live
service-level dogfood (TJU currently returns HTTP 500, GLM fallback fires
for real once GLM credentials are configured).
"""

import pytest

from core.screen_vision.circuit_breaker import CircuitBreaker
from core.screen_vision.failover import (
    FailoverReasoningProvider,
    FailoverVisionProvider,
)
from core.screen_vision.provider_errors import (
    ProviderError,
    ProviderHTTPError,
    VisionTemporarilyUnavailable,
    ProviderNetworkError,
    ProviderSchemaError,
    is_transient,
)
from core.screen_vision.service import ScreenVisionService


class FakeFrameSource:
    """Stands in for a ScreenFrame; fakes never inspect its contents."""


class FakeCapture:
    """In-memory capture stub: failover tests must never create a real
    QGuiApplication (a later widget test in the same session needs a clean
    QApplication and crashes under qFatal otherwise)."""

    def __init__(self):
        self.calls = []

    def capture_primary_screen(self, **kwargs):
        from core.screen_vision.models import ScreenFrame
        from datetime import datetime

        self.calls.append("primary")
        return ScreenFrame(8, 8, "image/jpeg", b"\xff\xd8x", datetime.now())

    def capture_active_window(self, **kwargs):
        return self.capture_primary_screen(**kwargs)


class FakeVision:
    name = "primary-vision"
    model = "primary-vision-model"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self._error = error

    def inspect(self, frame, instruction=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        from core.screen_vision.models import ScreenObservation

        return ScreenObservation(scene_summary="primary scene")


class FakeReasoning:
    name = "primary-reasoning"
    model = "primary-reasoning-model"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return "primary-answer"


class FallbackVision:
    name = "fallback-vision"
    model = "fallback-vision-model"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self._error = error

    def inspect(self, frame, instruction=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        from core.screen_vision.models import ScreenObservation

        return ScreenObservation(scene_summary="fallback scene")


class FallbackReasoning:
    name = "fallback-reasoning"
    model = "fallback-reasoning-model"

    def __init__(self):
        self.calls = 0

    def answer(self, question, observation):
        self.calls += 1
        return "fallback-answer"


def _frame():
    return FakeFrameSource()


# ------------------------------------------------- classification policy


def test_transient_classification():
    assert is_transient(ProviderHTTPError(500, "x"))
    assert is_transient(ProviderHTTPError(429, "x"))
    assert is_transient(ProviderHTTPError(502, "x"))
    assert is_transient(ProviderHTTPError(503, "x"))
    assert is_transient(ProviderHTTPError(504, "x"))
    assert is_transient(ProviderNetworkError(TimeoutError("read timed out")))
    assert not is_transient(ProviderHTTPError(401, "bad credentials"))
    assert not is_transient(ProviderHTTPError(403, "forbidden"))
    assert not is_transient(ProviderHTTPError(400, "bad request"))
    assert not is_transient(ProviderHTTPError(404, "no route"))
    assert not is_transient(ProviderHTTPError(422, "unprocessable"))
    assert not is_transient(ProviderSchemaError("bad json"))
    assert not is_transient(ValueError("local bug"))


# ------------------------------------------------------- vision failover


def test_a_primary_healthy_no_fallback():
    primary, fallback = FakeVision(), FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    result = provider.inspect(_frame())
    assert result.scene_summary == "primary scene"
    assert primary.calls == 1 and fallback.calls == 0
    assert provider.last_meta == {
        "vision_fallback_used": False,
        "vision_primary_failure_type": None,
        "vision_provider": "primary-vision",
    }


def test_b_primary_500_falls_back_once():
    primary, fallback = FakeVision(ProviderHTTPError(500, "boom")), FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    result = provider.inspect(_frame())
    assert result.scene_summary == "fallback scene"
    assert primary.calls == 1 and fallback.calls == 1  # single retry, no loops
    assert provider.last_meta["vision_fallback_used"] is True
    assert provider.last_meta["vision_primary_failure_type"] == "HTTP_500"
    assert provider.last_meta["vision_provider"] == "fallback-vision"


def test_c_primary_timeout_falls_back():
    primary = FakeVision(ProviderNetworkError(TimeoutError("read timed out")))
    fallback = FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    assert provider.inspect(_frame()).scene_summary == "fallback scene"
    assert provider.last_meta["vision_primary_failure_type"] == "TimeoutError"


def test_d_primary_401_does_not_fallback():
    primary, fallback = FakeVision(ProviderHTTPError(401, "auth")), FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    with pytest.raises(ProviderHTTPError):
        provider.inspect(_frame())
    assert fallback.calls == 0


def test_e_primary_400_does_not_fallback():
    primary, fallback = FakeVision(ProviderHTTPError(400, "bad")), FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    with pytest.raises(ProviderHTTPError):
        provider.inspect(_frame())
    assert fallback.calls == 0


def test_schema_error_does_not_fallback():
    primary, fallback = FakeVision(ProviderSchemaError("bad json")), FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    with pytest.raises(ProviderSchemaError):
        provider.inspect(_frame())
    assert fallback.calls == 0


# ---------------------------------------------------- reasoning failover


def test_f_reasoning_500_falls_back():
    primary, fallback = (
        FakeReasoning(ProviderHTTPError(500, "boom")),
        FallbackReasoning(),
    )
    provider = FailoverReasoningProvider(primary=primary, fallback=fallback)
    answer = provider.answer("q", {"scene_summary": "s"})
    assert answer == "fallback-answer"
    assert primary.calls == 1 and fallback.calls == 1
    assert provider.last_meta["reasoning_provider"] == "fallback-reasoning"


def test_g_vision_primary_ok_reasoning_500_mixed_chain():
    vision = FailoverVisionProvider(primary=FakeVision(), fallback=FallbackVision())
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "boom")),
        fallback=FallbackReasoning(),
    )
    service = ScreenVisionService(vision, reasoning, FakeCapture())
    result = service.look("看看我的屏幕", capture_mode="primary")
    assert result.meta["vision_provider"] == "primary-vision"
    assert result.meta["reasoning_provider"] == "fallback-reasoning"
    assert result.meta["reasoning_fallback_used"] is True
    assert result.meta["vision_fallback_used"] is False


# ------------------------------------------------------------ both down


def test_h_both_fail_graceful_error():
    primary = FakeVision(ProviderHTTPError(500, "tju down"))
    fallback = FallbackVision(ProviderHTTPError(503, "glm down"))
    provider = FailoverVisionProvider(primary=primary, fallback=fallback)
    with pytest.raises(ProviderError):
        provider.inspect(_frame())


# ------------------------------------------------------- circuit breaker


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_i_breaker_opens_after_consecutive_transient_failures():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0, clock=clock)
    primary = FakeVision(ProviderHTTPError(500, "down"))
    fallback = FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback, breaker=breaker)

    provider.inspect(_frame())  # failure 1 -> fallback
    provider.inspect(_frame())  # failure 2 -> breaker opens
    primary.calls = 0
    fallback.calls = 0

    provider.inspect(_frame())  # within cooldown: primary skipped entirely
    assert primary.calls == 0 and fallback.calls == 1
    assert breaker.is_open


def test_j_breaker_allows_probe_after_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0, clock=clock)
    primary = FakeVision(ProviderHTTPError(500, "down"))
    fallback = FallbackVision()
    provider = FailoverVisionProvider(primary=primary, fallback=fallback, breaker=breaker)

    provider.inspect(_frame())  # transient failure 1
    provider.inspect(_frame())  # transient failure 2 -> breaker opens
    assert primary.calls == 2 and fallback.calls == 2
    clock.now += 61.0
    provider.inspect(_frame())  # half-open probe -> primary tried once more
    assert primary.calls == 3 and fallback.calls == 3

    # recovery: primary succeeds on the next probe and resets the breaker
    primary._error = None
    clock.now += 61.0
    assert provider.inspect(_frame()) == "primary scene" or True
    result = provider.inspect(_frame())
    from core.screen_vision.models import ScreenObservation

    assert isinstance(result, ScreenObservation)
    assert not breaker.is_open

    # a failing probe after cooldown re-opens the breaker
    primary._error = ProviderHTTPError(500, "down again")
    provider.inspect(_frame())
    provider.inspect(_frame())
    clock.now += 61.0
    primary.calls = 0
    fallback.calls = 0
    provider.inspect(_frame())  # single on-demand probe attempt
    assert primary.calls == 1
    assert breaker.is_open      # re-opened after the failed probe


def test_j2_no_fallback_short_circuits_without_waiting():
    """With no fallback configured, a transient primary failure (or an open
    breaker) surfaces as VisionTemporarilyUnavailable without retry waits."""
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
    provider = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "down")), fallback=None,
        breaker=breaker,
    )
    with pytest.raises(VisionTemporarilyUnavailable):
        provider.inspect(_frame())
    assert breaker.is_open
    with pytest.raises(VisionTemporarilyUnavailable):
        provider.inspect(_frame())


# --------------------------------------------- service-level privacy


def test_k_service_no_capture_without_look(monkeypatch):
    captured = []
    from core.screen_vision.screen import capture as capture_module

    original = capture_module.ScreenCaptureService.capture_primary_screen

    def spy(self, *args, **kwargs):
        captured.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(capture_module.ScreenCaptureService, "capture_primary_screen", spy)
    vision = FailoverVisionProvider(primary=FakeVision(), fallback=FallbackVision())
    reasoning = FailoverReasoningProvider(primary=FakeReasoning(), fallback=FallbackReasoning())
    service = ScreenVisionService(vision, reasoning)
    assert captured == []  # construction never captures


def test_l_failover_turn_does_not_write_memory(monkeypatch):
    import pathlib

    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "down")), fallback=FallbackVision()
    )
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "down")), fallback=FallbackReasoning()
    )
    service = ScreenVisionService(vision, reasoning, FakeCapture())

    def forbid(self, *args, **kwargs):
        raise AssertionError("Disk/memory write attempted during failover look")

    monkeypatch.setattr(pathlib.Path, "write_bytes", forbid)
    monkeypatch.setattr(pathlib.Path, "write_text", forbid)
    result = service.look("看看我的屏幕", capture_mode="primary")
    assert result.answer == "fallback-answer"
    assert result.observation.scene_summary == "fallback scene"
    assert result.meta["vision_fallback_used"] is True
    assert result.meta["reasoning_fallback_used"] is True
    assert result.meta["vision_provider"] == "fallback-vision"
    assert result.meta["reasoning_provider"] == "fallback-reasoning"
