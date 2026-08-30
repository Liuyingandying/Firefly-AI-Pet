"""DeepSeek vision + official reasoning fallback tests.

Real probe passed on 2026-08-30: deepseek-v4-flash-vision-exp read
FIREFLY_DS_VISION_827 verbatim over a JPEG data URI (HTTP 200). These tests
pin the chain wiring, image-only-to-vision, text-only-to-reasoning, model
ids, and meta — with transports stubbed so no real API is called.
"""

import json
from datetime import datetime

import pytest

from core.screen_vision.failover import FailoverReasoningProvider, FailoverVisionProvider
from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    ReasoningTemporarilyUnavailable,
    VisionTemporarilyUnavailable,
)
from core.screen_vision.vision import deepseek_vision as ds_vision
from core.screen_vision.vision.deepseek_vision import (
    DEEPSEEK_VISION_MODEL,
    DeepSeekVisionProvider,
    build_vision_messages,
)
from core.screen_vision.vision.glm_vision import GlmVisionProvider
from core.screen_vision.vision.qwen_vision import QwenVisionProvider, to_observation


def _frame():
    return ScreenFrame(64, 32, "image/jpeg", b"\xff\xd8fake", datetime.now())


def _obs(scene="s"):
    return ScreenObservation(scene_summary=scene)


class FakeVision:
    name = "tju-qwen"
    model = "tju-llm"

    def __init__(self, error=None):
        self.calls = 0
        self._error = error

    def inspect(self, frame, instruction=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _obs("primary")


class FakeGlmVision:
    name = "zhipu-glm-4.6v-flash"
    model = "glm-4.6v-flash"

    def __init__(self, error=None):
        self.calls = 0
        self._error = error

    def inspect(self, frame, instruction=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _obs("glm")


class FakeDeepSeekVision:
    name = "deepseek-vision"
    model = DEEPSEEK_VISION_MODEL

    def __init__(self, error=None):
        self.calls = 0
        self.frames = []
        self._error = error

    def inspect(self, frame, instruction=None):
        self.calls += 1
        self.frames.append(frame)
        if self._error is not None:
            raise self._error
        return _obs("deepseek")


class FakeReasoning:
    name = "tju-deepseek"

    def __init__(self, error=None):
        self.calls = 0
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return "primary-brain"


class FakeGlmReasoning:
    name = "zhipu-glm"

    def __init__(self, error=None):
        self.calls = 0
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return "glm-brain"


class FakeOfficialDeepSeek:
    name = "deepseek-official"

    def __init__(self, error=None):
        self.calls = 0
        self.payloads = []
        self._error = error

    def answer(self, question, observation):
        self.calls += 1
        self.payloads.append(observation)
        if self._error is not None:
            raise self._error
        return "official-brain"


# ------------------------------------------------------------ vision A-I


def test_a_primary_success_no_glm_no_deepseek():
    vision = FailoverVisionProvider(
        primary=FakeVision(),
        fallbacks=(FakeGlmVision(), FakeDeepSeekVision()),
    )
    assert vision.inspect(_frame()).scene_summary == "primary"
    assert vision._fallbacks[0].calls == 0 and vision._fallbacks[1].calls == 0


def test_b_tju_500_glm_success_no_deepseek():
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju down")),
        fallbacks=(FakeGlmVision(), FakeDeepSeekVision()),
    )
    assert vision.inspect(_frame()).scene_summary == "glm"
    assert vision._fallbacks[1].calls == 0


def test_c_tju_500_glm_429_deepseek_success():
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju")),
        fallbacks=(
            FakeGlmVision(ProviderHTTPError(429, "glm busy")),
            FakeDeepSeekVision(),
        ),
    )
    obs = vision.inspect(_frame())
    assert obs.scene_summary == "deepseek"
    assert vision._fallbacks[1].calls == 1
    meta = vision.last_meta
    assert meta["vision_provider"] == "deepseek-vision"
    assert meta["vision_fallback_used"] is True
    assert meta["vision_primary_failure_type"] == "HTTP_500"
    assert meta["vision_secondary_failure_type"] == "HTTP_429"


def test_d_deepseek_vision_receives_image():
    ds = FakeDeepSeekVision()
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmVision(ProviderHTTPError(429, "glm")), ds),
    )
    vision.inspect(_frame())
    assert len(ds.frames) == 1
    assert ds.frames[0].image_bytes == b"\xff\xd8fake"


