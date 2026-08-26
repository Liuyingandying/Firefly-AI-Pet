"""Deterministic confidence scoring for Firefly history migration Stage 1.

Pure functions with no ``core``/``memory``/``character`` imports and no LLM.
Implements the additive confidence model plus band/disposition mapping from the
Stage 1 design review.
"""

from __future__ import annotations

import re

from .candidates import Band, BondSignalType, Disposition


EXPLICIT_MARKERS = ("记住", "我是", "我喜欢", "我的", "我们一起", "叫我")
HEDGING_MARKERS = ("可能", "也许", "大概", "不确定", "感觉")

_SPECIFICITY_DIGIT = re.compile(r"\d")
_SPECIFICITY_LATIN = re.compile(r"[A-Za-z]{2,}")


def clamp(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def band_for(score: float) -> Band:
    if score >= 0.7:
        return Band.HIGH
    if score >= 0.4:
        return Band.MEDIUM
    if score >= 0.15:
        return Band.LOW
    return Band.REJECTED


def disposition_for(band: Band, *, sensitive: bool = False) -> Disposition:
    if band is Band.REJECTED:
        return Disposition.AUTO_REJECT
    if band is Band.HIGH:
        return Disposition.AUTO_APPROVE if not sensitive else Disposition.NEEDS_REVIEW
    if band is Band.MEDIUM:
        return Disposition.NEEDS_REVIEW
    return Disposition.NEEDS_REVIEW if sensitive else Disposition.AUTO_REJECT


def has_explicit_marker(text: str) -> bool:
    return any(marker in text for marker in EXPLICIT_MARKERS)


def has_hedging(text: str) -> bool:
    return any(marker in text for marker in HEDGING_MARKERS)


def has_specific_detail(text: str) -> bool:
    return bool(_SPECIFICITY_DIGIT.search(text) or _SPECIFICITY_LATIN.search(text))


def memory_confidence(
    *,
    user_evidence_count: int,
    explicit: bool = False,
    hedged: bool = False,
    specific: bool = False,
    roleplay: bool = False,
    contradicted: bool = False,
    red_line: bool = False,
    sensitive: bool = False,
) -> tuple[float, Band, Disposition]:
    """Score a memory candidate and return (confidence, band, disposition).

    The score is the additive model: ``base(0.4) + explicit(0.3) +
    repetition(0.1/0.2) + specificity(0.1) - hedging(0.2) - roleplay(0.3)``,
    clamped to [0, 1]. Red-line, contradiction, or zero user evidence force a
    rejected result.
    """
    if red_line or contradicted or user_evidence_count <= 0:
        return (0.0, Band.REJECTED, Disposition.AUTO_REJECT)

    score = 0.4
    if explicit:
        score += 0.3
    if user_evidence_count >= 3:
        score += 0.2
    elif user_evidence_count == 2:
        score += 0.1
    if specific:
        score += 0.1
    if hedged:
        score -= 0.2
    if roleplay:
        score -= 0.3

    score = round(clamp(score), 2)
    band = band_for(score)
    disposition = disposition_for(band, sensitive=sensitive)
    return (score, band, disposition)


def bond_confidence(
    signal_type: BondSignalType,
    *,
    label_source: str = "user",
    strong: bool = True,
    roleplay: bool = False,
) -> tuple[float, Band, Disposition]:
    """Score a bond candidate; mechanical signals are fixed-high by default.

    ``roleplay`` lowers semantic signals (thanked/milestone/promise) but never
    the mechanical ``TURN_COMPLETED`` count.
    """
    if signal_type is BondSignalType.TURN_COMPLETED:
        score = 0.9
    elif signal_type in (BondSignalType.THANKED, BondSignalType.CORRECTION):
        score = 0.8 if strong else 0.5
    elif signal_type is BondSignalType.SHARED_MILESTONE:
        score = 0.8 if label_source == "user" else 0.4
    else:  # PROMISE_MADE / PROMISE_KEPT / PROMISE_MISSED
        score = 0.6

    if roleplay and signal_type is not BondSignalType.TURN_COMPLETED:
        score -= 0.3
    score = round(clamp(score), 2)
    band = band_for(score)
    disposition = disposition_for(band, sensitive=False)
    return (score, band, disposition)


def style_confidence(
    explicit: bool, *, roleplay: bool = False
) -> tuple[float, Band, Disposition]:
    score = 0.8 if explicit else 0.4
    if roleplay:
        score -= 0.3
    score = round(clamp(score), 2)
    band = band_for(score)
    disposition = disposition_for(band, sensitive=False)
    return (score, band, disposition)


__all__ = [
    "band_for",
    "bond_confidence",
    "clamp",
    "disposition_for",
    "has_explicit_marker",
    "has_hedging",
    "has_specific_detail",
    "memory_confidence",
    "style_confidence",
]
