"""Thin, transport-independent client for teach-mcp TutorTurn tools.

The caller supplies an official MCP ``call_tool`` transport. This module does
not read the teach-mcp database, infer learning state, or implement TutorLoop.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any


class TeachMcpClientError(ValueError):
    """The tool transport or its response cannot be used safely."""


ToolCaller = Callable[[str, dict[str, str]], Any]


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeachMcpClientError(f"{name} must be a non-empty string")
    return value


def _field(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _tool_payload(result: Any) -> dict[str, Any]:
    """Accept a normalized dict or the standard MCP tool-result envelope."""
    if _field(result, "isError"):
        raise TeachMcpClientError("teach-mcp tool call failed")
    if isinstance(result, Mapping) and "status" in result:
        return dict(result)

    structured = _field(result, "structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)

    content = _field(result, "content")
    if not isinstance(content, (list, tuple)) or len(content) != 1:
        raise TeachMcpClientError("teach-mcp returned no single structured result")
    block = content[0]
    if _field(block, "type") != "text":
        raise TeachMcpClientError("teach-mcp returned a non-text tool result")
    try:
        payload = json.loads(_field(block, "text"))
    except (TypeError, ValueError) as exc:
        raise TeachMcpClientError("teach-mcp returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TeachMcpClientError("teach-mcp result must be a JSON object")
    return payload


class TeachMcpClient:
    """Forward TutorTurn operations without adding a second learning state."""

    def __init__(self, call_tool: ToolCaller):
        if not callable(call_tool):
            raise TeachMcpClientError("call_tool transport is required")
        self._call_tool = call_tool

    def _call(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        return _tool_payload(self._call_tool(name, arguments))

    def tutor_resume_turn(self, session_id: str) -> dict[str, Any]:
        return self._call("tutor_resume_turn", {
            "session_id": _required_text(session_id, "session_id"),
        })

    def tutor_next_question(self, session_id: str) -> dict[str, Any]:
        return self._call("tutor_next_question", {
            "session_id": _required_text(session_id, "session_id"),
        })

    def tutor_mark_presented(
        self, session_id: str, question_id: str, rendered_text: str,
    ) -> dict[str, Any]:
        return self._call("tutor_mark_presented", {
            "session_id": _required_text(session_id, "session_id"),
            "question_id": _required_text(question_id, "question_id"),
            "rendered_text": _required_text(rendered_text, "rendered_text"),
        })

    def tutor_submit_answer(
        self, session_id: str, question_id: str, student_answer: str,
    ) -> dict[str, Any]:
        return self._call("tutor_submit_answer", {
            "session_id": _required_text(session_id, "session_id"),
            "question_id": _required_text(question_id, "question_id"),
            "student_answer": _required_text(student_answer, "student_answer"),
        })

    def tutor_advance(self, session_id: str) -> dict[str, Any]:
        return self._call("tutor_advance", {
            "session_id": _required_text(session_id, "session_id"),
        })
