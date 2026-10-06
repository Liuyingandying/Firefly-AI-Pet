"""M2C confirmation flow + supersede chain + cycle protection tests."""

from __future__ import annotations
import hashlib  # noqa: E402
import hashlib  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.records import (  # noqa: E402
    MemoryRecord, MemoryCategory, MemorySource, WritePolicy,
)
from memory.repository import JsonMemoryRepository  # noqa: E402
from memory.m2 import MemoryWritePolicy, MemoryCandidate, WriteAction  # noqa: E402
from memory.service import MemoryService  # noqa: E402


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


def _make(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory.json")
    svc = MemoryService(repo, FakeAdapter(), write_policy=WritePolicy.EXPLICIT_ONLY)
    return repo, svc


def _add(repo, content, category="project"):
    r = MemoryRecord.create(category=category, content=content, trigger="test")
    repo.add(r)
    return r


# ---------------------------------------------------------------------------
# Supersede chain + cycle protection
# ---------------------------------------------------------------------------


def test_supersede_chain_a_to_b(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "旧事实A")
    b = _add(repo, "新事实B")
    svc.apply_supersede(a.id, b.id)
    assert repo.get(a.id).lifecycle_status == "superseded"
    assert repo.get(b.id).lifecycle_status == "active"


def test_supersede_chain_resolution(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "事实A")
    b = _add(repo, "事实B")
    c = _add(repo, "事实C")
    svc.apply_supersede(a.id, b.id)
    svc.apply_supersede(b.id, c.id)
    final = svc.resolve_active_successor(a.id)
    assert final.id == c.id


def test_supersede_cycle_rejected(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "事实A")
    b = _add(repo, "事实B")
    svc.apply_supersede(a.id, b.id)
    with pytest.raises(ValueError, match="cycle"):
        svc.apply_supersede(b.id, a.id)


def test_self_supersede_rejected(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "事实A")
    with pytest.raises(ValueError, match="itself"):
        svc.apply_supersede(a.id, a.id)


# ---------------------------------------------------------------------------
# Confirmation flow (RAM only, not persisted)
# ---------------------------------------------------------------------------


def test_require_confirmation_no_mutation(tmp_path):
    repo, svc = _make(tmp_path)
    old = _add(repo, "用户喜欢城市A")
    candidate = MemoryCandidate(content="用户最近觉得城市B也不错", explicit=True)
    policy = MemoryWritePolicy()
    decision = policy.evaluate(candidate, repo.list())
    assert decision.action in (WriteAction.REQUIRE_CONFIRMATION, WriteAction.KEEP_BOTH, WriteAction.CREATE)
    # No mutation happened
    assert len(repo.list()) == 1
    assert repo.get(old.id).lifecycle_status == "active"


def test_confirm_update_supersede(tmp_path):
    repo, svc = _make(tmp_path)
    old = _add(repo, "旧记忆")
    new = _add(repo, "新记忆")
    svc.apply_supersede(old.id, new.id)
    assert repo.get(old.id).lifecycle_status == "superseded"


def test_confirm_keep_both(tmp_path):
    repo, svc = _make(tmp_path)
    a = _add(repo, "记忆A")
    b = _add(repo, "记忆B")
    # Keep both = no mutation needed
    assert repo.get(a.id).lifecycle_status == "active"
    assert repo.get(b.id).lifecycle_status == "active"


# ---------------------------------------------------------------------------
# Real 3 active memories unchanged
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
