"""Qwen vision provider over the TJU v3 OpenAI-compatible multimodal endpoint.

This is the only module that handles screenshot bytes. They are sent as a
base64 data URI inside the multimodal message and never logged or stored.
"""

import base64
import json
import re
from time import perf_counter

import requests

from core.screen_vision.config import VISION_TIMEOUT_SECONDS, VisionConfig, load_vision_config
from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderSchemaError,
)
from core.screen_vision.safety import sanitize_error_text
from core.screen_vision.vision.base import DEFAULT_VISION_INSTRUCTION, VisionProvider


def build_vision_messages(frame: ScreenFrame, instruction: str = DEFAULT_VISION_INSTRUCTION) -> list:
    """Build the multimodal message payload (text + base64 image data URI)."""
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


def extract_json(text: str) -> dict:
    """Extract a JSON object from model output, tolerating markdown fences
    and surrounding prose."""
    if not text:
        raise ValueError("Vision model returned empty text.")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in vision output: {text[:120]!r}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("Vision output JSON is not an object.")
    return obj


def to_observation(payload: dict, raw_model_text: str = "") -> ScreenObservation:
    """Coerce a (possibly partial) JSON payload into a ScreenObservation."""
    lists = ("visible_text", "ui_elements", "warnings", "errors", "uncertain")
    strings = ("scene_summary", "active_application", "window_title")
    data = {}
    for name in strings:
        value = payload.get(name, "")
        data[name] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for name in lists:
        value = payload.get(name, [])
        if isinstance(value, list):
            data[name] = [str(item) for item in value]
        elif value:
            data[name] = [str(value)]
        else:
            data[name] = []
    data["raw_model_text"] = raw_model_text
    return ScreenObservation(**data)


class QwenVisionProvider(VisionProvider):
    """TJU tju-llm vision implementation of VisionProvider."""

    name = "tju-qwen"

    def __init__(self, config: VisionConfig | None = None, extra_body: dict | None = None):
        self._config = config or load_vision_config()
        self._extra_body = extra_body or {}

    @property
    def model(self) -> str:
        return self._config.model

    def inspect(
        self,
        frame: ScreenFrame,
        instruction: str = DEFAULT_VISION_INSTRUCTION,
    ) -> ScreenObservation:
        cfg = self._config
        payload = {
            "model": cfg.model,
            "messages": build_vision_messages(frame, instruction),
            "stream": False,
            "max_tokens": 1500,
        }
        extra = dict(cfg.extra_body)
        extra.update(self._extra_body)
        if extra:
            payload.update(extra)

        started = perf_counter()
        try:
            response = requests.post(
                f"{cfg.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=VISION_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ProviderNetworkError(exc) from None
        _ = (perf_counter() - started) * 1000

        if not response.ok:
            # Never include auth headers or image bytes in error output.
            raise ProviderHTTPError(
                response.status_code,
                sanitize_error_text(response.text[:300], cfg.api_key),
            )
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderSchemaError(f"unexpected vision response shape: {exc}")
        raw_text = message.get("content") or ""
        try:
            parsed = extract_json(raw_text)
        except ValueError as exc:
            raise ProviderSchemaError(str(exc))
        return to_observation(parsed, raw_model_text=raw_text)
