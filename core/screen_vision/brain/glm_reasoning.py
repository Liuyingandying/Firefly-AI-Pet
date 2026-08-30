"""GLM reasoning fallback reusing the project's existing Zhipu client.

Wraps providers/zhipu_glm.ZhipuGLMProvider (OpenAI-compatible,
glm-4.7-flash, ZHIPU_API_KEY via resolve_setting) instead of maintaining a
second Zhipu HTTP client. glm-4.7-flash is verified TEXT_ONLY, which is
correct here: the reasoning stage only ever receives the user question and
a text ScreenObservation — the payload passes the same image-refusal check
as every other reasoning provider before send.
"""

from providers.zhipu_glm import ZhipuGLMProvider

from core.screen_vision.brain.base import assert_text_only_payload
from core.screen_vision.brain.deepseek_brain import build_brain_messages
from core.screen_vision.provider_errors import (
    ProviderSchemaError,
    classify_provider_exception,
)


class GlmReasoningProvider:
    """Zhipu glm-4.7-flash implementation of the ReasoningProvider fallback."""

    name = "zhipu-glm"

    def __init__(self, provider: ZhipuGLMProvider | None = None):
        self._provider = provider or ZhipuGLMProvider()

    @property
    def model(self) -> str:
        return str(getattr(self._provider, "default_model", "glm-4.7-flash") or "glm-4.7-flash")

    def answer(self, question: str, observation: dict) -> str:
        assert_text_only_payload(observation)
        messages = build_brain_messages(question, observation)
        try:
            response = self._provider.chat(messages)
        except Exception as exc:  # noqa: BLE001 - classified at the boundary
            raise classify_provider_exception(exc)
        if not isinstance(response, dict):
            raise ProviderSchemaError("reasoning response must be an object")
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderSchemaError(f"unexpected reasoning response shape: {exc}")
        return content or ""
