"""Authoritative orchestration for explicit Firefly memory operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from .mem0_adapter import Hit, Mem0Adapter
from .records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from .repository import MemoryRepository
from .security_guard import (
    MemorySecurityGuard,
    MemorySecurityViolation,
    NoopMemorySecurityGuard,
)
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


class WriteOutcome(str, Enum):
    CREATED = "created"
    EXACT_DUPLICATE = "exact_duplicate"
    SEMANTIC_DUPLICATE = "semantic_duplicate"


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: WriteOutcome
    record: MemoryRecord


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    missing_indexes: tuple[str, ...]
    orphan_indexes: tuple[str, ...]
    healthy: int


class MemoryService:
    """The sole production write path for authoritative MemoryRecords."""

    def __init__(
        self,
        repository: MemoryRepository,
        adapter: SemanticIndex | Mem0Adapter,
        *,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
        search_top_k: int = 5,
        security_guard: MemorySecurityGuard | None = None,
        dedup_enabled: bool = True,
        dedup_similarity_threshold: float = 0.85,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.write_policy = WritePolicy(write_policy)
        self.search_top_k = search_top_k
        self.security_guard: MemorySecurityGuard = (
            security_guard if security_guard is not None else NoopMemorySecurityGuard()
        )
        self.dedup_enabled = dedup_enabled
        self.dedup_similarity_threshold = dedup_similarity_threshold

    @classmethod
    def local(
        cls,
        repository: MemoryRepository,
        storage_dir: str | None = None,
        *,
        user_id: str | None = None,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
        search_top_k: int = 5,
        security_guard: MemorySecurityGuard | None = None,
        dedup_enabled: bool = True,
        dedup_similarity_threshold: float = 0.85,
    ) -> MemoryService:
        """Construct the standard local semantic adapter behind this service."""
        adapter = (
            Mem0Adapter(storage_dir)
            if user_id is None
            else Mem0Adapter(storage_dir, user_id=user_id)
        )
        return cls(
            repository,
            adapter,
            write_policy=write_policy,
            search_top_k=search_top_k,
            security_guard=security_guard,
            dedup_enabled=dedup_enabled,
            dedup_similarity_threshold=dedup_similarity_threshold,
        )

    def remember(
        self,
        user_input: str,
        *,
        category: MemoryCategory | str | None = None,
        trigger: str = "explicit-command",
        permission: WritePolicy | str | None = None,
        asserted_explicit: bool = False,
    ) -> MemoryRecord | None:
        """Persist an explicit request; returns the stored record (existing on dedup)."""
        result = self.remember_detailed(
            user_input,
            category=category,
            trigger=trigger,
            permission=permission,
            asserted_explicit=asserted_explicit,
        )
        return result.record if result is not None else None

    def remember_detailed(
        self,
        user_input: str,
        *,
        category: MemoryCategory | str | None = None,
        trigger: str = "explicit-command",
        permission: WritePolicy | str | None = None,
        asserted_explicit: bool = False,
    ) -> WriteResult | None:
        """Run the full write pipeline and report the outcome explicitly.

        Ordinary chat returns None. Otherwise the outcome is CREATED,
        EXACT_DUPLICATE, or SEMANTIC_DUPLICATE. Deduplication never mutates the
        authoritative content and never writes repository or Mem0.
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
        content = self._apply_security_guard(content)
        effective_category = (
            infer_category(content) if category is None else MemoryCategory(category)
        )

        existing = self._find_exact_duplicate(content)
        if existing is not None:
            return WriteResult(WriteOutcome.EXACT_DUPLICATE, existing)

        existing = self._find_semantic_duplicate(content, effective_category)
        if existing is not None:
            return WriteResult(WriteOutcome.SEMANTIC_DUPLICATE, existing)

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
            updated = self.repository.update(record.id, {"vector_id": vector_id})
        except Exception as exc:
            raise MemorySynchronizationError(
                "semantic index exists but vector ID could not be recorded", record
            ) from exc
        return WriteResult(WriteOutcome.CREATED, updated)

    @staticmethod
    def _normalize_for_compare(content: str) -> str:
        """Comparison-only normalization; never mutates stored content."""
        return "".join(ch for ch in content.casefold() if ch.isalnum())

    def _find_exact_duplicate(self, content: str) -> MemoryRecord | None:
        normalized = self._normalize_for_compare(content)
        for record in self.repository.list():
            if self._normalize_for_compare(record.content) == normalized:
                return record
        return None

    def _find_semantic_duplicate(
        self, content: str, category: MemoryCategory
    ) -> MemoryRecord | None:
        if not self.dedup_enabled:
            return None
        if not self.repository.all_ids():
            return None
        try:
            hits = self.adapter.search(content, limit=self.search_top_k)
        except Exception:
            return None
        for hit in hits:
            if hit.score < self.dedup_similarity_threshold:
                continue
            record_id = hit.metadata.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                continue
            record = self.repository.get(record_id)
            if record is not None and record.category is category:
                return record
        return None

    def _apply_security_guard(self, content: str) -> str:
        """Run the optional second-layer guard after Firefly's write guards."""
        decision = self.security_guard.guard(content)
        if decision.action == "block":
            raise MemorySecurityViolation(
                decision.reason or "memory write blocked by security guard"
            )
        if decision.action == "redact" and decision.content is not None:
            redacted = decision.content.strip()
            if redacted:
                return redacted
        return content

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

    def search(self, query: str, *, limit: int | None = None) -> list[MemoryRecord]:
        """Resolve semantic hits back to authoritative MemoryRecords."""
        effective_limit = self.search_top_k if limit is None else limit
        hits = self.adapter.search(query, limit=effective_limit)
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

    def reconcile(self, *, dry_run: bool = True) -> ReconcileResult:
        """Repair divergence between repository (truth) and Mem0 (derived index).

        ``dry_run=True`` only reports. ``dry_run=False`` additionally re-indexes
        records missing a semantic index and deletes orphan index entries.
        """
        missing = [
            record for record in self.repository.list() if record.vector_id is None
        ]

        orphan_ids: list[str] = []
        list_entries = getattr(self.adapter, "list_entries", None)
        if callable(list_entries):
            try:
                entries = list_entries()
            except Exception:
                entries = []
            valid_ids = self.repository.all_ids()
            for entry in entries:
                record_id = entry.metadata.get("record_id")
                if isinstance(record_id, str) and record_id not in valid_ids:
                    orphan_ids.append(entry.vector_id)

        if not dry_run:
            for record in missing:
                vector_id = self.adapter.add(
                    record.content, {"record_id": record.id}
                )
                self.repository.update(record.id, {"vector_id": vector_id})
            for vector_id in orphan_ids:
                self.adapter.delete(vector_id)

        healthy = sum(
            1 for record in self.repository.list() if record.vector_id is not None
        )
        return ReconcileResult(
            missing_indexes=tuple(record.id for record in missing),
            orphan_indexes=tuple(orphan_ids),
            healthy=healthy,
        )


__all__ = [
    "MemoryService",
    "MemorySecurityViolation",
    "MemorySynchronizationError",
    "ReconcileResult",
    "SemanticIndex",
    "WriteOutcome",
    "WriteResult",
]
