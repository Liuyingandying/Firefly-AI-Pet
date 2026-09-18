"""Shared OpenAI-compatible chat provider primitives.

This module is intentionally stdlib-only. Provider credentials are resolved
from the process environment first and from the project's ``.env`` file
second; credentials are never persisted in router state or included in error
messages.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"
DEFAULT_TIMEOUT_SECONDS = 20.0

ChatCompletion = dict[str, Any]
Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]

# Provider Manager Phase 1: an optional per-machine credential source,
# consulted between the process environment and the .env file.  The source is
# a callable mapping a canonical setting name to its stored value (or None).
# Unregistered (the default) keeps resolve_setting behavior byte-identical to
# the previous implementation; the concrete store lives in
# core.credential_store and is registered by the composition root, keeping
# this module stdlib-only.
CredentialSource = Callable[[str], "str | None"]
_credential_source: CredentialSource | None = None


def register_credential_source(source: CredentialSource | None) -> None:
    """Register (or clear, with ``None``) the credential store used by
    :func:`resolve_setting`.  Thread-safe by GIL-atomic reference swap."""
    global _credential_source
    _credential_source = source


class ProviderError(Exception):
    """Base class for failures that may trigger provider fallback."""


class ProviderHTTPError(ProviderError):
    """The provider returned a non-success HTTP status."""

    def __init__(self, status_code: int, message: str = "") -> None:
        self.status_code = int(status_code)
        super().__init__(message or f"provider returned HTTP {self.status_code}")


class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its timeout."""


class ProviderUnavailableError(ProviderError):
    """Provider configuration or transport is currently unavailable."""


class ProviderProtocolError(ProviderError):
    """The provider returned a malformed OpenAI-compatible response."""


def read_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Read a small dotenv-compatible ``KEY=VALUE`` file without dependencies.

    Blank lines and comments are ignored. Single- and double-quoted values are
    accepted. Malformed or unreadable files safely behave like an empty file.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_FILE
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_setting(
    name: str,
    default: str = "",
    *,
    env_file: Path | str | None = None,
) -> str:
    """Resolve one setting: process env > credential store > .env > default.

    The credential store (Provider Manager Phase 1) is consulted only when a
    source has been registered; store lookup failures degrade silently to the
    .env layer so a damaged store can never break provider resolution.
    """
    process_value = os.environ.get(name)
    if process_value is not None and process_value.strip():
        return process_value.strip()
    if _credential_source is not None:
        try:
            stored_value = _credential_source(name)
        except Exception:
            stored_value = None
        if stored_value is not None and stored_value.strip():
            return stored_value.strip()
    return read_env_file(env_file).get(name, default).strip()


def normalize_chat_endpoint(base_url: str) -> str:
    """Return a URL ending in exactly ``/chat/completions``."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def validate_messages(messages: Any) -> list[dict[str, Any]]:
    """Validate the text-only subset of OpenAI chat messages used by Firefly."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"messages[{index}] has an unsupported role")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"messages[{index}].content must be non-empty text")
        normalized.append(dict(message))
    return normalized


def default_transport(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """POST one non-streaming OpenAI-compatible request using stdlib urllib."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(1024).decode("utf-8", "replace").strip()
        except OSError:
            detail = ""
        message = f"provider returned HTTP {exc.code}"
        if detail:
            message += f": {detail}"
        raise ProviderHTTPError(exc.code, message) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderTimeoutError("provider request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ProviderTimeoutError("provider request timed out") from exc
        raise ProviderUnavailableError("provider transport is unavailable") from exc
    except OSError as exc:
        raise ProviderUnavailableError("provider transport is unavailable") from exc

    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderProtocolError("provider returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderProtocolError("provider response must be a JSON object")
    return parsed


def normalize_chat_completion(
    response: dict[str, Any],
    *,
    provider: str,
    requested_model: str,
) -> ChatCompletion:
    """Validate and normalize an OpenAI chat completion response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderProtocolError("provider response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderProtocolError("provider response choice is malformed")
    message = first.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ProviderProtocolError("provider response has no assistant content")

    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    return {
        "id": str(response.get("id") or ""),
        "object": str(response.get("object") or "chat.completion"),
        "created": int(response.get("created") or time.time()),
        "provider": provider,
        "model": str(response.get("model") or requested_model),
        "choices": choices,
        "usage": usage,
    }


class BaseProvider(ABC):
    """Unified provider interface consumed by :class:`ProviderRouter`."""

    name: str

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
        *,
        timeout: float | None = None,
    ) -> ChatCompletion:
        """Return one normalized, non-streaming chat completion."""


class OpenAICompatibleProvider(BaseProvider):
    """Reusable HTTP implementation for OpenAI-compatible providers."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        self.name = name
        self.api_key = (api_key or "").strip()
        self.endpoint = normalize_chat_endpoint(base_url)
        self.default_model = (default_model or "").strip()
        self.timeout = float(timeout)
        self._transport = transport or default_transport

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
        *,
        timeout: float | None = None,
    ) -> ChatCompletion:
        normalized_messages = validate_messages(messages)
        if not self.api_key:
            raise ProviderUnavailableError(f"{self.name} API key is not configured")
        if not self.endpoint:
            raise ProviderUnavailableError(f"{self.name} endpoint is not configured")
        request_model = (model or self.default_model).strip()
        if not request_model:
            raise ProviderUnavailableError(f"{self.name} model is not configured")

        payload = {
            "model": request_model,
            "messages": normalized_messages,
            "temperature": float(temperature),
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        request_timeout = self.timeout
        if timeout is not None:
            request_timeout = min(request_timeout, float(timeout))
        if request_timeout <= 0:
            raise ProviderTimeoutError("provider request timeout budget is exhausted")
        response = self._transport(
            self.endpoint,
            payload,
            headers,
            request_timeout,
        )
        return normalize_chat_completion(
            response,
            provider=self.name,
            requested_model=request_model,
        )
