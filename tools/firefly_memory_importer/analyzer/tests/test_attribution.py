"""Tests for the Stage 1 attribution rules."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Conversation, Role, Turn
from tools.firefly_memory_importer.analyzer.attribution import (
    enforce_user_evidence,
    has_ooc_marker,
    has_stage_direction,
    has_user_evidence,
    is_roleplay_heavy,
    roleplay_score,
)
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    SourceRef,
)


def _turn(role: Role, content: str, seq: int = 0) -> Turn:
    return Turn(role=role, content=content, seq=seq)


def _conv(*turns: Turn) -> Conversation:
    return Conversation(id="c1", turns=list(turns))


def test_has_stage_direction() -> None:
    assert has_stage_direction("（眼神疯狂）你好")
    assert has_stage_direction("*心里揪紧* 是谁")
    assert has_stage_direction("(smiling) hi")
    assert not has_stage_direction("普通的一句话")


def test_roleplay_score_fraction_of_assistant_turns() -> None:
    conv = _conv(
        _turn(Role.ASSISTANT, "（眼神疯狂）你好"),
        _turn(Role.USER, "你好"),
        _turn(Role.ASSISTANT, "普通回复"),
    )

    assert roleplay_score(conv) == 0.5
    assert is_roleplay_heavy(conv, threshold=0.5) is True
    assert is_roleplay_heavy(conv, threshold=0.6) is False


def test_roleplay_score_no_assistant_turns() -> None:
    assert roleplay_score(_conv(_turn(Role.USER, "只有用户"))) == 0.0


def test_has_ooc_marker() -> None:
    assert has_ooc_marker("((跳出角色") is True
    assert has_ooc_marker("ooc: 你好") is True
    assert has_ooc_marker("现实里我是程序员") is True
    assert not has_ooc_marker("你好呀")


def test_has_user_evidence() -> None:
    user_ref = SourceRef(conversation_id="c1", turn_seq=0, role=Role.USER)
    assistant_ref = SourceRef(conversation_id="c1", turn_seq=1, role=Role.ASSISTANT)

    assert has_user_evidence((user_ref,)) is True
    assert has_user_evidence((user_ref, assistant_ref)) is True
    assert has_user_evidence((assistant_ref,)) is False
    assert has_user_evidence(()) is False


def _memory(evidence: tuple[SourceRef, ...]) -> MemoryCandidate:
    return MemoryCandidate(
        id="m1",
        category=MemoryCategory.USER_FACT,
        content="用户是医生",
        confidence=0.8,
        band=Band.HIGH,
        evidence=evidence,
        rule="mem.user_fact.occupation",
        disposition=Disposition.AUTO_APPROVE,
    )


def test_enforce_user_evidence_keeps_user_backed_candidate() -> None:
    candidate = _memory((SourceRef("c1", 0, Role.USER),))

    result = enforce_user_evidence(candidate)

    assert result is candidate


def test_enforce_user_evidence_rejects_assistant_backed_candidate() -> None:
    candidate = _memory((SourceRef("c1", 0, Role.ASSISTANT),))

    result = enforce_user_evidence(candidate)

    assert result.band is Band.REJECTED
    assert result.disposition is Disposition.AUTO_REJECT
    assert "assistant_derived" in result.flags
