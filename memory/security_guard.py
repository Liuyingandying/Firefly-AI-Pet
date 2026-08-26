"""Optional second-layer security guard seam for memory writes.

Firefly's deterministic write guards (red-line, negation, policy) always run
first inside :mod:`memory.write_guards`. This seam lets an optional external
guard (e.g. OWASP Agent Memory Guard) add defense-in-depth before a record
reaches the repository, without becoming a required dependency.

The default is a pass-through guard, so behavior is unchanged unless a guard is
explicitly configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """Outcome of a security guard check on proposed memory content."""

    action: Literal["allow", "block", "redact"]
    reason: str | None = None
    content: str | None = None


class MemorySecurityViolation(PermissionError):
    """Raised when the optional security guard blocks a write."""


class MemorySecurityGuard(Protocol):
    """Contract for an optional write-time security middleware."""

    def guard(self, content: str) -> GuardDecision: ...


class NoopMemorySecurityGuard:
    """Pass-through guard used when no external guard is configured."""

    def guard(self, content: str) -> GuardDecision:
        return GuardDecision(action="allow")


__all__ = [
    "GuardDecision",
    "MemorySecurityGuard",
    "MemorySecurityViolation",
    "NoopMemorySecurityGuard",
]
