"""Public synthetic advisory review tests; imported-course evidence stays local."""

from __future__ import annotations

import json

import pytest

from curriculum_review.agent import CurriculumReviewAgent
from curriculum_review.evidence import REVIEW_IDS, build_review_cases
from curriculum_review.prompt import parse_recommendation


def test_structured_recommendation_rejects_invalid_or_unapproved_decision() -> None:
    valid = {"decision": "KEEP_ORIGINAL", "confidence": 0.7,
             "reason": "bookmarks cross a boundary", "impact": "hold for human review",
             "recommended_assignment": "review queue", "human_confirmation_points": ["check page 33"]}
    assert parse_recommendation(json.dumps(valid)).decision == "KEEP_ORIGINAL"
    with pytest.raises(ValueError, match="decision"):
        parse_recommendation(json.dumps({**valid, "decision": "APPROVE"}))
    with pytest.raises(ValueError, match="confidence"):
        parse_recommendation(json.dumps({**valid, "confidence": 1.5}))
    with pytest.raises(ValueError, match="valid JSON"):
        parse_recommendation("I recommend MOVE")


def test_agent_uses_existing_provider_interface_and_only_returns_advice() -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.messages = None

        def chat(self, messages, *, temperature):
            self.messages = messages
            assert temperature == 0.1
            return {"choices": [{"message": {"content": json.dumps({
                "decision": "MERGE", "confidence": 0.6,
                "reason": "same bookmarked section", "impact": "one review unit",
                "recommended_assignment": "book_ch03", "human_confirmation_points": ["verify questions"],
            })}}]}

    from curriculum_review.schema import ReviewCase

    case = ReviewCase("control_theory", "ch10", "3-2", {}, {"status": "REVIEW_REQUIRED"},
                      "SECTION_NUMBER_CONFLICT", [], [])
    provider = FakeProvider()
    recommendation, raw = CurriculumReviewAgent(provider).recommend(case)
    assert recommendation.decision == "MERGE"
    assert recommendation.confidence == 0.6
    assert provider.messages[0]["role"] == "system"
    assert '"chapter_id":"ch10"' in provider.messages[1]["content"]
    assert raw.startswith("{")
