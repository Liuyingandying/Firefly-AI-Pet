"""Provider configuration status layer (Provider Manager Phase 3A).

Read-only query surface between the provider stack and the (future) model
settings UI:

- :meth:`ProviderManager.list_providers` — one row per credential-bearing
  ``core.providers.catalog`` entry (ids, categories, credential bindings and
  status are never duplicated here)
- :meth:`ProviderManager.get_provider_status` — single-provider variant
- :meth:`ProviderManager.state_version` / ``connect_runtime_bus`` — cheap
  change detection: ``core.ai_router.reload_default_routers`` publishes the
  ``providers.updated`` runtime event through the composition root, this
  module counts it so UIs can re-query (queries are always live anyway).

Source precedence mirrors ``providers.base.resolve_setting`` exactly:
  environment > credential_store > dotenv > missing

Values are secrets: **no method on this module returns a key value** — only
booleans, source labels and catalog-derived metadata.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from core.credential_store import default_store
from core.providers.catalog import CATEGORY_CODING_AGENT, CATALOG, STATUS_DEPRECATED
from providers.base import read_env_file

SOURCE_ENVIRONMENT = "environment"
SOURCE_CREDENTIAL_STORE = "credential_store"
SOURCE_DOTENV = "dotenv"
SOURCE_MISSING = "missing"

PROVIDERS_UPDATED_EVENT = "providers.updated"

# Presentation-only labels; ids/status/credentials stay single-sourced in the
# catalog. Unknown ids fall back to a derived label.
_DISPLAY_NAMES = {
    "tju": "TJU LLM",
    "zhipu": "Zhipu GLM",
    "deepseek": "DeepSeek",
    "tju-qwen": "TJU Qwen Vision",
    "glm-vision": "GLM Vision",
    "deepseek-vision": "DeepSeek Vision",
    "tju-reasoning": "TJU Reasoning",
    "glm-reasoning": "GLM Reasoning",
    "deepseek-reasoning": "DeepSeek Reasoning",
    "dashscope-qwen": "DashScope Qwen (deprecated)",
}


def _display_name(spec_id: str) -> str:
    return _DISPLAY_NAMES.get(spec_id, spec_id.replace("-", " ").title())


class ProviderManager:
    """Answer "which providers are configured, and from where?".

    ``store`` accepts any object exposing ``get(name) -> str | None`` (the
    Phase 1 :class:`core.credential_store.CredentialStore` qualifies); the
    process default store is used when omitted.  ``env_file`` overrides the
    dotenv layer for tests; production uses the project ``.env``.
    """

    def __init__(self, store: Any | None = None, *, env_file: Any | None = None) -> None:
        self._store = store if store is not None else default_store()
        self._env_file = env_file
        self._lock = threading.RLock()
        self._state_version = 0

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list_providers(self) -> list[dict[str, Any]]:
        """One status row per credential-bearing catalog provider.

        Coding-agent entries (claude-cli / codex-cli) carry no credential and
        are excluded; deprecated entries are included with ``enabled=False``
        so UIs can render them greyed out from the same rows.
        """
        rows: list[dict[str, Any]] = []
        for spec in CATALOG.values():
            if spec.category == CATEGORY_CODING_AGENT or not spec.credential_env:
                continue
            source = self._source_for(spec.credential_env)
            rows.append(
                {
                    "id": spec.id,
                    "display_name": _display_name(spec.id),
                    "credential_key": spec.credential_env[0],
                    "configured": source != SOURCE_MISSING,
                    "source": source,
                    "enabled": spec.status != STATUS_DEPRECATED,
                }
            )
        return rows

    def get_provider_status(self, provider_id: str) -> dict[str, Any]:
        """``{"configured": bool, "source": str}`` for one catalog entry."""
        spec = CATALOG[provider_id]  # unknown ids raise KeyError
        if spec.category == CATEGORY_CODING_AGENT or not spec.credential_env:
            raise KeyError(f"provider '{provider_id}' has no credential to manage")
        source = self._source_for(spec.credential_env)
        return {"configured": source != SOURCE_MISSING, "source": source}

    # ------------------------------------------------------------------
    # change tracking (RuntimeBus providers.updated)
    # ------------------------------------------------------------------
    def state_version(self) -> int:
        """Monotonic counter bumped on every observed ``providers.updated``."""
        with self._lock:
            return self._state_version

    def connect_runtime_bus(self, bus: Any) -> Callable[[], None]:
        """Subscribe to the bus's event channel; returns an unsubscribe."""
        return bus.subscribe_event(self._handle_event)

    def _handle_event(self, event: Any) -> None:
        if getattr(event, "kind", "") != PROVIDERS_UPDATED_EVENT:
            return
        with self._lock:
            self._state_version += 1

    # ------------------------------------------------------------------
    # internal: source detection (mirrors resolve_setting precedence)
    # ------------------------------------------------------------------
    def _source_for(self, names: tuple[str, ...]) -> str:
        for name in names:
            value = os.environ.get(name)
            if value and value.strip():
                return SOURCE_ENVIRONMENT
        if self._store is not None:
            for name in names:
                try:
                    value = self._store.get(name)
                except Exception:
                    value = None
                if value and value.strip():
                    return SOURCE_CREDENTIAL_STORE
        env_map = read_env_file(self._env_file)
        for name in names:
            value = env_map.get(name)
            if value and value.strip():
                return SOURCE_DOTENV
        return SOURCE_MISSING
