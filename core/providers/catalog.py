"""Provider Catalog — provider base-configuration single source of truth.

Firefly Routing Cleanup Phase 2.1: providers/* (text stack) and
core/screen_vision/config.py (vision/reasoning stack) previously carried
their own copies of base_url / model defaults. This catalog is the one
place those values are defined; adapters reference it at import time so a
future change lands in exactly one file.

Scope (deliberately narrow):
- provider metadata: id / category / endpoint / model / credential env / status
- NOT routing order (see core/ai_router.py PROVIDER_ORDER and
  core/screen_vision/config.py get_*_provider_order)
- NOT fallback policy (see core/screen_vision/failover.py)
- NOT provider lifecycle (instances are owned by the consuming stacks)

Endpoint forms:
- ``base_url``            — bare origin/API root (OpenAI-SDK style); callers
                            that need ``/chat/completions`` must NOT append it
                            themselves, they use ``completion_endpoint``.
- ``completion_endpoint`` — full ``.../chat/completions`` URL for
                            requests-style adapters. Empty for providers
                            whose callers pass ``base_url`` through the
                            shared normalize helper.

Status values: active / fallback / deprecated. Deprecated entries are
catalog documentation only — nothing in production reads them (removal is a
later, separate task).
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
CATEGORY_TEXT = "text"
CATEGORY_VISION = "vision"
CATEGORY_REASONING = "reasoning"
CATEGORY_CODING_AGENT = "coding_agent"

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_FALLBACK = "fallback"
STATUS_DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Immutable base configuration for one provider entry."""

    id: str
    category: str
    base_url: str
    model: str
    credential_env: tuple[str, ...] = ()
    status: str = STATUS_ACTIVE
    completion_endpoint: str = ""
    note: str = ""


CATALOG: dict[str, ProviderSpec] = {
    # ------------------------------------------------------------------
    # text (core.ai_router chain: tju -> zhipu -> deepseek)
    # ------------------------------------------------------------------
    "tju": ProviderSpec(
        id="tju",
        category=CATEGORY_TEXT,
        base_url="https://ai.tju.edu.cn/api/v3",
        model="tju-llm",
        credential_env=("TJULLM_API_KEY",),
        status=STATUS_ACTIVE,
        completion_endpoint="https://ai.tju.edu.cn/api/v3/chat/completions",
        note="Tianjin University LLM — primary text provider (unlimited quota).",
    ),
    "zhipu": ProviderSpec(
        id="zhipu",
        category=CATEGORY_TEXT,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.7-flash",
        credential_env=("ZHIPU_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        note="Zhipu GLM text fallback. .env.example still ships ZHIPU_MODEL=glm-4-plus "
             "(template drift, catalog value wins).",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        category=CATEGORY_TEXT,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        credential_env=("DEEPSEEK_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://api.deepseek.com/chat/completions",
        note="DeepSeek official platform — text fallback only, never primary.",
    ),
    # ------------------------------------------------------------------
    # vision (screen_vision failover chain: tju-qwen primary)
    # ------------------------------------------------------------------
    "tju-qwen": ProviderSpec(
        id="tju-qwen",
        category=CATEGORY_VISION,
        base_url="https://ai.tju.edu.cn/api/v3",
        model="tju-llm",
        credential_env=("TJULLM_API_KEY",),
        status=STATUS_ACTIVE,
        completion_endpoint="https://ai.tju.edu.cn/api/v3/chat/completions",
        note="TJU-hosted Qwen multimodal (same endpoint as text tju) — primary vision.",
    ),
    "glm-vision": ProviderSpec(
        id="glm-vision",
        category=CATEGORY_VISION,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.6v-flash",
        credential_env=("ZHIPU_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        note="glm-4.7-flash is TEXT_ONLY and hard-guarded; never a vision model.",
    ),
    "deepseek-vision": ProviderSpec(
        id="deepseek-vision",
        category=CATEGORY_VISION,
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash-vision-exp",
        credential_env=("DEEPSEEK_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://api.deepseek.com/chat/completions",
        note="DeepSeek official multimodal — vision fallback only.",
    ),
    # ------------------------------------------------------------------
    # reasoning (screen_vision two-stage chain)
    # ------------------------------------------------------------------
    "tju-reasoning": ProviderSpec(
        id="tju-reasoning",
        category=CATEGORY_REASONING,
        base_url="https://ai.tju.edu.cn/api/v1",
        model="deepseek-v4-flash",
        credential_env=("TJUTOKEN",),
        status=STATUS_ACTIVE,
        completion_endpoint="https://ai.tju.edu.cn/api/v1/chat/completions",
        note="TJU v1 deepseek-v4-flash — primary reasoning (text-only stage).",
    ),
    "glm-reasoning": ProviderSpec(
        id="glm-reasoning",
        category=CATEGORY_REASONING,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.7-flash",
        credential_env=("ZHIPU_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
    ),
    "deepseek-reasoning": ProviderSpec(
        id="deepseek-reasoning",
        category=CATEGORY_REASONING,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        credential_env=("DEEPSEEK_API_KEY",),
        status=STATUS_FALLBACK,
        completion_endpoint="https://api.deepseek.com/chat/completions",
    ),
    # ------------------------------------------------------------------
    # coding agents (local execution entries, not chat providers)
    # ------------------------------------------------------------------
    "claude-cli": ProviderSpec(
        id="claude-cli",
        category=CATEGORY_CODING_AGENT,
        base_url="",
        model="",
        status=STATUS_ACTIVE,
        note="Local Claude Code CLI (subprocess); backend via user's own "
             "~/.claude settings/proxy — never an in-process provider.",
    ),
    "codex-cli": ProviderSpec(
        id="codex-cli",
        category=CATEGORY_CODING_AGENT,
        base_url="",
        model="",
        status=STATUS_ACTIVE,
        note="Local Codex CLI (subprocess); backend via user's own ~/.codex login.",
    ),
    # ------------------------------------------------------------------
    # deprecated (catalog documentation only; nothing reads these)
    # ------------------------------------------------------------------
    "dashscope-qwen": ProviderSpec(
        id="dashscope-qwen",
        category=CATEGORY_VISION,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-vl-plus",
        credential_env=("DASHSCOPE_API_KEY",),
        status=STATUS_DEPRECATED,
        note="Dead config: only .env.example mentions DASHSCOPE_*; no code reads it. "
             "Removal is a later task.",
    ),
}


def get(spec_id: str) -> ProviderSpec:
    """Return one spec (KeyError when unknown)."""
    return CATALOG[spec_id]


def active_ids(category: str | None = None) -> list[str]:
    """Ids of active providers, optionally filtered by category."""
    return [
        spec.id
        for spec in CATALOG.values()
        if spec.status == STATUS_ACTIVE
        and (category is None or spec.category == category)
    ]
