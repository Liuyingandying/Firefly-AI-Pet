"""Offline compatibility tests for the legacy local Memory Layer API."""

from __future__ import annotations

from memory.mem0_adapter import Hit
from memory.mem0_client import LocalMem0Client
from memory.memory_manager import MemoryManager


class FakeAdapter:
    def __init__(self, storage_dir, user_id: str) -> None:
        self.storage_dir = storage_dir
        self.user_id = user_id
        self._entries: dict[str, tuple[str, dict]] = {}

    def add(self, content: str, metadata: dict | None = None) -> str:
        vector_id = f"vector-{len(self._entries) + 1}"
        self._entries[vector_id] = (content, dict(metadata or {}))
        return vector_id

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        return [
            Hit(vector_id, content, metadata, 1.0)
            for vector_id, (content, metadata) in self._entries.items()
        ][:limit]


def test_add_search_and_context_recall(tmp_path) -> None:
    user_id = "firefly-memory-test-user"
    adapter = FakeAdapter(tmp_path / "memory-store", user_id)
    manager = MemoryManager(
        client=LocalMem0Client(adapter=adapter),
    )
    content = "用户正在开发 Firefly AI Pet，希望它成为长期记忆型 AI Companion"

    added = manager.add_memory(content, {"source": "phase-1-test"})
    results = manager.search_memory("用户最近在做什么项目？")
    context = manager.get_memory_context("用户最近在做什么项目？")

    assert added.get("results")
    assert results
    assert any("Firefly AI Pet" in item.get("memory", "") for item in results)
    assert "Firefly AI Pet" in context
