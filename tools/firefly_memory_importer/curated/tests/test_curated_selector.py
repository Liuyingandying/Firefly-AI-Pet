"""Tests for the curated selector."""

from __future__ import annotations

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
from tools.firefly_memory_importer.curated.curated_selector import select


def _ref() -> SourceRef:
    return SourceRef("c1", 0, Role.USER)


def _memory(category, *, flags=(), band=Band.HIGH, confidence=0.8) -> MemoryCandidate:
    return MemoryCandidate(
        id=f"m-{category.value}",
        category=category,
        content=f"内容-{category.value}",
        confidence=confidence,
        band=band,
        evidence=(_ref(),),
        rule="mem.test",
        disposition=Disposition.AUTO_APPROVE,
        flags=flags,
    )


def _bond(signal_type, *, flags=()) -> BondCandidate:
    return BondCandidate(
        id=f"b-{signal_type.value}",
        signal_type=signal_type,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="bond.test",
        disposition=Disposition.AUTO_APPROVE,
        flags=flags,
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


def _preview(memory=(), bond=(), style=()) -> dict:
    migration = MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=list(memory),
        bond=list(bond),
        style=list(style),
    )
    return build_preview(migration)


def test_prioritizes_high_value_memory() -> None:
    preview = _preview(
        memory=[
            _memory(MemoryCategory.USER_FACT),
            _memory(MemoryCategory.PREFERENCE),
            _memory(MemoryCategory.PROJECT),
            _memory(MemoryCategory.SHARED_EXPERIENCE),
        ]
    )

    result = select(preview)

    assert len(result["selected"]["memory"]) == 4
    assert result["skipped"] == []


def test_deprioritizes_low_confidence_emotion() -> None:
    preview = _preview(memory=[_memory(MemoryCategory.EMOTION, confidence=0.3)])

    result = select(preview)

    assert result["selected"]["memory"] == []
    assert result["skipped"][0]["reason"] == "low-value-emotion"


def test_keeps_high_confidence_emotion() -> None:
    preview = _preview(memory=[_memory(MemoryCategory.EMOTION, confidence=0.7)])

    result = select(preview)

    assert len(result["selected"]["memory"]) == 1


def test_deprioritizes_roleplay_content() -> None:
    preview = _preview(
        memory=[_memory(MemoryCategory.USER_FACT, flags=("roleplay",))]
    )

    result = select(preview)

    assert result["selected"]["memory"] == []
    assert result["skipped"][0]["reason"] == "roleplay-risk"


def test_skips_rejected_band() -> None:
    preview = _preview(
        memory=[_memory(MemoryCategory.USER_FACT, band=Band.REJECTED)]
    )

    result = select(preview)

    assert result["selected"]["memory"] == []


def test_bond_signal_selection() -> None:
    preview = _preview(
        bond=[
            _bond(BondSignalType.SHARED_MILESTONE),
            _bond(BondSignalType.PROMISE_MADE),
            _bond(BondSignalType.TURN_COMPLETED),
            _bond(BondSignalType.THANKED),
        ]
    )

    result = select(preview)

    selected_signals = {b["signal_type"] for b in result["selected"]["bond"]}
    assert selected_signals == {"shared_milestone", "promise_made"}
    assert any("mechanical-signal" in s["reason"] for s in result["skipped"])


def test_style_selected() -> None:
    preview = _preview(style=[_style()])

    result = select(preview)

    assert len(result["selected"]["style"]) == 1
