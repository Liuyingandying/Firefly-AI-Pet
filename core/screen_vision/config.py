"""Screen Vision configuration and provider chain assembly.

Credential policy (single secret system, converged with the project's
existing provider layer):

- Credentials resolve through ``providers.base.resolve_setting``:
  process environment first, then the project ``.env``.
- Only the project's existing variable names are used in production:
  TJULLM_API_KEY (TJU v3 vision), TJUTOKEN (TJU v1 reasoning),
  ZHIPU_API_KEY (optional GLM, currently disabled for vision),
  DEEPSEEK_API_KEY (optional official-DeepSeek reasoning fallback).
- FIREFLY_VISION_API_KEY / FIREFLY_REASONING_API_KEY / GLM_API_KEY are
  DEPRECATED aliases kept only for backwards compatibility with earlier
  PoC tooling; they are consulted last and will be removed.
- No Screen Vision production module reads personal tool config files
  (e.g. Qwen Code settings.json). Qwen Code config belongs to dev tooling.

Provider chain policy:
- Vision: TJU Qwen (v3 multimodal) primary; Zhipu glm-4.6v-flash
  (verified VISION_CAPABLE live, free tier) fallback whenever ZHIPU_API_KEY
  resolves. glm-4.7-flash is TEXT_ONLY (verified HTTP 400 error 1210) and is
  hard-guarded against ever being used as a vision model.
- Reasoning: TJU v1 deepseek-v4-flash first (verified), optional fallback
  to the existing providers.deepseek.DeepSeekProvider (official platform)
  when DEEPSEEK_API_KEY is configured.
"""

import os

from providers.base import resolve_setting

VISION_TIMEOUT_SECONDS = 180
BRAIN_TIMEOUT_SECONDS = 120

DEFAULT_VISION_BASE_URL = "https://ai.tju.edu.cn/api/v3"
DEFAULT_VISION_MODEL = "tju-llm"
DEFAULT_REASONING_BASE_URL = "https://ai.tju.edu.cn/api/v1"
DEFAULT_REASONING_MODEL = "deepseek-v4-flash"
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

DEPRECATED_VISION_KEY_ALIASES = ("TJU_QWEN_API_KEY", "FIREFLY_VISION_API_KEY")
DEPRECATED_REASONING_KEY_ALIASES = ("FIREFLY_REASONING_API_KEY",)
DEPRECATED_GLM_KEY_ALIASES = ("GLM_API_KEY",)


def _resolve_credential(primary_name: str, deprecated_aliases=()) -> str:
    """Resolve one credential: canonical project name first, deprecated
    aliases last. Values never leave this module's return value."""
    value = resolve_setting(primary_name)
    if value:
        return value
    for alias in deprecated_aliases:
        value = resolve_setting(alias)
        if value:
            return value
    return ""


