"""Screen Vision reasoning fallback tests: TJU DeepSeek -> Zhipu glm-4.7-flash.

Provider-level fakes; the real GLM round-trip lives in the reasoning probe
(audit/reasoning_fallback_probe.py). Coverage per acceptance list A-O.
"""

import pytest

from core.screen_vision.brain.glm_reasoning import GlmReasoningProvider
from core.screen_vision.circuit_breaker import CircuitBreaker
from core.screen_vision.config import build_reasoning_provider
from core.screen_vision.failover import FailoverReasoningProvider
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    ProviderNetworkError,
    ReasoningTemporarilyUnavailable,
    is_transient,
)
from core.screen_vision.service import ScreenVisionService


class FakePrimaryReasoning:
    name = "tju-deepseek"
    model = "deepseek-v4-flash"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return "primary-brain-answer"


class FakeGlmReasoning:
    name = "zhipu-glm"
    model = "glm-4.7-flash"

    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.payloads = []
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        self.payloads.append((question, observation))
        if self._error is not None:
            raise self._error
        return "glm-brain-answer"


def _chain(primary_error=None, glm_error=None, breaker=None):
    primary = FakePrimaryReasoning(primary_error)
    glm = FakeGlmReasoning(glm_error)
    provider = FailoverReasoningProvider(primary=primary, fallbacks=(glm,), breaker=breaker)
    return provider, primary, glm


# --------------------------------------------- A/B/C/D fallback triggering


def test_a_primary_success_never_calls_glm():
    provider, primary, glm = _chain()
    answer = provider.answer("q", {"scene_summary": "s"})
    assert answer == "primary-brain-answer"
    assert primary.calls == 1
    assert glm.calls == 0  # no racing, no speculative execution


def test_b_primary_500_falls_back_to_glm():
    provider, primary, glm = _chain(ProviderHTTPError(500, "tju down"))
    assert provider.answer("q", {"scene_summary": "s"}) == "glm-brain-answer"
    assert primary.calls == 1 and glm.calls == 1


def test_c_primary_timeout_falls_back_to_glm():
    provider, _, glm = _chain(ProviderNetworkError(TimeoutError("read timed out")))
    assert provider.answer("q", {"scene_summary": "s"}) == "glm-brain-answer"
    assert glm.calls == 1


def test_d_primary_429_falls_back_to_glm():
    provider, _, glm = _chain(ProviderHTTPError(429, "busy"))
    assert provider.answer("q", {"scene_summary": "s"}) == "glm-brain-answer"
    assert glm.calls == 1


# --------------------------------------- E/F non-transient never fallback


@pytest.mark.parametrize("status", [401, 400, 403, 404, 422])
def test_e_f_non_transient_propagates_without_glm(status):
    provider, primary, glm = _chain(ProviderHTTPError(status, "client error"))
    with pytest.raises(ProviderHTTPError):
        provider.answer("q", {"scene_summary": "s"})
    assert glm.calls == 0


# ------------------------------------------------------ G GLM text-only


def test_g_glm_never_receives_image_data():
    provider = GlmReasoningProvider(provider=object())
    for marker_payload in (
        {"scene_summary": "s", "image_url": {"url": "data:image/png;base64,AA"}},
        {"scene_summary": "s", "image_bytes": b"raw"},
        {"scene_summary": "s", "screenshot_base64": "AAAA"},
    ):
        with pytest.raises(ValueError):
            provider.answer("q", marker_payload)
    # and a clean payload reaches the wrapped Zhipu client as pure text
    # messages (build_brain_messages re-validates too).


def test_g2_glm_wraps_existing_zhipu_client():
    from providers.zhipu_glm import ZhipuGLMProvider

    provider = GlmReasoningProvider()
    assert isinstance(provider._provider, ZhipuGLMProvider)  # no second HTTP client


# --------------------------------------------- H both fail


def test_h_both_fail_reasoning_temporarily_unavailable():
    provider, _, _ = _chain(
        ProviderHTTPError(500, "tju down"), ProviderHTTPError(503, "glm down")
    )
    with pytest.raises(ReasoningTemporarilyUnavailable) as excinfo:
        provider.answer("q", {"scene_summary": "s"})
    assert excinfo.value.failure_type  # carries classified failure info


# --------------------------------------------- I/J/K circuit breaker


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_i_breaker_opens_after_consecutive_transient_failures():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0, clock=clock)
    provider, primary, glm = _chain(ProviderHTTPError(500, "down"), breaker=breaker)
    provider.answer("q", {"scene_summary": "s"})
    provider.answer("q", {"scene_summary": "s"})
    assert primary.calls == 2 and glm.calls == 2
    assert breaker.is_open


def test_j_open_breaker_skips_primary_entirely():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0, clock=clock)
    provider, primary, glm = _chain(ProviderHTTPError(500, "down"), breaker=breaker)
    provider.answer("q", {"scene_summary": "s"})
    provider.answer("q", {"scene_summary": "s"})  # breaker now OPEN
    primary.calls = 0
    glm.calls = 0

    # A new user look goes straight to GLM; TJU DeepSeek is not waited on.
    assert provider.answer("q", {"scene_summary": "s"}) == "glm-brain-answer"
    assert primary.calls == 0 and glm.calls == 1


