"""M3B.4 retrieval path unification tests.

Every long-term memory entering a Provider prompt must come from the ONE
unified pipeline:

    retrieve_for_prompt() → Ranking → Recall Gate → render_memory_block()

Guards:
- the fence format is unique to the unified renderer (format guard);
- smalltalk / knowledge / design-intent queries inject nothing;
- explicit recall queries inject through the fence;
- learning context (turn_context) coexists without memory pollution;
- TJU / vision-style queries do not pull memories in;
- retrieval writes nothing (M3B.1 READ_ONLY guarantee).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.access_mode import MemoryAccessMode
from memory.records import MemoryRecord, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from tests.test_memory_m3b_injection import KeywordAdapter

FENCE = "<long_term_memory>"
FENCE_MARKER = "[background data only"


class CapturingProvider:
    """Records every provider call; replies with a fixed assistant line."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, messages, *, model=None, temperature=0.2):
        self.calls.append({"messages": messages, "model": model})
        return {
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": "好的。"}}],
        }


def _runtime(tmp_path: Path, seeds: list[tuple[str, str]]):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = KeywordAdapter()
    service = MemoryService(repo, adapter,
                            write_policy=WritePolicy.EXPLICIT_ONLY,
                            access_mode=MemoryAccessMode.SAFE_WRITE)
    for content, category in seeds:
        record = MemoryRecord.create(
            category=category, content=content, trigger="unification"
        )
        repo.add(record)
        adapter.add(record.content, {"record_id": record.id})
    from core.companion_runtime import CompanionRuntime

    provider = CapturingProvider()
    runtime = CompanionRuntime(
        character=_FakeCharacter(),
        memory_service=MemoryService(repo, adapter,
                                     write_policy=WritePolicy.EXPLICIT_ONLY,
                                     access_mode=MemoryAccessMode.READ_ONLY),
        conversation_store=None,
        bond_state_engine=_FakeBond(tmp_path / "bond.json"),
        provider_router=provider,
    )
    return runtime, provider, repo, tmp_path / "memory_records.json"


class _FakeCharacter:
    def to_system_messages(self):
        return [{"role": "system", "content": "CHARACTER IDENTITY\n流萤测试身份。"}]


