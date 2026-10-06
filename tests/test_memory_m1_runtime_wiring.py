#!/usr/bin/env python3
"""M1 runtime wiring tests (Firefly_Global_Memory_Audit P0-1 fix).

Covers:
1. production ConversationRuntime holds the FULL MemoryService
2. _LegacyMemoryReader no longer substitutes the real service
3. explicit "记住…" writes the Repository
4. explicit "记住…" writes the semantic index
5. ordinary chat never auto-writes (20 turns)
7. repository write failure → index untouched
7b. index write failure → repository still holds the memory
9. conversation-session deletion never deletes memory
10. memory deletion never deletes conversation
17. learning mastery updates never write memory
18/19/20. camera / screen / PageLens content never writes memory
21. persona stays independent of memory
22. BondState stays rule-owned, not memory-controlled
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.companion_runtime import CompanionRuntime, TurnStage, _LegacyMemoryReader
from core.conversation_runtime import ConversationRuntime
from core.conversation_store import ConversationStore
from core.bond_state import BondStateEngine
from memory.mem0_adapter import Hit, Mem0AdapterError
from memory.records import MemoryCategory, MemoryRecord, WritePolicy
from memory.access_mode import MemoryAccessMode
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


# ----------------------------------------------------------------------
# Fakes (no real mem0 / qdrant / network in this file)
# ----------------------------------------------------------------------

class FakeSemanticIndex:
    """In-memory semantic index with failure switches."""

    def __init__(self, *, fail_add: bool = False) -> None:
        self.fail_add = fail_add
        self.entries: dict[str, dict[str, Any]] = {}
        self.add_calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_calls: list[str] = []

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str:
        if self.fail_add:
            raise Mem0AdapterError("index unavailable")
        vid = f"vec-{len(self.entries) + 1}"
        self.entries[vid] = {"text": text, "metadata": dict(metadata or {})}
        self.add_calls.append((text, dict(metadata or {})))
        return vid

    def search(self, query: str, *, limit: int = 5, threshold: float = 0.0):
        return [
            Hit(vid, e["text"], dict(e["metadata"]), 1.0)
            for vid, e in self.entries.items()
        ][:limit]

    def delete(self, vector_id: str) -> bool:
        self.delete_calls.append(vector_id)
        return self.entries.pop(vector_id, None) is not None

    def clear(self, *, access_mode=None) -> int:
        count = len(self.entries)
        self.entries.clear()
        return count

    def list_entries(self, *, limit: int = 1000):
        return [
            Hit(vid, e["text"], dict(e["metadata"]), 1.0)
            for vid, e in self.entries.items()
        ][:limit]


class FakeProvider:
    """Deterministic chat provider (never touches a network)."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return {
            "choices": [{"message": {"role": "assistant", "content": "收到"}}]
        }


class _FakeCharacter:
    def to_system_messages(self):
        return [{"role": "system", "content": "You are Firefly (test)."}]


class _FakeBond:
    """Real engine over a temp file (defaults are phase-consistent)."""

    def __init__(self, base: Path):
        self.engine = BondStateEngine(base / "bond_state.json")

    def read(self):
        return self.engine.read()


def make_service(
    tmp_path: Path, *, dedup_enabled: bool = True
) -> tuple[MemoryService, JsonMemoryRepository, FakeSemanticIndex]:
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    index = FakeSemanticIndex()
    return (
        MemoryService(repo, index, dedup_enabled=dedup_enabled,
                      access_mode=MemoryAccessMode.CONFIRMED_WRITE),
        repo,
        index,
    )


def make_manager(tmp_path: Path, *, dedup_enabled: bool = True):
    """A MemoryManager wired to temp storage (mirrors production wiring)."""
    from memory.memory_manager import MemoryManager

    service, repo, index = make_service(tmp_path, dedup_enabled=dedup_enabled)
    manager = MemoryManager(repository=repo, service=service)
    return manager, service, repo, index


def make_runtime(tmp_path: Path, *, dedup_enabled: bool = True):
    manager, service, repo, index = make_manager(
        tmp_path, dedup_enabled=dedup_enabled)
    runtime = ConversationRuntime(
        memory_manager=manager,
        provider_router=FakeProvider(),
        conversation_store=ConversationStore(tmp_path / "conversation.json"),
        _bond_path=str(tmp_path / "bond_state.json"),
    )
    return runtime, runtime._runtime, service, repo, index


def make_companion(tmp_path: Path, service):
    return CompanionRuntime(
        _FakeCharacter(),
        service,
        ConversationStore(tmp_path / "conv.json"),
        _FakeBond(tmp_path),
        FakeProvider(),
    )


