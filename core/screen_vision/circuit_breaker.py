"""Lightweight circuit breaker for the primary provider.

Policy: after N consecutive transient failures the primary is marked
temporarily unavailable; for the cooldown window every request goes straight
to the fallback. After the cooldown a single on-demand probe is allowed (only
when a real user request arrives — there is no background polling): success
restores the primary, failure re-opens the breaker for another window.
"""

import threading
import time


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 2,
        cooldown_seconds: float = 60.0,
        clock=time.monotonic,
    ):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self.last_failure_type: str | None = None

    def allow_primary(self) -> bool:
        """True when the primary may be attempted (closed, or cooldown elapsed
        for a single half-open probe)."""
        with self._lock:
            if self._opened_at is None:
                return True
            return self._clock() - self._opened_at >= self._cooldown_seconds

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self.last_failure_type = None

    def record_transient_failure(self, failure_type: str | None = None) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if failure_type:
                self.last_failure_type = failure_type
            if self._consecutive_failures >= self._failure_threshold:
                self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None and (
                self._clock() - self._opened_at < self._cooldown_seconds
            )
