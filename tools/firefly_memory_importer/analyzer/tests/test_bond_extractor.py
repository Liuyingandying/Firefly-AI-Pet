"""Tests for the Stage 1.4 BondExtractor."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Conversation, ParsedHistory, Role, Turn
from tools.firefly_memory_importer.analyzer.candidates import BondSignalType
from tools.firefly_memory_importer.analyzer.bond_extractor import BondExtractor


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


def _of_type(result: list, signal_type: BondSignalType) -> list:
    return [c for c in result if c.signal_type is signal_type]


def test_milestone_extraction() -> None:
    history = _history(
        _conv("c1", [_turn(Role.USER, "我们一起完成了 Firefly v0.3", seq=0)])
    )

    result = BondExtractor().extract(history)

    milestones = _of_type(result, BondSignalType.SHARED_MILESTONE)
    assert len(milestones) == 1
    assert milestones[0].detail == "Firefly v0.3"
    assert milestones[0].rule == "bond.shared_milestone"


def test_thanks_extraction() -> None:
    history = _history(_conv("c1", [_turn(Role.USER, "谢谢你帮了大忙", seq=0)]))

    result = BondExtractor().extract(history)

    thanked = _of_type(result, BondSignalType.THANKED)
    assert len(thanked) == 1
    assert thanked[0].detail is None


def test_promise_extraction() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "下次继续做这个项目", seq=0),
                _turn(Role.USER, "做完了那个约定", seq=2),
                _turn(Role.USER, "没完成那个约定", seq=4),
            ],
        )
    )

    result = BondExtractor().extract(history)

    made = _of_type(result, BondSignalType.PROMISE_MADE)
    kept = _of_type(result, BondSignalType.PROMISE_KEPT)
    missed = _of_type(result, BondSignalType.PROMISE_MISSED)
    assert [c.detail for c in made] == ["做这个项目"]
    assert [c.detail for c in kept] == ["那个约定"]
    assert [c.detail for c in missed] == ["那个约定"]


def test_turn_completed_mechanical_count() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "第一句", seq=0),
                _turn(Role.ASSISTANT, "回复", seq=1),
                _turn(Role.USER, "第二句", seq=2),
                _turn(Role.ASSISTANT, "回复", seq=3),
            ],
        )
    )

    result = BondExtractor().extract(history)

    assert len(_of_type(result, BondSignalType.TURN_COMPLETED)) == 2


def test_assistant_pollution_blocked() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "我们一起完成了X", seq=0),
                _turn(Role.USER, "你好", seq=1),
            ],
        )
    )

    result = BondExtractor().extract(history)

    assert _of_type(result, BondSignalType.SHARED_MILESTONE) == []
    # only the mechanical TURN_COMPLETED from the user turn remains
    assert all(c.signal_type is BondSignalType.TURN_COMPLETED for c in result)


def test_roleplay_filtering_lowers_confidence() -> None:
    normal = _history(_conv("c1", [_turn(Role.USER, "我们一起完成了X", seq=0)]))
    roleplay = _history(
        _conv(
            "c1",
            [
                _turn(Role.ASSISTANT, "（眼神疯狂）你好", seq=0),
                _turn(Role.USER, "我们一起完成了X", seq=1),
            ],
        )
    )

    normal_milestone = _of_type(
        BondExtractor().extract(normal), BondSignalType.SHARED_MILESTONE
    )[0]
    roleplay_milestone = _of_type(
        BondExtractor().extract(roleplay), BondSignalType.SHARED_MILESTONE
    )[0]

    assert roleplay_milestone.confidence < normal_milestone.confidence


def test_time_ordering() -> None:
    history = _history(
        _conv(
            "c1",
            [
                _turn(Role.USER, "谢谢你", seq=0),
                _turn(Role.USER, "我们一起完成了X", seq=2),
                _turn(Role.USER, "下次继续Y", seq=4),
            ],
        )
    )

    result = BondExtractor().extract(history)

    seqs = [min(ref.turn_seq for ref in c.evidence) for c in result]
    assert seqs == sorted(seqs)


def test_evidence_chain_preserved() -> None:
    history = _history(
        _conv(
            "c1",
            [_turn(Role.USER, "我们一起完成了X", seq=9, message_id="m-9")],
        )
    )

    result = BondExtractor().extract(history)

    milestone = _of_type(result, BondSignalType.SHARED_MILESTONE)[0]
    assert len(milestone.evidence) == 1
    ref = milestone.evidence[0]
    assert ref.conversation_id == "c1"
    assert ref.turn_seq == 9
    assert ref.role is Role.USER
    assert ref.message_id == "m-9"
