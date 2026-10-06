"""M3B.3 recall gate tests — deterministic inject/don't-inject policy.

Covers the phase-specified cases:
- positives (explicit recall requests) → ALLOW
- negatives (smalltalk / knowledge Q&A) → no injection
- boundary ("帮我设计一个AI助手" must NOT inject just because a Firefly
  memory exists)

Plus rule-interaction cases (explicit beats smalltalk; knowledge needs a
strong match; kind policy; multi-candidate consistency) and an E2E check
through ``retrieve_for_prompt`` (READ_ONLY, per M3B.1).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.access_mode import MemoryAccessMode
from memory.records import MemoryRecord, WritePolicy
from memory.recall_gate import GateDecision, MemoryRecallGate
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from memory.m3b import MemoryRetrievalCandidate
from tests.test_memory_m3b_injection import KeywordAdapter


def _cand(rid, content, score, kind="project_context"):
    return MemoryRetrievalCandidate(
        record_id=rid, content=content, semantic_score=score,
        kind=kind, source="explicit", created_ts=0, updated_ts=0,
        lifecycle_status="active",
    )


GATE = MemoryRecallGate()


# ---------------------------------------------------------------------------
# Phase-specified positives: explicit recall requests → ALLOW
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "继续之前那个项目",
    "之前的THz方案",
    "那个PCB设计",
    "我们之前讨论的论文",
])
def test_explicit_recall_requests_allowed(query):
    candidates = [_cand("m1", "星桥计划的进展每周同步一次", 0.50)]
    decision = GATE.should_inject(query, candidates)
    assert decision.decision is GateDecision.ALLOW
    assert "explicit_reference" in decision.reason
    assert decision.allowed_ids == ("m1",)


# ---------------------------------------------------------------------------
# Phase-specified negatives: smalltalk / knowledge Q&A → BLOCK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", [
    "你好",
    "今天怎么样",
    "什么是PID",
    "解释attention",
])
def test_negatives_blocked(query):
    candidates = [_cand("m1", "用户喜欢喝咖啡", 0.47, kind="preference")]
    decision = GATE.should_inject(query, candidates)
    assert decision.decision is GateDecision.BLOCK
    assert decision.allowed == ()


def test_smalltalk_blocked_even_with_medium_matches():
    candidates = [
        _cand("m1", "用户习惯晚饭后散步", 0.52, kind="preference"),
        _cand("m2", "星桥计划的进展同步", 0.50, kind="project_context"),
    ]
    decision = GATE.should_inject("随便聊聊", candidates)
    assert decision.decision is GateDecision.BLOCK
    assert "smalltalk" in decision.reason


def test_knowledge_qa_blocked_on_weak_only():
    candidates = [
        _cand("m1", "什么是傅里叶变换的笔记", 0.48, kind="other"),
        _cand("m2", "信号的频域分析资料", 0.47, kind="profile_fact"),
    ]
    decision = GATE.should_inject("介绍一下傅里叶变换", candidates)
    assert decision.decision is GateDecision.BLOCK
    assert "weak" in decision.reason


def test_knowledge_qa_allowed_on_strong_match():
    candidates = [_cand("m1", "用户的PID参数整定经验记录", 0.72, kind="profile_fact")]
    decision = GATE.should_inject("什么是PID", candidates)
    assert decision.decision is GateDecision.ALLOW
    assert "high_score" in decision.reason


# ---------------------------------------------------------------------------
# Boundary from the phase spec
# ---------------------------------------------------------------------------


def test_boundary_ai_assistant_request_not_injected_for_firefly_memory():
    """帮我设计一个AI助手 — a Firefly memory must NOT leak in."""
    candidates = [
        _cand("m1", "我正在开发 Firefly AI Pet，希望它成为长期 AI Companion。",
              0.47, kind="profile_fact"),
        _cand("m2", "用户正在开发 Firefly 的记忆系统", 0.46, kind="project_context"),
    ]
    decision = GATE.should_inject("帮我设计一个AI助手", candidates)
    assert decision.decision is GateDecision.BLOCK
    assert decision.allowed == ()
    # the same query WITH a strong match still injects — the gate blocks
    # weak leakage, not the topic
    strong = [_cand("m3", "我正在开发 Firefly AI Pet。", 0.70, kind="project_context")]
    decision2 = GATE.should_inject("帮我设计一个AI助手", strong)
    assert decision2.decision is GateDecision.ALLOW


# ---------------------------------------------------------------------------
# Rule interactions
# ---------------------------------------------------------------------------


def test_explicit_reference_beats_smalltalk():
    candidates = [_cand("m1", "上次讨论的电影清单", 0.48, kind="episodic")]
    decision = GATE.should_inject("还记得我们之前聊的电影吗，顺便聊聊", candidates)
    assert decision.decision is GateDecision.ALLOW
    assert "explicit_reference" in decision.reason


def test_episodic_requires_explicit_or_high():
    medium = [_cand("m1", "上周参加了自动控制原理考试", 0.48, kind="episodic")]
    decision = GATE.should_inject("自动控制原理考试", medium)
    assert decision.decision is GateDecision.BLOCK

    high = [_cand("m1", "上周参加了自动控制原理考试", 0.66, kind="episodic")]
    assert GATE.should_inject("自动控制原理考试", high).decision is GateDecision.ALLOW

    explicit = [_cand("m1", "上周参加了自动控制原理考试", 0.48, kind="episodic")]
    assert GATE.should_inject("还记得那次考试吗", explicit).decision is GateDecision.ALLOW


def test_strict_kinds_require_high_band():
    strict = [_cand("m1", "用户喜欢喝咖啡", 0.48, kind="preference")]
    assert GATE.should_inject("咖啡口味", strict).decision is GateDecision.BLOCK
    high = [_cand("m1", "用户喜欢喝咖啡", 0.62, kind="preference")]
    assert GATE.should_inject("咖啡口味", high).decision is GateDecision.ALLOW


def test_easy_kinds_qualify_at_medium():
    easy = [_cand("m1", "星桥计划的进展同步", 0.48, kind="project_context")]
    # Rule 4 "较容易召回": a single MEDIUM project_context IS enough —
    # strict kinds are the ones a single weak match cannot carry
    decision = GATE.should_inject("项目情况", easy)
    assert decision.decision is GateDecision.ALLOW
    assert "medium_easy_kind" in decision.reason
    # …but two consistent medium easy-kind candidates are
    two = [
        _cand("m1", "星桥计划的进展同步", 0.48, kind="project_context"),
        _cand("m2", "毕业设计的实验推进顺利", 0.47, kind="project_context"),
    ]
    decision = GATE.should_inject("项目情况", two)
    assert decision.decision is GateDecision.ALLOW
    assert "multiple_medium" in decision.reason


def test_empty_candidates_blocks():
    decision = GATE.should_inject("继续之前那个项目", [])
    assert decision.decision is GateDecision.BLOCK
    assert decision.reason == "no_candidates_above_threshold"


# ---------------------------------------------------------------------------
# E2E through retrieve_for_prompt (READ_ONLY, M3B.1)
# ---------------------------------------------------------------------------


def test_retrieve_for_prompt_applies_gate(tmp_path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = KeywordAdapter()
    svc = MemoryService(repo, adapter, write_policy=WritePolicy.EXPLICIT_ONLY,
                        access_mode=MemoryAccessMode.READ_ONLY)
    record = MemoryRecord.create(
        category="project", content="星桥计划的进展同步", trigger="t"
    )
    repo.add(record)
    adapter.add(record.content, {"record_id": record.id})

    # explicit recall → injected
    assert svc.retrieve_for_prompt("之前说的星桥计划进展同步") != []
    # smalltalk → the gate blocks it even at FULL similarity (this query
    # would pass the 0.45 threshold on its own — the gate is what stops it)
    assert svc.retrieve_for_prompt("随便聊聊星桥计划的进展同步") == []
    # context passthrough accepted and harmless
    assert svc.retrieve_for_prompt(
        "之前说的星桥计划进展同步", context={"mode": "chat"}
    ) != []
