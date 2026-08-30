"""Provider failover for Screen Vision (vision + reasoning chains).

Policy (see provider_errors.py):
- Automatic fallback ONLY on transient failures: HTTP 429/500/502/503/504,
  connection/read timeout, refused connection, DNS/network errors.
- 401/403/404/400/422 and schema/programming errors propagate unchanged so
  a fallback can never mask credential bugs or our own mistakes.
- The primary gets at most ONE attempt per user request (fail fast).
- A circuit breaker skips the primary entirely while it is marked
  temporarily unavailable (2 consecutive transient failures -> 60s), with a
  single on-demand probe after the cooldown. No background polling.
- With no fallback configured, a skipped or transient-failing primary
  raises VisionTemporarilyUnavailable immediately: the user never waits on
  a provider already known to be down, and no unverified model is quietly
  substituted.

ScreenVisionService stays unaware of any of this: the failover providers
implement the same VisionProvider / ReasoningProvider interfaces and expose
per-call metadata via ``last_meta``.
"""

import threading

from core.screen_vision.circuit_breaker import CircuitBreaker
from core.screen_vision.provider_errors import (
    ReasoningTemporarilyUnavailable,
    VisionTemporarilyUnavailable,
    classify_provider_exception,
    is_transient,
)


class _FailoverBase:
    """Shared primary -> [fallback...] attempt logic."""

    name = "failover"
    _meta_key = "provider"
    _unavailable_error = VisionTemporarilyUnavailable

    def __init__(
        self,
        primary,
        fallback=None,
        fallbacks=(),
        breaker: CircuitBreaker | None = None,
        breaker_factory=CircuitBreaker,
    ):
        self._primary = primary
        self._fallbacks = tuple(fallbacks) if fallbacks else ((fallback,) if fallback else ())
        self._breaker = breaker or breaker_factory()
        self._meta_lock = threading.Lock()
        self.last_meta: dict = {}

    def _meta(self, used: str, fallback_used: bool, failure_type: str | None,
              secondary_failure_type: str | None = None) -> dict:
        meta = {
            f"{self._meta_key}_fallback_used": fallback_used,
            f"{self._meta_key}_primary_failure_type": failure_type,
            f"{self._meta_key}_provider": used,
        }
        if secondary_failure_type:
            meta[f"{self._meta_key}_secondary_failure_type"] = secondary_failure_type
        return meta

    def _set_meta(self, meta: dict) -> None:
        with self._meta_lock:
            self.last_meta = meta

    def _run_with_failover(self, call_primary, call_fallbacks):
        """Primary once (breaker permitting), then each fallback once, in order.

        Non-transient primary errors propagate immediately. When no fallback
        exists, a transient primary failure (or an open breaker) surfaces as
        VisionTemporarilyUnavailable without further waiting.
        """
        has_fallback = bool(self._fallbacks)
        skip_primary = has_fallback and not self._breaker.allow_primary()
        failure_type = None

        if not skip_primary:
            try:
                result = call_primary()
                self._breaker.record_success()
                self._set_meta(
                    self._meta(getattr(self._primary, "name", "primary"), False, None)
                )
                return result
            except Exception as exc:  # noqa: BLE001 - classified below
                if not is_transient(exc):
                    raise
                failure_type = classify_provider_exception(exc).failure_type
                self._breaker.record_transient_failure(failure_type)
                if not has_fallback:
                    raise self._unavailable_error(
                        failure_type=failure_type
                    ) from exc

        if not has_fallback:
            raise self._unavailable_error(
                failure_type=self._breaker.last_failure_type or "circuit_open"
            )

        last_exc: Exception | None = None
        failure_chain = [failure_type] if failure_type else []
        for index, fallback in enumerate(self._fallbacks):
            try:
                result = call_fallbacks(index, fallback)
                used = getattr(fallback, "name", f"fallback_{index}")
                secondary = failure_chain[1] if len(failure_chain) > 1 else None
                self._set_meta(self._meta(used, True, failure_type, secondary))
                return result
            except Exception as exc:  # noqa: BLE001 - classified below
                if not is_transient(exc):
                    raise
                failure_chain.append(classify_provider_exception(exc).failure_type)
                last_exc = exc
        raise self._unavailable_error(
            failure_type=classify_provider_exception(last_exc).failure_type
            if last_exc
            else "all_fallbacks_failed"
        ) from last_exc


class FailoverVisionProvider(_FailoverBase):
    """VisionProvider chain: primary (TJU Qwen) -> optional fallbacks."""

    _meta_key = "vision"
    _unavailable_error = VisionTemporarilyUnavailable

    def inspect(self, frame, instruction=None):
        from core.screen_vision.vision.base import DEFAULT_VISION_INSTRUCTION

        instruction = instruction or DEFAULT_VISION_INSTRUCTION

        def call_primary():
            return self._primary.inspect(frame, instruction)

        def call_fallbacks(index, fallback):
            return fallback.inspect(frame, instruction)

        return self._run_with_failover(call_primary, call_fallbacks)


class FailoverReasoningProvider(_FailoverBase):
    """ReasoningProvider chain: primary (TJU DeepSeek) -> optional fallbacks."""

    _meta_key = "reasoning"
    _unavailable_error = ReasoningTemporarilyUnavailable

    def answer(self, question: str, observation: dict) -> str:
        def call_primary():
            return self._primary.answer(question, observation)

        def call_fallbacks(index, fallback):
            return fallback.answer(question, observation)

        return self._run_with_failover(call_primary, call_fallbacks)


# Backwards-compatible alias for earlier single-fallback construction sites.
ChainedReasoningProvider = FailoverReasoningProvider