def test_e_model_id_is_vision_exp():
    assert DEEPSEEK_VISION_MODEL == "deepseek-v4-flash-vision-exp"
    assert DeepSeekVisionProvider(api_key="k").model == "deepseek-v4-flash-vision-exp"


def test_e2_build_messages_data_uri():
    messages = build_vision_messages(_frame(), "instruction")
    url = messages[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


def test_f_deepseek_401_no_retry_no_further_fallback():
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmVision(ProviderHTTPError(429, "glm")),
                   FakeDeepSeekVision(ProviderHTTPError(401, "bad key"))),
    )
    with pytest.raises(ProviderHTTPError):
        vision.inspect(_frame())


def test_g_deepseek_429_all_fail_temporarily_unavailable():
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmVision(ProviderHTTPError(429, "glm")),
                   FakeDeepSeekVision(ProviderHTTPError(429, "ds busy"))),
    )
    with pytest.raises(VisionTemporarilyUnavailable):
        vision.inspect(_frame())


def test_h_observation_schema_identical():
    via_tju = to_observation({"scene_summary": "s", "visible_text": ["a"]})
    via_deepseek = to_observation({"scene_summary": "s", "visible_text": ["a"]})
    assert set(via_tju.__dict__) == set(via_deepseek.__dict__)


def test_i_no_credential_or_base64_in_meta():
    vision = FailoverVisionProvider(
        primary=FakeVision(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmVision(ProviderHTTPError(429, "glm")), FakeDeepSeekVision()),
    )
    vision.inspect(_frame())
    serialized = json.dumps(vision.last_meta).lower()
    assert "api_key" not in serialized and "secret" not in serialized
    assert "base64" not in serialized and "image" not in serialized


# -------------------------------------------------------- reasoning J-N


def test_j_reasoning_primary_success_no_fallback():
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(),
        fallbacks=(FakeGlmReasoning(), FakeOfficialDeepSeek()),
    )
    assert reasoning.answer("q", {"scene_summary": "s"}) == "primary-brain"
    assert reasoning._fallbacks[0].calls == 0 and reasoning._fallbacks[1].calls == 0


def test_k_tju_500_glm_success_no_deepseek():
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmReasoning(), FakeOfficialDeepSeek()),
    )
    assert reasoning.answer("q", {"scene_summary": "s"}) == "glm-brain"
    assert reasoning._fallbacks[1].calls == 0


def test_l_tju_500_glm_429_official_deepseek_success():
    official = FakeOfficialDeepSeek()
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmReasoning(ProviderHTTPError(429, "glm")), official),
    )
    assert reasoning.answer("q", {"scene_summary": "s"}) == "official-brain"
    assert official.calls == 1
    meta = reasoning.last_meta
    assert meta["reasoning_provider"] == "deepseek-official"
    assert meta["reasoning_primary_failure_type"] == "HTTP_500"
    assert meta["reasoning_secondary_failure_type"] == "HTTP_429"


def test_m_official_deepseek_receives_text_only():
    official = FakeOfficialDeepSeek()
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmReasoning(ProviderHTTPError(429, "glm")), official),
    )
    reasoning.answer("q", {"scene_summary": "text only", "visible_text": ["x"]})
    payload = official.payloads[0]
    serialized = json.dumps(payload).lower()
    for marker in ("base64", "image_url", "image_bytes", "data:image"):
        assert marker not in serialized


def test_n_reasoning_payload_never_has_image_data():
    """Across the whole chain the observation sent to reasoning stays
    text-only even after vision fallbacks ran."""
    official = FakeOfficialDeepSeek()
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmReasoning(ProviderHTTPError(429, "glm")), official),
    )
    observation = _obs("screen")
    reasoning.answer("q", observation.__dict__)
    assert all(
        marker not in json.dumps(official.payloads[0]).lower()
        for marker in ("base64", "image_url", "image_bytes", "data:image")
    )


def test_all_fail_reasoning_temporarily_unavailable():
    reasoning = FailoverReasoningProvider(
        primary=FakeReasoning(ProviderHTTPError(500, "tju")),
        fallbacks=(FakeGlmReasoning(ProviderHTTPError(429, "glm")),
                   FakeOfficialDeepSeek(ProviderHTTPError(503, "ds"))),
    )
    with pytest.raises(ReasoningTemporarilyUnavailable):
        reasoning.answer("q", {"scene_summary": "s"})
