"""Deterministic guards for explicit long-term memory writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .records import MemoryCategory, WritePolicy


class MemoryWriteDeniedError(PermissionError):
    """Raised when an explicit memory request is blocked by policy."""


@dataclass(frozen=True, slots=True)
class ExplicitMemoryRequest:
    """Normalized content extracted from an explicit user request."""

    content: str


_NEGATIVE_PATTERNS = (
    re.compile(r"^\s*(?:请)?(?:不要|别|不用)记(?:住|录|下来)", re.IGNORECASE),
    re.compile(r"^\s*(?:do\s+not|don't)\s+remember\b", re.IGNORECASE),
)
_EXPLICIT_PATTERNS = (
    re.compile(
        r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?记(?:住|一下|下来)"
        r"(?:这件事|这一点|这条)?[\s:：,，]*(?P<content>.+?)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:please\s+)?remember(?:\s+that)?[\s:：,，]+"
        r"(?P<content>.+?)\s*$",
        re.IGNORECASE,
    ),
)


def extract_explicit_memory_request(value: Any) -> ExplicitMemoryRequest | None:
    """Return normalized content only when ``value`` is an explicit request."""
    if not isinstance(value, str) or not value.strip():
        return None
    for pattern in _NEGATIVE_PATTERNS:
        if pattern.search(value):
            return None
    for pattern in _EXPLICIT_PATTERNS:
        match = pattern.match(value)
        if match:
            content = match.group("content").strip()
            if content:
                return ExplicitMemoryRequest(content)
    return None


def authorize_explicit_write(
    value: Any,
    policy: WritePolicy | str,
    *,
    asserted_explicit: bool = False,
) -> str | None:
    """Authorize and normalize a write, returning None for ordinary chat."""
    normalized_policy = WritePolicy(policy)
    if asserted_explicit:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("memory content must be a non-empty string")
        content = value.strip()
    else:
        request = extract_explicit_memory_request(value)
        if request is None:
            return None
        content = request.content
    if normalized_policy is WritePolicy.OFF:
        raise MemoryWriteDeniedError("memory writes are disabled by policy")
    return content


def infer_category(content: str) -> MemoryCategory:
    """Apply a small deterministic category fallback for explicit writes."""
    lowered = content.casefold()
    rules = (
        (MemoryCategory.PREFERENCE, ("喜欢", "偏好", "prefer", "favorite")),
        (MemoryCategory.PROJECT, ("项目", "开发", "project", "repository")),
        (MemoryCategory.EMOTION, ("开心", "难过", "焦虑", "感到", "feel")),
        (
            MemoryCategory.SHARED_EXPERIENCE,
            ("我们一起", "共同", "一起完成", "together"),
        ),
        (MemoryCategory.RELATIONSHIP, ("信任", "关系", "约定", "relationship")),
    )
    for category, keywords in rules:
        if any(keyword in lowered for keyword in keywords):
            return category
    return MemoryCategory.USER_FACT


__all__ = [
    "ExplicitMemoryRequest",
    "MemoryWriteDeniedError",
    "authorize_explicit_write",
    "extract_explicit_memory_request",
    "infer_category",
]
