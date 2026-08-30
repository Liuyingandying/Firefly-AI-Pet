"""Provider error types and the failover classification policy.

Fallback is allowed ONLY for transient upstream failures (429/5xx, network,
timeouts). Client mistakes (401/403/404, 400/422, malformed payloads) and
local programming/parse errors must propagate: a fallback provider must
never mask our own bugs or credential errors.

This taxonomy is shared by the Screen Vision providers. The legacy
``providers/base.py`` error family used by core.ai_router is bridged here
for classification so a mixed chain classifies consistently; unifying both
families into one module is a recorded TODO for the provider layer.
"""

import requests

from core.screen_vision.safety import sanitize_error_text

try:  # bridge the legacy provider error family (no hard dependency)
    from providers.base import ProviderError as LegacyProviderError
    from providers.base import (
        ProviderHTTPError as LegacyProviderHTTPError,
    )
    from providers.base import (
        ProviderProtocolError as LegacyProviderProtocolError,
    )
    from providers.base import (
        ProviderTimeoutError as LegacyProviderTimeoutError,
    )
    from providers.base import (
        ProviderUnavailableError as LegacyProviderUnavailableError,
    )
except ImportError:  # pragma: no cover - providers always present in Firefly
    LegacyProviderError = None

TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}


class ProviderError(Exception):
    """Base class for provider failures that carry a classification."""

    transient = False
    failure_type = "provider_error"


class ProviderHTTPError(ProviderError):
    """An HTTP-level failure from a vision/reasoning endpoint."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = sanitize_error_text(detail)
        self.transient = status_code in TRANSIENT_HTTP_STATUS
        self.failure_type = f"HTTP_{status_code}"
        super().__init__(f"provider returned HTTP {status_code}: {self.detail}")


class ProviderNetworkError(ProviderError):
    """Connection timeout, read timeout, refused connection, DNS failure."""

    transient = True

    def __init__(self, exc: Exception):
        self.failure_type = type(exc).__name__
        # Never retain the original request exception text/repr: requests may
        # attach a PreparedRequest containing authentication headers.
        super().__init__(f"provider network failure ({self.failure_type})")


class ProviderSchemaError(ProviderError):
    """The provider answered but with unusable content (bad JSON etc.)."""

    def __init__(self, detail: str):
        self.failure_type = "schema_error"
        super().__init__(f"provider response unusable: {detail}")


class EmptyProviderResponse(ProviderSchemaError):
    """The upstream response had no final assistant text."""

    def __init__(self):
        self.failure_type = "empty_response"
        ProviderError.__init__(self, "provider returned no final answer")


class VisionTemporarilyUnavailable(ProviderError):
    """Vision cannot be served right now (primary down/transient, and no
    verified fallback is configured). Companion turns this into a friendly
    user-facing message; internals are never shown."""

    def __init__(self, failure_type: str = "unavailable"):
        self.failure_type = failure_type
        self.transient = True
        super().__init__(f"vision temporarily unavailable ({failure_type})")


class ReasoningTemporarilyUnavailable(ProviderError):
    """Reasoning cannot be served right now: the primary failed transiently
    and every configured fallback failed too. Companion turns this into a
    friendly user-facing message; internals are never shown."""

    def __init__(self, failure_type: str = "unavailable"):
        self.failure_type = failure_type
        self.transient = True
        super().__init__(f"reasoning temporarily unavailable ({failure_type})")


def classify_provider_exception(exc: Exception) -> ProviderError:
    """Normalize any provider exception into a classified ProviderError."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, requests.RequestException):
        return ProviderNetworkError(exc)
    if LegacyProviderError is not None and isinstance(exc, LegacyProviderError):
        if isinstance(exc, LegacyProviderHTTPError):
            return ProviderHTTPError(exc.status_code, sanitize_error_text(str(exc)))
        if isinstance(exc, LegacyProviderTimeoutError):
            bridge = ProviderNetworkError.__new__(ProviderNetworkError)
            bridge.failure_type = "timeout"
            return bridge
        if isinstance(exc, LegacyProviderUnavailableError):
            # Local config/transport unavailable: falling back to another
            # provider with its own credentials is legitimate.
            bridge = ProviderNetworkError.__new__(ProviderNetworkError)
            bridge.failure_type = "unavailable"
            return bridge
        if isinstance(exc, LegacyProviderProtocolError):
            return ProviderSchemaError("legacy provider protocol failure")
        bridge = ProviderError(f"legacy provider failure ({type(exc).__name__})")
        return bridge
    return ProviderError(f"unexpected provider failure ({type(exc).__name__})")


def is_transient(exc: Exception) -> bool:
    """True when automatic fallback to another provider is allowed."""
    return classify_provider_exception(exc).transient