# ======================================================================
# 1-2. production wiring holds the FULL service
# ======================================================================

class TestRuntimeWiring:
    def test_production_runtime_holds_full_memory_service(self, tmp_path):
        runtime, companion, service, _repo, _index = make_runtime(tmp_path)
        assert companion.memory_service is service
        # Full-service surface: remember/delete/clear/list/search all callable.
        for api in ("remember", "remember_detailed", "delete", "list",
                    "search", "clear_all", "consistency_report", "reconcile"):
            assert callable(getattr(companion.memory_service, api)), api

    def test_from_legacy_no_longer_substitutes_legacy_reader(self, tmp_path):
        manager, service, _repo, _index = make_manager(tmp_path)
        companion = CompanionRuntime.from_legacy(
            memory_manager=manager,
            conversation_store=ConversationStore(tmp_path / "conv.json"),
        )
        assert not isinstance(companion.memory_service, _LegacyMemoryReader)
        assert companion.memory_service is manager.service

    def test_legacy_reader_kept_as_read_only_view_only(self, tmp_path):
        """The class survives as a narrow read view — but has no write APIs."""
        manager, _service, _repo, _index = make_manager(tmp_path)
        reader = _LegacyMemoryReader(manager)
        assert callable(reader.search)
        assert not hasattr(reader, "remember_detailed")
        assert not hasattr(reader, "delete")
        assert not hasattr(reader, "clear_all")

    def test_runner_property_walks_to_full_service(self, tmp_path):
        """The runner's memory_service property exposes the same full service."""
        runtime, companion, service, _repo, _index = make_runtime(tmp_path)
        # CharacterConversationRunner.memory_service walks
        # self.runtime._runtime.memory_service — same attribute chain:
        walked = getattr(getattr(runtime, "_runtime", None), "memory_service", None)
        assert walked is service and walked is companion.memory_service


# ======================================================================
# 3-6. explicit remember through a real chat turn
# ======================================================================

class TestExplicitRemember:
    def test_explicit_remember_writes_repository(self, tmp_path):
        runtime, companion, _service, repo, _index = make_runtime(tmp_path)
        before = len(repo.list())
        runtime.chat("记住，我喜欢测试城市。")
        records = repo.list()
        assert len(records) == before + 1
        # The extracted content is stored, never the raw "记住" prefix.
        assert "我喜欢测试城市" in records[-1].content
        assert not records[-1].content.startswith("记住")

    def test_explicit_remember_writes_semantic_index(self, tmp_path):
        runtime, _companion, _service, _repo, index = make_runtime(tmp_path)
        runtime.chat("记住，我喜欢测试城市。")
        assert len(index.entries) == 1
        *_, metadata = index.add_calls[0]
        assert set(metadata) == {"record_id"}
        record_id = metadata["record_id"]
        assert any(r.id == record_id for r in _repo_list(runtime))

    def test_repository_write_failure_blocks_index_write(self, tmp_path):
        class FailingRepo(JsonMemoryRepository):
            def add(self, record, *, access_mode=None):
                raise OSError("disk full")

        repo = FailingRepo(tmp_path / "broken.json")
        index = FakeSemanticIndex()
        companion = make_companion(tmp_path, MemoryService(repo, index, access_mode=MemoryAccessMode.CONFIRMED_WRITE))
        companion.chat("记住，测试写入失败顺序。")
        assert index.add_calls == []          # index never invoked
        assert any(
            e.stage is TurnStage.MEMORY_WRITE
            for e in companion.last_turn_errors
        )

    def test_index_failure_keeps_memory_and_records_error(self, tmp_path):
        repo = JsonMemoryRepository(tmp_path / "memory_records.json")
        index = FakeSemanticIndex(fail_add=True)
        companion = make_companion(tmp_path, MemoryService(repo, index, access_mode=MemoryAccessMode.CONFIRMED_WRITE))
        runtime = companion
        runtime.chat("记住，索引坏了也要保住记忆。")
        records = repo.list()
        assert len(records) == 1              # truth survives
        assert companion.memory_service.index_dirty is True
        assert any(
            e.stage is TurnStage.MEMORY_WRITE for e in companion.last_turn_errors
        )

    def test_ordinary_chat_never_auto_writes(self, tmp_path):
        runtime, _companion, _service, repo, index = make_runtime(tmp_path)
        before_records = len(repo.list())
        before_entries = len(index.entries)
        for i in range(20):
            runtime.chat(f"普通聊天第 {i} 轮，没有记住任何东西")
        assert len(repo.list()) == before_records
        assert len(index.entries) == before_entries


