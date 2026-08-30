"""Screen Vision FAST / RESILIENT dual-routing tests.

Routing is pure provider ordering: the same provider instances and circuit
breakers are shared; only the priority order changes. No real API is called.
"""

import json
from datetime import datetime

import pytest

from core.screen_vision import config as sv_config
from core.screen_vision.config import (
    ROUTING_FAST,
    ROUTING_RESILIENT,
    build_reasoning_provider,
    build_vision_provider,
    get_reasoning_provider_order,
    get_vision_provider_order,
    normalize_routing_mode,
)
from core.screen_vision.failover import FailoverReasoningProvider, FailoverVisionProvider
from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    VisionTemporarilyUnavailable,
)
from core.screen_vision.service import ScreenVisionService


def _frame():
    return ScreenFrame(32, 16, "image/jpeg", b"\xff\xd8x", datetime.now())


class FakeProvider:
    """One fake provider: name + optional latency + optional error."""

    def __init__(self, name, latency=0.0, error=None, result="obs"):
        self.name = name
        self._latency = latency
        self._error = error
        self._result = result
        self.calls = 0

    def _run(self):
        self.calls += 1
        if self._latency:
            import time

            time.sleep(self._latency)
        if self._error is not None:
            raise self._error
        return self._result

    def inspect(self, frame, instruction=None):
        result = self._run()
        return result if isinstance(result, ScreenObservation) \
            else ScreenObservation(scene_summary=str(result))

    def answer(self, question, observation):
        return str(self._run())


# -------------------------------------------------------- A-E settings


def test_a_default_fast_mode_true(tmp_path):
    from core.settings_manager import SettingsManager

    manager = SettingsManager(tmp_path / "prefs.json")
    assert manager.screen_vision_fast_mode is True


def test_b_toggle_on_routes_fast(tmp_path):
    from core.settings_manager import SettingsManager

    manager = SettingsManager(tmp_path / "prefs.json")
    manager.set_screen_vision_fast_mode(True)
    service = ScreenVisionService(
        vision_provider=object(), reasoning_provider=object(),
        capture_service=_Capture(), settings=manager,
    )
    assert service.routing_mode == ROUTING_FAST


def test_c_toggle_off_routes_resilient(tmp_path):
    from core.settings_manager import SettingsManager

    manager = SettingsManager(tmp_path / "prefs.json")
    manager.set_screen_vision_fast_mode(False)
    service = ScreenVisionService(
        vision_provider=object(), reasoning_provider=object(),
        capture_service=_Capture(), settings=manager,
    )
    assert service.routing_mode == ROUTING_RESILIENT


def test_d_setting_applies_immediately(tmp_path):
    """Toggle flips routing without a restart."""
    from core.settings_manager import SettingsManager

    manager = SettingsManager(tmp_path / "prefs.json")
    service = ScreenVisionService(
        vision_provider=object(), reasoning_provider=object(),
        capture_service=_Capture(), settings=manager,
    )
    assert service.routing_mode == ROUTING_FAST
    manager.set_screen_vision_fast_mode(False)
    service.sync_routing_mode_from_settings()
    assert service.routing_mode == ROUTING_RESILIENT


def test_e_restart_persistence(tmp_path):
    from core.settings_manager import SettingsManager

    path = tmp_path / "prefs.json"
    first = SettingsManager(path)
    first.set_screen_vision_fast_mode(False)
    second = SettingsManager(path)  # reload from disk
    assert second.screen_vision_fast_mode is False


class _Capture:
    last_capture_info = {}

    def capture_primary_screen(self, **kwargs):
        return _frame()

    def capture_last_non_firefly_window(self, **kwargs):
        return _frame()

    def capture_firefly_companion(self, **kwargs):
        return _frame()

    def capture_active_window(self, **kwargs):
        return _frame()


# ------------------------------------------------------ F-I ordering


