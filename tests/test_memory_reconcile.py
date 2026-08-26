"""Memory reconciliation tests."""

from __future__ import annotations

from pathlib import Path

from memory.mem0_adapter import Hit
from memory.records import MemoryRecord
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


class FakeIndex:
    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.add_calls = 0
        self.delete_calls: list[str] = []

    def add(self, text, metadata=None):
        self.add_calls += 1
        vid = f"vec-{len(self.entries) + 1}"
        self.entries[vid] = {"text": text, "metadata": dict(metadata or {})}
        return vid

    def search(self, query, *, limit=5, threshold=0.0):
        return []

    def delete(self, vector_id):
        self.delete_calls.append(vector_id)
        return self.entries.pop(vector_id, None) is not None

    def list_entries(self):
        return [
            Hit(vid, e["text"], dict(e["metadata"]), 1.0)
            for vid, e in self.entries.items()
        ]


def _service(tmp_path: Path) -> tuple[MemoryService, JsonMemoryRepository, FakeIndex]:
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeIndex()
    return MemoryService(repo, adapter), repo, adapter


def test_reconcile_repairs_missing_index(tmp_path: Path) -> None:
    service, repo, adapter = _service(tmp_path)

    record = MemoryRecord.create(
        category="user_fact", content="缺失索引的记录", source="explicit", trigger="t"
    )
    repo.add(record)  # vector_id stays None

    result = service.reconcile(dry_run=False)

    assert result.missing_indexes == (record.id,)
    assert adapter.add_calls == 1
    assert repo.get(record.id).vector_id is not None


def test_reconcile_removes_orphan(tmp_path: Path) -> None:
    service, repo, adapter = _service(tmp_path)

    written = service.remember_detailed("我喜欢猫", asserted_explicit=True)
    vector_id = written.record.vector_id
    repo.delete(written.record.id)  # leave the mem0 entry orphaned

    result = service.reconcile(dry_run=False)

    assert vector_id in result.orphan_indexes
    assert vector_id not in adapter.entries


def test_reconcile_dry_run_does_not_modify(tmp_path: Path) -> None:
    service, repo, adapter = _service(tmp_path)

    record = MemoryRecord.create(
        category="user_fact", content="缺失索引", source="explicit", trigger="t"
    )
    repo.add(record)

    result = service.reconcile(dry_run=True)

    assert result.missing_indexes == (record.id,)
    assert adapter.add_calls == 0
    assert repo.get(record.id).vector_id is None


def test_reconcile_idempotent(tmp_path: Path) -> None:
    service, repo, _ = _service(tmp_path)

    record = MemoryRecord.create(
        category="user_fact", content="缺失索引", source="explicit", trigger="t"
    )
    repo.add(record)

    service.reconcile(dry_run=False)
    second = service.reconcile(dry_run=False)

    assert second.missing_indexes == ()
    assert second.orphan_indexes == ()
    assert second.healthy == 1


def test_reconcile_clean_state_noop(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    service.remember_detailed("我喜欢猫", asserted_explicit=True)

    result = service.reconcile(dry_run=True)

    assert result.missing_indexes == ()
    assert result.orphan_indexes == ()
    assert result.healthy == 1
