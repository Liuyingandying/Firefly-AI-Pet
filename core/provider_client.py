"""Provider-neutral Anthropic-compatible Messages client helpers (9D.6-H4).

Qt-free and stdlib-only. This module owns the *shape* of a direct provider call
— configuration loading, endpoint normalization, request payload/headers,
response text extraction, and an HTTP error taxonomy — but performs no I/O and
imports no GUI. The actual HTTP transport and threading live in
:mod:`ui.workflow_provider_runner`.

Only the Anthropic-compatible Messages contract is modeled here. Production
code never names the upstream vendor (DeepSeek / cc-switch): it reads an
Anthropic-compatible base URL + auth token + request model from the active
provider configuration and talks to ``.../v1/messages``.

Secrets are handled strictly in-memory: the auth token is read into a config
object and used only to build request headers. It is never printed, logged,
written to an artifact, or placed in a process argv. ``redact_token`` exists
so diagnostics can emit a non-secret fingerprint.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# The Workflow Plan/Review steps use the strongest configured tier, matching the
# CLI's ``WORKFLOW_CLAUDE_MODEL = "opus"`` alias but resolved directly from the
# provider env instead of by the Claude Code compatibility layer.
WORKFLOW_MODEL_TIER = "opus"

# Anthropic Messages API path appended to the normalized base URL.
MESSAGES_PATH = "/v1/messages"

# Environment keys read from the active Claude-compatible provider config. These
# are the same keys the Claude Code CLI / proxy setup already uses; no vendor
# brand or cc-switch schema is hardcoded here.
ENV_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_DEFAULT_OPUS_MODEL = "ANTHROPIC_DEFAULT_OPUS_MODEL"
ENV_DEFAULT_OPUS_MODEL_NAME = "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"
ENV_DEFAULT_MODEL = "ANTHROPIC_MODEL"

# Generous output budget so a valid plan/review is not truncated before it can
# name the task's files; well under the proxy's non-streaming timeout budget.
DEFAULT_MAX_TOKENS = 4096

# Match the proxy's non-streaming timeout so a slow-but-valid model is not cut
# off by the client.
DEFAULT_TIMEOUT_SECONDS = 600


class ProviderConfigurationError(Exception):
    """A required provider setting is missing or unusable (never a crash)."""


class ProviderHttpError(Exception):
    """The provider returned a non-2xx HTTP status."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = int(status)
        super().__init__(message or f"provider HTTP {status}")


class ProviderTimeoutError(Exception):
    """The provider request timed out."""


class ProviderTransportError(Exception):
    """A network/connection failure (DNS, refused, reset, ...)."""


class ProviderProtocolError(Exception):
    """The provider response could not be parsed (malformed body)."""


@dataclass(frozen=True, slots=True, repr=False)
class WorkflowProviderConfig:
    """Resolved provider configuration for a Workflow Plan/Review call.

    ``model`` is the wire model id (the ``[1M]`` long-context beta suffix that
    Claude Code strips on the wire is already removed). ``auth_token`` is held
    in-memory only and must never be serialized.
    """

    base_url: str
    auth_token: str
    model: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def endpoint(self) -> str:
        return normalize_messages_endpoint(self.base_url)

    def __repr__(self) -> str:
        # Never leak the credential into repr/log output.
        return (
            f"WorkflowProviderConfig(base_url={self.base_url!r}, "
            f"auth_token={redact_token(self.auth_token)!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds}, max_tokens={self.max_tokens})"
        )


def normalize_messages_endpoint(base_url: str | None) -> str:
    """Normalize a provider base URL into the Messages API endpoint.

    Handles bases that already carry an ``/anthropic`` or ``/v1`` suffix so the
    final endpoint is ``.../v1/messages`` without ever producing a doubled
    ``/anthropic/v1/messages/v1/messages``.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + MESSAGES_PATH


def strip_context_suffix(model: str | None) -> str:
    """Remove the ``[1M]`` long-context beta suffix Claude Code strips on the wire."""
    return re.sub(r"\[\d+[mM]\]\s*$", "", (model or "")).strip()


def redact_token(token: str | None) -> str:
    """Return a non-secret fingerprint of a credential, for logging only."""
    if not token:
        return "<empty>"
    if len(token) <= 6:
        return "<redacted>"
    return f"{token[:2]}…{token[-2:]} (len={len(token)})"


def read_claude_settings_env(settings_path: Path | str | None = None) -> dict[str, str]:
    """Read the user's global ``~/.claude/settings.json`` ``env`` block, read-only.

    Mirrors the same source the isolated Claude CLI child uses. Returns {} if the
    file is missing or malformed. Values are config, not secrets (the proxy owns
    the real credential); the auth token is still treated as sensitive downstream.
    """
    if settings_path is None:
        settings_path = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))}


def _resolve_model(env: dict[str, str]) -> str:
    """Resolve the wire request model from the provider env, tier-first.

    The workflow steps use the ``opus`` tier. The ``[1M]`` suffix is stripped
    from the tier model (Claude Code does the same on the wire). Fallbacks are
    the upstream model name and the generic default, mirroring the H3 diagnostic
    resolution order without hardcoding any vendor brand.
    """
    for key in (ENV_DEFAULT_OPUS_MODEL, ENV_DEFAULT_OPUS_MODEL_NAME, ENV_DEFAULT_MODEL):
        value = strip_context_suffix(env.get(key))
        if value:
            return value
    return ""


def load_workflow_provider_config(
    env: dict[str, str] | None = None,
    *,
    settings_path: Path | str | None = None,
) -> WorkflowProviderConfig:
    """Resolve and validate Workflow provider configuration.

    ``env`` may be injected (tests / the diagnostic); otherwise the current
    ``~/.claude/settings.json`` ``env`` block is read. Raises
    :class:`ProviderConfigurationError` naming the missing keys when required
    config is absent — it never crashes and never falls back to a hardcoded
    vendor.
    """
    if env is None:
        env = read_claude_settings_env(settings_path)

    base_url = (env.get(ENV_BASE_URL) or "").strip().rstrip("/")
    auth_token = (env.get(ENV_AUTH_TOKEN) or "").strip()
    model = _resolve_model(env)

    missing = [
        name
        for name, value in (
            (ENV_BASE_URL, base_url),
            (ENV_AUTH_TOKEN, auth_token),
            (ENV_DEFAULT_OPUS_MODEL, model),
        )
        if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "missing provider config: " + ", ".join(missing)
        )

    return WorkflowProviderConfig(
        base_url=base_url,
        auth_token=auth_token,
        model=model,
    )


def build_messages_payload(prompt: str, config: WorkflowProviderConfig) -> dict:
    """Build the Anthropic-compatible Messages request body (non-streaming)."""
    return {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }


def build_messages_headers(config: WorkflowProviderConfig) -> dict:
    """Build request headers, carrying the auth token in-memory only."""
    return {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": config.auth_token,
        "authorization": f"Bearer {config.auth_token}",
    }


def extract_text(response_json) -> str:
    """Join the text blocks of an Anthropic Messages API response."""
    if not isinstance(response_json, dict):
        return ""
    content = response_json.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)
