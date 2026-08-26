"""Offline contract tests for the narrow Mem0 semantic index adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from memory.mem0_adapter import Hit, Mem0Adapter, Mem0AdapterError
from memory.records import MemoryRecord
from memory.repository import JsonMemoryRepository


class FakeVectorBackend:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.add_calls: list[dict[str, Any]] = []
        self.get_all_calls: list[dict[str, Any]] = []

    def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
        vector_id = f"vector-{len(self.entries) + 1}"
        self.add_calls.append({"text": text, **kwargs})
        self.entries[vector_id] = {
            "id": vector_id,
            "memory": text,
            "metadata": dict(kwargs.get("metadata", {})),
            "score": 0.5,
        }
        return {"results": [{"id": vector_id}]}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        results = list(self.entries.values())
        for index, item in enumerate(results):
            item["score"] = 0.1 + index
        return {"results": list(reversed(results))}

    def delete(self, *, memory_id: str) -> dict[str, bool]:
        return {"deleted": self.entries.pop(memory_id, None) is not None}

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        self.get_all_calls.append(kwargs)
        return {"results": list(self.entries.values())}

    def delete_all(self, **kwargs: Any) -> dict[str, bool]:
        self.entries.clear()
        return {"success": True}


def adapter_with_fake(tmp_path: Path) -> tuple[Mem0Adapter, FakeVectorBackend]:
    backend = FakeVectorBackend()
    adapter = Mem0Adapter(
        tmp_path / "vectors",
        user_id="test-user",
        backend_factory=lambda config: backend,
    )
    return adapter, backend


def test_adapter_is_lazy_and_installs_privacy_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    backend = FakeVectorBackend()

    def factory(config: dict[str, Any]) -> FakeVectorBackend:
        assert os.environ["MEM0_TELEMETRY"] == "false"
        assert os.environ["MEM0_DIR"] == str(tmp_path / "vectors" / "mem0-home")
        calls.append(config)
        return backend

    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    monkeypatch.delenv("MEM0_DIR", raising=False)
    adapter = Mem0Adapter(tmp_path / "vectors", backend_factory=factory)

    assert calls == []
    assert adapter.health() is True
    assert len(calls) == 1
    config = calls[0]
    assert config["llm"]["config"]["openai_base_url"] == "http://127.0.0.1:9/v1"
    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["on_disk"] is True
    assert config["embedder"]["provider"] == "fastembed"
    assert config["embedder"]["config"]["embedding_dims"] == 512


def test_add_always_disables_inference_and_search_normalizes_hits(
    tmp_path: Path,
) -> None:
    adapter, backend = adapter_with_fake(tmp_path)
    first_id = adapter.add("第一条", {"record_id": "record-1"})
    second_id = adapter.add("第二条", {"record_id": "record-2"})

    assert backend.add_calls[0]["infer"] is False
    assert backend.add_calls[0]["user_id"] == "test-user"
    assert first_id == "vector-1"
    assert second_id == "vector-2"
    hits = adapter.search("语义查询", limit=2)
    assert all(isinstance(hit, Hit) for hit in hits)
    assert [hit.score for hit in hits] == sorted(
        [hit.score for hit in hits], reverse=True
    )
    assert hits[0].metadata["record_id"] == "record-2"


def test_repository_is_authority_across_index_add_search_and_delete(
    tmp_path: Path,
) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter, backend = adapter_with_fake(tmp_path)
    record = MemoryRecord.create(
        category="project",
        content="用户正在开发 Firefly AI Pet",
        trigger="explicit-command",
    )

    repository.add(record)
    vector_id = adapter.add(record.content, {"record_id": record.id})
    indexed_record = repository.update(record.id, {"vector_id": vector_id})

    hit = adapter.search("用户在开发什么？", limit=1)[0]
    associated = repository.get(hit.metadata["record_id"])
    assert associated is not None
    assert associated.id == indexed_record.id
    assert associated.content == record.content
    assert associated.vector_id == vector_id

    assert adapter.delete(vector_id) is True
    assert vector_id not in backend.entries
    assert repository.get(record.id) is not None


def test_clear_only_clears_vector_index(tmp_path: Path) -> None:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter, backend = adapter_with_fake(tmp_path)
    records = [
        MemoryRecord.create(
            category="user_fact",
            content=f"记录 {index}",
            trigger="test",
        )
        for index in range(2)
    ]
    for record in records:
        repository.add(record)
        adapter.add(record.content, {"record_id": record.id})

    assert adapter.clear() == 2
    assert backend.entries == {}
    assert backend.get_all_calls == [
        {"filters": {"user_id": "test-user"}, "top_k": 1_000_000}
    ]
    assert repository.all_ids() == {record.id for record in records}


def test_initialization_failure_is_controlled(tmp_path: Path) -> None:
    def fail(config: dict[str, Any]) -> Any:
        raise RuntimeError("backend unavailable")

    adapter = Mem0Adapter(tmp_path / "vectors", backend_factory=fail)

    assert adapter.health() is False
    with pytest.raises(Mem0AdapterError, match="initialize"):
        adapter.search("query")
