"""Read-only Provider status snapshot service (Phase 2.2).

Purpose: surface what Firefly currently uses (text / vision / coding agent /
fallback state) to users and developers WITHOUT any write path. Nothing here
modifies routing, providers, the catalog, the environment, or any config.

Data sources (no re-hardcoding of endpoints/models):
- provider metadata (id / category / base_url / model / credential env /
  status)  -> core.providers.catalog
- routing order (text)     -> core.ai_router.PROVIDER_ORDER
- routing order (vision)   -> core.screen_vision.config.get_vision_provider_order
- availability             -> credential env presence (providers.base.
                              resolve_setting) for API providers; executable
                              presence (shutil.which) for local CLI agents;
                              always available for local memory.

Display names live here only as a presentation mapping (Phase 1 already
aligned user-visible names with the real providers). They are not routing
metadata and never affect behavior.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from core.providers.catalog import (
    CATALOG,
    CATEGORY_CODING_AGENT,
    CATEGORY_REASONING,
    CATEGORY_TEXT,
    CATEGORY_VISION,
    STATUS_DEPRECATED,
)

CATEGORY_MEMORY = "memory"

# User-visible names (presentation only; endpoints/models come from catalog).
DISPLAY_NAMES: dict[str, str] = {
    "tju": "TJU LLM",
    "zhipu": "Zhipu",
    "deepseek": "DeepSeek",
    "tju-qwen": "TJU-Qwen",
    "glm-vision": "GLM Vision",
    "deepseek-vision": "DeepSeek Vision",
    "tju-reasoning": "TJU Reasoning",
    "glm-reasoning": "GLM Reasoning",
    "deepseek-reasoning": "DeepSeek Reasoning",
    "claude-cli": "Claude Code CLI",
    "codex-cli": "Codex CLI",
    "local-memory": "Local Memory",
}

# screen_vision registry names -> catalog ids (the order helpers use the
# registry names; the catalog is keyed by provider id).
_REGISTRY_TO_CATALOG: dict[str, str] = {
    "qwen_vision": "tju-qwen",
    "deepseek_vision": "deepseek-vision",
    "glm_vision": "glm-vision",
    "tju_reasoning": "tju-reasoning",
    "glm_reasoning": "glm-reasoning",
    "deepseek_reasoning": "deepseek-reasoning",
}

ROLE_PRIMARY = "primary"
ROLE_FALLBACK = "fallback"
ROLE_INDEPENDENT = "independent"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """One read-only status row."""

    category: str
    provider_id: str
    display_name: str
    role: str
    status: str
    available: bool
    fallback_rank: int
    model: str = ""


def _env_available(spec) -> bool:
    """True when any credential env name of the spec resolves non-empty."""
    from providers.base import resolve_setting

    return any(bool(resolve_setting(name)) for name in spec.credential_env)


def _cli_available(command: str) -> bool:
    return shutil.which(command) is not None


class ProviderStatusService:
    """Builds the current read-only status snapshot."""

    def __init__(self, *, which: object | None = None) -> None:
        # Injectable executable probe for tests.
        self._which = which if which is not None else shutil.which

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> list[ProviderStatus]:
        statuses: list[ProviderStatus] = []

        # Text: order comes from the router itself (never re-hardcoded).
        from core.ai_router import PROVIDER_ORDER

        for rank, spec_id in enumerate(PROVIDER_ORDER):
            spec = CATALOG[spec_id]
            if spec.status == STATUS_DEPRECATED:
                continue
            statuses.append(self._api_status(spec, ROLE_PRIMARY if rank == 0 else ROLE_FALLBACK, rank))

        # Vision: order comes from the screen-vision config.
        from core.screen_vision.config import get_vision_provider_order

        for rank, registry_name in enumerate(get_vision_provider_order("fast")):
            spec_id = _REGISTRY_TO_CATALOG[registry_name]
            spec = CATALOG[spec_id]
            if spec.status == STATUS_DEPRECATED:
                continue
            statuses.append(self._api_status(spec, ROLE_PRIMARY if rank == 0 else ROLE_FALLBACK, rank))

        # Reasoning: same source as vision config.
        from core.screen_vision.config import get_reasoning_provider_order

        for rank, registry_name in enumerate(get_reasoning_provider_order("fast")):
            spec_id = _REGISTRY_TO_CATALOG[registry_name]
            spec = CATALOG[spec_id]
            if spec.status == STATUS_DEPRECATED:
                continue
            statuses.append(self._api_status(spec, ROLE_PRIMARY if rank == 0 else ROLE_FALLBACK, rank))

        # Coding agents: local CLI entries (independent channels).
        for spec_id in ("codex-cli", "claude-cli"):
            spec = CATALOG[spec_id]
            statuses.append(
                ProviderStatus(
                    category=spec.category,
                    provider_id=spec.id,
                    display_name=DISPLAY_NAMES.get(spec.id, spec.id),
                    role=ROLE_INDEPENDENT,
                    status=spec.status,
                    available=self._which(spec_id.replace("-cli", "")) is not None,
                    fallback_rank=0,
                )
            )

        # Memory: local only (not a provider; always available).
        statuses.append(
            ProviderStatus(
                category=CATEGORY_MEMORY,
                provider_id="local-memory",
                display_name=DISPLAY_NAMES["local-memory"],
                role=ROLE_INDEPENDENT,
                status="active",
                available=True,
                fallback_rank=0,
                model="",
            )
        )
        return statuses

    def by_category(self) -> dict[str, list[ProviderStatus]]:
        grouped: dict[str, list[ProviderStatus]] = {}
        for status in self.snapshot():
            grouped.setdefault(status.category, []).append(status)
        return grouped

    def _api_status(self, spec, role: str, rank: int) -> ProviderStatus:
        return ProviderStatus(
            category=spec.category,
            provider_id=spec.id,
            display_name=DISPLAY_NAMES.get(spec.id, spec.id),
            role=role,
            status=spec.status,
            available=_env_available(spec),
            fallback_rank=rank,
            model=spec.model,
        )

    # -- rendering (read-only presentation text) -------------------------

    def render_text(self) -> str:
        """Multi-line summary for the read-only Settings -> AI Status view."""
        grouped = self.by_category()
        lines: list[str] = ["Firefly AI Status"]

        def _row(status: ProviderStatus) -> str:
            mark = "✓" if status.available else "○"
            name = status.display_name
            if not status.available:
                name += " (unavailable)"
            return f"  {mark} {name}"

        def _category(title: str, category: str) -> None:
            rows = grouped.get(category, [])
            if not rows:
                return
            lines.append(title)
            for status in rows:
                lines.append(_row(status))
            fallbacks = [s for s in rows if s.role == ROLE_FALLBACK]
            if fallbacks:
                lines.append("  Fallback: " + ", ".join(
                    f"{s.display_name}{' (unavailable)' if not s.available else ''}"
                    for s in fallbacks
                ))

        _category("Text", CATEGORY_TEXT)
        _category("Vision", CATEGORY_VISION)
        _category("Coding", CATEGORY_CODING_AGENT)
        _category("Memory", CATEGORY_MEMORY)
        return "\n".join(lines)