class VisionConfig:
    def __init__(self, base_url: str, model: str, api_key: str, extra_body: dict | None = None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.extra_body = extra_body or {}


class ReasoningConfig:
    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key


def load_vision_config() -> VisionConfig:
    """TJU v3 vision config via TJULLM_API_KEY (canonical project name)."""
    api_key = _resolve_credential("TJULLM_API_KEY", DEPRECATED_VISION_KEY_ALIASES)
    if not api_key:
        raise RuntimeError(
            "TJULLM_API_KEY is not configured (process env or project .env)."
        )
    return VisionConfig(
        base_url=os.environ.get("TJULLM_BASE_URL", DEFAULT_VISION_BASE_URL).rstrip("/"),
        model=os.environ.get("TJULLM_MODEL", DEFAULT_VISION_MODEL),
        api_key=api_key,
    )


def load_reasoning_config() -> ReasoningConfig:
    """TJU v1 deepseek-v4-flash config via TJUTOKEN."""
    api_key = _resolve_credential("TJUTOKEN", DEPRECATED_REASONING_KEY_ALIASES)
    if not api_key:
        raise RuntimeError(
            "TJUTOKEN is not configured (process env or project .env); "
            "required for the TJU deepseek-v4-flash reasoning route."
        )
    return ReasoningConfig(
        base_url=os.environ.get("TJU_REASONING_BASE_URL", DEFAULT_REASONING_BASE_URL).rstrip("/"),
        model=os.environ.get("TJU_REASONING_MODEL", DEFAULT_REASONING_MODEL),
        api_key=api_key,
    )


DEFAULT_GLM_VISION_MODEL = "glm-4.6v-flash"  # verified VISION_CAPABLE (free tier)


def glm_vision_fallback_enabled() -> bool:
    """GLM vision fallback (glm-4.6v-flash, verified VISION_CAPABLE) is
    enabled whenever the shared ZHIPU_API_KEY credential resolves.
    glm-4.7-flash is TEXT_ONLY and can never be the vision model."""
    return bool(_resolve_credential("ZHIPU_API_KEY", DEPRECATED_GLM_KEY_ALIASES))


def glm_reasoning_fallback_enabled() -> bool:
    choice = (os.environ.get("FIREFLY_REASONING_FALLBACK") or "").strip().lower()
    return choice == "glm" and bool(os.environ.get("GLM_REASONING_MODEL", "").strip())


def official_deepseek_fallback_enabled() -> bool:
    """Reuse providers.deepseek when the official DEEPSEEK_API_KEY exists."""
    return bool(resolve_setting("DEEPSEEK_API_KEY"))


def load_glm_config() -> dict:
    """GLM credentials via ZHIPU_API_KEY (deprecated GLM_API_KEY last)."""
    api_key = _resolve_credential("ZHIPU_API_KEY", DEPRECATED_GLM_KEY_ALIASES)
    if not api_key:
        raise RuntimeError("ZHIPU_API_KEY is not configured.")
    return {
        "api_key": api_key,
        "vision_model": os.environ.get("GLM_VISION_MODEL", DEFAULT_GLM_VISION_MODEL),
        "reasoning_model": os.environ.get("GLM_REASONING_MODEL", ""),
        "base_url": os.environ.get("GLM_BASE_URL", DEFAULT_GLM_BASE_URL).rstrip("/"),
    }


def build_vision_provider():
    """Vision chain: TJU Qwen primary; Zhipu glm-4.6v-flash fallback
    (verified VISION_CAPABLE) enabled whenever ZHIPU_API_KEY resolves."""
    from core.screen_vision.failover import FailoverVisionProvider
    from core.screen_vision.vision.qwen_vision import QwenVisionProvider

    primary_choice = (os.environ.get("FIREFLY_VISION_PRIMARY") or "tju").strip().lower()
    if primary_choice != "tju":
        raise RuntimeError(
            f"Unsupported FIREFLY_VISION_PRIMARY={primary_choice!r} (supported: tju)"
        )
    primary = QwenVisionProvider()
    fallback = None
    if glm_vision_fallback_enabled():
        from core.screen_vision.vision.glm_vision import GlmVisionProvider

        fallback = GlmVisionProvider()
    if fallback is None:
        return FailoverVisionProvider(primary=primary, fallback=None)
    return FailoverVisionProvider(primary=primary, fallback=fallback)


def build_reasoning_provider():
    """Reasoning chain: TJU deepseek-v4-flash first (verified), Zhipu
    glm-4.7-flash as the default transient-failure fallback (TEXT_ONLY is
    correct — the reasoning stage only receives text). The official
    providers.deepseek client stays available as an explicit extra layer but
    is NOT auto-enabled, to avoid unintended paid calls."""
    from core.screen_vision.brain.deepseek_brain import DeepSeekV4FlashProvider
    from core.screen_vision.failover import ChainedReasoningProvider
    from providers.zhipu_glm import ZhipuGLMProvider

    primary_choice = (os.environ.get("FIREFLY_REASONING_PRIMARY") or "tju").strip().lower()
    if primary_choice != "tju":
        raise RuntimeError(
            f"Unsupported FIREFLY_REASONING_PRIMARY={primary_choice!r} (supported: tju)"
        )
    primary = DeepSeekV4FlashProvider()

    fallbacks = []
    if _resolve_credential("ZHIPU_API_KEY", DEPRECATED_GLM_KEY_ALIASES):
        from core.screen_vision.brain.glm_reasoning import GlmReasoningProvider

        fallbacks.append(GlmReasoningProvider())
    if glm_reasoning_fallback_enabled():
        glm = load_glm_config()
        if glm["reasoning_model"]:
            fallbacks.append(
                GlmReasoningProvider(
                    provider=ZhipuGLMProvider(
                        api_key=glm["api_key"],
                        base_url=glm["base_url"],
                        model=glm["reasoning_model"],
                    )
                )
            )
    if not fallbacks:
        return primary
    return ChainedReasoningProvider(primary=primary, fallbacks=tuple(fallbacks))
