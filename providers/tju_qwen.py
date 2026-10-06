"""Tianjin University Qwen OpenAI-compatible provider."""

from __future__ import annotations

from pathlib import Path

from .base import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleProvider, Transport, resolve_setting
from core.providers.catalog import CATALOG


DEFAULT_BASE_URL = CATALOG["tju"].base_url
DEFAULT_MODEL = CATALOG["tju"].model


class TJUQwenProvider(OpenAICompatibleProvider):
    name = "tju"
    supports_model_routing = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        env_file: Path | str | None = None,
        transport: Transport | None = None,
        routing_enabled: bool = True,
        model_router=None,
    ) -> None:
        self._routing_enabled = routing_enabled
        self._model_router = model_router
        self._explicit_model = model
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
        if self._explicit_model is None and self.default_model != DEFAULT_MODEL:
            self._explicit_model = self.default_model

    def chat(self, messages, model=None, temperature=0.2, *, timeout=None,
             task="chat", profile=None, variant=None):
        from .base import validate_messages
        normalized = validate_messages(messages)
        if not self._routing_enabled:
            return super().chat(normalized, model=model, temperature=temperature,
                                timeout=timeout, variant=variant)
        if variant is not None:
            raise ValueError("select a profile to use a model variant")
        from core.model_router import get_model_router
        router = self._model_router or get_model_router()
        explicit = model or (self._explicit_model if profile is None else None)
        parent_chat = super().chat
        def send(payload, budget):
            return parent_chat(normalized, model=payload["model"], temperature=temperature,
                               timeout=budget, variant=payload.get("variant"))
        return router.request({}, send, task=task, profile=profile, model=explicit,
                              timeout=min(self.timeout, timeout if timeout is not None else self.timeout))
