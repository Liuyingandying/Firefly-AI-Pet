"""Production service + panel security contracts; isolated repository only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from memory.access_mode import MemoryAccessMode, MemoryAccessViolation
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService, MemoryConflictError, MemoryPolicyError
from memory.m2 import WriteAction, MemoryWritePolicy
from memory.suggestion.suggestion_service import SuggestionService
from memory.suggestion_store import SuggestionStore
from ui.memory_panel import MemoryPanel
from ui.confirmed_memory import confirmed_memory_handle


class Index:
    def __init__(self): self.rows = {}
    def add(self, text, metadata=None):
        key = str(len(self.rows) + 1); self.rows[key] = metadata; return key
    def search(self, *args, **kwargs): return []
    def delete(self, key): return self.rows.pop(key, None) is not None
    def clear(self): self.rows.clear()


@pytest.fixture
def service(tmp_path):
    return MemoryService(JsonMemoryRepository(tmp_path / 'memory.json'), Index(), dedup_enabled=False)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_delete_clear_require_confirmation(service, qapp, monkeypatch):
    record = service.remember('记住：我喜欢散步')
    panel = MemoryPanel(service, None)
    with pytest.raises(PermissionError): panel.delete_memory(record.id)
    with pytest.raises(MemoryAccessViolation): service.delete(record.id)
    panel.memory_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.No)
    panel._delete_selected(); assert service.repository.get(record.id)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.Yes)
    panel._delete_selected(); assert not service.repository.get(record.id)
    assert '已删除' in panel.status_label.text()
    assert not panel.detail_text.toPlainText()
    service.remember('记住：我喜欢晴天'); panel.refresh()
    panel._clear_all(); assert service.repository.list() == []
    assert '已清空' in panel.status_label.text()
    assert service.access_mode is MemoryAccessMode.SAFE_WRITE
    panel.close()


def test_read_only_denial_visible(service, qapp, monkeypatch):
    service.remember('记住：我喜欢散步')
    service.access_mode = MemoryAccessMode.READ_ONLY
    panel = MemoryPanel(service, None); panel.memory_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.Yes)
    panel._delete_selected(); assert '失败' in panel.status_label.text()
    panel._clear_all(); assert '失败' in panel.status_label.text()
    assert len(service.repository.list()) == 1
    panel.close()


def test_city_conflict_real_pending_panel(service, qapp, tmp_path, monkeypatch):
    original = service.remember('记住：我住在城市A')
    pending = SuggestionService(service, store=SuggestionStore(tmp_path/'pending.json'))
    with pytest.raises(MemoryConflictError) as exc:
        service.remember('记住：我搬到城市B')
    pending.queue_conflict(exc.value)
    assert len(service.repository.list()) == 1
    with pytest.raises(MemoryAccessViolation): service.resolve_conflict(exc.value)
    panel = MemoryPanel(service, None, suggestion_service=pending)
    panel.pending_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.No)
    panel._accept_selected(); assert service.repository.get(original.id).lifecycle_status == 'active'
    assert len(pending.list_pending()) == 1
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.Yes)
    panel._accept_selected()
    assert not pending.list_pending()
    active = [r for r in service.repository.list() if r.lifecycle_status == 'active']
    assert [r.content for r in active] == ['我搬到城市B']
    assert service.repository.get(original.id).lifecycle_status == 'superseded'
    panel.close()


def test_stale_confirmation_refused(service):
    old = service.remember('记住：我住在城市A')
    with pytest.raises(MemoryConflictError) as exc: service.remember('记住：我搬到城市B')
    handle = confirmed_memory_handle(service)
    handle.delete(old.id)
    with pytest.raises(MemoryPolicyError): handle.resolve_conflict(exc.value)
    assert not service.repository.list()


@pytest.mark.parametrize('action', list(WriteAction) + ['future_unknown'])
def test_every_write_action_explicit(service, action):
    old = service.remember('记住：测试原始事实')
    policy = MemoryWritePolicy()
    def evaluate(candidate, records):
        decision = policy.evaluate(candidate, [])
        return replace(decision, action=action, target_record_ids=(old.id,))
    service.m2_policy = SimpleNamespace(evaluate=evaluate)
    if action == WriteAction.REQUIRE_CONFIRMATION or not isinstance(action, WriteAction):
        with pytest.raises(MemoryPolicyError): service.remember('记住：完全不同的新事实')
        assert len(service.repository.list()) == 1
    else:
        result = service.remember('记住：完全不同的新事实')
        if action == WriteAction.DO_NOT_PERSIST: assert result is None
        elif action == WriteAction.IGNORE_DUPLICATE: assert result.id == old.id
        else:
            assert result.id != old.id
            if action in (WriteAction.MERGE_UPDATE, WriteAction.SUPERSEDE):
                assert service.repository.get(old.id).lifecycle_status == 'superseded'


def test_machine_observations_only_pending(service, tmp_path):
    from core.learning.skill.loop.memory_writer import LearningMemoryWriter
    from core.extension_api import ExtensionMemory
    pending = SuggestionService(service, store=SuggestionStore(tmp_path/'pending.json'))
    result = LearningMemoryWriter(memory_manager=pending).write_learning_event(
        concept_id='sample', content='隔离测试学习观察', outcome='correct')
    assert result.ok and result.memory_id
    assert ExtensionMemory(pending).remember('隔离测试插件观察')
    assert len(pending.list_pending()) == 2
    assert service.repository.list() == []


@pytest.mark.parametrize('failure', ['index', 'supersede'])
def test_conflict_failure_keeps_only_original_active(service, monkeypatch, failure):
    from memory.service import MemorySynchronizationError
    old = service.remember('记住：我住在城市A')
    with pytest.raises(MemoryConflictError) as exc:
        service.remember('记住：我搬到城市B')
    handle = confirmed_memory_handle(service)
    def fail(*args, **kwargs):
        raise RuntimeError('injected boundary failure')
    if failure == 'index':
        monkeypatch.setattr(service.adapter, 'add', fail)
    else:
        monkeypatch.setattr(handle, 'apply_supersede', fail)
    with pytest.raises((MemoryPolicyError, MemorySynchronizationError)):
        handle.resolve_conflict(exc.value)
    active = [r.id for r in service.repository.list() if r.lifecycle_status == 'active']
    assert active == [old.id]
    assert service.index_dirty and handle.index_dirty


def test_rejected_machine_candidate_is_not_reported_as_written():
    from core.learning.skill.loop.memory_writer import LearningMemoryWriter
    sink = SimpleNamespace(suggest_memory=lambda *args: {'results': []})
    result = LearningMemoryWriter(memory_manager=sink).write_learning_event(
        concept_id='sample', content='rejected test candidate', outcome='correct')
    assert not result.ok and result.error
