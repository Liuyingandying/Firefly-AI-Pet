"""Zhipu GLM-4.7-Flash OpenAI-compatible provider."""

from __future__ import annotations

from pathlib import Path

from .base import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleProvider, Transport, resolve_setting


DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"


class ZhipuGLMProvider(OpenAICompatibleProvider):
    name = "zhipu"

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
                "ZHIPU_API_KEY", env_file=env_file
            ),
            base_url=base_url if base_url is not None else resolve_setting(
                "ZHIPU_BASE_URL", DEFAULT_BASE_URL, env_file=env_file
            ),
            default_model=model if model is not None else resolve_setting(
                "ZHIPU_MODEL", DEFAULT_MODEL, env_file=env_file
            ),
            timeout=timeout,
            transport=transport,
        )
