"""Reasoning fallback reusing the project's existing official DeepSeek client.

This wraps providers/deepseek.DeepSeekProvider (OpenAI-compatible,
DEEPSEEK_API_KEY via resolve_setting) instead of maintaining a second
DeepSeek HTTP client in Screen Vision. Text-only by construction: the
observation payload passes the same image-refusal check before send.
"""

from providers.deepseek import DeepSeekProvider
from providers.base import ProviderTimeoutError

from core.screen_vision.brain.base import assert_text_only_payload
from core.screen_vision.brain.deepseek_brain import build_brain_messages
from core.screen_vision.provider_errors import (
    ProviderNetworkError,
    ProviderSchemaError,
)


class OfficialDeepSeekReasoningProvider:
    """ReasoningProvider fallback over the official DeepSeek platform."""

    name = "deepseek-official"

    def __init__(self, provider: DeepSeekProvider | None = None):
        self._provider = provider or DeepSeekProvider()

    @property
    def model(self) -> str:
        return str(getattr(self._provider, "default_model", "deepseek-chat") or "deepseek-chat")

    def answer(self, question: str, observation: dict) -> str:
        assert_text_only_payload(observation)
        messages = build_brain_messages(question, observation)
        try:
            response = self._provider.chat(messages)
        except ProviderTimeoutError as exc:
            raise ProviderNetworkError(exc) from None
        except Exception as exc:  # noqa: BLE001 - classify legacy errors
            from core.screen_vision.provider_errors import classify_provider_exception

            raise classify_provider_exception(exc) from None
        if not isinstance(response, dict):
            raise ProviderSchemaError("reasoning response must be an object")
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderSchemaError(f"unexpected reasoning response shape: {exc}")
        return content or ""
