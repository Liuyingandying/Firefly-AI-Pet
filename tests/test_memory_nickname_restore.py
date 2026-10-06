"""Isolated nickname revision through existing Memory owner operations.

The named records below are synthetic fixtures in tmp_path, never reads of
production data. The semantic scores are injected test evidence; they do not
claim to measure the real embedder. No provider or GUI window is constructed.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from character.character_loader import CharacterProfile
from core.companion_context_builder import CompanionContextBuilder
from memory.access_mode import MemoryAccessMode, MemoryAccessViolation
from memory.m3b import MIN_RELEVANCE_SCORE
from memory.mem0_adapter import Mem0Adapter
from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService, MemorySynchronizationError, WriteOutcome
from test_memory_m1_consistency import FakeIndex


OLD_NICKNAME = "测试旧称"
CURRENT_CONTENT = "用户希望流萤在当前日常关系中称呼自己为‘测试新称’。"
CONFIRMATION_TRIGGER = "explicit-user-confirmation:synthetic-nickname-restore"
SUPERSEDE_REASON = "current-human-confirmation:synthetic-supporting-history"
IDENTITY_QUESTIONS = (
    "我是谁呀", "我叫什么", "你平时怎么叫我", "还记得怎么称呼我吗",
)


class ScoredIndex(FakeIndex):
    """Reuse the existing fake index with controllable query evidence."""

    def __init__(self):
        super().__init__()
        self.scores_by_query = {}
        self.search_calls = []

    def search(self, query, *, limit=5, threshold=0.0):
        self.search_calls.append((query, limit, threshold))
        score = self.scores_by_query.get(query, 1.0)
        return [replace(hit, score=score)
                for hit in super().search(query, limit=limit, threshold=threshold)]


@pytest.fixture(autouse=True)
def forbid_real_semantic_backend(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Nickname restoration tests must never construct Mem0")

    monkeypatch.setattr(Mem0Adapter, "__init__", forbidden)


def _seed_legacy(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    index = ScoredIndex()
    owner = MemoryService(repo, index, access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    old = MemoryRecord.create(
        category="preference", content=OLD_NICKNAME, source="migrated",
        trigger="migration:synthetic-historical-nickname",
        identity_kind="preferred_name",
    )
    repo.add(old)
    vector_id = index.add(old.content, {"record_id": old.id})
    old = repo.update(old.id, {"vector_id": vector_id})
    # A below-dedup score proves creation without disabling the existing gate.
    index.scores_by_query[CURRENT_CONTENT] = .40
    return owner, repo, index, old


def _create_current(owner, repo):
    result = owner.remember_detailed(
        CURRENT_CONTENT, category=MemoryCategory.PREFERENCE,
        trigger=CONFIRMATION_TRIGGER, permission=WritePolicy.EXPLICIT_ONLY,
        asserted_explicit=True,
    )
    assert result.outcome is WriteOutcome.CREATED
    new = repo.get(result.record.id)
    assert new.vector_id is not None
    assert new.source is MemorySource.EXPLICIT
    assert new.trigger == CONFIRMATION_TRIGGER
    assert new.category is MemoryCategory.PREFERENCE
    assert new.permission is WritePolicy.EXPLICIT_ONLY
    return new


@pytest.fixture()
def restored(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    new = _create_current(owner, repo)
    new = owner.set_identity_kind(new.id, "preferred_name")
    owner.apply_supersede(old.id, new.id, reason=SUPERSEDE_REASON)
    return owner, repo, index, old, repo.get(new.id)


def test_current_confirmation_creates_linked_typed_revision_before_superseding(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    new = _create_current(owner, repo)

    assert repo.get(old.id) == old
    assert new.identity_kind is None
    assert index.entries[new.vector_id] == {
        "text": CURRENT_CONTENT, "metadata": {"record_id": new.id},
    }
    assert owner.dedup_enabled and owner.dedup_similarity_threshold == .85

    marked = owner.set_identity_kind(new.id, "preferred_name")
    assert marked.content == CURRENT_CONTENT
    assert marked.vector_id == new.vector_id
    assert marked.identity_kind == "preferred_name"
    assert marked.source is MemorySource.EXPLICIT
    assert marked.trigger == CONFIRMATION_TRIGGER
    owner.apply_supersede(old.id, marked.id, reason=SUPERSEDE_REASON)

    historical = repo.get(old.id)
    assert historical.lifecycle_status == "superseded"
    assert historical.superseded_by == marked.id
    assert historical.supersede_reason == SUPERSEDE_REASON
    assert historical.content == old.content
    assert historical.source == old.source
    assert historical.trigger == old.trigger
    assert historical.created_ts == old.created_ts
    assert historical.identity_kind == old.identity_kind
    assert historical.vector_id == old.vector_id
    assert repo.get(marked.id).lifecycle_status == "active"
    assert len(owner.list()) == len(index.entries) == 2
    assert index.delete_calls == []
    report = owner.consistency_report()
    assert report.healthy == 2
    assert not report.missing_vectors
    assert not report.orphan_vectors
    assert not report.stale_vector_links
    reopened = JsonMemoryRepository(repo.path)
    assert reopened.get(marked.id) == repo.get(marked.id)
    assert reopened.get(old.id) == historical


def test_existing_confirmed_edit_preserves_history_and_uses_stored_vector(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    result = owner.edit_memory(old.id, CURRENT_CONTENT)

    assert result["action"] == "created"
    assert result["old_id"] == old.id
    # edit_memory may return its pre-index object; repository remains truth.
    new = repo.get(result["record"].id)
    assert new.vector_id is not None
    assert new.category == old.category
    assert new.permission == old.permission
    assert new.source is MemorySource.EXPLICIT
    assert new.trigger == "manual_edit"
    assert new.identity_kind is None
    marked = owner.set_identity_kind(new.id, "preferred_name")
    assert marked.vector_id == new.vector_id
    assert index.entries[marked.vector_id]["metadata"] == {"record_id": marked.id}
    historical = repo.get(old.id)
    assert historical.content == OLD_NICKNAME
    assert historical.lifecycle_status == "superseded"
    assert historical.superseded_by == marked.id
    assert historical.supersede_reason == "manual_edit"
    assert len(owner.list()) == 2
    assert owner.retrieve_for_prompt("我叫什么") == [marked]


def test_semantic_duplicate_result_does_not_replace_legacy_nickname(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    index.scores_by_query[CURRENT_CONTENT] = .95
    before = repo.path.read_bytes()

    result = owner.remember_detailed(
        CURRENT_CONTENT, category="preference", trigger=CONFIRMATION_TRIGGER,
        permission=WritePolicy.EXPLICIT_ONLY, asserted_explicit=True,
    )

    assert result.outcome is WriteOutcome.SEMANTIC_DUPLICATE
    assert result.record.id == old.id
    assert repo.get(old.id).lifecycle_status == "active"
    assert repo.path.read_bytes() == before
    assert len(index.entries) == 1


def test_new_index_failure_raises_before_legacy_nickname_is_superseded(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    index.fail_add = True

    with pytest.raises(MemorySynchronizationError):
        owner.remember_detailed(
            CURRENT_CONTENT, category="preference", trigger=CONFIRMATION_TRIGGER,
            permission=WritePolicy.EXPLICIT_ONLY, asserted_explicit=True,
        )

    assert repo.get(old.id) == old
    assert owner.index_dirty
    assert len(index.entries) == 1


@pytest.mark.parametrize("question", IDENTITY_QUESTIONS)
def test_owner_and_context_recall_only_current_typed_nickname(restored, question):
    owner, repo, index, old, new = restored
    reader = MemoryService(repo, index, access_mode=MemoryAccessMode.READ_ONLY)
    index.search_calls.clear()
    index.fail_search = True
    before = repo.path.read_bytes()

    assert reader.retrieve_for_prompt(question) == [new]
    character = CharacterProfile(
        "synthetic-character", "Synthetic identity", "", "", "",
    )
    context = CompanionContextBuilder(
        character=character, memory_reader=reader, bond_reader=None,
    ).build(question, history=[])

    assert "<long_term_memory>" in context.memory_prompt
    assert CURRENT_CONTENT in context.memory_prompt
    assert "测试新称" in context.memory_prompt
    assert OLD_NICKNAME not in context.memory_prompt
    assert context.to_messages(question)[-1] == {"role": "user", "content": question}
    assert index.search_calls == []
    assert repo.path.read_bytes() == before
    assert reader.retrieve_identity_for_prompt() == [new]
    assert reader.resolve_active_successor(old.id) == new


def test_ordinary_semantic_retrieval_keeps_current_only_and_threshold(restored):
    owner, repo, index, old, new = restored
    query = "日常称呼偏好"
    before = repo.path.read_bytes()

    assert owner.search(query) == [new]
    assert owner.retrieve_for_prompt(query) == [new]
    assert index.search_calls[-1][0] == query
    assert MIN_RELEVANCE_SCORE == .45
    assert owner.search_threshold == .0
    index.scores_by_query[query] = .44
    assert owner.retrieve_for_prompt(query) == []
    assert repo.path.read_bytes() == before


def test_owner_history_search_can_read_preserved_previous_nickname(restored):
    owner, repo, index, old, new = restored
    reader = MemoryService(repo, index, access_mode=MemoryAccessMode.READ_ONLY)
    before = repo.path.read_bytes()
    query = "以前为什么叫我测试旧称"

    results = reader.search(query, include_superseded=True)

    assert {record.id for record in results} == {old.id, new.id}
    historical = next(record for record in results if record.id == old.id)
    assert historical.content == OLD_NICKNAME
    assert historical.lifecycle_status == "superseded"
    assert historical.superseded_by == new.id
    assert reader.search(query) == [new]
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("mode", [MemoryAccessMode.READ_ONLY, MemoryAccessMode.SAFE_WRITE])
@pytest.mark.parametrize("operation", ["edit", "mark_identity"])
def test_nickname_revision_mutations_require_confirmed_write(tmp_path, mode, operation):
    owner, repo, index, old = _seed_legacy(tmp_path)
    new = _create_current(owner, repo)
    restricted = MemoryService(repo, index, access_mode=mode)
    before = repo.path.read_bytes()
    index_before = dict(index.entries)

    with pytest.raises(MemoryAccessViolation):
        if operation == "edit":
            restricted.edit_memory(old.id, CURRENT_CONTENT)
        else:
            restricted.set_identity_kind(new.id, "preferred_name")

    assert repo.path.read_bytes() == before
    assert index.entries == index_before
    assert repo.get(old.id).lifecycle_status == "active"


def test_read_only_refuses_existing_lifecycle_supersede(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    new = _create_current(owner, repo)
    restricted = MemoryService(repo, index, access_mode=MemoryAccessMode.READ_ONLY)
    before = repo.path.read_bytes()

    with pytest.raises(MemoryAccessViolation):
        restricted.apply_supersede(old.id, new.id, reason=SUPERSEDE_REASON)

    assert repo.path.read_bytes() == before
    assert repo.get(old.id).lifecycle_status == "active"


def test_safe_write_retains_existing_creation_lifecycle_permission(tmp_path):
    owner, repo, index, old = _seed_legacy(tmp_path)
    new = _create_current(owner, repo)
    safe = MemoryService(repo, index, access_mode=MemoryAccessMode.SAFE_WRITE)

    safe.apply_supersede(old.id, new.id, reason=SUPERSEDE_REASON)

    assert repo.get(old.id).lifecycle_status == "superseded"
    assert repo.get(old.id).superseded_by == new.id
    assert repo.get(new.id).content == CURRENT_CONTENT
    assert repo.get(new.id).identity_kind is None


class _ListSink:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items.clear()

    def addItem(self, item):
        self.items.append(item)


@pytest.mark.parametrize("status", ["active", "superseded"])
def test_existing_memory_ui_filters_read_active_or_history_without_a_window(restored, status):
    from PySide6.QtCore import Qt
    from ui.memory_manager import MemoryManagerWindow

    owner, repo, index, old, new = restored
    reader = MemoryService(repo, index, access_mode=MemoryAccessMode.READ_ONLY)
    before = repo.path.read_bytes()
    stats = []
    # Exercise the real UI read/filter methods with sinks, never a QWidget.
    view = SimpleNamespace(
        _service=reader, _list=_ListSink(),
        _status_filter=SimpleNamespace(currentData=lambda: status),
        _type_filter=SimpleNamespace(currentData=lambda: "all"),
        _search=SimpleNamespace(text=lambda: ""),
        _stats_label=SimpleNamespace(setText=stats.append),
    )
    view._all_records = lambda: MemoryManagerWindow._all_records(view)

    rows = view._all_records()
    assert len(rows) == 2
    MemoryManagerWindow._refresh_list(view)

    expected = new if status == "active" else repo.get(old.id)
    assert len(view._list.items) == 1
    item = view._list.items[0]
    assert item.data(Qt.ItemDataRole.UserRole) == expected.id
    assert item.data(Qt.ItemDataRole.UserRole + 1) == expected.content
    assert ("当前" if status == "active" else "历史") in item.text()
    assert "1 条当前 · 1 条历史" in stats[-1]
    assert repo.path.read_bytes() == before
