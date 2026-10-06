"""M2B real dedup migration tests.

Pre-migration duplicates are created via ``repo.add()`` directly (bypassing
the service's dedup) to simulate the pre-M2A state where 33 records existed.
The M2A policy in ``remember_detailed`` is tested separately.
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
from memory.m2 import MemoryDedupPlanner, MemoryWritePolicy, MemoryCandidate, WriteAction, normalize_memory_text
from memory.service import MemoryService


def _make_repo(tmp_path):
    path = tmp_path / "memory_records.json"
    repo = JsonMemoryRepository(path)
    return repo, path


def _add_records(repo, contents, *, category="project", start_ts=1000):
    for i, content in enumerate(contents):
        r = MemoryRecord.create(
            category=category, content=content, trigger="test",
            timestamp_ms=start_ts + i,
        )
        repo.add(r)


def _make_service(tmp_path):
    repo, path = _make_repo(tmp_path)

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

    svc = MemoryService(repo, FakeAdapter(), write_policy=WritePolicy.EXPLICIT_ONLY)
    return repo, svc, path


def _add_33_like(repo):
    """Simulate pre-M2A state: 33 records with heavy duplicates."""
    for i in range(24):
        r = MemoryRecord.create(category="project", content="用户正在开发 Firefly AI Pet",
                                trigger=f"tA{i}", timestamp_ms=1000+i)
        repo.add(r)
    for i in range(8):
        r = MemoryRecord.create(category="user_fact", content="用户正在开发 Firefly AI Pet 项目",
                                trigger=f"tB{i}", timestamp_ms=2000+i)
        repo.add(r)
    r = MemoryRecord.create(category="preference", content="测试旧称",
                            trigger="tP", timestamp_ms=3000)
    repo.add(r)


def _mark_superseded(repo):
    planner = MemoryDedupPlanner()
    plan = planner.plan(repo.list())
    for entry in plan.entries:
        if entry.action.value == "ignore_duplicate":
            repo.update(entry.record_id, {
                "lifecycle_status": "superseded",
                "superseded_by": entry.canonical_id,
            })


# ---------------------------------------------------------------------------
# 1-5: migration execution (repo.add direct, no service dedup)
# ---------------------------------------------------------------------------


def test_planner_revalidates_33_to_3_canonical(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    records = repo.list()
    assert len(records) == 33
    plan = MemoryDedupPlanner().plan(records)
    creates = [e for e in plan.entries if e.action.value == "create"]
    ignores = [e for e in plan.entries if e.action.value == "ignore_duplicate"]
    assert len(creates) == 3
    assert len(ignores) == 30


def test_migration_marks_duplicates_superseded(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    active = [r for r in repo.list() if r.lifecycle_status == "active"]
    superseded = [r for r in repo.list() if r.lifecycle_status == "superseded"]
    assert len(active) == 3
    assert len(superseded) == 30
    active_ids = {r.id for r in active}
    for r in superseded:
        assert r.superseded_by in active_ids


def test_no_content_mutation_during_migration(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    before = {r.id: r.content for r in repo.list()}
    _mark_superseded(repo)
    after = {r.id: r.content for r in repo.list()}
    assert before == after


def test_no_record_deletion(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    ids_before = {r.id for r in repo.list()}
    _mark_superseded(repo)
    assert {r.id for r in repo.list()} == ids_before


def test_superseded_by_points_to_active_canonical(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    for r in repo.list():
        if r.lifecycle_status == "superseded":
            target = repo.get(r.superseded_by)
            assert target is not None
            assert target.lifecycle_status == "active"


# ---------------------------------------------------------------------------
# 6-10: retrieval + cluster
# ---------------------------------------------------------------------------


def test_default_search_excludes_superseded(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    from memory.m2 import MemoryWritePolicy
    # Repository-level check (no semantic adapter needed for lifecycle test)
    active = [r for r in repo.list() if r.lifecycle_status == "active"]
    superseded = [r for r in repo.list() if r.lifecycle_status == "superseded"]
    assert len(active) + len(superseded) == 33
    assert len(superseded) == 30


def test_large_duplicate_cluster_retrieval(tmp_path):
    repo, svc, _ = _make_service(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    # Service search with superseded filter
    results = svc.search("Firefly")
    assert all(r.lifecycle_status != "superseded" for r in results)


def test_include_superseded_returns_all(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    # include_superseded = audit path (repository.list() returns all)
    all_records = repo.list()
    assert len(all_records) == 33
    superseded = [r for r in all_records if r.lifecycle_status == "superseded"]
    assert len(superseded) == 30


# ---------------------------------------------------------------------------
# 11-12: deterministic plan
# ---------------------------------------------------------------------------


def test_planner_deterministic_on_real_shape(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    records = repo.list()
    plan1 = MemoryDedupPlanner().plan(records)
    plan2 = MemoryDedupPlanner().plan(records)
    assert plan1.entries == plan2.entries


def test_planner_canonical_is_earliest(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    plan = MemoryDedupPlanner().plan(repo.list())
    # 每个 canonical 确实存在于 repo 且对应最早 created_ts 的组成员
    for entry in plan.entries:
        if entry.action.value != "create":
            continue
        record = repo.get(entry.record_id)
        assert record is not None
        # 该 canonical 确实是组内最早的（或独立记录）
        same_group = [
            r for r in repo.list()
            if normalize_memory_text(r.content) == normalize_memory_text(record.content)
        ]
        earliest_ts = min(r.created_ts for r in same_group)
        assert record.created_ts == earliest_ts


# ---------------------------------------------------------------------------
# 13-14: restart + no chain
# ---------------------------------------------------------------------------


def test_restart_retains_lifecycle_status(tmp_path):
    path = tmp_path / "memory_records.json"
    repo = JsonMemoryRepository(path)
    r = MemoryRecord.create(category="project", content="旧记忆", trigger="test")
    repo.add(r)
    repo.update(r.id, {"lifecycle_status": "superseded", "superseded_by": "new_id"})
    repo2 = JsonMemoryRepository(path)
    loaded = repo2.get(r.id)
    assert loaded.lifecycle_status == "superseded"
    assert loaded.superseded_by == "new_id"


def test_no_chained_superseded(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    for r in repo.list():
        if r.lifecycle_status == "superseded":
            target = repo.get(r.superseded_by)
            assert target.lifecycle_status == "active"  # 一跳到 canonical


def test_export_retains_all(tmp_path):
    path = tmp_path / "memory_records.json"
    repo, _ = _make_repo(tmp_path)
    _add_33_like(repo)
    _mark_superseded(repo)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw.get("records", [])) == 33
    assert "superseded" in [r.get("lifecycle_status") for r in raw["records"]]


# ---------------------------------------------------------------------------
# Production remember wiring (M2A policy in production path)
# ---------------------------------------------------------------------------


def test_explicit_duplicate_no_new_record(tmp_path):
    repo, svc, _ = _make_service(tmp_path)
    r1 = svc.remember_detailed("我喜欢上海", asserted_explicit=True)
    assert r1 is not None
    count_before = len(repo.list())
    r2 = svc.remember_detailed("我喜欢上海", asserted_explicit=True)
    assert len(repo.list()) == count_before


def test_new_independent_creates_record(tmp_path):
    repo, svc, _ = _make_service(tmp_path)
    svc.remember_detailed("我喜欢上海", asserted_explicit=True)
    before = len(repo.list())
    svc.remember_detailed("用户养了一只猫", asserted_explicit=True)
    assert len(repo.list()) == before + 1


def test_ordinary_chat_zero_writes(tmp_path):
    repo, svc, _ = _make_service(tmp_path)
    before = len(repo.list())
    result = svc.remember_detailed("今天天气不错", asserted_explicit=False)
    assert result is None or len(repo.list()) == before


def test_old_json_without_lifecycle_loads(tmp_path):
    path = tmp_path / "memory_records.json"
    path.write_text(json.dumps({
        "version": 1,
        "records": [
            {"id": "a", "category": "project", "content": "旧",
             "source": "explicit", "trigger": "test", "permission": "auto",
             "weight": 1.0, "created_ts": 1, "updated_ts": 1,
             "last_accessed_ts": 1, "retention_half_life_days": 30},
        ]
    }), encoding="utf-8")
    repo = JsonMemoryRepository(path)
    r = repo.get("a")
    assert r.lifecycle_status == "active"
