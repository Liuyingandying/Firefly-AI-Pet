"""Tests for the Stage 1 confidence scoring."""

from __future__ import annotations

from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    BondSignalType,
    Disposition,
)
from tools.firefly_memory_importer.analyzer.confidence import (
    band_for,
    bond_confidence,
    clamp,
    disposition_for,
    has_explicit_marker,
    has_hedging,
    has_specific_detail,
    memory_confidence,
    style_confidence,
)


def test_clamp() -> None:
    assert clamp(0.5) == 0.5
    assert clamp(1.5) == 1.0
    assert clamp(-0.5) == 0.0


def test_band_for_boundaries() -> None:
    assert band_for(0.7) is Band.HIGH
    assert band_for(0.69) is Band.MEDIUM
    assert band_for(0.4) is Band.MEDIUM
    assert band_for(0.39) is Band.LOW
    assert band_for(0.15) is Band.LOW
    assert band_for(0.14) is Band.REJECTED


def test_disposition_for() -> None:
    assert disposition_for(Band.REJECTED, sensitive=True) is Disposition.AUTO_REJECT
    assert disposition_for(Band.HIGH, sensitive=False) is Disposition.AUTO_APPROVE
    assert disposition_for(Band.HIGH, sensitive=True) is Disposition.NEEDS_REVIEW
    assert disposition_for(Band.MEDIUM) is Disposition.NEEDS_REVIEW
    assert disposition_for(Band.LOW, sensitive=False) is Disposition.AUTO_REJECT
    assert disposition_for(Band.LOW, sensitive=True) is Disposition.NEEDS_REVIEW


def test_memory_confidence_base() -> None:
    score, band, disposition = memory_confidence(user_evidence_count=1)

    assert score == 0.4
    assert band is Band.MEDIUM
    assert disposition is Disposition.NEEDS_REVIEW


def test_memory_confidence_explicit_reaches_high() -> None:
    score, band, disposition = memory_confidence(user_evidence_count=1, explicit=True)

    assert score == 0.7
    assert band is Band.HIGH
    assert disposition is Disposition.AUTO_APPROVE


def test_memory_confidence_repetition() -> None:
    assert memory_confidence(user_evidence_count=2)[0] == 0.5
    assert memory_confidence(user_evidence_count=3)[0] == 0.6


def test_memory_confidence_hedged_and_roleplay() -> None:
    assert memory_confidence(user_evidence_count=1, hedged=True)[0] == 0.2
    assert memory_confidence(user_evidence_count=1, roleplay=True)[0] == 0.1
    # explicit marker offsets the roleplay penalty back to medium
    assert memory_confidence(user_evidence_count=1, explicit=True, roleplay=True)[0] == 0.4


def test_memory_confidence_forced_rejections() -> None:
    assert memory_confidence(user_evidence_count=0)[1] is Band.REJECTED
    assert memory_confidence(user_evidence_count=1, red_line=True)[1] is Band.REJECTED
    assert memory_confidence(user_evidence_count=1, contradicted=True)[1] is Band.REJECTED


def test_memory_confidence_sensitive_high_needs_review() -> None:
    _, band, disposition = memory_confidence(
        user_evidence_count=1, explicit=True, sensitive=True
    )

    assert band is Band.HIGH
    assert disposition is Disposition.NEEDS_REVIEW


def test_bond_confidence() -> None:
    assert bond_confidence(BondSignalType.TURN_COMPLETED)[0] == 0.9
    assert bond_confidence(BondSignalType.THANKED)[0] == 0.8
    assert bond_confidence(BondSignalType.THANKED, strong=False)[0] == 0.5
    assert bond_confidence(BondSignalType.SHARED_MILESTONE, label_source="user")[0] == 0.8
    assert bond_confidence(BondSignalType.SHARED_MILESTONE, label_source="assistant")[0] == 0.4
    assert bond_confidence(BondSignalType.PROMISE_MADE)[0] == 0.6


def test_bond_confidence_roleplay_penalty() -> None:
    assert bond_confidence(BondSignalType.THANKED, roleplay=True)[0] == 0.5
    # TURN_COMPLETED is mechanical and is not penalized by roleplay
    assert bond_confidence(BondSignalType.TURN_COMPLETED, roleplay=True)[0] == 0.9


def test_style_confidence() -> None:
    assert style_confidence(True)[0] == 0.8
    assert style_confidence(False)[0] == 0.4


def test_style_confidence_roleplay_penalty() -> None:
    assert style_confidence(True, roleplay=True)[0] == 0.5
    assert style_confidence(True, roleplay=True)[1] is Band.MEDIUM


def test_marker_helpers() -> None:
    assert has_explicit_marker("我是医生") is True
    assert has_explicit_marker("天气不错") is False
    assert has_hedging("我可能不去了") is True
    assert has_hedging("我会去的") is False
    assert has_specific_detail("用了 Python") is True
    assert has_specific_detail("2024年") is True
    assert has_specific_detail("在杭州") is False
