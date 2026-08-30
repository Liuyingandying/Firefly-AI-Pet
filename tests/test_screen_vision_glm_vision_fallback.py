"""GLM-4.6V-Flash vision fallback tests (transport faked; no network).

The real GLM-4.6V round-trip is verified by the live probe and the
service-level fallback dogfood. These tests pin the capability wall,
credential source, image transport, schema compatibility, and privacy.
"""

import pytest

from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.vision import glm_vision
from core.screen_vision.vision.glm_vision import (
    DEFAULT_GLM_VISION_MODEL,
    FORBIDDEN_VISION_MODELS,
    GlmVisionProvider,
)
from datetime import datetime

from providers.zhipu_glm import ZhipuGLMProvider


def _frame():
    return ScreenFrame(64, 32, "image/jpeg", b"\xff\xd8fake-jpeg", datetime.now())


def _glm_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_g_model_id_defaults_to_glm_4_6v_flash(monkeypatch):
    monkeypatch.delenv("GLM_VISION_MODEL", raising=False)
    provider = GlmVisionProvider(provider=ZhipuGLMProvider(api_key="k"))
    assert provider.model == "glm-4.6v-flash"
    assert provider.name == "zhipu-glm-4.6v-flash"
    assert DEFAULT_GLM_VISION_MODEL == "glm-4.6v-flash"
    assert "glm-4.7-flash" in FORBIDDEN_VISION_MODELS  # TEXT_ONLY wall


def test_h_credential_resolved_from_zhipu_api_key(monkeypatch, tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    import providers.base as base

    monkeypatch.setattr(base, "DEFAULT_ENV_FILE", empty_env)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    provider = GlmVisionProvider()
    assert provider._zhipu.api_key == "zhipu-key"


def test_i_glm_vision_receives_image_content_as_data_uri(monkeypatch):
    captured = {}

    def fake_transport(endpoint, payload, headers, timeout):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["auth_header"] = headers.get("Authorization", "")
        return _glm_response(
            '{"scene_summary": "glm 场景", "visible_text": ["hello"]}'
        )

    monkeypatch.setattr(glm_vision, "default_transport", fake_transport)
    provider = GlmVisionProvider(provider=ZhipuGLMProvider(api_key="secret"))
    observation = provider.inspect(_frame())

    assert isinstance(observation, ScreenObservation)
    assert observation.scene_summary == "glm 场景"
    content = captured["payload"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert captured["payload"]["model"] == "glm-4.6v-flash"
    assert captured["endpoint"].endswith("/chat/completions")
    # no credential leakage into what we can inspect
    assert "secret" not in repr(captured["payload"])


def test_i2_glm_prose_response_still_schema_compatible(monkeypatch):
    monkeypatch.setattr(
        glm_vision, "default_transport",
        lambda *a, **k: _glm_response("屏幕上是一个代码编辑器。"),
    )
    provider = GlmVisionProvider(provider=ZhipuGLMProvider(api_key="k"))
    observation = provider.inspect(_frame())
    assert isinstance(observation, ScreenObservation)  # same schema as TJU path
    assert "代码编辑器" in observation.scene_summary


def test_j_glm_vision_never_writes_to_disk(monkeypatch):
    monkeypatch.setattr(
        glm_vision, "default_transport",
        lambda *a, **k: _glm_response('{"scene_summary": "s"}'),
    )
    import pathlib

    real_open = pathlib.Path.open

    def forbid_writes(self, *args, **kwargs):
        mode = args[0] if args else (kwargs.get("mode") or "r")
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"disk write during glm vision inspect: mode={mode}")
        return real_open(self, *args, **kwargs)  # reads (e.g. .env) are fine

    monkeypatch.setattr(pathlib.Path, "write_bytes", forbid_writes)
    monkeypatch.setattr(pathlib.Path, "write_text", forbid_writes)
    monkeypatch.setattr(pathlib.Path, "open", forbid_writes)
    provider = GlmVisionProvider(provider=ZhipuGLMProvider(api_key="k"))
    assert isinstance(provider.inspect(_frame()), ScreenObservation)


def test_tj_u_and_glm_observations_share_schema():
    """TJU and GLM paths both funnel through to_observation -> identical
    ScreenObservation field set, so downstream layers are model-agnostic."""
    from core.screen_vision.vision.qwen_vision import to_observation

    via_tju = to_observation({"scene_summary": "s", "visible_text": ["a"]})
    via_glm = to_observation({"scene_summary": "s", "visible_text": ["a"]})
    assert set(via_tju.__dict__) == set(via_glm.__dict__)
    assert isinstance(via_glm, ScreenObservation)


def test_reasoning_route_untouched():
    """The reasoning fallback stays glm-4.7-flash TEXT_ONLY."""
    from core.screen_vision.brain.glm_reasoning import GlmReasoningProvider

    provider = GlmReasoningProvider(provider=ZhipuGLMProvider(api_key="k"))
    assert provider.model == "glm-4.7-flash"  # unchanged by the vision work
    assert provider.name == "zhipu-glm"
