"""Screen Vision provider cleanup tests (converged config & capability walls).

Covers the Stage-cleanup acceptance items not already covered by
test_screen_vision_failover.py / test_screen_vision_integration.py:
credential convergence (G), no personal-tool-config dependency (H),
TEXT_ONLY capability wall (E/F), sanitizer (R).
"""

import os
from pathlib import Path

import pytest

from providers.base import validate_messages
from core.screen_vision import config as sv_config
from core.screen_vision.circuit_breaker import CircuitBreaker
from core.screen_vision.failover import FailoverVisionProvider
from core.screen_vision.provider_errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderSchemaError,
    VisionTemporarilyUnavailable,
    is_transient,
)
from core.screen_vision.safety import sanitize_error_text


SCREEN_VISION_DIR = Path(r"E:\Firefly_AI_Pet\core\screen_vision")


# ------------------------------------------------------------ G credentials


@pytest.fixture
def isolated_env_file(monkeypatch, tmp_path):
    """Point providers.base at an empty .env so tests control resolution."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    import providers.base as base

    monkeypatch.setattr(base, "DEFAULT_ENV_FILE", empty_env)
    return empty_env


def test_g_vision_credential_uses_tjullm_api_key(monkeypatch, isolated_env_file):
    monkeypatch.delenv("TJULLM_API_KEY", raising=False)
    monkeypatch.delenv("FIREFLY_VISION_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TJULLM_API_KEY"):
        sv_config.load_vision_config()
    monkeypatch.setenv("TJULLM_API_KEY", "canonical-key")
    cfg = sv_config.load_vision_config()
    assert cfg.api_key == "canonical-key"
    assert cfg.base_url == sv_config.DEFAULT_VISION_BASE_URL
    assert cfg.model == sv_config.DEFAULT_VISION_MODEL


def test_g_reasoning_credential_uses_tjutoken(monkeypatch, isolated_env_file):
    monkeypatch.delenv("TJUTOKEN", raising=False)
    monkeypatch.delenv("FIREFLY_REASONING_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TJUTOKEN"):
        sv_config.load_reasoning_config()
    monkeypatch.setenv("TJUTOKEN", "canonical-token")
    cfg = sv_config.load_reasoning_config()
    assert cfg.api_key == "canonical-token"
    assert cfg.model == sv_config.DEFAULT_REASONING_MODEL


def test_g_deprecated_aliases_consulted_last(monkeypatch, isolated_env_file):
    monkeypatch.delenv("TJULLM_API_KEY", raising=False)
    monkeypatch.setenv("FIREFLY_VISION_API_KEY", "legacy-key")
    assert sv_config.load_vision_config().api_key == "legacy-key"
    monkeypatch.setenv("TJULLM_API_KEY", "canonical-key")
    assert sv_config.load_vision_config().api_key == "canonical-key"


# -------------------------------------------------- H no personal tool config


def test_h_no_qwencode_settings_dependency_in_production_code():
    offenders = [
        str(path.relative_to(SCREEN_VISION_DIR))
        for path in SCREEN_VISION_DIR.rglob("*.py")
        if "QwenCode" in path.read_text(encoding="utf-8")
        or "qwen_code" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"personal tool config leaked into: {offenders}"


# ------------------------------------------------ capability walls (E/F)


def test_e_glm_vision_requires_zhipu_credential(monkeypatch, isolated_env_file):
    # Without ZHIPU_API_KEY the glm-4.6v-flash fallback stays disabled.
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    assert sv_config.glm_vision_fallback_enabled() is False
    monkeypatch.setenv("TJULLM_API_KEY", "k")
    provider = sv_config.build_vision_provider()
    names = [getattr(p, "name", "") for p in _flatten_chain(provider)]
    assert not any(n.startswith("zhipu-") for n in names)


def test_e2_glm_vision_default_model_is_4_6v_flash(monkeypatch, isolated_env_file):
    # With the shared credential the fallback activates on glm-4.6v-flash,
    # never on the TEXT_ONLY glm-4.7-flash.
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("GLM_VISION_MODEL", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "z")
    monkeypatch.setenv("TJULLM_API_KEY", "k")
    provider = sv_config.build_vision_provider()
    glm = [p for p in _flatten_chain(provider) if getattr(p, "name", "").startswith("zhipu-")]
    assert len(glm) == 1
    assert glm[0].model == "glm-4.6v-flash"
    assert glm[0].name == "zhipu-glm-4.6v-flash"

    # and the TEXT_ONLY model is hard-guarded even if someone configures it
    from core.screen_vision.vision.glm_vision import (
        FORBIDDEN_VISION_MODELS,
        GlmVisionProvider,
    )
    from providers.zhipu_glm import ZhipuGLMProvider

    assert "glm-4.7-flash" in FORBIDDEN_VISION_MODELS
    with pytest.raises(ProviderError):
        GlmVisionProvider(provider=ZhipuGLMProvider(api_key="z"), model="glm-4.7-flash")


def _flatten_chain(provider):
    yielded = [provider]
    for attr in ("_primary", "_fallback", "_fallbacks"):
        value = getattr(provider, attr, None)
        if value is None:
            continue
        if isinstance(value, tuple):
            yielded.extend(value)
        else:
            yielded.append(value)
    return yielded


def test_e3_glm_flash_4_7_text_only_wall_documented():
    """Verified live on 2026-08-30: image content -> HTTP 400, error 1210,
    content.type allowed values ['text']. The source must encode that wall."""
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in SCREEN_VISION_DIR.rglob("*.py")
    )
    assert "TEXT_ONLY" in source  # documented capability wall
    assert "1210" in source       # the verification evidence is recorded


def test_f_text_only_message_validation_rejects_image_payload():
    """The shared text-provider layer refuses multimodal content parts, so a
    TEXT_ONLY provider can never receive an image payload by construction."""
    with pytest.raises(ValueError):
        validate_messages([{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }])


# ------------------------------------------------- B/C classification


def test_b_transient_500_maps_to_vision_temporarily_unavailable():
    class Down:
        name = "tju-qwen"

        def inspect(self, frame, instruction=None):
            raise ProviderHTTPError(500, "internal")

    provider = FailoverVisionProvider(primary=Down(), fallback=None)
    with pytest.raises(VisionTemporarilyUnavailable) as excinfo:
        provider.inspect(object())
    assert excinfo.value.failure_type == "HTTP_500"


def test_c_429_is_transient():
    assert is_transient(ProviderHTTPError(429, "rate limited"))


def test_schema_error_propagates_unchanged():
    class Broken:
        name = "tju-qwen"

        def inspect(self, frame, instruction=None):
            raise ProviderSchemaError("bad json")

    provider = FailoverVisionProvider(primary=Broken(), fallback=None)
    with pytest.raises(ProviderSchemaError):  # NOT VisionTemporarilyUnavailable
        provider.inspect(object())


# ------------------------------------------------------- R sanitizer


def test_r_sanitize_never_leaks_credentials():
    secret = "ZHIPU-secret-value-123"
    dirty = f"HTTP 400 from upstream, Authorization: Bearer {secret}"
    clean = sanitize_error_text(dirty, secret)
    assert secret not in clean
    assert "Bearer <REDACTED>" in clean or "[auth-credentials removed]" in clean


def test_r_circuit_breaker_records_failure_type():
    clock = lambda: 0.0  # noqa: E731
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0, clock=clock)
    breaker.record_transient_failure("HTTP_500")
    assert breaker.last_failure_type == "HTTP_500"
    assert breaker.is_open
