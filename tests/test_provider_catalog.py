"""Provider Catalog tests (Phase 2.1).

Locks:
- catalog loads every active provider
- provider ids are unique
- text / vision provider entries exist with correct endpoints, models,
  credential env names, and status
- DeepSeek exposes BOTH base_url and completion_endpoint forms
- router order is untouched by the catalog (text: tju -> zhipu -> deepseek;
  vision: tju-qwen primary)
- deprecated entries exist for documentation but are never referenced by
  production defaults
"""

from __future__ import annotations

from core.providers.catalog import (
    CATALOG,
    CATEGORY_TEXT,
    CATEGORY_VISION,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_FALLBACK,
    active_ids,
    get,
)


def test_catalog_loads_all_active_providers() -> None:
    ids = active_ids()
    assert "tju" in ids
    assert "tju-qwen" in ids
    assert "tju-reasoning" in ids
    # Every active id resolves to a spec.
    for spec_id in ids:
        spec = get(spec_id)
        assert spec.id == spec_id
        assert spec.status == STATUS_ACTIVE


def test_provider_ids_are_unique() -> None:
    ids = [spec.id for spec in CATALOG.values()]
    assert len(ids) == len(set(ids)), f"duplicate provider ids: {ids}"


def test_text_provider_configuration() -> None:
    tju = get("tju")
    assert tju.category == CATEGORY_TEXT
    assert tju.status == STATUS_ACTIVE
    assert tju.base_url == "https://ai.tju.edu.cn/api/v3"
    assert tju.model == "tju-llm"
    assert tju.credential_env == ("TJULLM_API_KEY",)

    zhipu = get("zhipu")
    assert zhipu.category == CATEGORY_TEXT
    assert zhipu.status == STATUS_FALLBACK
    assert zhipu.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert zhipu.model == "glm-4.7-flash"
    assert zhipu.credential_env == ("ZHIPU_API_KEY",)

    deepseek = get("deepseek")
    assert deepseek.category == CATEGORY_TEXT
    assert deepseek.status == STATUS_FALLBACK
    assert deepseek.model == "deepseek-chat"
    assert deepseek.credential_env == ("DEEPSEEK_API_KEY",)


def test_vision_provider_configuration() -> None:
    tju_qwen = get("tju-qwen")
    assert tju_qwen.category == CATEGORY_VISION
    assert tju_qwen.status == STATUS_ACTIVE
    assert tju_qwen.base_url == "https://ai.tju.edu.cn/api/v3"
    assert tju_qwen.model == "tju-llm"
    assert tju_qwen.credential_env == ("TJULLM_API_KEY",)

    glm_vision = get("glm-vision")
    assert glm_vision.category == CATEGORY_VISION
    assert glm_vision.status == STATUS_FALLBACK
    assert glm_vision.model == "glm-4.6v-flash"
    assert glm_vision.credential_env == ("ZHIPU_API_KEY",)

    deepseek_vision = get("deepseek-vision")
    assert deepseek_vision.category == CATEGORY_VISION
    assert deepseek_vision.status == STATUS_FALLBACK
    assert deepseek_vision.model == "deepseek-v4-flash-vision-exp"
    assert deepseek_vision.credential_env == ("DEEPSEEK_API_KEY",)


def test_deepseek_exposes_both_url_forms() -> None:
    """SDK-style base_url AND full completion_endpoint must both exist so
    callers never assemble the chat endpoint themselves."""
    for spec_id in ("deepseek", "deepseek-vision", "deepseek-reasoning"):
        spec = get(spec_id)
        assert spec.base_url == "https://api.deepseek.com"
        assert spec.completion_endpoint == "https://api.deepseek.com/chat/completions"


def test_credential_env_names_match_production() -> None:
    # The catalog must agree with the canonical env names the adapters read.
    from providers.tju_qwen import TJUQwenProvider
    from providers.deepseek import DeepSeekProvider
    from providers.zhipu_glm import ZhipuGLMProvider
    from core.screen_vision.config import load_vision_config  # noqa: F401 - import smoke

    assert get("tju").credential_env == ("TJULLM_API_KEY",)
    assert get("deepseek").credential_env == ("DEEPSEEK_API_KEY",)
    assert get("zhipu").credential_env == ("ZHIPU_API_KEY",)
    assert get("tju-reasoning").credential_env == ("TJUTOKEN",)
    # The adapter classes resolve those names (constructor smoke, no I/O).
    TJUQwenProvider(api_key="x")
    DeepSeekProvider(api_key="x")
    ZhipuGLMProvider(api_key="x")


def test_deprecated_entries_are_documentation_only() -> None:
    deprecated = [spec for spec in CATALOG.values() if spec.status == STATUS_DEPRECATED]
    assert any(spec.id == "dashscope-qwen" for spec in deprecated)
    # Deprecated ids must NOT appear in active_ids().
    for spec in deprecated:
        assert spec.id not in active_ids()


def test_router_order_is_unchanged() -> None:
    """The catalog must not alter routing order — this pins the current
    production chain so future catalog edits cannot silently reorder."""
    from core.ai_router import PROVIDER_ORDER

    assert PROVIDER_ORDER == ("tju", "zhipu", "deepseek")

    from core.screen_vision.config import get_vision_provider_order

    assert get_vision_provider_order("fast") == (
        "qwen_vision", "deepseek_vision", "glm_vision",
    )
    assert get_vision_provider_order("resilient") == (
        "qwen_vision", "glm_vision", "deepseek_vision",
    )


def test_catalog_values_match_adapter_defaults() -> None:
    """The migrated adapters must keep the exact pre-migration values."""
    from providers.tju_qwen import DEFAULT_BASE_URL as TJU_URL
    from providers.tju_qwen import DEFAULT_MODEL as TJU_MODEL
    from providers.deepseek import DEFAULT_BASE_URL as DS_URL
    from providers.deepseek import DEFAULT_MODEL as DS_MODEL
    from providers.zhipu_glm import DEFAULT_BASE_URL as ZP_URL
    from providers.zhipu_glm import DEFAULT_MODEL as ZP_MODEL
    from core.screen_vision.config import (
        DEFAULT_GLM_BASE_URL,
        DEFAULT_GLM_VISION_MODEL,
        DEFAULT_REASONING_BASE_URL,
        DEFAULT_REASONING_MODEL,
        DEFAULT_VISION_BASE_URL,
        DEFAULT_VISION_MODEL,
    )
    from core.screen_vision.vision.deepseek_vision import (
        DEFAULT_DEEPSEEK_BASE_URL,
        DEEPSEEK_VISION_MODEL,
    )

    assert TJU_URL == get("tju").base_url
    assert TJU_MODEL == get("tju").model
    assert DS_URL == get("deepseek").completion_endpoint
    assert DS_MODEL == get("deepseek").model
    assert ZP_URL == get("zhipu").base_url
    assert ZP_MODEL == get("zhipu").model

    assert DEFAULT_VISION_BASE_URL == get("tju-qwen").base_url
    assert DEFAULT_VISION_MODEL == get("tju-qwen").model
    assert DEFAULT_REASONING_BASE_URL == get("tju-reasoning").base_url
    assert DEFAULT_REASONING_MODEL == get("tju-reasoning").model
    assert DEFAULT_GLM_BASE_URL == get("glm-vision").base_url
    assert DEFAULT_GLM_VISION_MODEL == get("glm-vision").model
    assert DEFAULT_DEEPSEEK_BASE_URL == get("deepseek-vision").base_url
    assert DEEPSEEK_VISION_MODEL == get("deepseek-vision").model
