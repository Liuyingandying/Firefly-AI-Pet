"""Closed, deterministic transition rules for Firefly bond state.

This module accepts structured system events only. It has no text detection,
LLM, UI, Qt, provider, or memory dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class BondPhase(str, Enum):
    STRANGER = "stranger"
    ACQUAINTANCE = "acquaintance"
    FAMILIAR = "familiar"
    TRUSTED = "trusted"
    COMPANION = "companion"


class BondSignalType(str, Enum):
    TURN_COMPLETED = "turn_completed"
    THANKED = "thanked"
    CORRECTION = "correction"
    SHARED_MILESTONE = "shared_milestone"
    PROMISE_MADE = "promise_made"
    PROMISE_KEPT = "promise_kept"
    PROMISE_MISSED = "promise_missed"


_DETAIL_SIGNALS = frozenset(
    {
        BondSignalType.SHARED_MILESTONE,
        BondSignalType.PROMISE_MADE,
        BondSignalType.PROMISE_KEPT,
        BondSignalType.PROMISE_MISSED,
    }
)


@dataclass(frozen=True, slots=True)
class BondSignal:
    """A trusted system event accepted by ``BondStateEngine.apply``."""

    type: BondSignalType
    detail: str | None = None

    def __post_init__(self) -> None:
        try:
            signal_type = BondSignalType(self.type)
        except (TypeError, ValueError) as exc:
            raise ValueError("type must be a valid BondSignalType") from exc
        object.__setattr__(self, "type", signal_type)

        if signal_type in _DETAIL_SIGNALS:
            if not isinstance(self.detail, str) or not self.detail.strip():
                raise ValueError(f"{signal_type.value} requires non-empty detail")
            detail = self.detail.strip()
            if len(detail) > 500:
                raise ValueError("signal detail must not exceed 500 characters")
            object.__setattr__(self, "detail", detail)
        elif self.detail is not None:
            raise ValueError(f"{signal_type.value} does not accept detail")


class BondStateView(Protocol):
    trust_level: float
    familiarity_level: float
    shared_milestones: tuple[str, ...]
    pending_promises: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BondTransition:
    phase: BondPhase
    trust_level: float
    familiarity_level: float
    shared_milestones: tuple[str, ...]
    pending_promises: tuple[str, ...]


def apply_bond_rule(state: BondStateView, signal: BondSignal) -> BondTransition:
    """Return the deterministic next values for one validated system signal."""
    if not isinstance(signal, BondSignal):
        raise TypeError("signal must be a BondSignal")

    trust = state.trust_level
    familiarity = state.familiarity_level
    milestones = state.shared_milestones
    promises = state.pending_promises
    signal_type = signal.type

    if signal_type is BondSignalType.TURN_COMPLETED:
        familiarity += 0.005
    elif signal_type is BondSignalType.THANKED:
        trust += 0.02
    elif signal_type is BondSignalType.CORRECTION:
        trust += 0.01
    elif signal_type is BondSignalType.SHARED_MILESTONE:
        assert signal.detail is not None
        if signal.detail not in milestones:
            milestones = (*milestones, signal.detail)
            trust += 0.05
            familiarity += 0.05
    elif signal_type is BondSignalType.PROMISE_MADE:
        assert signal.detail is not None
        if signal.detail not in promises:
            promises = (*promises, signal.detail)
    elif signal_type in {
        BondSignalType.PROMISE_KEPT,
        BondSignalType.PROMISE_MISSED,
    }:
        assert signal.detail is not None
        if signal.detail not in promises:
            raise ValueError("promise signal must reference a pending promise")
        promises = tuple(item for item in promises if item != signal.detail)
        trust += 0.08 if signal_type is BondSignalType.PROMISE_KEPT else -0.05

    trust = _clamp(trust)
    familiarity = _clamp(familiarity)
    return BondTransition(
        phase=phase_for(trust, familiarity),
        trust_level=trust,
        familiarity_level=familiarity,
        shared_milestones=milestones,
        pending_promises=promises,
    )


def phase_for(trust_level: float, familiarity_level: float) -> BondPhase:
    """Derive phase from state levels; phase is never independently writable."""
    trust = _validated_level(trust_level, "trust_level")
    familiarity = _validated_level(familiarity_level, "familiarity_level")
    score = (trust + familiarity) / 2.0
    if score < 0.2:
        return BondPhase.STRANGER
    if score < 0.4:
        return BondPhase.ACQUAINTANCE
    if score < 0.6:
        return BondPhase.FAMILIAR
    if score < 0.8:
        return BondPhase.TRUSTED
    return BondPhase.COMPANION


def _validated_level(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    return normalized


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, round(value, 12)))


__all__ = [
    "BondPhase",
    "BondSignal",
    "BondSignalType",
    "BondTransition",
    "apply_bond_rule",
    "phase_for",
]
