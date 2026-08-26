"""MemoryCommitter: write MemoryCandidate -> authoritative MemoryRecord.

Stage 3 commit component. Unlike the read-only analyzer, this module imports the
real ``memory`` package and writes through a trusted import path (repository
first, then vector). Every record is tagged with ``trigger = migration:<run_id>``
and ``source = migrated`` so it can be audited and rolled back by run_id.
"""

from __future__ import annotations

from typing import Any, Protocol

from memory.records import MemoryRecord, MemorySource, WritePolicy

from ..analyzer.candidates import MemoryCandidate


def _trigger(run_id: str) -> str:
    return f"migration:{run_id}"


class VectorStore(Protocol):
    """Narrow vector operations consumed by the committer."""

    def add(self, text: str, metadata: dict[str, Any] | None = None) -> str: ...

    def delete(self, vector_id: str) -> bool: ...


class RecordRepository(Protocol):
    """Narrow record operations consumed by the committer."""

    def add(self, record: MemoryRecord) -> str: ...

    def update(self, record_id: str, patch: dict[str, Any]) -> MemoryRecord: ...

    def delete(self, record_id: str) -> bool: ...

    def list(self) -> list[MemoryRecord]: ...


class MemoryCommitter:
    """Import accepted memory candidates as ``MemoryRecord`` entries."""

    def __init__(self, repository: RecordRepository, adapter: VectorStore | None = None) -> None:
        self.repository = repository
        self.adapter = adapter

    def build_record(self, category: str, content: str, run_id: str) -> MemoryRecord:
        return MemoryRecord.create(
            category=category,
            content=content,
            trigger=_trigger(run_id),
            source=MemorySource.MIGRATED,
            permission=WritePolicy.AUTO,
        )

    def dry_run(self, candidates: list[MemoryCandidate], run_id: str) -> list[dict[str, Any]]:
        """Return what would be written without touching the repository."""
        return [
            self.build_record(c.category.value, c.content, run_id).to_dict()
            for c in candidates
        ]

    def commit(self, candidates: list[MemoryCandidate], run_id: str) -> list[str]:
        """Write records (index first, then vector) and return their IDs."""
        record_ids: list[str] = []
        for candidate in candidates:
            record = self.build_record(candidate.category.value, candidate.content, run_id)
            self.repository.add(record)
            if self.adapter is not None:
                try:
                    vector_id = self.adapter.add(
                        record.content, {"record_id": record.id}
                    )
                except Exception:
                    vector_id = None
                if vector_id:
                    self.repository.update(record.id, {"vector_id": vector_id})
            record_ids.append(record.id)
        return record_ids

    def rollback(self, run_id: str) -> list[str]:
        """Delete every record tagged with this run_id and its vector."""
        trigger = _trigger(run_id)
        targets = [r for r in self.repository.list() if r.trigger == trigger]
        deleted: list[str] = []
        for record in targets:
            if self.adapter is not None and record.vector_id:
                try:
                    self.adapter.delete(record.vector_id)
                except Exception:
                    pass
            if self.repository.delete(record.id):
                deleted.append(record.id)
        return deleted


__all__ = ["MemoryCommitter"]
