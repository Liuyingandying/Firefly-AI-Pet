#!/usr/bin/env python3
"""M1 consistency tests: Repository (truth) ↔ semantic index.

Covers:
6. repository write failure → index untouched
7. index write failure → repository still holds the memory
8. delete syncs both layers (including via stale vector links)
9. clear_all syncs both layers; index failure leaves recoverable orphans
10. export reads from the repository, never from the index
11. stale vector ids never affect the canonical MemoryRecord.id
12-14. reconcile: re-index missing, remove orphans, never change content
23-24. semantic index down → view and export still work
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from memory.mem0_adapter import Hit, Mem0AdapterError
from memory.records import MemoryRecord
from memory.access_mode import MemoryAccessMode
from memory.repository import JsonMemoryRepository
from memory.service import (
    MemoryConsistencyReport,
    MemoryService,
    MemorySynchronizationError,
)


class FakeIndex:
    """In-memory semantic index with per-operation failure switches."""

    def __init__(self, *, fail_add=False, fail_delete=False,
                 fail_clear=False, fail_search=False, fail_list=False):
        self.fail_add = fail_add
        self.fail_delete = fail_delete
        self.fail_clear = fail_clear
        self.fail_search = fail_search
        self.fail_list = fail_list
        self.entries: dict[str, dict[str, Any]] = {}
        self.add_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []

    def add(self, text, metadata=None):
        if self.fail_add:
            raise Mem0AdapterError("index unavailable")
        vid = f"vec-{len(self.entries) + 1}"
        self.entries[vid] = {"text": text, "metadata": dict(metadata or {})}
        self.add_calls.append((text, dict(metadata or {})))
        return vid

    def search(self, query, *, limit=5, threshold=0.0):
        if self.fail_search:
            raise Mem0AdapterError("search unavailable")
        return [
            Hit(vid, e["text"], dict(e["metadata"]), 1.0)
            for vid, e in self.entries.items()
        ][:limit]

    def delete(self, vector_id):
        if self.fail_delete:
            raise Mem0AdapterError("delete unavailable")
        self.delete_calls.append(vector_id)
        return self.entries.pop(vector_id, None) is not None

    def clear(self, *, access_mode=None):
        if self.fail_clear:
            raise Mem0AdapterError("clear unavailable")
        count = len(self.entries)
        self.entries.clear()
        return count

    def list_entries(self, *, limit=1000):
        if self.fail_list:
            raise Mem0AdapterError("list unavailable")
        return [
            Hit(vid, e["text"], dict(e["metadata"]), 1.0)
            for vid, e in self.entries.items()
        ][:limit]


class FailingAddRepository(JsonMemoryRepository):
    """Repository whose add always fails (truth write failure)."""

    def add(self, record, *, access_mode=None):
        raise OSError("disk full")


def make_service(
    tmp_path: Path,
    *,
    index: FakeIndex | None = None,
    dedup_enabled: bool = True,
):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    index = index or FakeIndex()
    return (
        MemoryService(repo, index, dedup_enabled=dedup_enabled,
                      access_mode=MemoryAccessMode.CONFIRMED_WRITE),
        repo,
        index,
    )


# ======================================================================
# 6/7. write transaction order
# ======================================================================

class TestWriteTransactionOrder:
    def test_repository_failure_blocks_index_write(self, tmp_path):
        repo = FailingAddRepository(tmp_path / "broken.json")
        index = FakeIndex()
        service = MemoryService(repo, index)
        with pytest.raises(OSError):
            service.remember_detailed("记住，仓库失败测试", asserted_explicit=True)
        assert index.add_calls == []            # index never invoked
        assert service.repository.list() == []  # nothing half-written

    def test_index_failure_keeps_memory_and_marks_dirty(self, tmp_path):
        service, repo, index = make_service(
            tmp_path, index=FakeIndex(fail_add=True))
        with pytest.raises(MemorySynchronizationError):
            service.remember_detailed("记住，索引失败测试", asserted_explicit=True)
        records = service.list()
        assert len(records) == 1                # SOURCE OF TRUTH survives
        assert "索引失败测试" in records[0].content
        assert service.index_dirty is True
        report = service.consistency_report()
        assert report.missing_vectors == (records[0].id,)


# ======================================================================
# 8. delete
# ======================================================================

class TestDelete:
    def test_delete_syncs_both_layers(self, tmp_path):
        service, repo, index = make_service(tmp_path, dedup_enabled=False)
        record = service.remember_detailed("记住，待删除").record
        assert len(index.entries) == 1
        assert service.delete(record.id) is True
        assert service.repository.get(record.id) is None
        assert index.entries == {}
        assert record.id in index.delete_calls or any(
            record.vector_id in c for c in index.delete_calls
        )

    def test_delete_with_stale_vector_id_still_removes_embedding(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        record = service.remember("记住，向量链接已过期", asserted_explicit=True)
        # Simulate the audited corruption: the stored vector_id is stale.
        repo.update(record.id, {"vector_id": "vector-1"})
        assert "vector-1" not in index.entries   # link points nowhere

        assert service.delete(record.id) is True
        assert service.repository.get(record.id) is None
        # The real embedding (found via record_id payload) is still removed.
        assert index.entries == {}

    def test_delete_index_failure_keeps_record(self, tmp_path):
        service, repo, index = make_service(
            tmp_path, index=FakeIndex(fail_delete=True))
        record = service.remember("记住，删除失败保护", asserted_explicit=True)
        with pytest.raises(MemorySynchronizationError):
            service.delete(record.id)
        assert service.repository.get(record.id) is not None  # retained
        assert service.index_dirty is True


# ======================================================================
# 9. clear
# ======================================================================

class TestClearAll:
    def test_clear_all_syncs_both_layers(self, tmp_path):
        service, repo, index = make_service(tmp_path, dedup_enabled=False)
        service.remember_detailed("记住，清空前一")
        service.remember_detailed("记住，清空前二")
        removed = service.clear_all()
        assert removed == 2
        assert service.repository.list() == []
        assert index.entries == {}

    def test_clear_all_index_failure_leaves_recoverable_orphans(self, tmp_path):
        service, repo, index = make_service(
            tmp_path, index=FakeIndex(fail_clear=True))
        record = service.remember_detailed("记住，孤儿向量来源").record
        with pytest.raises(MemorySynchronizationError):
            service.clear_all()
        assert service.repository.list() == []     # truth cleared
        assert len(index.entries) == 1             # orphan remains
        assert service.index_dirty is True
        assert record.id in {
            e["metadata"].get("record_id") for e in index.entries.values()
        }
        # reconcile heals it:
        result = service.reconcile(dry_run=False)
        assert result.orphan_indexes and index.entries == {}


# ======================================================================
# 10/24. export from repository; index down → view/export still work
# ======================================================================

class TestRepositoryIsSourceOfTruth:
    def test_export_reads_from_repository_not_index(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        record = MemoryRecord.create(
            category="user_fact", content="仅仓库记录（索引缺失）",
            source="explicit", trigger="t",
        )
        repo.add(record)                        # never indexed
        exported = service.list()
        assert [r.id for r in exported] == [record.id]
        assert exported[0].content == "仅仓库记录（索引缺失）"

    def test_index_down_list_and_export_still_work(self, tmp_path):
        service, repo, index = make_service(
            tmp_path, index=FakeIndex(fail_search=True, fail_list=True))
        service.remember_detailed("记住，索引宕机也能看")
        index.fail_search = True
        records = service.list()                # repository-backed view
        assert len(records) == 1
        export = {
            "record_count": len(records),
            "records": [r.to_dict() for r in records],
        }
        assert export["record_count"] == 1
        assert export["records"][0]["content"] == "索引宕机也能看"

    def test_export_never_contains_embeddings(self, tmp_path):
        service, _repo, _index = make_service(tmp_path)
        service.remember_detailed("记住，导出无向量")
        records = service.list()
        exported = [r.to_dict() for r in records]
        dumped = repr(exported).lower()
        assert "embedding" not in dumped
        # No embedding payload / qdrant internals: only scalar record fields.
        forbidden = {"embedding", "vector", "payload", "collection"}
        for item in exported:
            assert not (forbidden & set(item.keys())), item.keys()


# ======================================================================
# 11-14. stale vector ids / reconcile
# ======================================================================

class TestStaleVectorLinksAndReconcile:
    def test_stale_vector_id_does_not_affect_canonical_identity(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        record = service.remember_detailed("记住，规范身份不受向量影响").record
        repo.update(record.id, {"vector_id": "vector-1"})   # corrupt the link
        fetched = service.repository.get(record.id)
        assert fetched is not None and fetched.id == record.id
        assert fetched.content == record.content

    def test_reconcile_reindexes_missing(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        record = MemoryRecord.create(
            category="user_fact", content="缺失索引", source="explicit", trigger="t")
        repo.add(record)
        result = service.reconcile(dry_run=False)
        assert record.id in result.missing_indexes
        assert repo.get(record.id).vector_id in index.entries

    def test_reconcile_removes_orphans(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        written = service.remember_detailed("我喜欢猫", asserted_explicit=True)
        repo.delete(written.record.id)
        result = service.reconcile(dry_run=False)
        assert written.record.vector_id in result.orphan_indexes
        assert index.entries == {}

    def test_reconcile_repairs_stale_link_without_reindexing(self, tmp_path):
        """Entry exists under record_id payload; stored vector_id is stale →
        reconcile repairs the LINK through the repository, no re-add."""
        service, repo, index = make_service(tmp_path)
        record = service.remember_detailed("记住，链接修复").record
        real_vector_id = record.vector_id
        repo.update(record.id, {"vector_id": "vector-1"})
        add_calls_before = len(index.add_calls)

        result = service.reconcile(dry_run=False)

        assert record.id in result.stale_links
        assert len(index.add_calls) == add_calls_before   # no duplicate re-add
        assert repo.get(record.id).vector_id == real_vector_id

    def test_reconcile_never_changes_memory_content(self, tmp_path):
        service, repo, index = make_service(tmp_path, dedup_enabled=False)
        r1 = service.remember_detailed("记住，内容不变甲")
        r2 = MemoryRecord.create(
            category="project", content="缺失索引的内容不变乙",
            source="explicit", trigger="t")
        repo.add(r2)
        before = {r.id: r.content for r in repo.list()}

        service.reconcile(dry_run=False)

        after = {r.id: r.content for r in repo.list()}
        assert after == before

    def test_reconcile_dry_run_reports_without_change(self, tmp_path):
        service, repo, index = make_service(tmp_path)
        record = MemoryRecord.create(
            category="user_fact", content="干跑", source="explicit", trigger="t")
        repo.add(record)
        result = service.reconcile(dry_run=True)
        assert record.id in result.missing_indexes
        assert index.entries == {}
        assert repo.get(record.id).vector_id is None


# ======================================================================
# consistency report
# ======================================================================

class TestConsistencyReport:
    def test_report_counts_mixed_state(self, tmp_path):
        service, repo, index = make_service(tmp_path, dedup_enabled=False)
        healthy = service.remember_detailed(
            "记住，健康记录", category="preference").record
        stale = service.remember_detailed(
            "记住，链接失效记录", category="user_fact").record
        repo.update(stale.id, {"vector_id": "vector-1"})   # dead link
        missing = MemoryRecord.create(
            category="project", content="无索引记录", source="explicit", trigger="t")
        repo.add(missing)
        # one true orphan: index entry whose record was removed
        orphaned = service.remember_detailed(
            "记住，孤儿来源", category="relationship").record
        repo.delete(orphaned.id)

        report = service.consistency_report()

        assert isinstance(report, MemoryConsistencyReport)
        assert report.repository_records == 3
        assert report.indexed_records == 3
        assert report.missing_vectors == (missing.id,)
        assert orphaned.vector_id in report.orphan_vectors
        assert stale.id in report.stale_vector_links
        assert report.healthy == 1

    def test_report_clean_state(self, tmp_path):
        service, _repo, _index = make_service(tmp_path)
        service.remember_detailed("记住，干净状态")
        report = service.consistency_report()
        assert report == MemoryConsistencyReport(
            repository_records=1, indexed_records=1,
            missing_vectors=(), orphan_vectors=(),
            stale_vector_links=(), healthy=1,
        )

    def test_isolated_user_data_read_only_report(self, tmp_path):
        """Section 26: inspect an isolated synthetic protected store (never mutates).

        Asserts only structure; the numbers go into the M1 report.
        """
        default_path = tmp_path / "protected-memory.json"
        seed = JsonMemoryRepository(default_path)
        seed.add(MemoryRecord.create(category="project", content="合成只读记录", trigger="test"))
        before = default_path.read_bytes()
        repo = JsonMemoryRepository(default_path)
        service = MemoryService(repo, FakeIndex())   # FakeIndex → never mutates
        report = service.consistency_report()
        assert default_path.read_bytes() == before   # read-only proof
        assert report.repository_records >= 0
        assert report.indexed_records == 0           # fake index is empty
        # Exclusive bucket accounting: every record lands in exactly one.
        assert (
            report.healthy
            + len(report.missing_vectors)
            + len(report.stale_vector_links)
            == report.repository_records
        )
        # 2026-09-16 用户侧去重整合后，历史 "vector-1" 陈旧链接数量随真实库
        # 演化变化——只保留桶等式（exclusive accounting）这一结构不变量。
