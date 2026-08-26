"""Tests for MemoryCommitter."""

from __future__ import annotations

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    SourceRef,
)
from tools.firefly_memory_importer.committer.memory_committer import MemoryCommitter


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict = {}

    def add(self, record):
        self.records[record.id] = record
        return record.id

    def update(self, record_id, patch):
        record = self.records[record_id]
        self.records[record_id] = record.__class__(**{**record.to_dict(), **patch})
        return self.records[record_id]

    def delete(self, record_id):
        return self.records.pop(record_id, None) is not None

    def list(self):
        return list(self.records.values())


class FakeAdapter:
    def __init__(self) -> None:
        self.added = []
        self.deleted = []

    def add(self, text, metadata=None):
        self.added.append((text, metadata))
        return "vec-1"

    def delete(self, vector_id):
        self.deleted.append(vector_id)
        return True


def _memory(content="用户是医生", category=MemoryCategory.USER_FACT) -> MemoryCandidate:
    return MemoryCandidate(
        id="m-1",
        category=category,
        content=content,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(SourceRef("c1", 0, Role.USER),),
        rule="mem.user_fact.occupation",
        disposition=Disposition.AUTO_APPROVE,
    )


def test_dry_run_does_not_write() -> None:
    repo = FakeRepository()
    committer = MemoryCommitter(repo)

    result = committer.dry_run([_memory()], "run-1")

    assert len(result) == 1
    assert repo.records == {}
    assert result[0]["trigger"] == "migration:run-1"
    assert result[0]["source"] == "migrated"


def test_commit_writes_records_with_run_id() -> None:
    repo = FakeRepository()
    adapter = FakeAdapter()
    committer = MemoryCommitter(repo, adapter)

    ids = committer.commit([_memory()], "run-1")

    assert len(ids) == 1
    record = repo.records[ids[0]]
    assert record.trigger == "migration:run-1"
    assert record.source.value == "migrated"
    assert record.category.value == "user_fact"
    assert adapter.added  # vector written after index


def test_rollback_deletes_by_run_id() -> None:
    repo = FakeRepository()
    adapter = FakeAdapter()
    committer = MemoryCommitter(repo, adapter)

    ids = committer.commit([_memory()], "run-1")
    committer.commit([_memory(content="用户是老师")], "run-2")

    deleted = committer.rollback("run-1")

    assert deleted == ids
    assert len(repo.records) == 1  # run-2 record survives
    assert repo.records[list(repo.records)[0]].trigger == "migration:run-2"
