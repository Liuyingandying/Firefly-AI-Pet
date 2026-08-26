"""Pure data model for Firefly's long-term memory records.

This module intentionally has no dependency on Mem0, ``core``, or ``ui``.
"""

from __future__ import annotations

import math
import secrets
import time
import uuid
from dataclasses import dataclass, fields
from enum import Enum
from threading import Lock
from typing import Any, Mapping, TypeVar


class MemoryCategory(str, Enum):
    RELATIONSHIP = "relationship"
    SHARED_EXPERIENCE = "shared_experience"
    EMOTION = "emotion"
    PREFERENCE = "preference"
    USER_FACT = "user_fact"
    PROJECT = "project"


class MemorySource(str, Enum):
    EXPLICIT = "explicit"
    SUGGESTED = "suggested"
    MIGRATED = "migrated"


class WritePolicy(str, Enum):
    OFF = "off"
    EXPLICIT_ONLY = "explicit_only"
    ASK = "ask"
    AUTO = "auto"


CATEGORY_DEFAULTS: dict[MemoryCategory, dict[str, float]] = {
    MemoryCategory.RELATIONSHIP: {"weight": 1.5, "half_life_days": 90.0},
    MemoryCategory.SHARED_EXPERIENCE: {
        "weight": 1.2,
        "half_life_days": 60.0,
    },
    MemoryCategory.EMOTION: {"weight": 1.0, "half_life_days": 7.0},
    MemoryCategory.PREFERENCE: {"weight": 0.9, "half_life_days": 14.0},
    MemoryCategory.USER_FACT: {"weight": 0.8, "half_life_days": 30.0},
    MemoryCategory.PROJECT: {"weight": 0.6, "half_life_days": 14.0},
}


_EnumT = TypeVar("_EnumT", bound=Enum)
_id_lock = Lock()
_last_id_timestamp_ms = 0
_last_id_random = 0
_UUID7_RANDOM_MASK = (1 << 74) - 1


def _new_sortable_id(timestamp_ms: int | None = None) -> str:
    """Return a monotonic UUIDv7-shaped identifier."""
    global _last_id_timestamp_ms, _last_id_random

    candidate_ms = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    with _id_lock:
        effective_ms = max(candidate_ms, _last_id_timestamp_ms)
        if effective_ms == _last_id_timestamp_ms:
            random_bits = (_last_id_random + 1) & _UUID7_RANDOM_MASK
            if random_bits == 0:
                effective_ms += 1
                random_bits = secrets.randbits(74)
        else:
            random_bits = secrets.randbits(74)
        _last_id_timestamp_ms = effective_ms
        _last_id_random = random_bits

    value = (effective_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (random_bits >> 62) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def _coerce_enum(value: Any, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid {enum_type.__name__}") from exc


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _timestamp(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer timestamp")
    return value


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    category: MemoryCategory
    content: str
    source: MemorySource
    trigger: str
    permission: WritePolicy
    weight: float
    created_ts: int
    updated_ts: int
    last_accessed_ts: int
    retention_half_life_days: float
    vector_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "id"))
        object.__setattr__(
            self,
            "category",
            _coerce_enum(self.category, MemoryCategory, "category"),
        )
        object.__setattr__(self, "content", _required_text(self.content, "content"))
        object.__setattr__(
            self, "source", _coerce_enum(self.source, MemorySource, "source")
        )
        object.__setattr__(self, "trigger", _required_text(self.trigger, "trigger"))
        object.__setattr__(
            self,
            "permission",
            _coerce_enum(self.permission, WritePolicy, "permission"),
        )

        weight = _finite_number(self.weight, "weight")
        if not 0.0 <= weight <= 2.0:
            raise ValueError("weight must be between 0.0 and 2.0")
        object.__setattr__(self, "weight", weight)

        created_ts = _timestamp(self.created_ts, "created_ts")
        updated_ts = _timestamp(self.updated_ts, "updated_ts")
        last_accessed_ts = _timestamp(self.last_accessed_ts, "last_accessed_ts")
        if updated_ts < created_ts:
            raise ValueError("updated_ts must be greater than or equal to created_ts")
        if last_accessed_ts < created_ts:
            raise ValueError(
                "last_accessed_ts must be greater than or equal to created_ts"
            )

        half_life = _finite_number(
            self.retention_half_life_days, "retention_half_life_days"
        )
        if half_life <= 0:
            raise ValueError("retention_half_life_days must be greater than 0")
        object.__setattr__(self, "retention_half_life_days", half_life)

        if self.vector_id is not None:
            object.__setattr__(
                self, "vector_id", _required_text(self.vector_id, "vector_id")
            )

    @classmethod
    def create(
        cls,
        *,
        category: MemoryCategory | str,
        content: str,
        trigger: str,
        source: MemorySource | str = MemorySource.EXPLICIT,
        permission: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
        weight: float | None = None,
        retention_half_life_days: float | None = None,
        vector_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> MemoryRecord:
        """Create a validated record with generated ID, timestamps, and defaults."""
        normalized_category = _coerce_enum(category, MemoryCategory, "category")
        now_ms = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
        defaults = CATEGORY_DEFAULTS[normalized_category]
        return cls(
            id=_new_sortable_id(now_ms),
            category=normalized_category,
            content=content,
            source=source,
            trigger=trigger,
            permission=permission,
            weight=defaults["weight"] if weight is None else weight,
            created_ts=now_ms,
            updated_ts=now_ms,
            last_accessed_ts=now_ms,
            retention_half_life_days=(
                defaults["half_life_days"]
                if retention_half_life_days is None
                else retention_half_life_days
            ),
            vector_id=vector_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the record into JSON-compatible primitives."""
        return {
            "id": self.id,
            "category": self.category.value,
            "content": self.content,
            "source": self.source.value,
            "trigger": self.trigger,
            "permission": self.permission.value,
            "weight": self.weight,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "last_accessed_ts": self.last_accessed_ts,
            "retention_half_life_days": self.retention_half_life_days,
            "vector_id": self.vector_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryRecord:
        """Deserialize and validate a record, rejecting unknown/missing fields."""
        if not isinstance(data, Mapping):
            raise ValueError("memory record data must be a mapping")
        field_names = {field.name for field in fields(cls)}
        unknown = set(data) - field_names
        if unknown:
            raise ValueError(f"unknown MemoryRecord fields: {sorted(unknown)}")
        required = field_names - {"vector_id"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing MemoryRecord fields: {sorted(missing)}")
        return cls(**dict(data))


def validate_memory_record(record: MemoryRecord) -> None:
    """Validate that ``record`` is a fully valid MemoryRecord instance."""
    if not isinstance(record, MemoryRecord):
        raise TypeError("record must be a MemoryRecord")
    # Reconstructing exercises all invariants even if an instance was created
    # through low-level means that bypassed ``__post_init__``.
    MemoryRecord.from_dict(record.to_dict())


__all__ = [
    "CATEGORY_DEFAULTS",
    "MemoryCategory",
    "MemoryRecord",
    "MemorySource",
    "WritePolicy",
    "validate_memory_record",
]
