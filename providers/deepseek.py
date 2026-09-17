"""DeepSeek Chat OpenAI-compatible provider."""

from __future__ import annotations

from pathlib import Path

from .base import (
    DEFAULT_TIMEOUT_SECONDS,
    OpenAICompatibleProvider,
    Transport,
    resolve_setting,
)
from core.providers.catalog import CATALOG


# Catalog exposes both forms; this adapter needs the full chat endpoint
# (normalize_chat_endpoint keeps it verbatim).
DEFAULT_BASE_URL = CATALOG["deepseek"].completion_endpoint
DEFAULT_MODEL = CATALOG["deepseek"].model


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        env_file: Path | str | None = None,
        transport: Transport | None = None,
    ) -> None:
        super().__init__(
            name=self.name,
            api_key=api_key if api_key is not None else resolve_setting(
                "DEEPSEEK_API_KEY", env_file=env_file
            ),
            base_url=base_url if base_url is not None else resolve_setting(
                "DEEPSEEK_BASE_URL", DEFAULT_BASE_URL, env_file=env_file
            ),
            default_model=model if model is not None else resolve_setting(
                "DEEPSEEK_MODEL", DEFAULT_MODEL, env_file=env_file
            ),
            timeout=timeout,
            transport=transport,
        )
