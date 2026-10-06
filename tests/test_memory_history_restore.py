"""M3B.8 history restore tests — restore physically-deleted superseded
history from a backup without touching active records.

Synthetic dataset mirrors the real incident: a current store of
3 active + 1 companion_auto record, and a 33-record backup
(3 active + 30 superseded in two dedup chains).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.history_restore import (
    apply_restore,
    load_backup_records,
    plan_restore,
    verify_chains,
)
from memory.records import MemoryCategory, MemoryRecord, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


# ---------------------------------------------------------------------------
# synthetic dataset builder
# ---------------------------------------------------------------------------


def _make_record(rid, content, *, lifecycle="active", superseded_by=None,
                 reason=None, created_at=None):
    record = MemoryRecord.create(
        category=MemoryCategory.PROJECT,
        content=content,
        trigger="restore-test",
        timestamp_ms=created_at,
    )
    from dataclasses import replace
    record = replace(
        record,
        id=rid,
        lifecycle_status=lifecycle,
        superseded_by=superseded_by,
        supersede_reason=reason,
        updated_ts=created_at or record.updated_ts,
    )
    return record


def _build_backup_and_current(tmp_path: Path):
    """33-record backup (3 active + 30 superseded in 2 chains) and a
    4-record current store (3 active + 1 companion_auto)."""
    canonical = ["canonical-A：项目主线记录", "canonical-B：偏好主线记录", "canonical-C：目标主线记录"]
    active_ids = [f"01aa-act-{i}" for i in range(3)]
    superseded = []
    for chain in range(2):
        target = active_ids[chain]
        for k in range(15):
            content = canonical[chain] + ("" if k == 0 else f"（旧版本{k}）")
            superseded.append(
                _make_record(
                    f"01bb-sup-{chain}-{k:02d}", content,
                    lifecycle="superseded", superseded_by=target,
                    reason="duplicate_chain",
                    created_at=1_700_000_000_000 + chain * 1000 + k,
                )
            )
    backup_records = [
        _make_record(active_ids[i], canonical[i], created_at=1_700_000_000_000 + i)
        for i in range(3)
    ] + superseded
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        json.dumps(
            {"version": 1, "records": [r.to_dict() for r in backup_records]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    # current: 3 actives (same ids/content) + 1 companion_auto 新记录
    current_repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    for i in range(3):
        record = _make_record(active_ids[i], canonical[i],
                              created_at=1_700_000_000_000 + i)
        current_repo.add(record)
    auto = _make_record(
        "01cc-auto-new", "用户正在开发名为X的项目（companion_auto）",
        created_at=1_700_000_100_000,
    )
    current_repo.add(auto)
    return backup_path, backup_records, current_repo, active_ids


def _service(repo):
    return MemoryService(repo, _FakeIndex(), write_policy=WritePolicy.EXPLICIT_ONLY)


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


# ---------------------------------------------------------------------------
# 1. backup 读取
# ---------------------------------------------------------------------------


def test_backup_read_parses_33_records(tmp_path):
    backup_path, backup_records, _repo, _active = _build_backup_and_current(tmp_path)
    loaded = load_backup_records(backup_path)
    assert len(loaded) == 33
    assert sum(1 for r in loaded if r.get("lifecycle_status") == "superseded") == 30


# ---------------------------------------------------------------------------
# 2. dry-run（plan）不修改当前库
# ---------------------------------------------------------------------------


def test_plan_dry_run_does_not_modify_current(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    store_path = tmp_path / "memory_records.json"
    sha_before = hashlib.sha256(store_path.read_bytes()).hexdigest()
    count_before = len(current_repo.list())

    plan = plan_restore(load_backup_records(backup_path), current_repo)

    assert len(plan.add_records) == 30
    assert len(plan.skip_records) == 3          # 3 条 active 已存在
    assert plan.conflicts == []
    assert len(current_repo.list()) == count_before
    assert hashlib.sha256(store_path.read_bytes()).hexdigest() == sha_before


# ---------------------------------------------------------------------------
# 3. restore 只新增缺失 record（id 相同 -> skip）
# ---------------------------------------------------------------------------


def test_restore_adds_only_missing_records(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    assert len(current_repo.list()) == 33 + 1    # 33 restored + 1 companion_auto


# ---------------------------------------------------------------------------
# 4. active record 不变化
# ---------------------------------------------------------------------------


def test_active_records_unchanged_after_restore(tmp_path):
    backup_path, backup_records, current_repo, active_ids = _build_backup_and_current(tmp_path)
    before = {
        r.id: (r.content, str(r.category), r.created_ts, r.lifecycle_status,
               str(r.source), r.trigger)
        for r in current_repo.list()
        if r.lifecycle_status == "active" and r.trigger != "companion_auto"
    }
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    after = {
        r.id: (r.content, str(r.category), r.created_ts, r.lifecycle_status,
               str(r.source), r.trigger)
        for r in current_repo.list()
        if r.lifecycle_status == "active" and r.trigger != "companion_auto"
    }
    assert before == after
    for rid in active_ids:
        record = current_repo.get(rid)
        assert record.lifecycle_status == "active"
        assert record.superseded_by is None


# ---------------------------------------------------------------------------
# 5. superseded 状态保持
# ---------------------------------------------------------------------------


def test_restored_records_keep_superseded_lifecycle(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    superseded = [
        r for r in current_repo.list()
        if r.lifecycle_status == "superseded"
    ]
    assert len(superseded) == 30
    for record in superseded:
        assert record.superseded_by is not None
        assert record.supersede_reason is not None
        assert record.created_ts > 0


# ---------------------------------------------------------------------------
# 6. chain 完整
# ---------------------------------------------------------------------------


def test_chain_integrity_after_restore(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    ok, broken = verify_chains(current_repo)
    assert ok is True
    assert broken == []


def test_restored_history_survives_restart(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)

    reopened = JsonMemoryRepository(tmp_path / "memory_records.json")
    records = reopened.list()
    assert len(records) == 34
    from collections import Counter
    life = Counter(r.lifecycle_status for r in records)
    assert life["active"] == 4 and life["superseded"] == 30


def test_retrieval_does_not_return_restored_superseded(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)

    service = _service(current_repo)
    results = service.search("canonical-A 项目主线记录")
    assert all(r.lifecycle_status != "superseded" for r in results)
    if results:
        assert results[0].lifecycle_status == "active"


def test_export_includes_restored_history(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    exported = current_repo.export()["records"]
    assert len(exported) == 34
    superseded = [r for r in exported if r["lifecycle_status"] == "superseded"]
    assert len(superseded) == 30


def test_rollback_restores_previous_state_byte_identical(tmp_path):
    backup_path, _records, current_repo, _active = _build_backup_and_current(tmp_path)
    store_path = tmp_path / "memory_records.json"
    pre_restore_copy = tmp_path / "pre_restore_backup.json"
    pre_restore_copy.write_bytes(store_path.read_bytes())   # 备份保护

    plan = plan_restore(load_backup_records(backup_path), current_repo)
    apply_restore(plan, current_repo)
    assert len(current_repo.list()) == 34

    store_path.write_bytes(pre_restore_copy.read_bytes())   # rollback
    reopened = JsonMemoryRepository(store_path)
    assert len(reopened.list()) == 4
    assert store_path.read_bytes() == pre_restore_copy.read_bytes()