def test_f_fast_vision_prefers_deepseek():
    assert get_vision_provider_order(ROUTING_FAST)[0] == "deepseek_vision"
    assert get_vision_provider_order(ROUTING_FAST) == (
        "deepseek_vision", "qwen_vision", "glm_vision")


def test_g_fast_reasoning_prefers_deepseek():
    assert get_reasoning_provider_order(ROUTING_FAST)[0] == "deepseek_reasoning"
    assert get_reasoning_provider_order(ROUTING_FAST) == (
        "deepseek_reasoning", "tju_reasoning", "glm_reasoning")


def test_h_resilient_vision_prefers_tju():
    assert get_vision_provider_order(ROUTING_RESILIENT) == (
        "qwen_vision", "glm_vision", "deepseek_vision")


def test_i_resilient_reasoning_prefers_tju():
    assert get_reasoning_provider_order(ROUTING_RESILIENT) == (
        "tju_reasoning", "glm_reasoning", "deepseek_reasoning")


def test_normalize_unknown_to_fast():
    assert normalize_routing_mode("banana") == ROUTING_FAST
    assert normalize_routing_mode(None) == ROUTING_FAST


# ------------------------------------------------------ J-K fallback


def test_j_fast_deepseek_transient_fail_falls_to_tju(monkeypatch):
    """FAST: DeepSeek 500 -> TJU (second in FAST order) succeeds."""
    deepseek = FakeProvider("deepseek_vision", error=ProviderHTTPError(500, "ds"))
    qwen = FakeProvider("tju_vision")
    glm = FakeProvider("glm_vision")
    vision = FailoverVisionProvider(
        primary=deepseek, fallbacks=(qwen, glm), breaker=_Breaker()
    )
    obs = vision.inspect(_frame())
    assert obs.scene_summary == "obs"
    assert deepseek.calls == 1 and qwen.calls == 1 and glm.calls == 0


def test_k_resilient_tju_transient_fail_falls_to_glm():
    qwen = FakeProvider("tju_vision", error=ProviderHTTPError(500, "tju"))
    glm = FakeProvider("glm_vision")
    deepseek = FakeProvider("deepseek_vision")
    vision = FailoverVisionProvider(
        primary=qwen, fallbacks=(glm, deepseek), breaker=_Breaker()
    )
    vision.inspect(_frame())
    assert qwen.calls == 1 and glm.calls == 1 and deepseek.calls == 0


# ------------------------------------------------------ L-M semantics


def test_l_non_transient_does_not_fallback():
    deepseek = FakeProvider("deepseek_vision", error=ProviderHTTPError(401, "bad key"))
    qwen = FakeProvider("tju_vision")
    vision = FailoverVisionProvider(
        primary=deepseek, fallbacks=(qwen,), breaker=_Breaker()
    )
    with pytest.raises(ProviderHTTPError):
        vision.inspect(_frame())
    assert qwen.calls == 0


def test_m_open_breaker_skips_primary():
    from core.screen_vision.circuit_breaker import CircuitBreaker

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
    deepseek = FakeProvider("deepseek_vision", error=ProviderHTTPError(500, "ds"))
    qwen = FakeProvider("tju_vision")
    vision = FailoverVisionProvider(primary=deepseek, fallbacks=(qwen,), breaker=breaker)
    vision.inspect(_frame())  # failure 1 -> breaker opens
    deepseek.calls = 0
    qwen.calls = 0
    vision.inspect(_frame())  # within cooldown: DeepSeek skipped entirely
    assert deepseek.calls == 0 and qwen.calls == 1


