"""Tests for the curated profile builder."""

from __future__ import annotations

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
from tools.firefly_memory_importer.curated.curated_profile import (
    build_profile,
    curate,
    load,
    to_migration,
    write,
)
from tools.firefly_memory_importer.curated.curated_selector import select


def _ref() -> SourceRef:
    return SourceRef("c1", 0, Role.USER, ts="2024-10-29T12:00:00Z", message_id="m-9")


def _memory(category, content) -> MemoryCandidate:
    return MemoryCandidate(
        id=f"m-{category.value}",
        category=category,
        content=content,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="mem.test",
        disposition=Disposition.AUTO_APPROVE,
    )


def _bond(signal_type, detail) -> BondCandidate:
    return BondCandidate(
        id=f"b-{signal_type.value}",
        signal_type=signal_type,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="bond.test",
        disposition=Disposition.AUTO_APPROVE,
        detail=detail,
    )


def _style() -> StyleCandidate:
    return StyleCandidate(
        id="s-1",
        dimension=StyleDimension.NICKNAME,
        content="小萤",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="style.nickname",
        disposition=Disposition.NEEDS_REVIEW,
    )


def _preview() -> dict:
    migration = MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=[
            _memory(MemoryCategory.USER_FACT, "用户是医生"),
            _memory(MemoryCategory.PREFERENCE, "回复短一点"),
            _memory(MemoryCategory.PROJECT, "在做 Firefly"),
            _memory(MemoryCategory.SHARED_EXPERIENCE, "完成了 v0.3"),
        ],
        bond=[
            _bond(BondSignalType.SHARED_MILESTONE, "完成 v0.3"),
            _bond(BondSignalType.PROMISE_MADE, "下次继续"),
        ],
        style=[_style()],
    )
    return build_preview(migration)


def test_build_profile_groups_buckets() -> None:
    curated = build_profile(
        select(_preview()),
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    profile = curated["profile"]
    assert [e["content"] for e in profile["identity"]] == ["用户是医生"]
    assert {e["content"] for e in profile["preferences"]} == {"回复短一点", "小萤"}
    assert {e["content"] for e in profile["goals"]} == {"在做 Firefly", "下次继续"}
    assert {e["content"] for e in profile["important_events"]} == {"完成了 v0.3", "完成 v0.3"}


def test_curate_end_to_end() -> None:
    curated = curate(
        _preview(),
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    assert curated["run_id"] == "run-1"
    assert len(curated["memory"]) == 4
    assert len(curated["bond"]) == 2
    assert len(curated["style"]) == 1


def test_to_migration_reconstructs() -> None:
    curated = curate(
        _preview(),
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    migration = to_migration(curated)

    assert isinstance(migration, MigrationCandidate)
    assert len(migration.memory) == 4
    assert migration.memory[0].category is MemoryCategory.USER_FACT
    assert migration.memory[0].content == "用户是医生"
    assert migration.bond[0].detail == "完成 v0.3"


def test_evidence_chain_preserved() -> None:
    curated = curate(
        _preview(),
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    entry = curated["profile"]["identity"][0]
    assert entry["evidence"][0]["conversation_id"] == "c1"
    assert entry["evidence"][0]["message_id"] == "m-9"


def test_write_load_roundtrip(tmp_path: Path) -> None:
    curated = curate(
        _preview(),
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )
    path = tmp_path / "curated_migration.json"

    write(curated, path)
    loaded = load(path)

    assert loaded["run_id"] == "run-1"
    assert len(loaded["profile"]["identity"]) == 1
    assert len(loaded["memory"]) == 4
