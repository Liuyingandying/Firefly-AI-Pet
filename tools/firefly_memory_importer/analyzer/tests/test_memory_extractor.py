"""Tests for the Stage 1.2 MemoryExtractor."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Conversation, ParsedHistory, Role, Turn
from tools.firefly_memory_importer.analyzer.candidates import Band, MemoryCategory
from tools.firefly_memory_importer.analyzer.memory_extractor import MemoryExtractor


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


def _contents(result: list) -> list[str]:
    return [candidate.content for candidate in result]


def test_user_fact_extraction() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "我叫张三", seq=0),
                _turn(Role.USER, "我是医生", seq=1),
                _turn(Role.USER, "我在杭州", seq=2),
            ],
        )
    )

    result = MemoryExtractor().extract(history)

    facts = [(c.category, c.content) for c in result]
    assert (MemoryCategory.USER_FACT, "张三") in facts
    assert (MemoryCategory.USER_FACT, "医生") in facts
    assert (MemoryCategory.USER_FACT, "杭州") in facts
    assert all(c.category is MemoryCategory.USER_FACT for c in result)


def test_project_extraction() -> None:
    history = _history(
        _conv("c1", [_turn(Role.USER, "我在做 Firefly 项目", seq=0)])
    )

    result = MemoryExtractor().extract(history)

    assert len(result) == 1
    assert result[0].category is MemoryCategory.PROJECT
    assert result[0].content == "Firefly 项目"


def test_assistant_pollution_blocked() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "我在杭州", seq=0),
                _turn(Role.ASSISTANT, "你之前说过你住在北京", seq=1),
                _turn(Role.USER, "你好", seq=2),
            ],
        )
    )

    result = MemoryExtractor().extract(history)

    assert result == []


def test_roleplay_penalty_lowers_confidence() -> None:
    normal = _history(_conv("c1", [_turn(Role.USER, "我在杭州", seq=0)]))
    roleplay = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "（眼神疯狂）你好", seq=0),
                _turn(Role.USER, "我在杭州", seq=1),
            ],
        )
    )

    normal_result = MemoryExtractor().extract(normal)
    roleplay_result = MemoryExtractor().extract(roleplay)

    assert normal_result[0].confidence == 0.4
    assert roleplay_result[0].confidence < normal_result[0].confidence
    assert roleplay_result[0].band is Band.REJECTED


def test_dedup_merges_duplicate_facts() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "我在杭州", seq=0),
                _turn(Role.ASSISTANT, "好的", seq=1),
                _turn(Role.USER, "我在杭州", seq=2),
            ],
        )
    )

    result = MemoryExtractor().extract(history)

    assert len(result) == 1
    assert len(result[0].evidence) == 2
    # repetition (2 distinct user turns) boosts confidence by +0.1
    assert result[0].confidence == 0.5


def test_evidence_chain_preserved() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "我是医生", seq=5, message_id="m-42"),
            ],
        )
    )

    result = MemoryExtractor().extract(history)

    candidate = result[0]
    assert len(candidate.evidence) == 1
    ref = candidate.evidence[0]
    assert ref.conversation_id == "c1"
    assert ref.turn_seq == 5
    assert ref.role is Role.USER
    assert ref.message_id == "m-42"
    assert candidate.rule == "mem.user_fact.occupation"
