"""Companion Memory Boundary Repair v1 — regression contract.

Freezes the repaired boundary:

1. 100 turns of ordinary chat (with a working LLM extractor that returns
   real JSON candidates every turn) -> MemoryRepository count UNCHANGED;
   candidates land in SuggestionService pending.
2. Explicit "记住我喜欢xxx" -> normal write.
3. companion_auto / conversation_summary triggers can NEVER write memory,
   even with asserted_explicit=True (hard guard in MemoryService).
4. Session summaries stay in the ConversationStore (set_summary) and never
   produce a memory write.

Uses the frozen production config (suggestion auto-extract enabled,
auto-write disabled) via load_companion_config to prove candidates remain
pending until the user confirms them.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.companion_config import load_companion_config
from core.companion_runtime import CompanionRuntime
from core.bond_state import BondStateEngine
from memory.records import MemoryCategory
from memory.repository import JsonMemoryRepository
from memory.service import AUTO_SOURCE_TRIGGERS, MemoryService
from memory.suggestion.memory_candidate_detector import MemorySuggestion


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


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


class _FakeChar:
    def to_system_messages(self):
        return [{"role": "system", "content": "CHARACTER IDENTITY\n流萤测试。"}]


class _DualProvider:
    """Chat reply for normal turns; JSON candidates for extraction prompts."""

    def __init__(self) -> None:
        self.chat_calls = 0
        self.extraction_calls = 0

    def chat(self, messages, *, model=None, temperature=0.2):
        text = " ".join(str(m.get("content", "")) for m in messages)
        if "记忆分析助手" in text:
            self.extraction_calls += 1
            payload = [
                {"content": f"自动候选内容{self.extraction_calls}号",
                 "category": "project", "reason": "boundary-test",
                 "evidence": [], "confidence": 0.9},
            ]
            body = json.dumps(payload, ensure_ascii=False)
        else:
            self.chat_calls += 1
            body = "收到，我在。"
        return {
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": body}}],
        }


def _runtime(tmp_path: Path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = _FakeIndex()
    config = load_companion_config()  # 生产冻结配置：抽取开启，自动写入关闭
    provider = _DualProvider()
    runtime = CompanionRuntime(
        character=_FakeChar(),
        memory_service=MemoryService(
            repo, adapter, write_policy=config.memory.write_policy
        ),
        conversation_store=None,
        bond_state_engine=BondStateEngine(tmp_path / "bond.json"),
        provider_router=provider,
        config=config,
    )
    return runtime, provider, repo


# ---------------------------------------------------------------------------
# 1. 100 turns of ordinary chat -> Memory count unchanged
# ---------------------------------------------------------------------------


def test_100_ordinary_turns_do_not_grow_memory(tmp_path):
    runtime, provider, repo = _runtime(tmp_path)

    for i in range(100):
        runtime.chat(f"普通聊天第{i}轮，聊聊今天的进展")

    assert len(repo.list()) == 0                      # 硬边界：一条都不写
    assert provider.extraction_calls == 100           # 抽取照常运行（不关闭）
    service = runtime.suggestion_service
    assert service is not None
    assert len(service.list_pending()) > 0            # 候选进 pending 等确认


# ---------------------------------------------------------------------------
# 2. explicit remember still writes
# ---------------------------------------------------------------------------


def test_explicit_remember_still_writes(tmp_path):
    runtime, _provider, repo = _runtime(tmp_path)
    runtime.chat("记住我喜欢喝茶")
    records = repo.list()
    assert len(records) == 1
    assert "喝茶" in records[0].content
    assert records[0].trigger == "explicit-command"


# ---------------------------------------------------------------------------
# 3. companion_auto -> suggestion pending, never memory; direct calls refused
# ---------------------------------------------------------------------------


def test_companion_auto_candidates_stay_pending(tmp_path):
    runtime, _provider, repo = _runtime(tmp_path)
    service = runtime.suggestion_service
    memory_path = tmp_path / "memory_records.json"
    before_count = len(repo.list())
    before_hash = (
        hashlib.sha256(memory_path.read_bytes()).hexdigest()
        if memory_path.exists()
        else None
    )

    assert service.auto_write_enabled is False

    runtime.chat("我正在做 Firefly 语音模块")
    pending = service.list_pending()
    after_hash = (
        hashlib.sha256(memory_path.read_bytes()).hexdigest()
        if memory_path.exists()
        else None
    )

    assert len(pending) >= 1
    assert all(s.source == "companion_auto" for s in pending)
    assert all(s.status == "pending" for s in pending)
    assert len(repo.list()) == before_count           # 不进 Memory
    assert after_hash == before_hash                  # Memory 文件不变化


def test_auto_triggers_are_refused_even_with_asserted_explicit(tmp_path):
    runtime, _provider, repo = _runtime(tmp_path)
    for trigger in sorted(AUTO_SOURCE_TRIGGERS):
        result = runtime.memory_service.remember_detailed(
            f"机器来源内容({trigger})", trigger=trigger, asserted_explicit=True
        )
        assert result is None, trigger
    assert len(repo.list()) == 0


def test_auto_trigger_guard_constant_frozen():
    assert AUTO_SOURCE_TRIGGERS == frozenset(
        {"companion_auto", "conversation_summary", "system_generated"}
    )


def test_allowed_triggers_are_not_banned():
    # user_explicit / suggestion_confirmed 是仅有的两条合法写入触发器
    assert "user_explicit" not in AUTO_SOURCE_TRIGGERS
    assert "suggestion_confirmed" not in AUTO_SOURCE_TRIGGERS


# ---------------------------------------------------------------------------
# 4. pending -> user confirmation -> memory (write path B)
# ---------------------------------------------------------------------------


def test_user_confirmation_promotes_pending_to_memory(tmp_path):
    runtime, _provider, repo = _runtime(tmp_path)
    service = runtime.suggestion_service
    runtime.chat("我正在做 Firefly 语音模块")
    accepted_candidate = service.list_pending()[0]    # chat 抽取出的候选
    record = service.accept(accepted_candidate)       # 用户在 待确认 tab 确认
    assert record is not None
    assert record.trigger == "suggestion_confirmed"   # M3B.7 确认触发器
    assert len(repo.list()) == 1
    assert service._decisions[accepted_candidate.id] == "accepted"

    rejected_candidate = MemorySuggestion(
        content="第二条候选", category=MemoryCategory.PROJECT, reason="r2",
        evidence=(), confidence=0.8, source="companion_auto", status="pending",
    )
    service._pending.append(rejected_candidate)
    service.reject(rejected_candidate)                # 拒绝 → 不写
    assert service._decisions[rejected_candidate.id] == "rejected"
    assert len(repo.list()) == 1


# ---------------------------------------------------------------------------
# 5. conversation summary -> ConversationStore only
# ---------------------------------------------------------------------------


def test_summary_goes_to_conversation_store_not_memory():
    import inspect

    import ui.v2.console as console_mod

    source = inspect.getsource(
        console_mod.CompanionConsole._summarize_session_before_switch
    )
    assert source.lstrip().startswith(
        "def _summarize_session_before_switch(self, store, old_session_id):"
    )
    assert "remember(" not in source
    assert "asserted_explicit" not in source
    assert "set_summary" in source


# ---------------------------------------------------------------------------
# extra: suggestion model carries the repaired source semantics
# ---------------------------------------------------------------------------


def test_suggestion_source_semantics():
    s = MemorySuggestion(
        content="内容", category=MemoryCategory.PROJECT, reason="r",
        evidence=(), confidence=0.9, source="companion_auto", status="pending",
    )
    assert s.source == "companion_auto" and s.status == "pending"
    d = s.to_dict()
    assert d["source"] == "companion_auto" and d["status"] == "pending"
    assert "explicit" not in s.source                  # 自动来源禁用 explicit
