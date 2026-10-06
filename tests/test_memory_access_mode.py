"""M3B.1 access-mode tests — Memory mutation isolation.

Matrix:
- READ_ONLY: search / retrieve / export / consistency report OK;
  remember / edit / delete / clear / supersede / migrate → MemoryAccessViolation
- SAFE_WRITE: + explicit remember, creation-flow bookkeeping (vector_id,
  supersede); delete / clear / import / migrate refused
- CONFIRMED_WRITE: everything

Synthetic protected-store tests (no user data is accessed):
- retrieval resolving synthetic active records leaves memory_records.json
  byte-identical (regression guard for the removed get() access bump)
- export leaves it byte-identical
- opening/refreshing/closing the Memory Manager leaves it byte-identical
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.access_mode import (
    MemoryAccessMode, MemoryAccessViolation,
)
from memory.mem0_adapter import Hit
from memory.records import MemoryRecord, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService

PROTECTED_DB = None


@pytest.fixture(autouse=True)
def isolated_reference_store(tmp_path, monkeypatch):
    path = tmp_path / "protected-memory.json"
    repository = JsonMemoryRepository(path)
    _seed(repository, "隔离存储中的合成记录")
    monkeypatch.setitem(globals(), "PROTECTED_DB", path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _service(tmp_path, mode, adapter=None):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    svc = MemoryService(repo, adapter or _FakeIndex(),
                        write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=mode)
    return repo, svc


class _FakeIndex:
    def __init__(self, score: float = 0.95):
        self._n = 0
        self.score = score
        self.entries: dict[str, dict] = {}

    def add(self, text, metadata=None):
        self._n += 1
        vid = f"vec-{self._n:04d}"
        self.entries[vid] = {"text": text, "metadata": dict(metadata or {})}
        return vid

    def search(self, query, limit=5, threshold=0.0):
        hits = [Hit(vid, e["text"], dict(e["metadata"]), self.score)
                for vid, e in self.entries.items()]
        return hits[:limit]

    def delete(self, vector_id):
        return self.entries.pop(vector_id, None) is not None


def _seed(repo, content, category="project"):
    record = MemoryRecord.create(category=category, content=content, trigger="test")
    repo.add(record)
    return record


# ---------------------------------------------------------------------------
# Mode matrix
# ---------------------------------------------------------------------------


def test_read_only_allows_reads(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.READ_ONLY)
    _seed(repo, "用户喜欢喝咖啡", category="preference")
    assert svc.search("咖啡偏好") is not None
    assert svc.retrieve_for_prompt("咖啡偏好") is not None
    assert repo.export() is not None          # export is a repository read
    assert svc.consistency_report() is not None
    assert svc.list() is not None


def test_read_only_refuses_all_mutations(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.READ_ONLY)
    target = _seed(repo, "将被拒绝的修改")

    with pytest.raises(MemoryAccessViolation):
        svc.remember_detailed("记住：新记忆", asserted_explicit=True)
    with pytest.raises(MemoryAccessViolation):
        svc.delete(target.id)
    with pytest.raises(MemoryAccessViolation):
        svc.clear_all()
    with pytest.raises(MemoryAccessViolation):
        svc.edit_memory(target.id, "改写内容")
    other = _seed(repo, "另一条记录")
    with pytest.raises(MemoryAccessViolation):
        svc.apply_supersede(target.id, other.id)
    with pytest.raises(MemoryAccessViolation):
        svc.import_migrated("迁移内容", category="project", trigger="migration:t")
    with pytest.raises(MemoryAccessViolation):
        svc.reconcile(dry_run=False)
        # and nothing changed beyond the two seeded records
        assert len(repo.list()) == 2


def test_safe_write_allows_remember_and_supersede(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    result = svc.remember_detailed("用户计划考研", category="project",
                                   asserted_explicit=True)
    assert result is not None and result.outcome.value == "created"
    # M2A-style supersede inside the creation flow
    other = _seed(repo, "用户计划保研")
    svc.apply_supersede(other.id, result.record.id, reason="confirmed_conflict")
    assert repo.get(other.id).lifecycle_status == "superseded"


def test_safe_write_refuses_delete_clear_import_migrate(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    result = svc.remember_detailed("记住：目标记录", asserted_explicit=True)

    with pytest.raises(MemoryAccessViolation):
        svc.delete(result.record.id)
    with pytest.raises(MemoryAccessViolation):
        svc.clear_all()
    with pytest.raises(MemoryAccessViolation):
        svc.import_migrated("迁移内容", category="project", trigger="migration:t")
    with pytest.raises(MemoryAccessViolation):
        svc.reconcile(dry_run=False)
    with pytest.raises(MemoryAccessViolation):
        svc.edit_memory(result.record.id, "改写")
    # record still there
    assert repo.get(result.record.id) is not None


def test_safe_write_update_patch_whitelist(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    record = _seed(repo, "原始内容")
    # creation-flow bookkeeping is allowed…
    repo.update(record.id, {"vector_id": "vec-x"}, access_mode=svc.access_mode)
    repo.update(record.id, {"lifecycle_status": "superseded",
                            "superseded_by": record.id,
                            "supersede_reason": "t"},
                access_mode=svc.access_mode)
    # …content/weight/timestamp edits are CONFIRMED_WRITE
    with pytest.raises(MemoryAccessViolation):
        repo.update(record.id, {"content": "改写"}, access_mode=svc.access_mode)
    with pytest.raises(MemoryAccessViolation):
        repo.update(record.id, {"weight": 2.0}, access_mode=svc.access_mode)
    with pytest.raises(MemoryAccessViolation):
        repo.update(record.id, {"last_accessed_ts": 1}, access_mode=svc.access_mode)


def test_confirmed_write_allows_everything(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.CONFIRMED_WRITE)
    result = svc.remember_detailed("记住：将被删除", asserted_explicit=True)
    assert svc.delete(result.record.id) is True
    assert repo.get(result.record.id) is None


def test_edit_memory_duplicate_of_other_record_is_refused(tmp_path):
    """Regression: the duplicate branch must return the graceful message.

    ``edit_memory`` referenced ``WriteAction.IGNORE_DUPLICATE`` while the
    module imports it as ``M2Action`` — the branch raised ``NameError``
    instead of returning ``{"action": "duplicate", ...}`` whenever the new
    content duplicated ANOTHER active record (release-readiness audit,
    2026-09-30). Editing a record to its own current content is not this
    branch: the candidate is evaluated against the OTHER records only.
    """
    repo, svc = _service(tmp_path, MemoryAccessMode.CONFIRMED_WRITE)
    first = _seed(repo, "项目甲进行中")
    second = _seed(repo, "项目乙进行中")

    result = svc.edit_memory(first.id, "项目乙进行中")  # duplicates `second`

    assert result["action"] == "duplicate"
    assert result["existing_id"] == second.id
    assert result["message"] == "这条记忆已经存在。"
    # nothing was written or superseded
    assert len(repo.list()) == 2
    assert repo.get(first.id).lifecycle_status == "active"
    assert repo.get(second.id).lifecycle_status == "active"


def test_edit_memory_normal_path_supersedes_original(tmp_path):
    """The full confirmed edit path: new revision active, old superseded."""
    repo, svc = _service(tmp_path, MemoryAccessMode.CONFIRMED_WRITE)
    original = _seed(repo, "项目甲进行中")

    result = svc.edit_memory(original.id, "项目丙已完成")

    assert result["action"] == "created"
    assert result["old_id"] == original.id
    new_record = result["record"]
    assert repo.get(new_record.id).content == "项目丙已完成"
    assert repo.get(new_record.id).lifecycle_status == "active"
    assert repo.get(original.id).lifecycle_status == "superseded"
    assert repo.get(original.id).superseded_by == new_record.id


def test_get_is_pure_read_no_persist(tmp_path):
    repo, svc = _service(tmp_path, MemoryAccessMode.CONFIRMED_WRITE)
    record = _seed(repo, "纯读检查")
    path = tmp_path / "memory_records.json"
    before = path.read_bytes()
    for _ in range(3):
        assert repo.get(record.id) is not None
    assert path.read_bytes() == before


def test_import_data_requires_confirmed_write(tmp_path):
    repo, _svc = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    payload = {"version": 1, "records": []}
    with pytest.raises(MemoryAccessViolation):
        repo.import_data(payload, access_mode=MemoryAccessMode.SAFE_WRITE)
    assert repo.import_data(payload, access_mode=MemoryAccessMode.CONFIRMED_WRITE) == 0


# ---------------------------------------------------------------------------
# Real-data protection (READ-ONLY on the real store, never written)
# ---------------------------------------------------------------------------


def _protected_active_record():
    if not PROTECTED_DB.is_file():
        pytest.skip("real memory_records.json not present")
    data = json.loads(PROTECTED_DB.read_text(encoding="utf-8"))
    active = [r for r in data.get("records", [])
              if r.get("lifecycle_status", "active") == "active"]
    if not active:
        pytest.skip("real store has no active records")
    return active[0]


def test_isolated_retrieval_resolving_real_records_leaves_file_identical(tmp_path, tmp_path_factory):
    """Regression guard: retrieval resolves hits via repository.get(); that
    path used to bump last_accessed_ts and persist — it must stay pure."""
    real = _protected_active_record()
    workdir = tmp_path_factory.mktemp("m3b1_real")
    copy = workdir / "memory_records.json"
    copy.write_bytes(PROTECTED_DB.read_bytes())

    class _ResolvingIndex(_FakeIndex):
        """Serves one hit pointing at the synthetic active record."""

        def search(self, query, limit=5, threshold=0.0):
            return [Hit("real-vec", real["content"],
                        {"record_id": real["id"]}, 0.95)]

    repo = JsonMemoryRepository(copy)
    svc = MemoryService(repo, _ResolvingIndex(),
                        write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=MemoryAccessMode.READ_ONLY)
    sha_before = _sha(copy)
    out = svc.retrieve_for_prompt("任何查询文本")
    assert len(out) == 1 and out[0].id == real["id"]
    assert _sha(copy) == sha_before


def test_isolated_export_leaves_file_identical():
    if not PROTECTED_DB.is_file():
        pytest.skip("real memory_records.json not present")
    repo = JsonMemoryRepository(PROTECTED_DB)
    before = _sha(PROTECTED_DB)
    snapshot = repo.export()
    assert snapshot["version"] == 1
    assert len(snapshot["records"]) == len(repo.list())
    assert _sha(PROTECTED_DB) == before


def test_isolated_remember_writes_copy_not_real_store(tmp_path):
    """remember changes the hash of ITS OWN store — and only there."""
    if not PROTECTED_DB.is_file():
        pytest.skip("real memory_records.json not present")
    real_sha_before = _sha(PROTECTED_DB)

    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    svc = MemoryService(repo, _FakeIndex(),
                        write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=MemoryAccessMode.SAFE_WRITE)
    store_path = tmp_path / "memory_records.json"
    _seed(repo, "已有一条既有记忆")            # materialise the store file
    sha_before = _sha(store_path)
    result = svc.remember_detailed("记住：访问隔离验证", asserted_explicit=True)
    assert result is not None
    assert _sha(store_path) != sha_before          # its own store changed
    assert _sha(PROTECTED_DB) == real_sha_before        # real store untouched


def test_isolated_delete_requires_confirmed_write_on_copy(tmp_path):
    """delete is CONFIRMED_WRITE: SAFE_WRITE raises (store unchanged),
    CONFIRMED_WRITE performs it (store changes)."""
    repo, svc_safe = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    record = _seed(repo, "删除目标")
    path = tmp_path / "memory_records.json"
    sha_before = _sha(path)
    with pytest.raises(MemoryAccessViolation):
        svc_safe.delete(record.id)
    assert _sha(path) == sha_before

    from memory.service import MemoryService as _MS
    svc_confirmed = _MS(repo, _FakeIndex(),
                        write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=MemoryAccessMode.CONFIRMED_WRITE)
    assert svc_confirmed.delete(record.id) is True
    assert _sha(path) != sha_before


# ---------------------------------------------------------------------------
# Memory Manager UI: viewing must not write
# ---------------------------------------------------------------------------


def test_manager_open_refresh_close_leaves_store_identical(tmp_path, qapp=None):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from ui.memory_manager import MemoryManagerWindow

    repo, svc = _service(tmp_path, MemoryAccessMode.SAFE_WRITE)
    _seed(repo, "窗口隔离检查一")
    _seed(repo, "窗口隔离检查二", category="preference")
    path = tmp_path / "memory_records.json"
    sha_before = _sha(path)

    window = MemoryManagerWindow(svc)
    try:
        # Opening a window never grants a mutation capability; confirmation does.
        from memory.access_mode import MemoryAccessMode as _M
        assert window._service.access_mode is _M.SAFE_WRITE
        assert window._service.repository is repo
        # …and opening/refreshing/closing wrote nothing
        window._refresh_list()
        assert _sha(path) == sha_before
    finally:
        window.close()
    assert _sha(path) == sha_before
