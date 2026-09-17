"""DeepSeek official multimodal vision provider (deepseek-v4-flash-vision-exp).

Third vision fallback: TJU Qwen -> Zhipu GLM-4.6V -> DeepSeek vision.

Credential: DEEPSEEK_API_KEY via resolve_setting (never a second key name).
Endpoint: https://api.deepseek.com (OpenAI-compatible). Only the current
frame's JPEG data URI plus the vision instruction is sent — never workspace
paths, credentials, memory, history, or other screen state.

``inspect`` keeps the structured ScreenObservation contract used by the
resilient two-stage pipeline. ``answer_direct`` uses the same client and
endpoint for FAST one-shot screenshot + question -> final answer turns.
"""

import base64
import json
from typing import Callable

import requests

from providers.base import resolve_setting

from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    EmptyProviderResponse,
    ProviderError,
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderSchemaError,
    classify_provider_exception,
)
from core.screen_vision.safety import sanitize_error_text
from core.screen_vision.vision.base import DEFAULT_VISION_INSTRUCTION
from core.screen_vision.vision.qwen_vision import extract_json, to_observation
from core.providers.catalog import CATALOG

# Catalog-provided defaults (Phase 2.1); values unchanged.
DEFAULT_DEEPSEEK_BASE_URL = CATALOG["deepseek-vision"].base_url
DEEPSEEK_VISION_MODEL = CATALOG["deepseek-vision"].model
VISION_TIMEOUT_SECONDS = 180

DEFAULT_DIRECT_STYLE_CONTEXT = """You are Firefly, the user's desktop companion.
Answer what you can actually see.
Be warm and concise, normally 2-5 sentences.
Do not pretend to see anything not visible.
Avoid long OCR transcripts, roleplay actions, and provider details."""


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


def build_direct_messages(
    frame: ScreenFrame,
    question: str,
    style_context: str | None = None,
) -> list:
    """Build the bounded FAST payload: style, current question, current frame.

    No history, memory, workspace data, or unrelated character state is
    accepted by this interface.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("direct screen-vision question must not be empty")
    style = (style_context or DEFAULT_DIRECT_STYLE_CONTEXT).strip()
    b64 = base64.b64encode(frame.image_bytes).decode("ascii")
    return [
        {"role": "system", "content": style},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{frame.mime_type};base64,{b64}"},
                },
            ],
        },
    ]


class DeepSeekVisionProvider:
    """DeepSeek official multimodal vision implementation of VisionProvider."""

    name = "deepseek-vision"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, transport: Callable | None = None):
        self._api_key = api_key or resolve_setting("DEEPSEEK_API_KEY")
        if not self._api_key:
            raise ProviderError("DEEPSEEK_API_KEY is not configured")
        self._base_url = (base_url or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        self._model = model or DEEPSEEK_VISION_MODEL
        self._transport = transport or requests.post

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
        raw_text = self._complete(payload)
        try:
            parsed = extract_json(raw_text)
            return to_observation(parsed, raw_model_text=raw_text)
        except (ValueError, ProviderSchemaError):
            # Prose instead of JSON: keep the schema contract by wrapping it
            # as the scene summary.
            return to_observation({"scene_summary": raw_text}, raw_model_text=raw_text)

    def answer_direct(
        self,
        frame: ScreenFrame,
        question: str,
        style_context: str | None = None,
    ) -> str:
        """Return the final companion answer in one multimodal remote call."""
        payload = {
            "model": self._model,
            "messages": build_direct_messages(frame, question, style_context),
            "stream": False,
            # DeepSeek Chat Completions enables thinking by default. FAST needs
            # the visible answer, not a long hidden reasoning prelude that can
            # consume the output budget before ``content`` begins.
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": 320,
        }
        return self._complete(payload)

    def _complete(self, payload: dict) -> str:
        """Send one request through the provider's existing HTTP client."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=VISION_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderNetworkError(exc) from None
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
            raise EmptyProviderResponse() from None
        return raw_text
