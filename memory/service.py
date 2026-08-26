"""Authoritative orchestration for explicit Firefly memory operations."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .mem0_adapter import Hit, Mem0Adapter
from .records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from .repository import MemoryRepository
from .write_guards import authorize_explicit_write, infer_category


class SemanticIndex(Protocol):
    """Narrow vector operations consumed by MemoryService."""

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str: ...

    def search(self, query: str, *, limit: int = 5) -> list[Hit]: ...

    def delete(self, vector_id: str) -> bool: ...


class MemorySynchronizationError(RuntimeError):
    """Raised when the semantic index cannot be synchronized with truth."""

    def __init__(self, message: str, record: MemoryRecord) -> None:
        super().__init__(message)
        self.record = record


class MemoryService:
    """The sole production write path for authoritative MemoryRecords."""

    def __init__(
        self,
        repository: MemoryRepository,
        adapter: SemanticIndex | Mem0Adapter,
        *,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.write_policy = WritePolicy(write_policy)

    @classmethod
    def local(
        cls,
        repository: MemoryRepository,
        storage_dir: str | None = None,
        *,
        user_id: str | None = None,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
    ) -> MemoryService:
        """Construct the standard local semantic adapter behind this service."""
        adapter = (
            Mem0Adapter(storage_dir)
            if user_id is None
            else Mem0Adapter(storage_dir, user_id=user_id)
        )
        return cls(repository, adapter, write_policy=write_policy)

    def remember(
        self,
        user_input: str,
        *,
        category: MemoryCategory | str | None = None,
        trigger: str = "explicit-command",
        permission: WritePolicy | str | None = None,
        asserted_explicit: bool = False,
    ) -> MemoryRecord | None:
        """Persist an explicit request; ordinary chat returns None unchanged.

        Repository is written first. If vector indexing fails, the authoritative
        record remains readable with ``vector_id=None`` and the error carries
        that record for retry/reconciliation.
        """
        requested_policy = self.write_policy if permission is None else WritePolicy(permission)
        # A call-site snapshot may further restrict a configured service, but
        # it must never override a globally disabled write policy.
        effective_policy = (
            WritePolicy.OFF
            if self.write_policy is WritePolicy.OFF
            else requested_policy
        )
        content = authorize_explicit_write(
            user_input,
            effective_policy,
            asserted_explicit=asserted_explicit,
        )
        if content is None:
            return None
        effective_category = (
            infer_category(content) if category is None else MemoryCategory(category)
        )
        record = MemoryRecord.create(
            category=effective_category,
            content=content,
            source=MemorySource.EXPLICIT,
            trigger=trigger,
            permission=effective_policy,
        )
        self.repository.add(record)
        try:
            vector_id = self.adapter.add(content, {"record_id": record.id})
        except Exception as exc:
            raise MemorySynchronizationError(
                "memory is authoritative but semantic indexing failed", record
            ) from exc
        try:
            return self.repository.update(record.id, {"vector_id": vector_id})
        except Exception as exc:
            raise MemorySynchronizationError(
                "semantic index exists but vector ID could not be recorded", record
            ) from exc

    def import_migrated(
        self,
        content: str,
        *,
        category: MemoryCategory | str,
        trigger: str,
    ) -> MemoryRecord:
        """Persist one reviewed migration item through the authoritative service.

        Unlike :meth:`remember`, this trusted path does not pretend historical
        content was a new explicit command. Callers must supply an auditable
        ``migration:<run_id>`` trigger.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("migration content must be a non-empty string")
        if not isinstance(trigger, str) or not trigger.startswith("migration:"):
            raise ValueError("migration trigger must start with 'migration:'")
        normalized_content = content.strip()
        record = MemoryRecord.create(
            category=category,
            content=normalized_content,
            source=MemorySource.MIGRATED,
            trigger=trigger,
            permission=WritePolicy.AUTO,
        )
        self.repository.add(record)
        try:
            vector_id = self.adapter.add(
                normalized_content, {"record_id": record.id}
            )
        except Exception as exc:
            raise MemorySynchronizationError(
                "migrated memory is authoritative but semantic indexing failed",
                record,
            ) from exc
        try:
            return self.repository.update(record.id, {"vector_id": vector_id})
        except Exception as exc:
            raise MemorySynchronizationError(
                "migrated semantic index exists but vector ID could not be recorded",
                record,
            ) from exc

    def search(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        """Resolve semantic hits back to authoritative MemoryRecords."""
        hits = self.adapter.search(query, limit=limit)
        by_vector_id = {
            record.vector_id: record.id
            for record in self.repository.list()
            if record.vector_id is not None
        }
        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for hit in hits:
            record_id = hit.metadata.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                record_id = by_vector_id.get(hit.vector_id)
            if not isinstance(record_id, str) or record_id in seen:
                continue
            record = self.repository.get(record_id)
            if record is None:
                continue
            records.append(record)
            seen.add(record.id)
        return records

    def delete(self, record_id: str) -> bool:
        """Delete semantic index entry first, then authoritative record."""
        record = next(
            (item for item in self.repository.list() if item.id == record_id),
            None,
        )
        if record is None:
            return False
        if record.vector_id is not None:
            try:
                self.adapter.delete(record.vector_id)
            except Exception as exc:
                raise MemorySynchronizationError(
                    "semantic index deletion failed; authoritative record retained",
                    record,
                ) from exc
        return self.repository.delete(record.id)

    def list(
        self,
        *,
        category: MemoryCategory | str | None = None,
        source: MemorySource | str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        return self.repository.list(
            category=category,
            source=source,
            before_ts=before_ts,
        )


__all__ = [
    "MemoryService",
    "MemorySynchronizationError",
    "SemanticIndex",
]
