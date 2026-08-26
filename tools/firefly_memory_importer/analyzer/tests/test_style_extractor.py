"""Tests for the Stage 1.3 StyleExtractor."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Conversation, ParsedHistory, Role, Turn
from tools.firefly_memory_importer.analyzer.candidates import Band, StyleDimension
from tools.firefly_memory_importer.analyzer.style_extractor import StyleExtractor


def _turn(
    role: Role, content: str, seq: int = 0, message_id: str | None = None
) -> Turn:
    return Turn(role=role, content=content, seq=seq, message_id=message_id)


def _conv(conv_id: str, turns: list[Turn]) -> Conversation:
    return Conversation(id=conv_id, turns=turns)


def _history(*conversations: Conversation) -> ParsedHistory:
    return ParsedHistory(
        source_file="conversations.txt",
        source="doubao",
        conversations=list(conversations),
    )


def test_explicit_preference_extraction() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "叫我小萤", seq=0),
                _turn(Role.USER, "说话温柔一点", seq=1),
                _turn(Role.USER, "回复短一点", seq=2),
            ],
        )
    )

    result = StyleExtractor().extract(history)

    pairs = {(c.dimension, c.content) for c in result}
    assert (StyleDimension.NICKNAME, "小萤") in pairs
    assert (StyleDimension.TONE, "温柔一点") in pairs
    assert (StyleDimension.VERBOSITY, "短一点") in pairs


def test_boundary_extraction() -> None:
    history = _history(_conv("c1", [_turn(Role.USER, "不用每次都问我", seq=0)]))

    result = StyleExtractor().extract(history)

    assert len(result) == 1
    assert result[0].dimension is StyleDimension.BOUNDARY
    assert result[0].content == "问我"


def test_assistant_pollution_blocked() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "叫我小萤", seq=0),
                _turn(Role.USER, "你好", seq=1),
            ],
        )
    )

    result = StyleExtractor().extract(history)

    assert result == []


def test_roleplay_filtering_lowers_confidence() -> None:
    normal = _history(_conv("c1", [_turn(Role.USER, "叫我小萤", seq=0)]))
    roleplay = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "（眼神疯狂）你好", seq=0),
                _turn(Role.USER, "叫我小萤", seq=1),
            ],
        )
    )

    normal_result = StyleExtractor().extract(normal)
    roleplay_result = StyleExtractor().extract(roleplay)

    assert normal_result[0].confidence == 0.8
    assert roleplay_result[0].confidence < normal_result[0].confidence
    assert roleplay_result[0].band is Band.MEDIUM


def test_style_candidate_review_only() -> None:
    history = _history(_conv("c1", [_turn(Role.USER, "叫我小萤", seq=0)]))

    result = StyleExtractor().extract(history)

    candidate = result[0]
    assert candidate.kind == "style"
    assert candidate.review_only is True
    assert candidate.rule == "style.nickname"


def test_evidence_chain_preserved() -> None:
    history = _history(
        _conv("c1", [_turn(Role.USER, "叫我小萤", seq=7, message_id="m-7")])
    )

    result = StyleExtractor().extract(history)

    candidate = result[0]
    assert len(candidate.evidence) == 1
    ref = candidate.evidence[0]
    assert ref.conversation_id == "c1"
    assert ref.turn_seq == 7
    assert ref.role is Role.USER
    assert ref.message_id == "m-7"