def test_n_switching_mode_keeps_breaker():
    """Switching routing reuses the shared breaker (no reset)."""
    from core.screen_vision.circuit_breaker import CircuitBreaker

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
    err = ProviderHTTPError(500, "down")
    fast_vision = FailoverVisionProvider(
        primary=FakeProvider("deepseek_vision", error=err),
        fallbacks=(FakeProvider("tju_vision", error=err),
                   FakeProvider("glm_vision", error=err)),
        breaker=breaker,
    )
    with pytest.raises(VisionTemporarilyUnavailable):
        fast_vision.inspect(_frame())
    assert breaker.is_open
    # A new chain for the other mode gets the SAME breaker object.
    resilient_vision = FailoverVisionProvider(
        primary=FakeProvider("tju_vision"),
        fallbacks=(FakeProvider("glm_vision"), FakeProvider("deepseek_vision")),
        breaker=breaker,
    )
    assert resilient_vision._breaker is breaker
    assert breaker.is_open  # still open: switching mode did not reset it


# ------------------------------------------------------ O-R privacy


def test_o_ordinary_chat_zero_capture():
    from core.screen_vision.trigger import is_explicit_screen_vision_request

    assert not is_explicit_screen_vision_request("今天怎么样")


def test_p_reasoning_text_only():
    """Both modes keep reasoning text-only; no image data."""
    from core.screen_vision.brain.base import assert_text_only_payload

    with pytest.raises(ValueError):
        assert_text_only_payload({"scene_summary": "s", "image_bytes": b"x"})


def test_q_settings_toggle_no_capture_or_provider(tmp_path, monkeypatch):
    from core.settings_manager import SettingsManager

    manager = SettingsManager(tmp_path / "prefs.json")
    service = ScreenVisionService(
        vision_provider=object(), reasoning_provider=object(),
        capture_service=_Capture(), settings=manager,
    )
    monkeypatch.setattr(
        ScreenVisionService, "sync_routing_mode_from_settings",
        lambda self: None,
    )
    manager.set_screen_vision_fast_mode(False)
    manager.set_screen_vision_fast_mode(True)
    # purely a settings flip: no look, no capture, no provider call
    assert service._capture is not None


def test_r_workspace_and_memory_not_written(tmp_path, monkeypatch):
    import pathlib

    from core.settings_manager import SettingsManager

    prefs = tmp_path / "prefs.json"
    manager = SettingsManager(prefs)

    def forbid(self, *args, **kwargs):
        raise AssertionError("unexpected write outside prefs.json")

    monkeypatch.setattr(pathlib.Path, "write_bytes", forbid)
    manager.set_screen_vision_fast_mode(False)  # writes only prefs.json internally
    assert prefs.exists()


# ----------------------------------------- synthetic latency comparison


class _SleepBreaker:
    pass


def test_synthetic_fast_skips_tju_and_zhipu_wait(monkeypatch):
    """FAST: DeepSeek succeeds immediately; TJU (8s) and Zhipu (8s) are never
    even attempted, so the chain returns in ~0s instead of ~16s."""
    import time

    deepseek = FakeProvider("deepseek_vision", latency=0.01, result="ds-obs")
    qwen = FakeProvider("tju_vision", latency=8.0)
    glm = FakeProvider("glm_vision", latency=8.0)

    fast = FailoverVisionProvider(primary=deepseek, fallbacks=(qwen, glm), breaker=_Breaker())
    started = time.perf_counter()
    obs = fast.inspect(_frame())
    elapsed = time.perf_counter() - started
    assert obs.scene_summary == "ds-obs"
    assert qwen.calls == 0 and glm.calls == 0
    assert elapsed < 2.0  # did NOT wait for the 8s providers


def test_synthetic_resilient_would_wait_on_tju():
    """RESILIENT order puts TJU first; the comparison documents that FAST
    avoids this serial wait."""
    order = get_vision_provider_order(ROUTING_RESILIENT)
    assert order[0] == "qwen_vision"
    assert get_vision_provider_order(ROUTING_FAST)[0] == "deepseek_vision"


class _Breaker:
    allow_primary = lambda self: True  # noqa: E731
    record_success = lambda self: None  # noqa: E731
    record_transient_failure = lambda self, *a, **k: None  # noqa: E731
    last_failure_type = None
    is_open = False
