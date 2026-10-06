"""Narrow identity routing stays in MemoryService and its existing prompt fence."""
from dataclasses import replace
from pathlib import Path

import pytest

from character.character_loader import CharacterLoader
from core.companion_context_builder import CompanionContextBuilder
from memory.access_mode import MemoryAccessMode, MemoryAccessViolation
from memory.mem0_adapter import Hit
from memory.m3b import MIN_RELEVANCE_SCORE
from memory.records import MemoryRecord
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


class Index:
    def __init__(self):
        self.calls = []
        self.hits = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.hits


def setup(tmp_path, *, mode=MemoryAccessMode.READ_ONLY):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    index = Index()
    return MemoryService(repo, index, access_mode=mode), repo, index


def nickname(value="测试称呼", **kwargs):
    return MemoryRecord.create(category="preference", content=value,
                               trigger="synthetic-confirmed", identity_kind="preferred_name",
                               **kwargs)


@pytest.mark.parametrize("question", ["我是谁呀", "我叫什么", "你平时怎么叫我"])
def test_service_to_existing_companion_memory_context(tmp_path, question):
    service, repo, index = setup(tmp_path)
    record = nickname()
    repo.add(record)
    before = repo.path.read_bytes()
    character = CharacterLoader(Path(__file__).parents[1] / "character/firefly").load()
    context = CompanionContextBuilder(character=character, memory_reader=service,
                                      bond_reader=None).build(question, history=[])
    assert "<long_term_memory>" in context.memory_prompt
    assert record.content in context.memory_prompt
    assert context.to_messages(question)[-1] == {"role": "user", "content": question}
    assert index.calls == []  # identity facts do not need an embedding or second LLM
    assert repo.path.read_bytes() == before


def test_ordinary_queries_keep_the_existing_semantic_path_and_threshold(tmp_path):
    service, repo, index = setup(tmp_path)
    record = replace(nickname(), vector_id="synthetic-vector")
    repo.add(record)
    index.hits = [Hit(record.vector_id, record.content, {"record_id": record.id}, .90)]
    assert service.retrieve_for_prompt(record.content) == [record]
    assert index.calls and all(q == record.content for q, _ in index.calls)
    index.calls.clear()
    index.hits = [Hit(record.vector_id, record.content, {"record_id": record.id}, .34)]
    assert service.retrieve_for_prompt("今天吃什么") == []
    assert index.calls and all(q == "今天吃什么" for q, _ in index.calls)
    assert MIN_RELEVANCE_SCORE == .45
    assert service.search_threshold == .0


def test_missing_identity_never_uses_unrelated_preferences_or_semantic_hits(tmp_path):
    service, repo, index = setup(tmp_path)
    unrelated = MemoryRecord.create(category="preference", content="咖啡",
                                    trigger="synthetic")
    repo.add(unrelated)
    index.hits = [Hit("synthetic-vector", unrelated.content,
                      {"record_id": unrelated.id}, .99)]
    assert service.retrieve_for_prompt("我是谁") == []
    assert index.calls == []


def test_identity_superseded_history_and_latest_active_owner_fact(tmp_path):
    service, repo, _ = setup(tmp_path)
    current = nickname("现在的称呼", timestamp_ms=200)
    old = replace(nickname("过去的称呼", timestamp_ms=100),
                  lifecycle_status="superseded", superseded_by=current.id,
                  updated_ts=500)
    repo.add(old); repo.add(current)
    assert service.retrieve_for_prompt("我是谁呀") == [current]
    newer = nickname("已确认的新称呼", timestamp_ms=300)
    repo.add(newer)
    assert service.retrieve_for_prompt("我是谁呀") == [newer]


def test_equal_timestamp_conflicts_fail_closed_without_choosing_a_name(tmp_path):
    service, repo, _ = setup(tmp_path)
    repo.add(nickname("称呼甲", timestamp_ms=100))
    repo.add(nickname("称呼乙", timestamp_ms=100))
    assert service.retrieve_for_prompt("我是谁呀") == []


@pytest.mark.parametrize("mode", [MemoryAccessMode.READ_ONLY, MemoryAccessMode.SAFE_WRITE])
def test_identity_confirmation_requires_confirmed_write(tmp_path, mode):
    service, repo, _ = setup(tmp_path, mode=mode)
    bare = MemoryRecord.create(category="preference", content="测试称呼", trigger="synthetic")
    repo.add(bare)
    before = repo.path.read_bytes()
    with pytest.raises(MemoryAccessViolation):
        service.set_identity_kind(bare.id, "preferred_name")
    assert repo.path.read_bytes() == before


def test_owner_confirms_existing_record_without_rewriting_content_or_creating_state(tmp_path):
    service, repo, _ = setup(tmp_path, mode=MemoryAccessMode.CONFIRMED_WRITE)
    bare = MemoryRecord.create(category="preference", content="任意用户称呼",
                               source="migrated", trigger="migration:synthetic")
    repo.add(bare)
    assert service.retrieve_for_prompt("我是谁呀") == []
    marked = service.set_identity_kind(bare.id, "preferred_name")
    assert marked.id == bare.id and marked.content == bare.content
    assert marked.source == bare.source and marked.trigger == bare.trigger
    assert len(repo.list()) == 1
    before = repo.path.read_bytes()
    assert service.set_identity_kind(bare.id, "preferred_name") == marked
    assert repo.path.read_bytes() == before
    assert service.retrieve_for_prompt("我是谁呀") == [marked]
    reopened = JsonMemoryRepository(repo.path)
    assert reopened.get(bare.id).identity_kind == "preferred_name"


def test_invalid_identity_type_or_category_cannot_be_persisted(tmp_path):
    service, repo, _ = setup(tmp_path, mode=MemoryAccessMode.CONFIRMED_WRITE)
    record = MemoryRecord.create(category="project", content="项目名称", trigger="synthetic")
    repo.add(record)
    before = repo.path.read_bytes()
    with pytest.raises(ValueError):
        service.set_identity_kind(record.id, "preferred_name")
    with pytest.raises(ValueError):
        service.set_identity_kind(record.id, "occupation")
    assert repo.path.read_bytes() == before


def test_legacy_records_without_optional_identity_field_roundtrip_unchanged():
    record = MemoryRecord.create(category="preference", content="短一点", trigger="synthetic")
    payload = record.to_dict()
    assert "identity_kind" not in payload
    assert MemoryRecord.from_dict(payload).to_dict() == payload
