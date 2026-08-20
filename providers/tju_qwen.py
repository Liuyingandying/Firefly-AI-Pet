"""Tianjin University Qwen OpenAI-compatible provider."""

from __future__ import annotations

from pathlib import Path

from .base import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleProvider, Transport, resolve_setting


DEFAULT_BASE_URL = "https://ai.tju.edu.cn/api/v3"
DEFAULT_MODEL = "tju-llm"


class TJUQwenProvider(OpenAICompatibleProvider):
    name = "tju"

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
                "TJULLM_API_KEY", env_file=env_file
            ),
            base_url=base_url if base_url is not None else resolve_setting(
                "TJULLM_BASE_URL", DEFAULT_BASE_URL, env_file=env_file
            ),
            default_model=model if model is not None else resolve_setting(
                "TJULLM_MODEL", DEFAULT_MODEL, env_file=env_file
            ),
            timeout=timeout,
            transport=transport,
        )
