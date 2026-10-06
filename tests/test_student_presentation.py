"""Phase 1 adapter contracts; no UI or real learner data involved."""

from __future__ import annotations

import json

import pytest

from learning.student_presentation import (
    PresentationError,
    feedback_to_display,
    question_to_display,
)
from learning.teach_mcp_client import TeachMcpClient, TeachMcpClientError


def _question() -> dict:
    return {
        "status": "success",
        "question_id": "THZ-CP1-06",
        "mode": "choice",
        "stem": "哪个频率落在 0.1–10 THz 内？",
        "options": {
            "A": "0.5 THz", "B": "50 GHz", "C": "20 THz", "D": "3 GHz",
        },
        "difficulty": "easy",
        "topic": "THz-ISAC",
        "session_id": "ff-private-session",
    }


def test_question_renders_exact_official_stem_and_options_without_metadata():
    displayed = question_to_display(_question())
    assert displayed == (
        "哪个频率落在 0.1–10 THz 内？\n"
        "A. 0.5 THz\nB. 50 GHz\nC. 20 THz\nD. 3 GHz\n"
        "请回答 A/B/C/D。"
    )
    assert "ff-private-session" not in displayed
    assert "THZ-CP1-06" not in displayed


def test_question_rejects_missing_stem():
    question = _question()
    question.pop("stem")
    with pytest.raises(PresentationError, match="stem"):
        question_to_display(question)


@pytest.mark.parametrize("injected", [
    '<function=tutor_submit_answer><parameter=question_id>THZ-CP1-06',
    '{"tool":"tutor_next_question","arguments":{"session_id":"ff-secret"}}',
    'answer: A; session: ff-secret',
])
def test_question_rejects_raw_tool_output(injected: str):
    question = _question()
    question["stem"] = injected
    with pytest.raises(PresentationError, match="internal tool output"):
        question_to_display(question)


def test_question_rejects_answer_or_explanation_fields():
    for field in ("answer", "explanation"):
        question = _question()
        question[field] = "A"
        with pytest.raises(PresentationError, match="non-student-facing fields"):
            question_to_display(question)


def test_feedback_uses_only_authoritative_feedback_and_rejects_leaks():
    result = {
        "status": "success", "correct": True, "stage": "ADVANCE",
        "feedback": "回答正确。0.5 THz 位于该范围内。",
        "question_id": "THZ-CP1-06", "mastery_after": 0.8,
    }
    assert feedback_to_display(result) == "回答正确。0.5 THz 位于该范围内。"
    for field in ("answer", "explanation", "tool"):
        unsafe = {**result, field: "internal"}
        with pytest.raises(PresentationError):
            feedback_to_display(unsafe)


def test_feedback_rejects_raw_tool_json():
    with pytest.raises(PresentationError, match="internal tool output"):
        feedback_to_display({
            "status": "success", "correct": False,
            "feedback": '{"function":"tutor_submit_answer","answer":"A"}',
        })


def test_client_forwards_five_official_tools_without_changing_arguments():
    calls = []

    def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"status": "success"}

    client = TeachMcpClient(call_tool)
    client.tutor_resume_turn("session-1")
    client.tutor_next_question("session-1")
    client.tutor_mark_presented("session-1", "Q-1", "题干\nA. 一\nB. 二")
    client.tutor_submit_answer("session-1", "Q-1", " B ")
    client.tutor_advance("session-1")
    assert calls == [
        ("tutor_resume_turn", {"session_id": "session-1"}),
        ("tutor_next_question", {"session_id": "session-1"}),
        ("tutor_mark_presented", {
            "session_id": "session-1", "question_id": "Q-1",
            "rendered_text": "题干\nA. 一\nB. 二",
        }),
        ("tutor_submit_answer", {
            "session_id": "session-1", "question_id": "Q-1",
            "student_answer": " B ",
        }),
        ("tutor_advance", {"session_id": "session-1"}),
    ]


def test_client_accepts_standard_mcp_text_envelope_and_rejects_tool_error():
    client = TeachMcpClient(lambda _name, _args: {
        "content": [{"type": "text", "text": json.dumps({
            "status": "success", "stage": "CHECK",
        })}],
    })
    assert client.tutor_resume_turn("session-1")["stage"] == "CHECK"

    failing = TeachMcpClient(lambda _name, _args: {
        "isError": True, "content": [{"type": "text", "text": "failure"}],
    })
    with pytest.raises(TeachMcpClientError, match="failed"):
        failing.tutor_resume_turn("session-1")
