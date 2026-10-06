"""Isolated 37-record / one-point repair regression; no Mem0 or provider.

The FakeIndex follows test_memory_m1_consistency's in-memory SemanticIndex.
Reconcile's returned operation plan may describe the pre-repair state; each
success assertion therefore uses a fresh consistency_report.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest

from memory.access_mode import MemoryAccessMode, MemoryAccessViolation
from memory.mem0_adapter import Hit, Mem0Adapter, Mem0AdapterError
from memory.records import MemoryRecord
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


class FakeIndex:
    def __init__(self):
        self.entries = {}
        self.add_calls = []
        self.delete_calls = []
        self.fail_next_add = False

    def add(self, text, metadata=None):
        if self.fail_next_add:
            self.fail_next_add = False
            raise Mem0AdapterError("synthetic index interruption")
        vector_id = f"vec-{len(self.entries) + 1}"
        metadata = dict(metadata or {})
        self.entries[vector_id] = {"text": text, "metadata": metadata}
        self.add_calls.append((text, dict(metadata)))
        return vector_id

    def search(self, query, *, limit=5, threshold=0.0):
        return self.list_entries(limit=limit)

    def delete(self, vector_id):
        self.delete_calls.append(vector_id)
        return self.entries.pop(vector_id, None) is not None

    def list_entries(self, *, limit=1000):
        return [Hit(vid, entry["text"], dict(entry["metadata"]), 1.0)
                for vid, entry in self.entries.items()][:limit]


@pytest.fixture(autouse=True)
def forbid_real_semantic_backend(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("This regression must use FakeIndex, never Mem0")
    monkeypatch.setattr(Mem0Adapter, "__init__", forbidden)


def synthetic_graph(tmp_path, mode=MemoryAccessMode.CONFIRMED_WRITE):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    index = FakeIndex()
    active = []
    for number in range(7):
        record = MemoryRecord.create(
            category="preference" if number == 3 else "project",
            content=f"Synthetic active memory {number}",
            source="explicit", trigger="synthetic-test",
            timestamp_ms=1_700_000_000_100 + number,
            vector_id=None if number >= 5 else f"legacy-active-{number}",
        )
        active.append(record)
    # The one existing point belongs to a new active record. The remaining
    # old links are stale, while the two newest records have no vector link.
    seeded = active[4]
    seeded_vector = index.add(seeded.content, {"record_id": seeded.id})
    active[4] = replace(seeded, vector_id=seeded_vector)
    for record in active:
        repo.add(record, access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    for number in range(30):
        record = MemoryRecord.create(
            category="project", content=active[0].content,
            source="explicit", trigger="synthetic-history",
            timestamp_ms=1_700_000_000_000 + number,
            vector_id=f"legacy-history-{number}",
        )
        record = replace(record, lifecycle_status="superseded",
                         superseded_by=active[0].id,
                         supersede_reason="confirmed_conflict")
        repo.add(record, access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    service = MemoryService(repo, index, access_mode=mode)
    return service, repo, index, {record.id for record in active}


def fact_snapshot(repo):
    # Derived vector maintenance advances updated_ts through the original
    # repository API; every factual and lifecycle field must remain unchanged.
    return {record.id: {key: value for key, value in record.to_dict().items()
                        if key not in {"vector_id", "updated_ts"}}
            for record in repo.list()}


def assert_complete(service, repo, index, active_ids, before_facts):
    report = service.consistency_report()
    assert report.repository_records == report.indexed_records == report.healthy == 37
    assert report.missing_vectors == report.stale_vector_links == report.orphan_vectors == ()
    assert fact_snapshot(repo) == before_facts
    assert len(repo.list()) == 37
    assert sum(record.lifecycle_status == "superseded" for record in repo.list()) == 30
    assert {record.id for record in repo.list() if record.lifecycle_status == "active"} == active_ids
    assert all(repo.get(record_id).vector_id in index.entries for record_id in active_ids)
    assert Counter(entry["metadata"]["record_id"] for entry in index.entries.values()) == {
        record.id: 1 for record in repo.list()}
    assert index.delete_calls == []
    # Historical points remain indexed but must not become recalled facts.
    assert {record.id for record in service.search("synthetic query", limit=37)} == active_ids


def test_dry_run_reports_36_missing_without_writing_any_owner(tmp_path):
    service, repo, index, _ = synthetic_graph(tmp_path)
    file_before = repo.path.read_bytes()
    entries_before = deepcopy(index.entries)
    records_before = [record.to_dict() for record in repo.list()]
    report = service.reconcile(dry_run=True)
    assert len(report.missing_indexes) == 36 and report.healthy == 1
    assert report.orphan_indexes == report.stale_links == ()
    assert repo.path.read_bytes() == file_before
    assert [record.to_dict() for record in repo.list()] == records_before
    assert index.entries == entries_before and len(index.add_calls) == 1
    assert index.delete_calls == []


def test_confirmed_repair_preserves_history_and_is_idempotent_on_restart(tmp_path):
    service, repo, index, active_ids = synthetic_graph(tmp_path)
    before_facts = fact_snapshot(repo)
    service.reconcile(dry_run=False)
    assert_complete(service, repo, index, active_ids, before_facts)
    assert len(index.add_calls) == 37
    # Restart only the authoritative repository/service; this in-memory index
    # is the same derived owner. No production path or persistent backend opens.
    reopened_repo = JsonMemoryRepository(repo.path)
    reopened = MemoryService(reopened_repo, index,
                             access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    bytes_after = reopened_repo.path.read_bytes()
    entries_after = deepcopy(index.entries)
    rerun = reopened.reconcile(dry_run=False)
    assert rerun.missing_indexes == rerun.orphan_indexes == rerun.stale_links == ()
    assert reopened_repo.path.read_bytes() == bytes_after
    assert index.entries == entries_after and len(index.add_calls) == 37
    assert_complete(reopened, reopened_repo, index, active_ids, before_facts)


@pytest.mark.parametrize("mode", [MemoryAccessMode.READ_ONLY, MemoryAccessMode.SAFE_WRITE])
def test_repair_requires_confirmed_write_without_partial_mutation(tmp_path, mode):
    service, repo, index, _ = synthetic_graph(tmp_path, mode)
    before_bytes = repo.path.read_bytes()
    before_entries = deepcopy(index.entries)
    with pytest.raises(MemoryAccessViolation):
        service.reconcile(dry_run=False)
    assert repo.path.read_bytes() == before_bytes
    assert index.entries == before_entries and len(index.add_calls) == 1
    assert index.delete_calls == []


@pytest.mark.parametrize("failure", ["index_add", "record_link"])
def test_interrupted_repair_retries_by_record_id_without_duplicate_points(
        tmp_path, monkeypatch, failure):
    service, repo, index, active_ids = synthetic_graph(tmp_path)
    before_facts = fact_snapshot(repo)
    if failure == "index_add":
        original_add = index.add

        def add_once_interrupted(text, metadata=None):
            if len(index.add_calls) == 3:
                index.fail_next_add = True
            return original_add(text, metadata)
        monkeypatch.setattr(index, "add", add_once_interrupted)
        with pytest.raises(Mem0AdapterError):
            service.reconcile(dry_run=False)
        monkeypatch.setattr(index, "add", original_add)
    else:
        original_update = repo.update

        def link_once_interrupted(record_id, patch, **kwargs):
            if "vector_id" in patch and len(index.add_calls) == 4:
                raise OSError("synthetic link persistence interruption")
            return original_update(record_id, patch, **kwargs)
        monkeypatch.setattr(repo, "update", link_once_interrupted)
        with pytest.raises(OSError):
            service.reconcile(dry_run=False)
        monkeypatch.setattr(repo, "update", original_update)
        # One point was persisted before its authoritative derived link failed.
        assert len(service.reconcile(dry_run=True).stale_links) == 1
    assert fact_snapshot(repo) == before_facts and len(repo.list()) == 37
    assert 1 < len(index.entries) < 37
    service.reconcile(dry_run=False)
    assert_complete(service, repo, index, active_ids, before_facts)
    assert len(index.add_calls) == 37
