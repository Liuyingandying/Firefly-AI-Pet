"""Deprecated compatibility shim for the v0.2 local Mem0 client API.

New code should import :class:`memory.mem0_adapter.Mem0Adapter`. This module
contains no Mem0 integration of its own and remains until P0.5 migrates
``memory_manager.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .mem0_adapter import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_DIMS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MODEL_CACHE_DIR,
    DEFAULT_STORAGE_DIR,
    DEFAULT_USER_ID,
    Hit,
    Mem0Adapter,
    Mem0AdapterError,
    MemoryDependencyError,
)


class LocalMem0Client:
    """Compatibility wrapper preserving v0.2 add/search result shapes."""

    def __init__(
        self,
        storage_dir: str | Path | None = None,
        *,
        user_id: str = DEFAULT_USER_ID,
        adapter: Mem0Adapter | None = None,
    ) -> None:
        self._adapter = adapter or Mem0Adapter(storage_dir, user_id=user_id)
        self.storage_dir = self._adapter.storage_dir
        self.user_id = self._adapter.user_id

    def add(self, content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        vector_id = self._adapter.add(content, metadata)
        return {"results": [{"id": vector_id, "memory": content.strip()}]}

    def search(self, query: str, *, limit: int = 5, threshold: float = 0.0) -> list[dict[str, Any]]:
        return [
            {
                "id": hit.vector_id,
                "memory": hit.text,
                "metadata": dict(hit.metadata),
                "score": hit.score,
            }
            for hit in self._adapter.search(query, limit=limit)
        ]

    def delete(self, vector_id: str) -> bool:
        return self._adapter.delete(vector_id)

    def clear(self) -> int:
        return self._adapter.clear()

    def health(self) -> bool:
        return self._adapter.health()


class LegacyClientSemanticAdapter:
    """Adapt a v0.2 client result shape to the semantic index contract."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.last_raw_results: list[dict[str, Any]] = []

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str:
        response = self.client.add(text, dict(metadata or {}))
        items = response.get("results", []) if isinstance(response, dict) else []
        for item in items:
            if isinstance(item, dict):
                value = item.get("id") or item.get("vector_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        record_id = dict(metadata or {}).get("record_id")
        if isinstance(record_id, str) and record_id:
            return f"legacy-index-{record_id}"
        raise RuntimeError("legacy semantic client returned no vector ID")

    def search(self, query: str, *, limit: int = 5, threshold: float = 0.0) -> list[Hit]:
        raw = self.client.search(query)
        self.last_raw_results = [item for item in raw if isinstance(item, dict)]
        hits: list[Hit] = []
        for item in self.last_raw_results[:limit]:
            text = item.get("memory") or item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            metadata = item.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            vector_id = item.get("id") or item.get("vector_id")
            if not isinstance(vector_id, str) or not vector_id.strip():
                record_id = metadata.get("record_id")
                vector_id = (
                    f"legacy-index-{record_id}"
                    if isinstance(record_id, str) and record_id
                    else "legacy-unlinked"
                )
            score = item.get("score", 0.0)
            score = float(score) if isinstance(score, (int, float)) else 0.0
            hits.append(Hit(vector_id, text.strip(), metadata, score))
        return hits

    def delete(self, vector_id: str) -> bool:
        delete = getattr(self.client, "delete", None)
        return bool(delete(vector_id)) if callable(delete) else False


__all__ = [
    "Hit",
    "LegacyClientSemanticAdapter",
    "LocalMem0Client",
    "Mem0Adapter",
    "Mem0AdapterError",
    "MemoryDependencyError",
]
