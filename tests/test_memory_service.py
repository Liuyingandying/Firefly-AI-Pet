"""Offline tests for the authoritative explicit MemoryService write path."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from memory.mem0_adapter import Hit
from memory.memory_manager import MemoryManager
from memory.records import MemoryCategory, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService, MemorySynchronizationError
from memory.write_guards import MemoryWriteDeniedError


class FakeSemanticIndex:
    def __init__(self, *, fail_add: bool = False) -> None:
        self.fail_add = fail_add
        self.entries: dict[str, dict[str, Any]] = {}
        self.add_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str:
        normalized_metadata = dict(metadata or {})
        self.add_calls.append((text, normalized_metadata))
        if self.fail_add:
            raise RuntimeError("index unavailable")
        vector_id = f"vector-{len(self.entries) + 1}"
        self.entries[vector_id] = {
            "text": text,
            "metadata": normalized_metadata,
        }
        return vector_id

    def search(self, query: str, *, limit: int = 5, threshold: float = 0.0) -> list[Hit]:
        return [
            Hit(
                vector_id,
                item["text"],
                dict(item["metadata"]),
                1.0 - index * 0.1,
            )
            for index, (vector_id, item) in enumerate(self.entries.items())
        ][:limit]

    def delete(self, vector_id: str) -> bool:
        self.delete_calls.append(vector_id)
        return self.entries.pop(vector_id, None) is not None


def make_service(
    tmp_path: Path, *, fail_add: bool = False, policy: WritePolicy = WritePolicy.EXPLICIT_ONLY
) -> tuple[MemoryService, JsonMemoryRepository, FakeSemanticIndex]:
    repository = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = FakeSemanticIndex(fail_add=fail_add)
    return (
        MemoryService(repository, adapter, write_policy=policy),
        repository,
        adapter,
    )


def test_explicit_request_writes_repository_then_semantic_index(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path)

    record = service.remember("请记住：我喜欢简洁的 Markdown 回答")

    assert record is not None
    assert record.content == "我喜欢简洁的 Markdown 回答"
    assert record.category is MemoryCategory.PREFERENCE
    assert record.permission is WritePolicy.EXPLICIT_ONLY
    assert record.vector_id == "vector-1"
    assert repository.list() == [record]
    assert service.list() == [record]
    assert adapter.add_calls == [
        (record.content, {"record_id": record.id})
    ]


def test_ordinary_chat_does_not_write_automatically(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path)

    result = service.remember("今天过得怎么样？")

    assert result is None
    assert repository.list() == []
    assert adapter.add_calls == []


def test_disabled_permission_rejects_explicit_request(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path, policy=WritePolicy.OFF)

    with pytest.raises(MemoryWriteDeniedError, match="disabled"):
        service.remember("记住：我住在上海")

    assert repository.list() == []
    assert adapter.add_calls == []

    with pytest.raises(MemoryWriteDeniedError, match="disabled"):
        service.remember("记住：仍然不能写", permission=WritePolicy.AUTO)


def test_search_resolves_hits_to_authoritative_repository_records(
    tmp_path: Path,
) -> None:
    service, repository, _ = make_service(tmp_path)
    written = service.remember("记住：我正在开发 Firefly AI Pet")
    assert written is not None

    results = service.search("我正在开发什么？")

    assert [record.id for record in results] == [written.id]
    assert results[0].content == repository.list()[0].content


def test_delete_removes_index_then_repository_record(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path)
    written = service.remember("记住：我偏好本地处理")
    assert written is not None and written.vector_id is not None

    assert service.delete(written.id) is True
    assert adapter.delete_calls == [written.vector_id]
    assert adapter.entries == {}
    assert repository.list() == []
    assert service.delete(written.id) is False


def test_index_failure_keeps_repository_as_source_of_truth(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path, fail_add=True)

    with pytest.raises(MemorySynchronizationError) as raised:
        service.remember("记住：索引失败时仍保留权威记录")

    records = repository.list()
    assert len(records) == 1
    assert records[0] == raised.value.record
    assert records[0].vector_id is None
    assert adapter.entries == {}


def test_asserted_explicit_supports_legacy_manual_entry(tmp_path: Path) -> None:
    service, repository, _ = make_service(tmp_path)

    record = service.remember(
        "旧接口调用方已明确要求保存",
        category="user_fact",
        trigger="legacy-add-memory",
        asserted_explicit=True,
    )

    assert record is not None
    assert repository.all_ids() == {record.id}


def test_memory_manager_legacy_write_forwards_through_service(tmp_path: Path) -> None:
    service, repository, adapter = make_service(tmp_path)
    manager = MemoryManager(service=service)

    result = manager.add_memory("旧入口内容", {"category": "user_fact"})

    records = repository.list()
    assert len(records) == 1
    assert records[0].content == "旧入口内容"
    assert result["results"][0]["metadata"]["record_id"] == records[0].id
    assert adapter.add_calls == [
        ("旧入口内容", {"record_id": records[0].id})
    ]
