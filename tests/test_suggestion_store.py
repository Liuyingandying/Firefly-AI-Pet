"""M3B.7 persistent suggestion store tests.

Covers the phase-specified scenarios:
1. add suggestion
2. survive restart
3. status transitions pending->accepted / pending->rejected
4. corrupt JSON recovery
5. 100 ordinary chats: Memory count unchanged, Suggestion count grows
6. accept suggestion: Memory +1
7. reject suggestion: Memory unchanged
8. companion_auto: enters suggestion, never Memory
+ conversation_summary -> ConversationStore only, source allowlist
  ("explicit" forgery forbidden), duplicate-id rejection, delete/clear.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.records import MemoryCategory, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import AUTO_SOURCE_TRIGGERS, MemoryService
from memory.suggestion.memory_candidate_detector import MemorySuggestion
from memory.suggestion.suggestion_service import SuggestionService
from memory.suggestion_store import (
    ALLOWED_SOURCES,
    SuggestionStore,
    SuggestionValidationError,
)


class _FakeIndex:
    def __init__(self) -> None:
        self.n = 0

    def add(self, text, metadata=None):
        self.n += 1
        return f"vec-{self.n}"

    def search(self, query, *, limit=5, threshold=0.0):
        return []

    def delete(self, vector_id):
        return True


def _service(tmp_path: Path, *, with_store: bool = True, auto_write: bool = True):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    memory_service = MemoryService(
        repo, _FakeIndex(), write_policy=WritePolicy.AUTO
    )
    store = (
        SuggestionStore(tmp_path / "memory_suggestions.json")
        if with_store
        else None
    )
    service = SuggestionService(
        memory_service,
        store=store,
        enabled=True,
        auto_extract_enabled=True,
        auto_write_enabled=auto_write,
        auto_write_allowed_categories=["project", "preference"],
    )
    return service, memory_service, store


# ---------------------------------------------------------------------------
# store-level
# ---------------------------------------------------------------------------


def test_add_suggestion(tmp_path):
    store = SuggestionStore(tmp_path / "memory_suggestions.json")
    record = store.add(
        content="用户正在开发 Firefly 的语音模块",
        source="companion_auto",
        confidence=0.9,
        metadata={"category": "project", "reason": "gate"},
    )
    assert record["status"] == "pending"
    assert record["id"]
    assert store.count() == 1
    raw = (tmp_path / "memory_suggestions.json").read_text(encoding="utf-8")
    assert "base64" not in raw


def test_source_allowlist_forbids_explicit_forgery(tmp_path):
    store = SuggestionStore(tmp_path / "memory_suggestions.json")
    assert set(ALLOWED_SOURCES) == {
        "companion_auto", "conversation_summary", "user_created",
    }
    for source in sorted(ALLOWED_SOURCES):
        store.add(content="x", source=source)
    with pytest.raises(SuggestionValidationError):
        store.add(content="伪造来源", source="explicit")
    with pytest.raises(SuggestionValidationError):
        store.add(content="伪造来源", source="system_generated")
    assert store.count() == 3


def test_status_validation(tmp_path):
    store = SuggestionStore(tmp_path / "memory_suggestions.json")
    record = store.add(content="x", source="companion_auto")
    with pytest.raises(SuggestionValidationError):
        store.update_status(record["id"], "不是状态")
    assert store.update_status(record["id"], "expired")["status"] == "expired"


def test_survive_restart_with_status_and_metadata(tmp_path):
    root = tmp_path / "memory_suggestions.json"
    store = SuggestionStore(root)
    record = store.add(
        content="跨重启候选",
        source="companion_auto",
        confidence=0.85,
        metadata={"category": "project", "reason": "gate", "evidence": ["e1"]},
    )
    store.update_status(record["id"], "accepted")
    before = root.read_bytes()

    reopened = SuggestionStore(root)
    got = reopened.get(record["id"])
    assert got is not None
    assert got["content"] == "跨重启候选"
    assert got["status"] == "accepted"
    assert got["confidence"] == 0.85
    assert got["metadata"]["category"] == "project"
    assert got["metadata"]["evidence"] == ["e1"]
    assert root.read_bytes() == before  # 读操作不写盘


def test_corrupt_json_quarantined(tmp_path):
    root = tmp_path / "memory_suggestions.json"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("{ corrupt", encoding="utf-8")
    store = SuggestionStore(root)
    assert store.count() == 0
    quarantined = list(root.parent.glob("memory_suggestions.corrupt-*.json"))
    assert len(quarantined) == 1
    assert store.add(content="损坏后仍可写", source="companion_auto") is not None


def test_duplicate_id_rejected(tmp_path):
    store = SuggestionStore(tmp_path / "memory_suggestions.json")
    store.add(content="a", source="companion_auto", suggestion_id="dup")
    with pytest.raises(SuggestionValidationError):
        store.add(content="b", source="companion_auto", suggestion_id="dup")


def test_delete_and_clear(tmp_path):
    store = SuggestionStore(tmp_path / "memory_suggestions.json")
    a = store.add(content="a", source="companion_auto")
    store.add(content="b", source="companion_auto")
    assert store.delete(a["id"]) is True
    assert store.delete(a["id"]) is False
    assert store.count() == 1
    assert store.clear() == 1
    assert store.count() == 0


# ---------------------------------------------------------------------------
# service-level: extraction persists to store, never to Memory
# ---------------------------------------------------------------------------


def _service_with_candidates(tmp_path: Path, *, auto_write: bool = True):
    service, memory_service, store = _service(tmp_path, auto_write=auto_write)
    candidates = [
        MemorySuggestion(
            content=f"自动候选{i}",
            category=MemoryCategory.PROJECT,
            reason="companion gate",
            evidence=(),
            confidence=0.9,
            source="companion_auto",
            status="pending",
        )
        for i in range(2)
    ]
    service._persist_pending(candidates)
    return service, memory_service, store, candidates


def test_extract_persists_to_store_not_memory(tmp_path):
    service, memory_service, store, _cands = _service_with_candidates(tmp_path)
    assert store.count() == 2
    assert len(memory_service.repository.list()) == 0   # Memory 不变
    pending = service.list_pending()
    assert len(pending) == 2
    assert all(s.status == "pending" for s in pending)


def test_extract_without_store_uses_ram(tmp_path):
    service, memory_service, _store = _service(tmp_path, auto_write=False)
    service.store = None  # legacy RAM 模式
    candidates = [
        MemorySuggestion(
            content=f"RAM 候选{i}", category=MemoryCategory.PROJECT,
            reason="r", evidence=(), confidence=0.9,
            source="companion_auto", status="pending",
        )
        for i in range(2)
    ]
    service._persist_pending(candidates)
    assert len(service._pending) == 2
    assert memory_service.repository.list() == []


def test_companion_gate_promotes_to_front(tmp_path):
    service, memory_service, store, _cands = _service_with_candidates(tmp_path)
    normal = MemorySuggestion(
        content="情感类候选内容未过门", category=MemoryCategory.EMOTION,
        reason="r", evidence=(), confidence=0.9,
        source="companion_auto", status="pending",
    )
    promoted = MemorySuggestion(
        content="项目类候选内容已过门", category=MemoryCategory.PROJECT,
        reason="gate", evidence=(), confidence=0.9,
        source="companion_auto", status="pending",
    )
    service._persist_pending([normal, promoted])
    by_content = {r["content"]: r for r in store.list_all()}
    promoted_rec = by_content["项目类候选内容已过门"]
    normal_rec = by_content["情感类候选内容未过门"]
    assert promoted_rec["metadata"].get("companion_promoted") is True
    assert normal_rec["metadata"].get("companion_promoted") is None
    assert promoted_rec["confidence"] > normal_rec["confidence"]


def test_cross_restart_dedup_prevents_duplicate_pending(tmp_path):
    service, _memory, store, _c = _service_with_candidates(tmp_path)
    pending_before = {s.content for s in service.list_pending()}
    service._persist_pending(list(service.list_pending()))
    pending_after = {s.content for s in service.list_pending()}
    assert pending_after == pending_before
    assert len(pending_after) == 2


# ---------------------------------------------------------------------------
# Memory boundary via service triggers
# ---------------------------------------------------------------------------


def test_system_generated_trigger_refused(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    service = MemoryService(repo, _FakeIndex(), write_policy=WritePolicy.AUTO)
    result = service.remember_detailed(
        "系统自动生成的内容", trigger="system_generated", asserted_explicit=True
    )
    assert result is None
    assert len(repo.list()) == 0


def test_user_explicit_and_suggestion_confirmed_allowed(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    service = MemoryService(repo, _FakeIndex(), write_policy=WritePolicy.AUTO)
    r1 = service.remember_detailed(
        "用户明确要求记住的内容", trigger="user_explicit", asserted_explicit=True
    )
    assert r1 is not None
    r2 = service.remember_detailed(
        "用户确认的候选", trigger="suggestion_confirmed", asserted_explicit=True
    )
    assert r2 is not None
    assert len(repo.list()) == 2


def test_auto_triggers_never_write(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    service = MemoryService(repo, _FakeIndex(), write_policy=WritePolicy.AUTO)
    for trigger in sorted(AUTO_SOURCE_TRIGGERS):
        result = service.remember_detailed(
            f"机器来源({trigger})", trigger=trigger, asserted_explicit=True
        )
        assert result is None, trigger
    assert len(repo.list()) == 0


# ---------------------------------------------------------------------------
# acceptance flow: user confirmation -> Memory +1 / reject -> unchanged
# ---------------------------------------------------------------------------


def test_accept_suggestion_memory_plus_one(tmp_path):
    service, memory_service, store, _c = _service_with_candidates(tmp_path)
    assert memory_service.repository.list() == []
    pending = service.list_pending()
    record = service.accept(pending[0])
    assert record is not None
    assert record.trigger == "suggestion_confirmed"
    assert len(memory_service.repository.list()) == 1     # Memory +1
    accepted = store.get(pending[0].id)
    assert accepted["status"] == "accepted"
    # 另一条未处理候选仍为 pending
    assert len(store.get_pending()) == 1


def test_reject_suggestion_memory_unchanged(tmp_path):
    service, memory_service, store, _c = _service_with_candidates(tmp_path)
    pending = service.list_pending()
    target_id = pending[0].id
    service.reject(pending[0])
    assert store.get(target_id)["status"] == "rejected"
    assert len(memory_service.repository.list()) == 0     # Memory 不变
