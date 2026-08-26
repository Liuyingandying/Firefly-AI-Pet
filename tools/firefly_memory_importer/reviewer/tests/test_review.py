"""Tests for the Stage 3.5 migration review."""

from __future__ import annotations

import json
from pathlib import Path

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
from tools.firefly_memory_importer.analyzer.preview import build_preview
from tools.firefly_memory_importer.reviewer.review import (
    ReviewDecision,
    apply_review,
    load_preview,
    write_reviewed,
)


def _ref() -> SourceRef:
    return SourceRef("c1", 0, Role.USER, ts="2024-10-29T12:00:00Z", message_id="m-1")


def _migration() -> MigrationCandidate:
    memory = MemoryCandidate(
        id="m-1",
        category=MemoryCategory.USER_FACT,
        content="用户是医生",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="mem.user_fact.occupation",
        disposition=Disposition.AUTO_APPROVE,
    )
    bond = BondCandidate(
        id="b-1",
        signal_type=BondSignalType.SHARED_MILESTONE,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="bond.shared_milestone",
        disposition=Disposition.AUTO_APPROVE,
        detail="完成 v0.3",
    )
    style = StyleCandidate(
        id="s-1",
        dimension=StyleDimension.NICKNAME,
        content="小萤",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="style.nickname",
        disposition=Disposition.NEEDS_REVIEW,
    )
    return MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=[memory],
        bond=[bond],
        style=[style],
    )


def _preview() -> dict:
    return build_preview(_migration())


def test_accept_keeps_candidate() -> None:
    reviewed = apply_review(
        _preview(), [ReviewDecision("m-1", "accept"), ReviewDecision("b-1", "accept"), ReviewDecision("s-1", "accept")]
    )

    assert len(reviewed["memory"]) == 1
    assert len(reviewed["bond"]) == 1
    assert len(reviewed["style"]) == 1
    assert reviewed["rejected"] == []


def test_reject_removes_candidate() -> None:
    reviewed = apply_review(
        _preview(),
        [
            ReviewDecision("m-1", "reject"),
            ReviewDecision("b-1", "accept"),
            ReviewDecision("s-1", "accept"),
        ],
    )

    assert len(reviewed["memory"]) == 0
    assert reviewed["rejected"][0]["id"] == "m-1"
    assert len(reviewed["bond"]) == 1


def test_edit_changes_content_and_detail() -> None:
    reviewed = apply_review(
        _preview(),
        [
            ReviewDecision("m-1", "edit", "用户是老师"),
            ReviewDecision("b-1", "edit", "完成 v0.4"),
            ReviewDecision("s-1", "accept"),
        ],
    )

    assert reviewed["memory"][0]["content"] == "用户是老师"
    assert reviewed["bond"][0]["detail"] == "完成 v0.4"


def test_missing_decision_defaults_to_reject() -> None:
    reviewed = apply_review(_preview(), [ReviewDecision("m-1", "accept")])

    # b-1 and s-1 have no decision -> rejected
    assert len(reviewed["memory"]) == 1
    assert reviewed["bond"] == []
    assert reviewed["style"] == []
    assert {r["id"] for r in reviewed["rejected"]} == {"b-1", "s-1"}


def test_evidence_chain_preserved() -> None:
    reviewed = apply_review(_preview(), [ReviewDecision("m-1", "accept")])

    evidence = reviewed["memory"][0]["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["conversation_id"] == "c1"
    assert evidence[0]["message_id"] == "m-1"


def test_reviewed_drops_preview_only_fields() -> None:
    reviewed = apply_review(_preview(), [ReviewDecision("m-1", "accept")])

    item = reviewed["memory"][0]
    assert "risk" not in item
    assert "preview" not in item
    assert "review" not in item


def test_write_and_load_roundtrip(tmp_path: Path) -> None:
    reviewed = apply_review(
        _preview(), [ReviewDecision("m-1", "accept"), ReviewDecision("b-1", "reject"), ReviewDecision("s-1", "accept")]
    )
    path = tmp_path / "reviewed_migration.json"

    write_reviewed(reviewed, path)
    loaded = load_preview(path)

    assert loaded["run_id"] == "run-1"
    assert len(loaded["memory"]) == 1
    assert len(loaded["bond"]) == 0
    assert len(loaded["style"]) == 1


def test_invalid_action_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        ReviewDecision("m-1", "maybe")
