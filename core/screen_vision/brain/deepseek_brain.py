"""DeepSeek-V4-Flash reasoning provider over the TJU v1 endpoint.

The request payload sent here is text-only by construction and is checked
against image-data markers before every send.
"""

import json

import requests

from core.screen_vision.brain.base import ReasoningProvider, assert_text_only_payload
from core.screen_vision.config import BRAIN_TIMEOUT_SECONDS, ReasoningConfig, load_reasoning_config
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderSchemaError,
)
from core.screen_vision.safety import sanitize_error_text

BRAIN_SYSTEM_PROMPT = """你是一个助手。视觉模块已经观察了用户的屏幕并给出结构化观察结果。
你只能依据用户问题和视觉观察结果回答。
不要声称自己直接看到了原图。
如果视觉观察结果存在不确定项，应明确说明不确定。
如果观察结果不足以回答，请直说。"""


def build_brain_messages(user_question: str, observation_payload: dict) -> list:
    """Text-only messages: the question plus the observation JSON."""
    assert_text_only_payload(observation_payload)
    serialized = json.dumps(observation_payload, ensure_ascii=False)
    return [
        {"role": "system", "content": BRAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户：\n{user_question}\n\n"
                f"视觉模块观察：\n{serialized}\n\n"
                "请根据视觉观察回答用户。"
            ),
        },
    ]


class DeepSeekV4FlashProvider(ReasoningProvider):
    """TJU deepseek-v4-flash implementation of ReasoningProvider."""

    name = "tju-deepseek"

    def __init__(self, config: ReasoningConfig | None = None):
        self._config = config or load_reasoning_config()

    @property
    def model(self) -> str:
        return self._config.model

    def answer(self, question: str, observation: dict) -> str:
        cfg = self._config
        assert_text_only_payload(observation)
        payload = {
            "model": cfg.model,
            "messages": build_brain_messages(question, observation),
            "stream": False,
        }
        try:
            response = requests.post(
                f"{cfg.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=BRAIN_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderNetworkError(exc)
        if not response.ok:
            # Never include auth headers in error output.
            raise ProviderHTTPError(
                response.status_code,
                sanitize_error_text(response.text[:300], cfg.api_key),
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"].get("content")
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderSchemaError(f"unexpected brain response shape: {exc}")
        return content or ""
