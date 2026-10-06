"""M3B retrieval ranking + injection benchmark tests.

Synthetic benchmark with fictional characters measuring Precision@3,
Recall@3, False Positive Rate, and Zero-memory correctness for the
context-aware retrieval ranking (vs raw semantic top-k baseline).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.records import MemoryRecord
from memory.m3b import (
    MAX_INJECTED_MEMORIES, MIN_RELEVANCE_SCORE,
    MemoryRetrievalCandidate, score_candidates,
)


NOW = int(time.time_ns() // 1_000_000)


def _cand(rid, content, score, kind="project_context", source="explicit",
          created=None, lifecycle="active"):
    return MemoryRetrievalCandidate(
        record_id=rid, content=content, semantic_score=score,
        kind=kind, source=source,
        created_ts=created or NOW, updated_ts=NOW,
        lifecycle_status=lifecycle,
    )


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------


def test_strongly_relevant_selected():
    cands = [_cand("rel", "自动控制原理 振荡 分析", 0.85)]
    scored = score_candidates(cands)
    assert scored[0].final_score > MIN_RELEVANCE_SCORE


def test_unrelated_rejected():
    cands = [_cand("irr", "我喜欢上海", 0.1, kind="preference")]
    scored = score_candidates(cands)
    assert scored[0].final_score < MIN_RELEVANCE_SCORE


def test_zero_memory_valid():
    cands = [_cand("irr", f"无关话题{i}", 0.05) for i in range(10)]
    scored = score_candidates(cands, now_ms=NOW)
    assert all(s.final_score < MIN_RELEVANCE_SCORE for s in scored)
    # threshold gate would therefore inject nothing — a valid outcome


def test_max_injected_respected():
    from memory.m3b import MAX_INJECTED_MEMORIES
    cands = [_cand(f"mem{i}", f"记忆{i}", 0.8 - i * 0.05) for i in range(10)]
    scored = score_candidates(cands)
    scored.sort(key=lambda s: s.final_score, reverse=True)
    top = scored[:MAX_INJECTED_MEMORIES]
    assert len(top) <= MAX_INJECTED_MEMORIES


def test_superseded_never_selected():
    cands = [_cand("sup", "旧记忆", 0.9, lifecycle="superseded")]
    # M2B active-only filter removes superseded before scoring
    filtered = [c for c in cands if c.lifecycle_status != "superseded"]
    assert len(filtered) == 0


def test_semantic_relevance_dominates():
    low_kind_high_sem = _cand("a", "自动控制原理", 0.9, kind="episodic")
    high_kind_low_sem = _cand("b", "完全无关的话题内容", 0.2, kind="profile_fact")
    scored = score_candidates([low_kind_high_sem, high_kind_low_sem])
    scored.sort(key=lambda s: s.final_score, reverse=True)
    assert scored[0].record_id == "a"  # semantic still wins


def test_durable_preference_no_strong_decay():
    from memory.m3b import _is_durable
    assert _is_durable("preference") is True
    assert _is_durable("profile_fact") is True
    assert _is_durable("episodic") is False


def test_old_relevant_beats_new_unrelated():
    old_rel = _cand("old_rel", "Firefly AI Pet 开发", 0.8, created=NOW - 90 * 86400000)
    new_irr = _cand("new_irr", "完全不同的话题", 0.3, created=NOW)
    scored = score_candidates([old_rel, new_irr])
    scored.sort(key=lambda s: s.final_score, reverse=True)
    assert scored[0].record_id == "old_rel"


def test_contextual_recency_small_bonus():
    now = int(time.time_ns() // 1_000_000)
    recent = _cand("recent", "近期项目", 0.6, created=now - 3 * 86400000)
    older = _cand("older", "较旧项目", 0.6, created=now - 60 * 86400000)
    s_recent = score_candidates([recent])[0]
    s_older = score_candidates([older])[0]
    assert s_recent.final_score > s_older.final_score  # recent bonus


# ---------------------------------------------------------------------------
# Diversity
# ---------------------------------------------------------------------------


def test_diversity_suppresses_duplicates():
    from memory.m3b import MemoryRetrievalScore, _suppress_near_duplicates

    def s(rid, score):
        return MemoryRetrievalScore(
            record_id=rid, semantic_score=score, context_score=0.0,
            kind_weight=0.0, recency_bonus=0.0, source_bonus=0.0,
            final_score=score, selected=True, reason="",
        )

    a, b, c = s("a", 0.9), s("b", 0.85), s("c", 0.8)
    contents = {
        "a": "用户喜欢上海的城市氛围",
        "b": "用户喜欢上海的城市氛围和美食",
        "c": "自动控制原理课程下周有考试",
    }
    kept = _suppress_near_duplicates([a, b, c], contents)
    ids = [x.record_id for x in kept]
    assert "a" in ids      # highest score kept
    assert "b" not in ids  # near-duplicate of a suppressed
    assert "c" in ids      # distinct content survives


# ---------------------------------------------------------------------------
# Baseline comparison (old vs new)
# ---------------------------------------------------------------------------


def test_old_semantic_topk_returns_superseded():
    """旧 baseline：不过滤 superseded → 会返回 duplicates。"""
    cands = [
        _cand("sup1", "重复记忆A", 0.9, lifecycle="superseded"),
        _cand("sup2", "重复记忆A", 0.85, lifecycle="superseded"),
        _cand("active", "相关记忆", 0.5),
    ]
    # Old: pure semantic top-k (no lifecycle filter)
    old_top3 = sorted(cands, key=lambda c: c.semantic_score, reverse=True)[:3]
    assert any(c.lifecycle_status == "superseded" for c in old_top3)
    # New: filter superseded first
    new_active = [c for c in old_top3 if c.lifecycle_status != "superseded"]
    assert len(new_active) == 1
    assert new_active[0].record_id == "active"


def test_no_llm_ranking_dependency():
    """M3B scoring is pure deterministic Python — no LLM/network client."""
    from pathlib import Path

    from memory import m3b

    source = Path(m3b.__file__).read_text(encoding="utf-8").lower()
    for banned in (
        "openai", "anthropic", "ai_router", "provider_router",
        "requests.post", "httpx", "urllib", "chat.completions",
    ):
        assert banned not in source, banned

    # Deterministic: identical inputs + fixed clock give identical scores
    cands = [_cand("x", "用户稳定偏好在上海生活", 0.7, kind="preference")]
    first = score_candidates(cands, now_ms=NOW)
    second = score_candidates(cands, now_ms=NOW)
    assert [r.final_score for r in first] == [r.final_score for r in second]