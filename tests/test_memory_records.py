"""Offline unit tests for the v0.3 MemoryRecord foundation."""

from __future__ import annotations

import json

import pytest

from memory.records import (
    CATEGORY_DEFAULTS,
    MemoryCategory,
    MemoryRecord,
    MemorySource,
    WritePolicy,
    validate_memory_record,
)


def test_create_applies_category_defaults_and_normalizes_content() -> None:
    record = MemoryRecord.create(
        category=MemoryCategory.RELATIONSHIP,
        content="  用户信任流萤  ",
        trigger="turn-42",
        timestamp_ms=1_800_000_000_000,
    )

    assert record.content == "用户信任流萤"
    assert record.source is MemorySource.EXPLICIT
    assert record.permission is WritePolicy.EXPLICIT_ONLY
    assert record.weight == 1.5
    assert record.retention_half_life_days == 90.0
    assert record.created_ts == record.updated_ts == record.last_accessed_ts
    validate_memory_record(record)


@pytest.mark.parametrize("category", list(MemoryCategory))
def test_every_category_default_is_applied(category: MemoryCategory) -> None:
    record = MemoryRecord.create(
        category=category,
        content="有效记忆",
        trigger="explicit-command",
    )

    assert record.weight == CATEGORY_DEFAULTS[category]["weight"]
    assert (
        record.retention_half_life_days
        == CATEGORY_DEFAULTS[category]["half_life_days"]
    )


def test_generated_ids_are_unique_and_sort_in_creation_order() -> None:
    records = [
        MemoryRecord.create(
            category="project",
            content=f"项目记忆 {index}",
            trigger="turn-1",
            timestamp_ms=1_800_000_000_000,
        )
        for index in range(20)
    ]
    ids = [record.id for record in records]

    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_serialization_round_trip_is_json_compatible() -> None:
    original = MemoryRecord.create(
        category="preference",
        content="偏好 Markdown",
        source="suggested",
        trigger="turn-8",
        permission="ask",
        vector_id="vector-123",
    )

    payload = json.loads(json.dumps(original.to_dict(), ensure_ascii=False))
    restored = MemoryRecord.from_dict(payload)

    assert restored == original
    assert payload["category"] == "preference"
    assert payload["source"] == "suggested"
    assert payload["permission"] == "ask"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "relationship_state"),
        ("source", "automatic"),
        ("permission", "always"),
    ],
)
def test_invalid_enum_values_are_rejected(field: str, value: str) -> None:
    kwargs = {
        "category": "emotion",
        "content": "今天很开心",
        "trigger": "turn-2",
        field: value,
    }

    with pytest.raises(ValueError, match=field):
        MemoryRecord.create(**kwargs)


def test_blank_content_and_trigger_are_rejected() -> None:
    with pytest.raises(ValueError, match="content"):
        MemoryRecord.create(category="emotion", content=" \t ", trigger="turn-2")
    with pytest.raises(ValueError, match="trigger"):
        MemoryRecord.create(category="emotion", content="有效", trigger="  ")


@pytest.mark.parametrize("weight", [-0.01, 2.01, float("nan"), float("inf")])
def test_invalid_weight_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError, match="weight"):
        MemoryRecord.create(
            category="user_fact",
            content="有效",
            trigger="turn-3",
            weight=weight,
        )


@pytest.mark.parametrize("field", ["updated_ts", "last_accessed_ts"])
def test_timestamp_invariants_are_enforced(field: str) -> None:
    payload = MemoryRecord.create(
        category="shared_experience",
        content="一起完成了测试",
        trigger="turn-4",
        timestamp_ms=2_000,
    ).to_dict()
    payload[field] = 1_999

    with pytest.raises(ValueError, match=field):
        MemoryRecord.from_dict(payload)


def test_deserialization_rejects_unknown_and_missing_fields() -> None:
    payload = MemoryRecord.create(
        category="project", content="Firefly v0.3", trigger="turn-5"
    ).to_dict()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unknown"):
        MemoryRecord.from_dict(payload)

    payload.pop("unexpected")
    payload.pop("content")
    with pytest.raises(ValueError, match="missing"):
        MemoryRecord.from_dict(payload)
