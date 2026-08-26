"""Tests for the Stage 1 candidate data model."""

from __future__ import annotations

import json

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    BondCandidate,
    BondSignalType,
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    MigrationCandidate,
    SourceRef,
    StyleCandidate,
    StyleDimension,
)


def _ref(role: Role, seq: int = 0) -> SourceRef:
    return SourceRef(conversation_id="c1", turn_seq=seq, role=role)


def test_source_ref_to_dict() -> None:
    ref = SourceRef(
        conversation_id="c1",
        turn_seq=3,
        role=Role.USER,
        ts="2024-10-29T12:09:29Z",
        message_id="m-1",
    )

    assert ref.to_dict() == {
        "conversation_id": "c1",
        "turn_seq": 3,
        "role": "user",
        "ts": "2024-10-29T12:09:29Z",
        "message_id": "m-1",
    }


def test_memory_candidate_to_dict() -> None:
    candidate = MemoryCandidate(
        id="m1",
        category=MemoryCategory.USER_FACT,
        content="用户是后端开发",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(Role.USER),),
        rule="mem.user_fact.occupation",
        disposition=Disposition.AUTO_APPROVE,
    )

    assert candidate.kind == "memory"
    assert candidate.to_dict()["category"] == "user_fact"
    assert candidate.to_dict()["band"] == "high"
    assert candidate.to_dict()["disposition"] == "auto_approve"
    assert candidate.to_dict()["flags"] == []


def test_bond_candidate_to_dict() -> None:
    candidate = BondCandidate(
        id="b1",
        signal_type=BondSignalType.SHARED_MILESTONE,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(Role.USER),),
        rule="bond.milestone",
        disposition=Disposition.AUTO_APPROVE,
        detail="完成 v0.3",
    )

    assert candidate.kind == "bond"
    assert candidate.to_dict()["signal_type"] == "shared_milestone"
    assert candidate.to_dict()["detail"] == "完成 v0.3"


def test_style_candidate_review_only_default() -> None:
    candidate = StyleCandidate(
        id="s1",
        dimension=StyleDimension.NICKNAME,
        content="叫我小萤",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(Role.USER),),
        rule="style.nickname",
        disposition=Disposition.AUTO_APPROVE,
    )

    assert candidate.kind == "style"
    assert candidate.review_only is True
    assert candidate.to_dict()["dimension"] == "nickname"
    assert candidate.to_dict()["review_only"] is True


def test_migration_candidate_summary_and_json() -> None:
    memory = MemoryCandidate(
        id="m1",
        category=MemoryCategory.PREFERENCE,
        content="回复短一点",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(Role.USER),),
        rule="mem.preference.verbosity",
        disposition=Disposition.AUTO_APPROVE,
    )
    bond = BondCandidate(
        id="b1",
        signal_type=BondSignalType.THANKED,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(Role.USER),),
        rule="bond.thank",
        disposition=Disposition.AUTO_APPROVE,
    )
    migration = MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=[memory],
        bond=[bond],
        style=[],
    )

    assert migration.to_dict()["summary"] == {
        "memory": {"total": 1, "auto_approve": 1, "needs_review": 0, "auto_reject": 0},
        "bond": {"total": 1, "auto_approve": 1, "needs_review": 0, "auto_reject": 0},
        "style": {"total": 0, "auto_approve": 0, "needs_review": 0, "auto_reject": 0},
    }
    parsed = json.loads(migration.to_json())
    assert parsed["run_id"] == "run-1"
    assert len(parsed["groups"]["memory"]) == 1
    assert len(parsed["groups"]["bond"]) == 1
    assert len(parsed["groups"]["style"]) == 0
