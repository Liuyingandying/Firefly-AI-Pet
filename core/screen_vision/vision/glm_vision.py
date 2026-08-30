"""GLM vision fallback provider (glm-4.6v-flash) over the Zhipu endpoint.

Capability status (verified live 2026-08-30):
- glm-4.6v-flash: VISION_CAPABLE - probe returned a runtime-generated random
  string verbatim from an in-memory JPEG (HTTP 200).
- glm-4.7-flash: TEXT_ONLY - image content is rejected (HTTP 400, error
  1210); it must never be configured here (guarded below).

The credential/endpoint/model resolution reuses providers/zhipu_glm.py and
providers.base (resolve_setting, default_transport, error family) - there is
no second Zhipu auth/transport implementation. The multimodal message uses
the same OpenAI-compatible text + image_url data-URI form as the TJU vision
provider; screenshot bytes stay in memory and are never logged or stored.

Output is converted into the same ScreenObservation schema as TJU Qwen, so
downstream reasoning/companion layers never know which vision model ran.
"""

import base64
import time

from providers.base import default_transport
from providers.zhipu_glm import ZhipuGLMProvider

from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderSchemaError,
    classify_provider_exception,
)
from core.screen_vision.vision.base import DEFAULT_VISION_INSTRUCTION
from core.screen_vision.vision.qwen_vision import extract_json, to_observation

DEFAULT_GLM_VISION_MODEL = "glm-4.6v-flash"
FORBIDDEN_VISION_MODELS = {"glm-4.7-flash"}  # verified TEXT_ONLY


def build_vision_messages(frame: ScreenFrame, instruction: str) -> list:
    """Same multimodal schema as the TJU vision provider (text + data URI)."""
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


class GlmVisionProvider:
    """Zhipu glm-4.6v-flash implementation of the VisionProvider interface."""

    def __init__(self, provider: ZhipuGLMProvider | None = None, model: str | None = None):
        self._zhipu = provider or ZhipuGLMProvider()
        self._model = (model or DEFAULT_GLM_VISION_MODEL).strip()
        if not self._model:
            raise ProviderError("GLM vision model is not configured")
        if self._model.lower() in FORBIDDEN_VISION_MODELS:
            raise ProviderError(
                f"{self._model} is TEXT_ONLY (verified) and cannot be a vision model"
            )
        if not self._zhipu.api_key:
            raise ProviderError("Zhipu vision provider has no ZHIPU_API_KEY")

    @property
    def name(self) -> str:
        return f"zhipu-{self._model}"

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
        # default_transport sends raw urllib requests and does not add
        # Content-Type itself; bigmodel rejects form-encoded bodies.
        headers = {
            "Authorization": f"Bearer {self._zhipu.api_key}",
            "Content-Type": "application/json",
        }
        # VLM inference takes longer than the 20s text default.
        vision_timeout = max(float(self._zhipu.timeout), 120.0)
        try:
            return self._send(payload, headers, vision_timeout)
        except ProviderHTTPError as exc:
            if exc.status_code != 429:
                raise
            # Free-tier rate limit: exactly ONE bounded retry after a short
            # wait. Never a loop — a second 429 propagates immediately.
            time.sleep(8.0)
            return self._send(payload, headers, vision_timeout)

    def _send(self, payload: dict, headers: dict, timeout: float) -> ScreenObservation:
        try:
            body = default_transport(self._zhipu.endpoint, payload, headers, timeout)
        except Exception as exc:  # noqa: BLE001 - classified at the boundary
            raise classify_provider_exception(exc) from None
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderSchemaError(f"unexpected GLM vision response shape: {exc}")
        raw_text = (message.get("content") or "").strip()
        if not raw_text:
            raise ProviderSchemaError("GLM vision returned empty content")
        try:
            parsed = extract_json(raw_text)
            return to_observation(parsed, raw_model_text=raw_text)
        except (ValueError, ProviderSchemaError):
            # The VLM answered in prose instead of JSON; keep the schema
            # contract by wrapping the prose as the scene summary.
            return to_observation({"scene_summary": raw_text}, raw_model_text=raw_text)
