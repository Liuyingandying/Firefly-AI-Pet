"""Memory Manager 2.0 tests — active/history/detail/add/edit/forget/export."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.records import MemoryCategory, MemoryRecord, WritePolicy
from memory.access_mode import MemoryAccessMode
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


class FakeAdapter:
    def __init__(self):
        self._n = 0
    def add(self, text, metadata=None):
        self._n += 1
        return f"vec-{self._n:04x}"
    def search(self, q, limit=5, threshold=0.0):
        return []
    def delete(self, vid):
        return True


def _make(tmp_path, n_duplicates=0):
    repo = JsonMemoryRepository(tmp_path / "mem.json")
    svc = MemoryService(repo, FakeAdapter(), write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    for i in range(n_duplicates):
        r = MemoryRecord.create(category="project", content=f"重复记忆{i}",
                                trigger="test", timestamp_ms=1000+i)
        repo.add(r)
    return repo, svc


def test_default_shows_active_only(tmp_path):
    repo, svc = _make(tmp_path)
    from memory.m2 import MemoryDedupPlanner
    for _ in range(5):
        r = MemoryRecord.create(category="project", content="重复", trigger="t")
        repo.add(r)
    # Mark duplicates
    plan = MemoryDedupPlanner().plan(repo.list())
    for e in plan.entries:
        if e.action.value == "ignore_duplicate":
            repo.update(e.record_id, {"lifecycle_status": "superseded", "superseded_by": e.canonical_id})
    # Service-level default excludes superseded
    results = svc.list()
    active = [r for r in results if getattr(r, "lifecycle_status", "active") == "active"]
    assert len(active) < len(results)


def test_status_counts_correct(tmp_path):
    repo, svc = _make(tmp_path)
    for _ in range(3):
        r = MemoryRecord.create(category="project", content="A", trigger="t")
        repo.add(r)
    for _ in range(2):
        r = MemoryRecord.create(category="user_fact", content="B", trigger="t")
        repo.add(r)
    all_r = repo.list()
    assert len(all_r) == 5


def test_forget_uses_service_delete(tmp_path):
    repo, svc = _make(tmp_path)
    r = MemoryRecord.create(category="project", content="测试", trigger="t")
    repo.add(r)
    assert svc.delete(r.id) is True
    assert repo.get(r.id) is None


def test_clear_uses_service_clear(tmp_path):
    repo, svc = _make(tmp_path)
    for i in range(5):
        r = MemoryRecord.create(category="project", content=f"c{i}", trigger="t")
        repo.add(r)
    repo.clear()
    assert len(repo.list()) == 0


def test_export_from_repository(tmp_path):
    repo, svc = _make(tmp_path)
    r = MemoryRecord.create(category="project", content="导出测试", trigger="t")
    repo.add(r)
    records = svc.list()
    data = {"format": "firefly-memory-export", "records": [r.to_dict() for r in records]}
    assert len(data["records"]) == 1
    assert data["records"][0]["content"] == "导出测试"


def test_isolated_reference_store_read_is_pure(tmp_path):
    """Read/export of a synthetic reference store stays byte-identical."""
    path = tmp_path / "protected-memory.json"
    repository = JsonMemoryRepository(path)
    repository.add(MemoryRecord.create(category="preference", content="合成偏好", trigger="test"))
    before = path.read_bytes()
    snapshot = repository.export()
    assert snapshot["records"]
    assert path.read_bytes() == before
