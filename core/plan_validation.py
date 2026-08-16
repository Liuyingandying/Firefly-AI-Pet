"""Deterministic Plan validity gate for workflow Plan steps (Phase 9D.6-H2).

Qt-free, stdlib-only, provider-free, zero LLM, zero network. The gate exists to
reject *obvious garbage* — output that clearly did not understand the task — so
an invalid plan never reaches the Codex Implement step. It never judges plan
quality: a valid plan may be suboptimal and still passes.

The vocabulary is deliberately minimal (first version only blocks obvious junk):

- ``EMPTY`` — stripped text is empty.
- ``TOO_SHORT`` — below ``MIN_PLAN_LEN`` characters.
- ``GENERIC_NO_TASK_RESPONSE`` — a known clarification stub ("I don't see a
  specific task..."). Only a small, audited list of near-equivalent phrases;
  not a refusal classifier.
- ``NO_TASK_REFERENCE`` — the request contained file-like tokens (e.g.
  ``greeting.py``, ``src/foo.py``) but the plan matched none of them. This rule
  is evaluated only when the request actually has such tokens; a request without
  them never triggers it.

``matched_task_terms`` records which request-derived file-like tokens appeared
in the plan. ``length`` is the stripped plan length. ``validate_plan_text`` is a
pure function: identical inputs always produce identical results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# A real plan with a heading is comfortably longer than this. 9D.2/9D.5 test
# plans are ~110+ chars; the real 9D.6 bad plan (191 bytes) fails on the
# generic no-task rule regardless of length.
MIN_PLAN_LEN = 80

# Audited surface phrases from the real 9D.6 bad plan and its close
# equivalents. Case-insensitive substring match. Deliberately small.
_NO_TASK_PATTERNS = (
    "i don't see a specific task",
    "i don't see a task",
    "no task or request",
    "don't see any task",
    "what would you like me to",
    "please provide a task",
    "please provide the task",
    "please provide more details",
    "i'll need more information",
    "could you provide",
    "can you provide a task",
    "no request yet",
    "specific request yet",
    "i'm not sure what you'd like",
    "i am not sure what you'd like",
    "as an ai language model",
    "i can't help",
)

# File-like token derived from the request: a filename with a dotted extension,
# optionally with path segments (greeting.py, src/foo.py, README.md). The
# lookbehind keeps the match from starting mid-identifier / mid-path; the
# lookahead rejects a following identifier char but tolerates trailing sentence
# punctuation ("test_greeting.py." still yields "test_greeting.py").
_FILE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.\\/-])([A-Za-z0-9_][A-Za-z0-9_.\-\\/]*\.[A-Za-z0-9]{1,10})(?![A-Za-z0-9_\\/-])"
)


class PlanValidationReason(str, Enum):
    """Stable machine-readable reason tokens; the UI may translate these."""

    EMPTY = "EMPTY"
    TOO_SHORT = "TOO_SHORT"
    NO_TASK_REFERENCE = "NO_TASK_REFERENCE"
    GENERIC_NO_TASK_RESPONSE = "GENERIC_NO_TASK_RESPONSE"


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    """Result of one deterministic Plan validity check.

    ``reason_codes`` is empty exactly when ``valid`` is True. ``valid`` is the
    single gate used by the executor; everything else is diagnostic.
    """

    valid: bool
    reason_codes: tuple[PlanValidationReason, ...] = field(default_factory=tuple)
    matched_task_terms: tuple[str, ...] = field(default_factory=tuple)
    length: int = 0


def file_terms(task_text: str | None) -> list[str]:
    """Extract distinct file-like tokens from a task request, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _FILE_TOKEN_RE.finditer(task_text or ""):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def validate_plan_text(plan_text: str | None, task_text: str | None) -> PlanValidationResult:
    """Return a PlanValidationResult for ``plan_text`` given ``task_text``.

    Pure and deterministic: identical inputs produce identical outputs. No LLM,
    no network, no pseudo-NLP — only the four structural rules above.
    """
    text = (plan_text or "").strip()
    if not text:
        return PlanValidationResult(valid=False, reason_codes=(PlanValidationReason.EMPTY,), length=0)

    codes: list[PlanValidationReason] = []
    if len(text) < MIN_PLAN_LEN:
        codes.append(PlanValidationReason.TOO_SHORT)

    lowered = text.lower()
    if any(pattern in lowered for pattern in _NO_TASK_PATTERNS):
        codes.append(PlanValidationReason.GENERIC_NO_TASK_RESPONSE)

    terms = file_terms(task_text)
    matched = [t for t in terms if t.lower() in lowered]
    if terms and not matched:
        codes.append(PlanValidationReason.NO_TASK_REFERENCE)

    return PlanValidationResult(
        valid=not codes,
        reason_codes=tuple(codes),
        matched_task_terms=tuple(matched),
        length=len(text),
    )
