"""DeepSeek official multimodal vision provider (deepseek-v4-flash-vision-exp).

Third vision fallback: TJU Qwen -> Zhipu GLM-4.6V -> DeepSeek vision.

Credential: DEEPSEEK_API_KEY via resolve_setting (never a second key name).
Endpoint: https://api.deepseek.com (OpenAI-compatible). Only the current
frame's JPEG data URI plus the vision instruction is sent — never workspace
paths, credentials, memory, history, or other screen state.

Output is converted into the same ScreenObservation schema as the TJU/GLM
vision providers so downstream reasoning/companion layers never know which
vision model ran.
"""

import base64
import json

import requests

from providers.base import resolve_setting

from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderSchemaError,
    classify_provider_exception,
)
from core.screen_vision.safety import sanitize_error_text
from core.screen_vision.vision.base import DEFAULT_VISION_INSTRUCTION
from core.screen_vision.vision.qwen_vision import extract_json, to_observation

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_VISION_MODEL = "deepseek-v4-flash-vision-exp"
VISION_TIMEOUT_SECONDS = 180


def build_vision_messages(frame: ScreenFrame, instruction: str) -> list:
    """Same multimodal schema as the other vision providers."""
    b64 = base64.b64encode(frame.image_bytes).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{frame.mime_type};base64,{b64}"},
                },
            ],
        }
    ]


class DeepSeekVisionProvider:
    """DeepSeek official multimodal vision implementation of VisionProvider."""

    name = "deepseek-vision"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self._api_key = api_key or resolve_setting("DEEPSEEK_API_KEY")
        if not self._api_key:
            raise ProviderError("DEEPSEEK_API_KEY is not configured")
        self._base_url = (base_url or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        self._model = model or DEEPSEEK_VISION_MODEL

    @property
    def model(self) -> str:
        return self._model

    def inspect(
        self,
        frame: ScreenFrame,
        instruction: str = DEFAULT_VISION_INSTRUCTION,
    ) -> ScreenObservation:
        payload = {
            "model": self._model,
            "messages": build_vision_messages(frame, instruction),
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=VISION_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderNetworkError(exc)
        if not response.ok:
            raise ProviderHTTPError(
                response.status_code,
                sanitize_error_text(response.text[:300], self._api_key),
            )
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderSchemaError(f"unexpected DeepSeek vision response shape: {exc}")
        raw_text = (message.get("content") or "").strip()
        if not raw_text:
            raise ProviderSchemaError("DeepSeek vision returned empty content")
        try:
            parsed = extract_json(raw_text)
            return to_observation(parsed, raw_model_text=raw_text)
        except (ValueError, ProviderSchemaError):
            # Prose instead of JSON: keep the schema contract by wrapping it
            # as the scene summary.
            return to_observation({"scene_summary": raw_text}, raw_model_text=raw_text)
