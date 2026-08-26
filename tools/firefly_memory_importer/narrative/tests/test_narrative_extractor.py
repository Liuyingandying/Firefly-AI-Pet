"""Tests for the NarrativeExtractor."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Conversation, ParsedHistory, Role, Turn
from tools.firefly_memory_importer.analyzer.candidates import NarrativeType
from tools.firefly_memory_importer.narrative.narrative_extractor import NarrativeExtractor


def _turn(role: Role, content: str, seq: int = 0, message_id: str | None = None) -> Turn:
    return Turn(role=role, content=content, seq=seq, message_id=message_id)


def _conv(conv_id: str, turns: list[Turn]) -> Conversation:
    return Conversation(id=conv_id, turns=turns)


def _history(*conversations: Conversation) -> ParsedHistory:
    return ParsedHistory(
        source_file="conversations.txt",
        source="doubao",
        conversations=list(conversations),
    )


def _of_type(result: list, narrative_type: NarrativeType) -> list:
    return [c for c in result if c.narrative_type is narrative_type]


def test_life_event_extraction() -> None:
    result = NarrativeExtractor().extract(
        _history(_conv("c1", [_turn(Role.USER, "我毕业了", seq=0)]))
    )

    events = _of_type(result, NarrativeType.LIFE_EVENT)
    assert len(events) == 1
    assert events[0].content == "毕业了"


def test_emotional_turning_point_extraction() -> None:
    result = NarrativeExtractor().extract(
        _history(_conv("c1", [_turn(Role.USER, "我终于放下了", seq=0)]))
    )

    points = _of_type(result, NarrativeType.EMOTIONAL_TURNING_POINT)
    assert len(points) == 1
    assert points[0].content == "放下了"


def test_shared_experience_extraction() -> None:
    result = NarrativeExtractor().extract(
        _history(_conv("c1", [_turn(Role.USER, "我们一起度过了那个夏天", seq=0)]))
    )

    experiences = _of_type(result, NarrativeType.SHARED_EXPERIENCE)
    assert len(experiences) == 1
    assert "度过了那个夏天" in experiences[0].content


def test_long_term_goal_extraction() -> None:
    result = NarrativeExtractor().extract(
        _history(_conv("c1", [_turn(Role.USER, "我想三年后开一家公司", seq=0)]))
    )

    goals = _of_type(result, NarrativeType.LONG_TERM_GOAL)
    assert len(goals) == 1
    assert "三年后开一家公司" in goals[0].content


def test_assistant_pollution_blocked() -> None:
    result = NarrativeExtractor().extract(
        _history(
            _conv(
                "c1",
                [
                    _turn(Role.ASSISTANT, "我毕业了", seq=0),
                    _turn(Role.USER, "你好", seq=1),
                ],
            )
        )
    )

    assert result == []


def test_roleplay_penalty_lowers_confidence() -> None:
    normal = _history(_conv("c1", [_turn(Role.USER, "我毕业了", seq=0)]))
    roleplay = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "（眼神疯狂）你好", seq=0),
                _turn(Role.USER, "我毕业了", seq=1),
            ],
        )
    )

    normal_result = NarrativeExtractor().extract(normal)
    roleplay_result = NarrativeExtractor().extract(roleplay)

    assert roleplay_result[0].confidence < normal_result[0].confidence


def test_narrative_is_sensitive_and_reviewable() -> None:
    result = NarrativeExtractor().extract(
        _history(_conv("c1", [_turn(Role.USER, "我毕业了", seq=0)]))
    )

    candidate = result[0]
    assert "sensitive" in candidate.flags
    assert candidate.disposition.value == "needs_review"


def test_evidence_chain_preserved() -> None:
    result = NarrativeExtractor().extract(
        _history(
            _conv("c1", [_turn(Role.USER, "我毕业了", seq=7, message_id="m-7")])
        )
    )

    candidate = result[0]
    assert candidate.evidence[0].turn_seq == 7
    assert candidate.evidence[0].role is Role.USER
    assert candidate.evidence[0].message_id == "m-7"