def _repo_list(runtime) -> list:
    return runtime._runtime.memory_service.repository.list()


# ======================================================================
# 9/10. conversation ↔ memory independence
# ======================================================================

class TestConversationMemoryIndependence:
    def test_memory_delete_does_not_touch_conversation(self, tmp_path):
        runtime, companion, service, repo, _index = make_runtime(tmp_path)
        runtime.chat("第一条普通对话")           # persists a turn
        conv_before = runtime.conversation_store.load_working_window()
        result = service.remember_detailed("记住，待删除的记忆", asserted_explicit=True)
        assert service.delete(result.record.id) is True
        conv_after = runtime.conversation_store.load_working_window()
        assert len(conv_after) == len(conv_before)
        assert [t.content for t in conv_after] == [t.content for t in conv_before]

    def test_conversation_delete_does_not_touch_memory(self, tmp_path):
        runtime, companion, service, repo, _index = make_runtime(tmp_path)
        service.remember_detailed("记住，会话删除后仍在的记忆", asserted_explicit=True)
        before = len(repo.list())
        store = runtime.conversation_store
        store.delete_session(store.active_session_id)
        assert len(repo.list()) == before


# ======================================================================
# 17-20. learning / vision boundaries
# ======================================================================

class TestBoundaries:
    def test_learning_mastery_update_never_writes_memory(self, tmp_path):
        from core.learning.store import LearningStore

        service, repo, _index = make_service(tmp_path)
        before = len(repo.list())

        store = LearningStore(tmp_path / "learning.sqlite3")
        store.initialize()
        course = store.create_course("审计课程")
        concept, _created = store.get_or_add_concept(course.id, "反馈控制")
        store.apply_mastery_update(concept.id, 3, "medium", "审计测试")
        assert store.get_concept(concept.id).mastery_level == 3
        assert len(repo.list()) == before       # memory untouched

    def test_learning_never_imports_memory(self):
        """AST: nothing under core/learning imports the memory package."""
        learning_dir = ROOT / "core" / "learning"
        offenders = []
        for path in learning_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module == "memory" or node.module.startswith("memory."):
                        offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "memory" or alias.name.startswith("memory."):
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []

    def test_camera_screen_pagelens_never_write_memory(self, tmp_path):
        """Vision content enters turn_context only; memory count unchanged."""
        runtime, companion, service, repo, index = make_runtime(tmp_path)
        before = len(repo.list())
        vision_blocks = [
            "【相机观察】画面中有一只猫（测试）",
            "【屏幕视觉】可见文本：季度报表（测试）",
            "【PageLens】当前页面：example.com（测试）",
        ]
        for block in vision_blocks:
            runtime.chat("看看这个", turn_context=block)
        assert len(repo.list()) == before
        assert len(index.entries) == 0

    def test_vision_modules_never_import_memory(self):
        for rel in ("core/screen_vision", "core/paper_context.py",
                    "core/page_context.py", "core/pagelens_bridge.py",
                    "core/pdf_ambient_context.py"):
            target = ROOT / rel
            paths = [target] if target.is_file() else sorted(target.rglob("*.py"))
            for path in paths:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        assert not node.module.startswith("memory"), path
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            assert not alias.name.startswith("memory"), path


# ======================================================================
# 21-22. persona / bond boundaries
# ======================================================================

class TestPersonaBondBoundaries:
    def test_persona_files_never_import_or_write_memory(self):
        character_dir = ROOT / "character"
        assert character_dir.is_dir()
        for path in character_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("memory"), path
        # Static persona YAML files exist and are data-only.
        assert (character_dir / "firefly" / "identity.yaml").is_file()

    def test_memory_operations_do_not_modify_bond_state(self, tmp_path):
        runtime, companion, service, _repo, _index = make_runtime(tmp_path)
        bond_before = companion.bond_state_engine.read()
        service.remember_detailed("记住，邦德不受记忆影响", asserted_explicit=True)
        service.remember_detailed("第二条", asserted_explicit=True)
        first = service.list()[0]
        service.delete(first.id)
        bond_after = companion.bond_state_engine.read()
        assert bond_after == bond_before

    def test_llm_turn_cannot_directly_write_bondstate_via_memory(self, tmp_path):
        """BondState changes only via rule signals, never via memory APIs."""
        engine = BondStateEngine(tmp_path / "bond.json")
        with pytest.raises((TypeError, ValueError)):
            engine.apply("记住，我们关系更亲近了")   # non-BondSignal rejected
