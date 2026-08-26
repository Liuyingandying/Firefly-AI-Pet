"""Tests for the Stage 2 migration preview."""

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
from tools.firefly_memory_importer.analyzer.preview import (
    build_preview,
    render_origin_report,
    write_origin_report,
    write_preview,
)


def _ref(role: Role = Role.USER, seq: int = 0) -> SourceRef:
    return SourceRef(conversation_id="c1", turn_seq=seq, role=role, ts="2024-10-29T12:00:00Z", message_id="m-1")


def _memory(**overrides) -> MemoryCandidate:
    defaults = dict(
        id="m-0001",
        category=MemoryCategory.USER_FACT,
        content="用户是医生",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="mem.user_fact.occupation",
        disposition=Disposition.AUTO_APPROVE,
    )
    defaults.update(overrides)
    return MemoryCandidate(**defaults)


def _bond(**overrides) -> BondCandidate:
    defaults = dict(
        id="b-0001",
        signal_type=BondSignalType.SHARED_MILESTONE,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="bond.shared_milestone",
        disposition=Disposition.AUTO_APPROVE,
        detail="完成 v0.3",
    )
    defaults.update(overrides)
    return BondCandidate(**defaults)


def _style(**overrides) -> StyleCandidate:
    defaults = dict(
        id="s-0001",
        dimension=StyleDimension.NICKNAME,
        content="小萤",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="style.nickname",
        disposition=Disposition.AUTO_APPROVE,
        review_only=True,
    )
    defaults.update(overrides)
    return StyleCandidate(**defaults)


def _migration(memory=None, bond=None, style=None) -> MigrationCandidate:
    return MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=memory if memory is not None else [],
        bond=bond if bond is not None else [],
        style=style if style is not None else [],
    )


def test_preview_groups_three_kinds() -> None:
    preview = build_preview(_migration(memory=[_memory()], bond=[_bond()], style=[_style()]))

    assert preview["summary"]["memory"]["total"] == 1
    assert preview["summary"]["bond"]["total"] == 1
    assert preview["summary"]["style"]["total"] == 1
    assert len(preview["groups"]["memory"]) == 1
    assert len(preview["groups"]["bond"]) == 1
    assert len(preview["groups"]["style"]) == 1


def test_preview_shows_evidence_confidence_and_review() -> None:
    preview = build_preview(_migration(memory=[_memory()]))

    item = preview["groups"]["memory"][0]
    assert item["confidence"] == 0.8
    assert len(item["evidence"]) == 1
    assert item["evidence"][0]["conversation_id"] == "c1"
    assert item["review"] == "pending"


def test_preview_marks_risk_flags() -> None:
    roleplay = _memory(id="m-r", flags=("roleplay",))
    sensitive = _memory(id="m-s", category=MemoryCategory.EMOTION)
    assistant = _memory(id="m-a", flags=("assistant_derived",))

    preview = build_preview(_migration(memory=[roleplay, sensitive, assistant]))

    risks = {item["id"]: item["risk"] for item in preview["groups"]["memory"]}
    assert "roleplay" in risks["m-r"]
    assert "sensitive" in risks["m-s"]
    assert "assistant_derived" in risks["m-a"]


def test_preview_derives_sensitive_from_category() -> None:
    preview = build_preview(_migration(memory=[_memory(category=MemoryCategory.EMOTION)]))

    assert "sensitive" in preview["groups"]["memory"][0]["risk"]


def test_write_preview_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "migration_preview.json"
    migration = _migration(memory=[_memory()])

    write_preview(migration, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"
    assert data["groups"]["memory"][0]["content"] == "用户是医生"


def test_origin_report_contains_provenance(tmp_path: Path) -> None:
    migration = _migration(memory=[_memory()], bond=[_bond()])

    report = render_origin_report(migration)

    assert "run-1" in report
    assert "m-0001" in report
    assert "b-0001" in report
    assert "c1" in report
    assert "m-1" in report
    assert "mem.user_fact.occupation" in report


def test_write_origin_report(tmp_path: Path) -> None:
    path = tmp_path / "Origin_Report.md"
    migration = _migration(memory=[_memory()])

    write_origin_report(migration, path)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Firefly 历史迁移溯源报告")
    assert "m-0001" in text
