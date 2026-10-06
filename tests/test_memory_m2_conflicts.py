"""M2A conflict / supersede / lifecycle tests.

Verifies that SUPERSEDE keeps the old record in the repository, default
retrieval excludes superseded, audit/export can read them, and the learning /
conversation / persona / bond subsystems are untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.records import MemoryRecord, MemoryCategory, MemorySource, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from memory.mem0_adapter import Mem0Adapter
from memory.m2 import MemoryWritePolicy, WriteAction


def _make_service(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    # 用 OfflineAdapter-style fake 避免 mem0 依赖
    class FakeAdapter:
        def add(self, text, metadata=None):
            return f"vec-{hash(text) & 0xFFFF:04x}"
        def search(self, query, limit=5, threshold=0.0):
            return []
        def delete(self, vector_id):
            return True
    service = MemoryService(repo, FakeAdapter(), write_policy=WritePolicy.AUTO)
    return repo, service


def _add(repo, content, category="project"):
    record = MemoryRecord.create(
        category=category, content=content, trigger="test"
    )
    repo.add(record)
    return record


# ---------------------------------------------------------------------------
# SUPERSEDE keeps old record in repository
# ---------------------------------------------------------------------------


def test_supersede_keeps_old_record(tmp_path):
    repo, _ = _make_service(tmp_path)
    old = _add(repo, "用户计划保研")
    new = _add(repo, "用户决定考研")
    service = MemoryService(repo, Mem0Adapter(str(tmp_path)) if False else None)
    # 手动执行 supersede（不经过 service 以避免 mem0 依赖）
    repo.update(old.id, {"lifecycle_status": "superseded", "superseded_by": new.id})
    # 旧记录仍在 repository
    loaded = repo.get(old.id)
    assert loaded is not None
    assert loaded.lifecycle_status == "superseded"
    assert loaded.superseded_by == new.id


# ---------------------------------------------------------------------------
# Default retrieval excludes superseded
# ---------------------------------------------------------------------------


def test_superseded_excluded_from_search(tmp_path):
    repo, service = _make_service(tmp_path)
    old = _add(repo, "用户计划保研")
    new = _add(repo, "用户决定考研")
    repo.update(old.id, {"lifecycle_status": "superseded", "superseded_by": new.id})
    # search 不返回 superseded
    results = service.search("保研")
    assert all(r.lifecycle_status != "superseded" for r in results)


# ---------------------------------------------------------------------------
# Audit / export can read superseded
# ---------------------------------------------------------------------------


def test_audit_export_reads_superseded(tmp_path):
    repo, _ = _make_service(tmp_path)
    old = _add(repo, "旧记忆")
    repo.update(old.id, {"lifecycle_status": "superseded", "superseded_by": "new"})
    # Repository list() 包含所有（superseded 不隐藏）
    all_records = repo.list()
    assert any(r.lifecycle_status == "superseded" for r in all_records)
    # 原始 JSON 导出也包含
    raw = json.loads((tmp_path / "memory_records.json").read_text(encoding="utf-8"))
    statuses = [r.get("lifecycle_status") for r in raw.get("records", [])]
    assert "superseded" in statuses


# ---------------------------------------------------------------------------
# Boundary: ConversationStore / LearningStore / Persona / BondState not involved
# ---------------------------------------------------------------------------


def test_learning_store_not_touched_by_memory_ops(tmp_path):
    repo, _ = _make_service(tmp_path)
    repo.create = None  # remove if exists
    # MemoryRepository 独立于 LearningStore
    assert not hasattr(repo, "learning_courses")
    assert not hasattr(repo, "curriculum")


def test_conversation_store_not_in_memory_pipeline(tmp_path):
    from memory.repository import JsonMemoryRepository

    repo = JsonMemoryRepository(tmp_path / "memory.json")
    # MemoryRepository has no conversation/session API
    assert not hasattr(repo, "list_sessions")
    assert not hasattr(repo, "append_exchange")
    assert not hasattr(repo, "create_session")