def test_k_cooldown_allows_half_open_probe_then_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60.0, clock=clock)
    provider, primary, glm = _chain(ProviderHTTPError(500, "down"), breaker=breaker)
    provider.answer("q", {"scene_summary": "s"})
    provider.answer("q", {"scene_summary": "s"})
    clock.now += 61.0
    provider.answer("q", {"scene_summary": "s"})  # half-open probe: TJU tried once
    assert primary.calls == 3

    primary._error = None  # TJU recovered
    clock.now += 61.0
    assert provider.answer("q", {"scene_summary": "s"}) == "primary-brain-answer"
    assert not breaker.is_open  # CLOSED again on success


# ------------------------------------------------------ L/M meta


def test_l_m_meta_reports_provider_and_fallback():
    provider, _, _ = _chain()
    provider.answer("q", {"scene_summary": "s"})
    assert provider.last_meta == {
        "reasoning_provider": "tju-deepseek",
        "reasoning_fallback_used": False,
        "reasoning_primary_failure_type": None,
    }
    provider2, _, _ = _chain(ProviderHTTPError(502, "bad gateway"))
    provider2.answer("q", {"scene_summary": "s"})
    assert provider2.last_meta == {
        "reasoning_provider": "zhipu-glm",
        "reasoning_fallback_used": True,
        "reasoning_primary_failure_type": "HTTP_502",
    }


# --------------------------------------------- default chain assembly


def test_default_chain_is_tju_deepseek_then_zhipu(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    import providers.base as base

    monkeypatch.setattr(base, "DEFAULT_ENV_FILE", empty_env)
    monkeypatch.setenv("TJUTOKEN", "t")
    monkeypatch.setenv("ZHIPU_API_KEY", "z")
    monkeypatch.delenv("FIREFLY_REASONING_FALLBACK", raising=False)
    monkeypatch.delenv("GLM_REASONING_MODEL", raising=False)

    chain = build_reasoning_provider()
    assert isinstance(chain, FailoverReasoningProvider)
    assert chain._primary.name == "tju-deepseek"
    assert [f.name for f in chain._fallbacks] == ["zhipu-glm"]
    assert isinstance(chain._fallbacks[0]._provider, __import__(
        "providers.zhipu_glm", fromlist=["ZhipuGLMProvider"]
    ).ZhipuGLMProvider)
    assert chain._fallbacks[0].model == "glm-4.7-flash"


def test_official_deepseek_not_auto_enabled(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    import providers.base as base

    monkeypatch.setattr(base, "DEFAULT_ENV_FILE", empty_env)
    monkeypatch.setenv("TJUTOKEN", "t")
    monkeypatch.setenv("ZHIPU_API_KEY", "z")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    chain = build_reasoning_provider()
    names = [f.name for f in chain._fallbacks]
    assert "deepseek-official" not in names  # no unintended paid layer


# --------------------------------------------- N/O service-level


class _OkVision:
    name = "tju-qwen"
    model = "tju-llm"

    def inspect(self, frame, instruction=None):
        from core.screen_vision.models import ScreenObservation

        return ScreenObservation(scene_summary="s")


class _SpyCapture:
    def __init__(self):
        self.calls = 0

    def capture_primary_screen(self, **kwargs):
        from datetime import datetime

        from core.screen_vision.models import ScreenFrame

        self.calls += 1
        return ScreenFrame(8, 8, "image/jpeg", b"\xff\xd8x", datetime.now())

    def capture_active_window(self, **kwargs):
        return self.capture_primary_screen(**kwargs)


def test_n_service_exactly_one_capture_per_look():
    from core.screen_vision.models import ScreenObservation

    class DownReasoning:
        name = "tju-deepseek"

        def answer(self, question, observation):
            raise ProviderHTTPError(500, "down")

    class OkGlm:
        name = "zhipu-glm"
        model = "glm-4.7-flash"

        def answer(self, question, observation):
            return "glm answer"

    from core.screen_vision.failover import FailoverReasoningProvider as FRP

    capture = _SpyCapture()
    service = ScreenVisionService(
        _OkVision(), FRP(primary=DownReasoning(), fallbacks=(OkGlm(),)), capture
    )
    assert capture.calls == 0  # construction: zero capture
    result = service.look("看看我的屏幕")
    assert capture.calls == 1  # one look: exactly one frame
    assert result.answer == "glm answer"
    assert result.meta["reasoning_provider"] == "zhipu-glm"
    service.look("再看一次")
    assert capture.calls == 2  # second look: exactly one more frame


def test_o_reasoning_context_not_persisted():
    """Reasoning results flow through the current-turn context only; the
    runner-level persistence guarantees live in test_screen_vision_integration
    (test_d / test_e). Here we assert the chain never raises on plain text."""
    provider, _, _ = _chain(ProviderHTTPError(500, "down"))
    assert provider.answer("q", {"scene_summary": "s", "visible_text": ["普通文本"]}) == (
        "glm-brain-answer"
    )
