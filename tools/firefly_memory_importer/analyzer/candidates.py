"""Candidate data model for Firefly history migration Stage 1.

Pure data layer: it imports neither ``core``, ``memory``, nor ``character``,
and performs no LLM calls. The closed value sets are declared locally as string
enums so the analyzer stays self-contained; their string values match the real
``memory.records.MemoryCategory`` and ``core.bond_rules.BondSignalType``
contracts so a later Stage 2 can map them trivially.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from ..models import Role


class MemoryCategory(str, Enum):
    RELATIONSHIP = "relationship"
    SHARED_EXPERIENCE = "shared_experience"
    EMOTION = "emotion"
    PREFERENCE = "preference"
    USER_FACT = "user_fact"
    PROJECT = "project"


class BondSignalType(str, Enum):
    TURN_COMPLETED = "turn_completed"
    THANKED = "thanked"
    CORRECTION = "correction"
    SHARED_MILESTONE = "shared_milestone"
    PROMISE_MADE = "promise_made"
    PROMISE_KEPT = "promise_kept"
    PROMISE_MISSED = "promise_missed"


class Band(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    REJECTED = "rejected"


class Disposition(str, Enum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_REVIEW = "needs_review"
    AUTO_REJECT = "auto_reject"


class StyleDimension(str, Enum):
    NICKNAME = "nickname"
    TONE = "tone"
    VERBOSITY = "verbosity"
    BOUNDARY = "boundary"


class NarrativeType(str, Enum):
    LIFE_EVENT = "life_event"
    EMOTIONAL_TURNING_POINT = "emotional_turning_point"
    SHARED_EXPERIENCE = "shared_experience"
    LONG_TERM_GOAL = "long_term_goal"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One traceable reference back to a parsed turn."""

    conversation_id: str
    turn_seq: int
    role: Role
    ts: str | None = None
    message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "turn_seq": self.turn_seq,
            "role": self.role.value,
            "ts": self.ts,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            conversation_id=data["conversation_id"],
            turn_seq=data["turn_seq"],
            role=Role(data["role"]),
            ts=data.get("ts"),
            message_id=data.get("message_id"),
        )


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    category: MemoryCategory
    content: str
    confidence: float
    band: Band
    evidence: tuple[SourceRef, ...]
    rule: str
    disposition: Disposition
    flags: tuple[str, ...] = ()
    kind: str = "memory"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "category": self.category.value,
            "content": self.content,
            "confidence": self.confidence,
            "band": self.band.value,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "rule": self.rule,
            "disposition": self.disposition.value,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            id=data["id"],
            category=MemoryCategory(data["category"]),
            content=data["content"],
            confidence=data["confidence"],
            band=Band(data["band"]),
            evidence=tuple(SourceRef.from_dict(r) for r in data["evidence"]),
            rule=data["rule"],
            disposition=Disposition(data["disposition"]),
            flags=tuple(data.get("flags", [])),
        )


@dataclass(frozen=True, slots=True)
class BondCandidate:
    id: str
    signal_type: BondSignalType
    confidence: float
    band: Band
    evidence: tuple[SourceRef, ...]
    rule: str
    disposition: Disposition
    detail: str | None = None
    flags: tuple[str, ...] = ()
    kind: str = "bond"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "signal_type": self.signal_type.value,
            "detail": self.detail,
            "confidence": self.confidence,
            "band": self.band.value,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "rule": self.rule,
            "disposition": self.disposition.value,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BondCandidate":
        return cls(
            id=data["id"],
            signal_type=BondSignalType(data["signal_type"]),
            confidence=data["confidence"],
            band=Band(data["band"]),
            evidence=tuple(SourceRef.from_dict(r) for r in data["evidence"]),
            rule=data["rule"],
            disposition=Disposition(data["disposition"]),
            detail=data.get("detail"),
            flags=tuple(data.get("flags", [])),
        )


@dataclass(frozen=True, slots=True)
class StyleCandidate:
    id: str
    dimension: StyleDimension
    content: str
    confidence: float
    band: Band
    evidence: tuple[SourceRef, ...]
    rule: str
    disposition: Disposition
    review_only: bool = True
    flags: tuple[str, ...] = ()
    kind: str = "style"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "dimension": self.dimension.value,
            "content": self.content,
            "confidence": self.confidence,
            "band": self.band.value,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "rule": self.rule,
            "disposition": self.disposition.value,
            "review_only": self.review_only,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StyleCandidate":
        return cls(
            id=data["id"],
            dimension=StyleDimension(data["dimension"]),
            content=data["content"],
            confidence=data["confidence"],
            band=Band(data["band"]),
            evidence=tuple(SourceRef.from_dict(r) for r in data["evidence"]),
            rule=data["rule"],
            disposition=Disposition(data["disposition"]),
            review_only=data.get("review_only", True),
            flags=tuple(data.get("flags", [])),
        )


@dataclass(frozen=True, slots=True)
class NarrativeCandidate:
    id: str
    narrative_type: NarrativeType
    content: str
    confidence: float
    band: Band
    evidence: tuple[SourceRef, ...]
    rule: str
    disposition: Disposition
    flags: tuple[str, ...] = ()
    kind: str = "narrative"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "narrative_type": self.narrative_type.value,
            "content": self.content,
            "confidence": self.confidence,
            "band": self.band.value,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "rule": self.rule,
            "disposition": self.disposition.value,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NarrativeCandidate":
        return cls(
            id=data["id"],
            narrative_type=NarrativeType(data["narrative_type"]),
            content=data["content"],
            confidence=data["confidence"],
            band=Band(data["band"]),
            evidence=tuple(SourceRef.from_dict(r) for r in data["evidence"]),
            rule=data["rule"],
            disposition=Disposition(data["disposition"]),
            flags=tuple(data.get("flags", [])),
        )


@dataclass(slots=True)
class MigrationCandidate:
    """Top-level candidate collection produced by the analyzer."""

    run_id: str
    source_file: str
    source: str
    rule_version: int
    memory: list[MemoryCandidate] = field(default_factory=list)
    bond: list[BondCandidate] = field(default_factory=list)
    style: list[StyleCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_file": self.source_file,
            "source": self.source,
            "rule_version": self.rule_version,
            "summary": self.summary(),
            "groups": {
                "memory": [item.to_dict() for item in self.memory],
                "bond": [item.to_dict() for item in self.bond],
                "style": [item.to_dict() for item in self.style],
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def summary(self) -> dict[str, Any]:
        return {
            "memory": _disposition_counts(self.memory),
            "bond": _disposition_counts(self.bond),
            "style": _disposition_counts(self.style),
        }


def _disposition_counts(items: Iterable[Any]) -> dict[str, int]:
    return {
        "total": len(list(items)),
        "auto_approve": sum(
            1 for item in items if item.disposition is Disposition.AUTO_APPROVE
        ),
        "needs_review": sum(
            1 for item in items if item.disposition is Disposition.NEEDS_REVIEW
        ),
        "auto_reject": sum(
            1 for item in items if item.disposition is Disposition.AUTO_REJECT
        ),
    }


__all__ = [
    "Band",
    "BondCandidate",
    "BondSignalType",
    "Disposition",
    "MemoryCandidate",
    "MemoryCategory",
    "MigrationCandidate",
    "NarrativeCandidate",
    "NarrativeType",
    "SourceRef",
    "StyleCandidate",
    "StyleDimension",
]
