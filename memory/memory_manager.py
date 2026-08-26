"""Stable, application-facing API for Firefly's local Memory Layer."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

from .mem0_client import LegacyClientSemanticAdapter, LocalMem0Client
from .records import MemoryCategory, MemoryRecord
from .repository import JsonMemoryRepository, MemoryRepository
from .service import MemoryService


class MemoryManager:
    """Deprecated v0.2 facade forwarding writes through MemoryService."""

    def __init__(
        self,
        storage_dir: str | Path | None = None,
        *,
        user_id: str | None = None,
        client: LocalMem0Client | None = None,
        repository: MemoryRepository | None = None,
        service: MemoryService | None = None,
    ) -> None:
        self._legacy_adapter: LegacyClientSemanticAdapter | None = None
        if service is not None:
            self.service = service
        else:
            if repository is None:
                repository_path = (
                    Path(storage_dir) / "memory_records.json"
                    if storage_dir is not None
                    else None
                )
                repository = JsonMemoryRepository(repository_path)
            if client is not None:
                adapter = LegacyClientSemanticAdapter(client)
                self._legacy_adapter = adapter
                self.service = MemoryService(repository, adapter)
            else:
                self.service = MemoryService.local(
                    repository,
                    str(storage_dir) if storage_dir is not None else None,
                    user_id=user_id,
                )

    def add_memory(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        metadata = dict(metadata or {})
        category = metadata.get("category", MemoryCategory.USER_FACT)
        record = self.service.remember(
            content,
            category=category,
            trigger="legacy-add-memory",
            asserted_explicit=True,
        )
        if record is None:  # asserted_explicit guarantees a record
            return {"results": []}
        return {"results": [self._legacy_result(record)]}

    def search_memory(self, query: str) -> list[dict[str, Any]]:
        records = self.service.search(query)
        if records:
            return [self._legacy_result(record) for record in records]
        # Read-only compatibility for tests/callers that inject a pre-populated
        # v0.2 client without authoritative records. New writes never use this.
        if self._legacy_adapter is not None:
            return list(self._legacy_adapter.last_raw_results)
        return []

    @staticmethod
    def _legacy_result(record: MemoryRecord) -> dict[str, Any]:
        return {
            "id": record.vector_id,
            "memory": record.content,
            "metadata": {
                "record_id": record.id,
                "category": record.category.value,
                "source": record.source.value,
            },
        }

    def get_memory_context(self, query: str) -> str:
        memories = self.search_memory(query)
        lines = [
            str(item.get("memory") or item.get("text") or "").strip()
            for item in memories
        ]
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return "Relevant memories:\n" + "\n".join(f"- {line}" for line in lines)


_default_manager: MemoryManager | None = None
_default_manager_lock = Lock()


def _manager() -> MemoryManager:
    global _default_manager
    if _default_manager is None:
        with _default_manager_lock:
            if _default_manager is None:
                _default_manager = MemoryManager()
    return _default_manager


def add_memory(
    content: str, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Persist explicit memory through the authoritative MemoryService."""
    return _manager().add_memory(content, metadata)


def search_memory(query: str) -> list[dict[str, Any]]:
    """Return memories relevant to ``query`` from the local store."""
    return _manager().search_memory(query)


def get_memory_context(query: str) -> str:
    """Return relevant memories formatted for later prompt injection."""
    return _manager().get_memory_context(query)
