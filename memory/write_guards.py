"""Deterministic guards for explicit long-term memory writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
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
    check_red_line(content)
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


# ---------------------------------------------------------------------------
# Red-line filter: hard-blocked credential / identity / payment content.
#
# The filter only fires on a *plausible concrete secret value*, never on a
# bare mention of a concept ("my API key") or a placeholder ("YOUR_API_KEY",
# "<token>"). This keeps documentation and examples writable while still
# blocking real credentials before anything is persisted.
# ---------------------------------------------------------------------------


class RedLineCategory(str, Enum):
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"
    PASSWORD_SECRET = "password_secret"
    PRIVATE_KEY = "private_key"
    IDENTITY = "identity"
    PAYMENT = "payment"
    CREDENTIAL = "credential"


@dataclass(frozen=True, slots=True)
class RedLineViolation:
    """A concrete red-line hit with enough context to explain the block."""

    category: RedLineCategory
    reason: str


class RedLineViolationError(PermissionError):
    """Raised when a memory write is blocked by the red-line filter."""

    def __init__(self, violation: RedLineViolation) -> None:
        self.violation = violation
        super().__init__(
            f"red-line blocked: {violation.category.value} — {violation.reason}"
        )


_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"
)

_API_KEY_PREFIX_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI/Anthropic/DeepSeek
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[opu]_[A-Za-z0-9]{36,}\b"),  # GitHub classic token
    re.compile(r"\bghs_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),  # GitHub fine-grained
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google
)

_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/]{20,}")

_PASSWORD_SECRET_RE = re.compile(
    r"(?:password|passwd|pwd|passcode|secret|密码|口令|暗号"
    r"|client[ _-]?secret|api[ _-]?secret|access[ _-]?secret)"
    r"\s*[=:：]\s*"
    r"(?:[\"'](?P<q>[^\"']+)[\"']|(?P<w>[^\s,;，。\"']+))",
    re.IGNORECASE,
)

_CREDENTIAL_RE = re.compile(
    r"(?:token|access[ _-]?token|auth[ _-]?token|refresh[ _-]?token"
    r"|api[ _-]?token|bearer[ _-]?token|api[ _-]?key|apikey"
    r"|secret[ _-]?key|private[ _-]?key|access[ _-]?key"
    r"|client[ _-]?id|credential)"
    r"\s*[=:：]\s*"
    r"(?:[\"'](?P<q>[^\"']+)[\"']|(?P<w>[^\s,;，。\"']+))",
    re.IGNORECASE,
)

_ID_CARD_RE = re.compile(r"(?<!\d)(\d{17})([0-9Xx])(?!\d)")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"

_CARD_DIGIT_RE = re.compile(r"(?<!\d)(\d[\d -]{12,22}\d)(?!\d)")


def _is_placeholder(value: str) -> bool:
    """Return True when a captured value is an obvious placeholder, not a secret."""
    if not value:
        return True
    if re.search(r"[<>{}()\[\]]", value):
        return True
    lowered = value.casefold()
    if any(
        token in lowered
        for token in (
            "your", "example", "sample", "placeholder", "dummy", "changeme",
            "redacted", "replace", "todo", "insert", "your_", "xxxx", "xxx",
        )
    ):
        return True
    if set(value) <= set("*xX.＿_"):
        return True
    return False


def _detect_private_key(text: str) -> RedLineViolation | None:
    if _PRIVATE_KEY_RE.search(text):
        return RedLineViolation(
            RedLineCategory.PRIVATE_KEY, "PEM private key block detected"
        )
    return None


def _detect_jwt(text: str) -> RedLineViolation | None:
    if _JWT_RE.search(text):
        return RedLineViolation(
            RedLineCategory.ACCESS_TOKEN, "JWT access token detected"
        )
    return None


def _detect_api_key(text: str) -> RedLineViolation | None:
    for pattern in _API_KEY_PREFIX_RES:
        if pattern.search(text):
            return RedLineViolation(
                RedLineCategory.API_KEY, "recognized API key prefix detected"
            )
    return None


def _detect_bearer(text: str) -> RedLineViolation | None:
    if _BEARER_RE.search(text):
        return RedLineViolation(
            RedLineCategory.ACCESS_TOKEN, "Bearer credential detected"
        )
    return None


def _detect_identity(text: str) -> RedLineViolation | None:
    for match in _ID_CARD_RE.finditer(text):
        digits, check = match.group(1), match.group(2)
        total = sum(int(d) * w for d, w in zip(digits, _ID_WEIGHTS))
        if _ID_CHECK[total % 11] == check.upper():
            return RedLineViolation(
                RedLineCategory.IDENTITY, "national ID number detected"
            )
    return None


def _detect_payment(text: str) -> RedLineViolation | None:
    for match in _CARD_DIGIT_RE.finditer(text):
        digits = "".join(ch for ch in match.group(0) if ch.isdigit())
        if not 13 <= len(digits) <= 19:
            continue
        if _luhn_valid(digits):
            return RedLineViolation(
                RedLineCategory.PAYMENT, "bank card / payment number detected"
            )
    return None


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        n = int(ch)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _detect_password_secret(text: str) -> RedLineViolation | None:
    for match in _PASSWORD_SECRET_RE.finditer(text):
        value = (match.group("q") or match.group("w") or "").strip()
        if _is_placeholder(value) or len(value) < 6:
            continue
        return RedLineViolation(
            RedLineCategory.PASSWORD_SECRET, "password / secret value detected"
        )
    return None


def _detect_credential(text: str) -> RedLineViolation | None:
    for match in _CREDENTIAL_RE.finditer(text):
        value = (match.group("q") or match.group("w") or "").strip()
        if _is_placeholder(value) or len(value) < 20:
            continue
        return RedLineViolation(
            RedLineCategory.CREDENTIAL, "long credential value detected"
        )
    return None


def detect_red_line(content: str) -> RedLineViolation | None:
    """Return the first red-line violation in ``content``, or None when clean."""
    if not isinstance(content, str) or not content.strip():
        return None
    text = content.strip()
    for detector in (
        _detect_private_key,
        _detect_jwt,
        _detect_api_key,
        _detect_bearer,
        _detect_identity,
        _detect_payment,
        _detect_password_secret,
        _detect_credential,
    ):
        violation = detector(text)
        if violation is not None:
            return violation
    return None


def check_red_line(content: str) -> str:
    """Raise :class:`RedLineViolationError` if content trips the red line, else return it."""
    violation = detect_red_line(content)
    if violation is not None:
        raise RedLineViolationError(violation)
    return content


__all__ = [
    "ExplicitMemoryRequest",
    "MemoryWriteDeniedError",
    "RedLineCategory",
    "RedLineViolation",
    "RedLineViolationError",
    "authorize_explicit_write",
    "check_red_line",
    "detect_red_line",
    "extract_explicit_memory_request",
    "infer_category",
]
