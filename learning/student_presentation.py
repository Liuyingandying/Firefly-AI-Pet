"""Fail-closed conversion of teach-mcp TutorTurn payloads to student text.

Only the returned strings may be handed to a student-facing renderer. No
question truth, session identifiers, tool traces, or model output is displayed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class PresentationError(ValueError):
    """A payload is incomplete or contains non-student-facing material."""


_QUESTION_FIELDS = frozenset({
    "status", "question_id", "mode", "stem", "options", "difficulty",
    "topic", "session_id",
})
_FEEDBACK_FIELDS = frozenset({
    "status", "correct", "stage", "feedback", "misconception_id",
    "mastery_before", "mastery_after", "next_step", "result_id", "question_id",
})
_RAW_TOOL_TEXT = re.compile(
    r"<\s*/?\s*(?:function|tool_call|parameter)\b"
    r"|\b(?:tool|function|tool_calls|function_call|arguments|answer|"
    r"explanation|session|session_id)\s*[:=]"
    r"|[\"'](?:tool|function|tool_calls|function_call|arguments|answer|"
    r"explanation|session|session_id|content|status|structuredContent)[\"']\s*:",
    re.IGNORECASE,
)


def _safe_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PresentationError(f"{field} is missing")
    text = value.strip()
    if _RAW_TOOL_TEXT.search(text):
        raise PresentationError(f"{field} contains internal tool output")
    return text


def _fields(payload: Any, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise PresentationError("expected a structured teach-mcp result")
    if payload.get("status") != "success":
        raise PresentationError("teach-mcp result is not successful")
    unexpected = set(payload) - allowed
    if unexpected:
        raise PresentationError("result contains non-student-facing fields")
    return payload


def question_to_display(question: Mapping[str, Any]) -> str:
    """Render one official ``pending_question`` without metadata or truth."""
    data = _fields(question, _QUESTION_FIELDS)
    if data.get("mode") != "choice":
        raise PresentationError("unsupported question mode")
    stem = _safe_text(data.get("stem"), "stem")
    options = data.get("options")
    if not isinstance(options, Mapping) or not 2 <= len(options) <= 6:
        raise PresentationError("complete choice options are required")
    labels = [chr(ord("A") + index) for index in range(len(options))]
    if list(options) != labels:
        raise PresentationError("choice labels must be ordered A onward")
    lines = [stem]
    for label in labels:
        lines.append(f"{label}. {_safe_text(options[label], f'option {label}')}")
    lines.append(f"请回答 {'/'.join(labels)}。")
    return "\n".join(lines)


def feedback_to_display(result: Mapping[str, Any]) -> str:
    """Show only the authoritative feedback from ``tutor_submit_answer``."""
    data = _fields(result, _FEEDBACK_FIELDS)
    if not isinstance(data.get("correct"), bool):
        raise PresentationError("correct verdict is missing")
    return _safe_text(data.get("feedback"), "feedback")
