"""Offline tests for the authoritative JSON MemoryRepository."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memory.records import MemoryCategory, MemoryRecord, MemorySource
from memory.repository import (
    DuplicateMemoryRecordError,
    JsonMemoryRepository,
    STORE_VERSION,
)


def make_record(
    content: str,
    *,
    category: str = "user_fact",
    source: str = "explicit",
    timestamp_ms: int = 1_800_000_000_000,
) -> MemoryRecord:
    return MemoryRecord.create(
        category=category,
        content=content,
        source=source,
        trigger="test",
        timestamp_ms=timestamp_ms,
    )


def test_crud_and_all_ids(tmp_path: Path) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    original = make_record("用户住在上海")

    assert repository.add(original) == original.id
    assert repository.all_ids() == {original.id}
    accessed = repository.get(original.id)
    assert accessed is not None
    assert accessed.content == original.content
    assert accessed.last_accessed_ts > original.last_accessed_ts

    updated = repository.update(
        original.id,
        {"content": "用户住在苏州", "weight": 1.1, "unknown": "ignored"},
    )
    assert updated.content == "用户住在苏州"
    assert updated.weight == 1.1
    assert updated.updated_ts > original.updated_ts
    assert "unknown" not in updated.to_dict()

    assert repository.delete(original.id) is True
    assert repository.delete(original.id) is False
    assert repository.get(original.id) is None


def test_duplicate_id_is_rejected(tmp_path: Path) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    record = make_record("唯一记录")
    repository.add(record)

    with pytest.raises(DuplicateMemoryRecordError):
        repository.add(record)


def test_list_filters_and_orders_records(tmp_path: Path) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    oldest = make_record("旧事实", timestamp_ms=1_000)
    preference = make_record(
        "偏好 Markdown",
        category="preference",
        source="suggested",
        timestamp_ms=2_000,
    )
    newest = make_record("新事实", timestamp_ms=3_000)
    for record in (oldest, preference, newest):
        repository.add(record)

    assert repository.list() == [newest, preference, oldest]
    assert repository.list(category=MemoryCategory.USER_FACT) == [newest, oldest]
    assert repository.list(source=MemorySource.SUGGESTED) == [preference]
    assert repository.list(before_ts=3_000) == [preference, oldest]


def test_clear_returns_removed_count(tmp_path: Path) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    repository.add(make_record("一", timestamp_ms=1_000))
    repository.add(make_record("二", timestamp_ms=2_000))

    assert repository.clear() == 2
    assert repository.clear() == 0
    assert repository.list() == []


def test_restart_reads_persisted_records(tmp_path: Path) -> None:
    path = tmp_path / "companion" / "memory_records.json"
    first = JsonMemoryRepository(path)
    record = make_record("跨重启保留", category="project")
    first.add(record)

    restarted = JsonMemoryRepository(path)

    assert restarted.list() == [record]
    assert restarted.all_ids() == {record.id}


def test_export_import_round_trip_skips_duplicates_and_bad_records(
    tmp_path: Path,
) -> None:
    source = JsonMemoryRepository(tmp_path / "source.json")
    first = make_record("事实", timestamp_ms=1_000)
    second = make_record("偏好", category="preference", timestamp_ms=2_000)
    source.add(first)
    source.add(second)
    payload = source.export()
    payload["records"].append({"id": "broken"})

    target = JsonMemoryRepository(tmp_path / "target.json")
    assert target.import_data(payload) == 2
    assert target.import_records(payload) == 0
    assert getattr(target, "import")(payload) == 0
    assert target.export() == source.export()


@pytest.mark.parametrize(
    "content",
    ["not-json", "[]", '{"version": 1, "records": "bad"}'],
)
def test_corrupt_or_invalid_file_degrades_to_empty(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "memory_records.json"
    path.write_text(content, encoding="utf-8")

    assert JsonMemoryRepository(path).list() == []


def test_single_corrupt_record_does_not_discard_valid_records(tmp_path: Path) -> None:
    path = tmp_path / "memory_records.json"
    valid = make_record("有效记录")
    path.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "records": [valid.to_dict(), {"id": "broken"}, 42],
            }
        ),
        encoding="utf-8",
    )

    assert JsonMemoryRepository(path).list() == [valid]


def test_unknown_future_version_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "memory_records.json"
    path.write_text(
        json.dumps(
            {"version": STORE_VERSION + 1, "records": [make_record("隐藏").to_dict()]}
        ),
        encoding="utf-8",
    )

    assert JsonMemoryRepository(path).list() == []


def test_failed_atomic_replace_preserves_file_and_in_memory_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory_records.json"
    repository = JsonMemoryRepository(path)
    original = make_record("已保存", timestamp_ms=1_000)
    pending = make_record("不能保存", timestamp_ms=2_000)
    repository.add(original)
    original_bytes = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("memory.repository.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        repository.add(pending)

    assert path.read_bytes() == original_bytes
    assert repository.all_ids() == {original.id}
    assert not path.with_name(path.name + ".tmp").exists()


def test_concurrent_adds_do_not_lose_records(tmp_path: Path) -> None:
    path = tmp_path / "memory_records.json"
    repository = JsonMemoryRepository(path)
    records = [
        make_record(f"并发记录 {index}", timestamp_ms=1_000 + index)
        for index in range(16)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(repository.add, records))

    assert set(ids) == {record.id for record in records}
    assert JsonMemoryRepository(path).all_ids() == set(ids)
