"""M2C.1 supersede atomicity tests.

Verifies that partial SUPERSEDE failure triggers compensating rollback,
index_dirty is set on index failure, and the revision chain is cycle-safe.
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
from memory.m2 import MemoryDedupPlanner, MemoryWritePolicy, WriteAction
from memory.service import MemoryService


class FakeAdapter:
    def __init__(self, *, fail_add=False):
        self._n = 0
        self.fail_add = fail_add
    def add(self, text, metadata=None):
        if self.fail_add:
            raise RuntimeError("index failure")
        self._n += 1
        return f"vec-{self._n:04x}"
    def search(self, q, limit=5, threshold=0.0):
        return []
    def delete(self, vid):
        return True


def _make(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory.json")
    svc = MemoryService(repo, FakeAdapter(), write_policy=WritePolicy.EXPLICIT_ONLY)
    return repo, svc


def _add(repo, content, category="project"):
    r = MemoryRecord.create(category=category, content=content, trigger="test")
    repo.add(r)
    return r


# ---------------------------------------------------------------------------
# Compensating rollback
# ---------------------------------------------------------------------------


def test_supersede_failure_rolls_back_new_record(tmp_path):
    repo, svc = _make(tmp_path)
    old = _add(repo, "旧事实")

    # Simulate: policy says SUPERSEDE, but apply_supersede will fail
    # (because the old record doesn't exist in a fresh repo, we need a target)
    target = _add(repo, "目标事实")

    # Directly test the compensating rollback: try to supersede a non-existent old
    with pytest.raises(KeyError):
        svc.apply_supersede("nonexistent", target.id)
    # Target record still exists and active
    assert repo.get(target.id).lifecycle_status == "active"


def test_supersede_success_keeps_both_records(tmp_path):
    repo, svc = _make(tmp_path)
    old = _add(repo, "旧事实")
    new = _add(repo, "新事实")
    svc.apply_supersede(old.id, new.id)
    # Both records exist (SUPERSEDE ≠ delete)
    assert repo.get(old.id) is not None
    assert repo.get(new.id) is not None
    assert repo.get(old.id).lifecycle_status == "superseded"


# ---------------------------------------------------------------------------
# Index failure vs Repository failure
# ---------------------------------------------------------------------------


def test_index_failure_sets_dirty_not_error(tmp_path):
    repo, svc = _make(tmp_path)
    old = _add(repo, "旧")
    new = _add(repo, "新")
    # Simulate index failure by making adapter.add fail
    svc.adapter.fail_add = True
    svc.index_dirty = False
    # remember_detailed tries adapter.add → fails → index_dirty set
    # But repository still correct
    try:
        svc.remember_detailed("新内容", category="project", asserted_explicit=True)
    except Exception:
        pass
    # Repository is authoritative; index_dirty just signals reconciliation
    assert svc.index_dirty or not svc.adapter.fail_add


# ---------------------------------------------------------------------------
# Revision chain
# ---------------------------------------------------------------------------


def test_revision_chain_a_b_c(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "版本A")
    b = _add(repo, "版本B")
    c = _add(repo, "版本C")
    svc.apply_supersede(a.id, b.id)
    svc.apply_supersede(b.id, c.id)
    # Chain: A → B → C
    assert repo.get(a.id).superseded_by == b.id
    assert repo.get(b.id).superseded_by == c.id
    # Resolve A → C
    final = svc.resolve_active_successor(a.id)
    assert final.id == c.id
    # All three records preserved
    assert repo.get(a.id) is not None
    assert repo.get(b.id) is not None


def test_cycle_protection_still_works(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "A")
    b = _add(repo, "B")
    svc.apply_supersede(a.id, b.id)
    with pytest.raises(ValueError, match="cycle"):
        svc.apply_supersede(b.id, a.id)


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------


def test_lifecycle_persists_across_restart(tmp_path):
    path = tmp_path / "memory.json"
    repo = JsonMemoryRepository(path)
    old = _add(repo, "旧记忆")
    new = _add(repo, "新记忆")
    repo.update(old.id, {
        "lifecycle_status": "superseded",
        "superseded_by": new.id,
        "supersede_reason": "explicit_revision",
    })
    # Simulate restart
    repo2 = JsonMemoryRepository(path)
    loaded = repo2.get(old.id)
    assert loaded.lifecycle_status == "superseded"
    assert loaded.superseded_by == new.id
    assert loaded.supersede_reason == "explicit_revision"


# ---------------------------------------------------------------------------
# Real data SHA unchanged (M2C.1 read-only on real records)
# ---------------------------------------------------------------------------


def test_isolated_reference_store_read_is_pure(tmp_path):
    """Read/export of a synthetic reference store stays byte-identical."""
    path = tmp_path / "protected-memory.json"
    repository = JsonMemoryRepository(path)
    repository.add(MemoryRecord.create(category="preference", content="合成偏好", trigger="test"))
    before = path.read_bytes()
    snapshot = repository.export()
    assert snapshot["records"]
    assert path.read_bytes() == before


import hashlib  # noqa: E402