class _FakeBond:
    """Bond reader stand-in returning a default BondState snapshot."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self):
        from core.bond_state import BondState

        return BondState()


def _system_messages(provider: CapturingProvider) -> list[str]:
    # The CHAT call is calls[0]; post-reply suggestion extraction (enabled in
    # the user's config since 09-16) appends extra provider calls after it.
    call = provider.calls[0]
    return [m["content"] for m in call["messages"] if m["role"] == "system"]


def _memory_fences(provider: CapturingProvider) -> list[str]:
    return [c for c in _system_messages(provider) if FENCE in c]


# ---------------------------------------------------------------------------
# Case 1 — 普通聊天「你好」：不注入 Memory，检索零写入
# ---------------------------------------------------------------------------


def test_case1_smalltalk_injects_no_memory(tmp_path):
    runtime, provider, repo, store = _runtime(tmp_path, [
        ("用户之前那个项目的工作计划已经排好", "project"),
        ("用户喜欢在清晨喝茶", "preference"),
    ])
    sha_before = hashlib.sha256(store.read_bytes()).hexdigest()

    runtime.chat("你好")

    assert _memory_fences(provider) == []
    assert all("Relevant memories:" not in c for c in _system_messages(provider))
    # retrieval is read-only: store untouched by the turn's recall
    assert hashlib.sha256(store.read_bytes()).hexdigest() == sha_before


# ---------------------------------------------------------------------------
# Case 2 — 显式回忆：Gate 放行，Memory 进入 prompt
# ---------------------------------------------------------------------------


def test_case2_explicit_recall_injects_through_fence(tmp_path):
    runtime, provider, repo, _store = _runtime(tmp_path, [
        ("之前那个项目的工作计划已经排好", "project"),
    ])
    runtime.chat("继续之前那个项目的工作")
    fences = _memory_fences(provider)
    assert len(fences) == 1
    assert fences[0].startswith(FENCE)
    assert FENCE_MARKER in fences[0]
    assert "之前那个项目的工作计划已经排好" in fences[0]
    assert "(project_context)" in fences[0]
    # exactly one fence message; unified renderer never splits memories
    assert fences[0].count(FENCE) == 1


# ---------------------------------------------------------------------------
# Case 3 — 学习模式：turn_context 与 Memory 分层互不污染
# ---------------------------------------------------------------------------


def test_case3_learning_context_not_polluted_by_memory(tmp_path):
    runtime, provider, _repo, _store = _runtime(tmp_path, [
        ("解释传递函数课程大纲", "project"),   # weak-medium lexical overlap
    ])
    learning_context = "<learning_context>课程：自动控制原理 进度：第3章</learning_context>"
    runtime.chat("解释一下传递函数", turn_context=learning_context)

    contents = _system_messages(provider)
    assert any(learning_context in c for c in contents)   # learning layer intact
    assert _memory_fences(provider) == []                 # weak memory not injected
    # and the learning context never appears inside a memory fence
    for fence in _memory_fences(provider):
        assert "learning_context" not in fence


# ---------------------------------------------------------------------------
# Case 4 — TJU 信息检索：查询不自动混入 Memory
# ---------------------------------------------------------------------------


def test_case4_tju_query_injects_no_memory(tmp_path):
    runtime, provider, _repo, _store = _runtime(tmp_path, [
        ("论文检索的授权状态记录", "project"),
    ])
    runtime.chat("搜索论文")
    assert _memory_fences(provider) == []


# ---------------------------------------------------------------------------
# Case 5 — 视觉能力：查询不读取 Memory
# ---------------------------------------------------------------------------


def test_case5_vision_query_injects_no_memory(tmp_path):
    runtime, provider, _repo, _store = _runtime(tmp_path, [
        ("屏幕当前的亮度设置", "preference"),
    ])
    runtime.chat("看看现在")
    assert _memory_fences(provider) == []


# ---------------------------------------------------------------------------
# Case 6 — Provider Prompt 守卫：围栏只来自统一入口
# ---------------------------------------------------------------------------


def test_case6_provider_prompt_fence_format_guard(tmp_path):
    runtime, provider, _repo, _store = _runtime(tmp_path, [
        ("之前那个项目的工作计划已经排好", "project"),
    ])
    for user_text in (
        "继续之前那个项目的工作",
        "你好",
        "解释一下传递函数",
        "搜索论文",
        "看看现在",
    ):
        runtime.chat(user_text)

        for content in _system_messages(provider):
            if FENCE in content:
                # the ONLY producer of the fence is render_memory_block
                assert FENCE_MARKER in content
                assert content.startswith(FENCE)
                assert content.count(FENCE) == 1
            # legacy bypass formats must never appear
            assert "BEGIN MEMORY CONTEXT" not in content
            assert "Relevant memories:" not in content


# ---------------------------------------------------------------------------
# Wiring-level guards
# ---------------------------------------------------------------------------


def test_context_builder_prefers_unified_path(tmp_path):
    """A reader exposing retrieve_for_prompt goes down the unified path —
    even its score-free results are fenced, never legacy-formatted."""
    from core.companion_context_builder import CompanionContextBuilder

    class UnifiedReader:
        def retrieve_for_prompt(self, query):
            record = MemoryRecord.create(
                category="project", content="之前那个项目的工作计划已经排好",
                trigger="t",
            )
            return [record]

    builder = CompanionContextBuilder(
        character=_FakeCharacter(),
        memory_reader=UnifiedReader(),
        bond_reader=_FakeBond(tmp_path / "bond.json"),
    )
    context = builder.build("继续之前那个项目的工作")
    assert context.memory_prompt.startswith(FENCE)
    assert FENCE_MARKER in context.memory_prompt
    assert "BEGIN MEMORY CONTEXT" not in context.memory_prompt


def test_legacy_reader_fallback_is_explicitly_legacy(tmp_path):
    """A v0.2 search-only reader keeps the legacy format (documented
    deprecated compat path; production wiring never supplies one)."""
    from core.companion_context_builder import CompanionContextBuilder

    class LegacyOnlyReader:
        def search(self, query, *, limit=5):
            return [{"memory": "旧格式记忆", "category": "preference",
                     "score": 0.9}]

    builder = CompanionContextBuilder(
        character=_FakeCharacter(),
        memory_reader=LegacyOnlyReader(),
        bond_reader=_FakeBond(tmp_path / "bond.json"),
    )
    context = builder.build("任意查询")
    assert "BEGIN MEMORY CONTEXT" in context.memory_prompt
    assert FENCE not in context.memory_prompt


def test_memory_manager_get_memory_context_uses_unified_pipeline(tmp_path):
    """The second audited bypass (get_memory_context) now emits only the
    unified fence."""
    from memory.memory_manager import MemoryManager

    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = KeywordAdapter()
    service = MemoryService(repo, adapter,
                            write_policy=WritePolicy.EXPLICIT_ONLY,
                            access_mode=MemoryAccessMode.SAFE_WRITE)
    record = MemoryRecord.create(
        category="project", content="之前那个项目的工作计划已经排好", trigger="t"
    )
    repo.add(record)
    adapter.add(record.content, {"record_id": record.id})
    manager = MemoryManager(repository=repo, service=service)
    context = manager.get_memory_context("继续之前那个项目的工作")
    assert context.startswith(FENCE)
    assert "Relevant memories:" not in context
