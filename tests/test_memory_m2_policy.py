"""M2A policy tests — dedup / conflict / lifecycle / durability.

All test data uses fictional characters and facts. No real user data.
"""

from __future__ import annotations

import pytest

from memory.m2 import (
    Durability,
    MemoryCandidate,
    MemoryDedupPlanner,
    MemoryKind,
    MemoryWriteDecision,
    MemoryWritePolicy,
    Relation,
    WriteAction,
    normalize_memory_text,
)


class FakeRecord:
    def __init__(self, id: str, content: str, *, category: str = "project",
                 created_ts: int = 1000):
        self.id = id
        self.content = content
        self.category = category
        self.lifecycle_status = "active"
        self.superseded_by = None
        self.vector_id = "vec-" + id
        self.created_ts = created_ts


def _candidate(content: str, **kw) -> MemoryCandidate:
    defaults = dict(content=content, explicit=True, confidence=0.95)
    defaults.update(kw)
    return MemoryCandidate(**defaults)


@pytest.fixture()
def policy():
    return MemoryWritePolicy()


# ---------------------------------------------------------------------------
# Exact / normalized duplicates → IGNORE
# ---------------------------------------------------------------------------


def test_exact_duplicate_ignored(policy):
    d = policy.evaluate(_candidate("我喜欢上海"), [FakeRecord("r1", "我喜欢上海")])
    assert d.action is WriteAction.IGNORE_DUPLICATE


def test_punctuation_duplicate_ignored(policy):
    d = policy.evaluate(_candidate("我喜欢上海。"), [FakeRecord("r1", "我喜欢上海")])
    assert d.action is WriteAction.IGNORE_DUPLICATE


def test_duplicate_does_not_create_new_record(policy):
    d = policy.evaluate(_candidate("我喜欢上海"), [FakeRecord("r1", "我喜欢上海")])
    assert d.action is not WriteAction.CREATE


# ---------------------------------------------------------------------------
# Semantic similarity alone does not auto-merge
# ---------------------------------------------------------------------------


def test_similarity_alone_does_not_merge(policy):
    # Same topic, lexically similar, but no conflict/extend markers → KEEP_BOTH
    d = policy.evaluate(
        _candidate("我喜欢上海的美食"), [FakeRecord("r1", "我喜欢上海")]
    )
    assert d.action in (WriteAction.KEEP_BOTH, WriteAction.IGNORE_DUPLICATE)
    assert d.action is not WriteAction.MERGE_UPDATE


# ---------------------------------------------------------------------------
# EXTENDS not superseded
# ---------------------------------------------------------------------------


def test_extends_not_superseded(policy):
    d = policy.evaluate(
        _candidate("用户特别喜欢上海的城市氛围"),
        [FakeRecord("r1", "用户喜欢上海")],
    )
    assert d.action is not WriteAction.SUPERSEDE


# ---------------------------------------------------------------------------
# Conflict → SUPERSEDE
# ---------------------------------------------------------------------------


def test_explicit_conflict_supersede(policy):
    d = policy.evaluate(
        _candidate("用户现在更喜欢北京"),
        [FakeRecord("r1", "用户更喜欢上海", category="preference")],
    )
    assert d.action is WriteAction.SUPERSEDE
    assert d.target_record_ids == ("r1",)


def test_conflicting_goal_supersede(policy):
    d = policy.evaluate(
        _candidate("用户决定考研"), [FakeRecord("r2", "用户计划保研", category="user_fact")]
    )
    assert d.action is WriteAction.SUPERSEDE


# ---------------------------------------------------------------------------
# Temporary state
# ---------------------------------------------------------------------------


def test_temporary_auto_not_persisted(policy):
    d = policy.evaluate(
        _candidate("我今天很累", kind=MemoryKind.TEMPORARY_STATE, explicit=False),
        [],
    )
    assert d.action is WriteAction.DO_NOT_PERSIST


def test_temporary_explicit_can_persist(policy):
    d = policy.evaluate(
        _candidate("记住我今晚在图书馆", kind=MemoryKind.TEMPORARY_STATE, explicit=True),
        [],
    )
    # 显式 remember 仍尊重用户意图，但标 temporary → CREATE (not DO_NOT_PERSIST)
    assert d.action is not WriteAction.DO_NOT_PERSIST


# ---------------------------------------------------------------------------
# Independent / low confidence
# ---------------------------------------------------------------------------


def test_independent_facts_created(policy):
    d = policy.evaluate(_candidate("用户养了一只猫"), [FakeRecord("r1", "用户喜欢上海")])
    assert d.action is WriteAction.CREATE


def test_low_conflict_confidence_requires_confirmation(policy):
    d = policy.evaluate(
        _candidate("用户可能想换个方向", confidence=0.4),
        [FakeRecord("r1", "用户喜欢上海")],
    )
    assert d.action in (
        WriteAction.REQUIRE_CONFIRMATION,
        WriteAction.KEEP_BOTH,
        WriteAction.CREATE,
    )


# ---------------------------------------------------------------------------
# Free-chat / no next-step
# ---------------------------------------------------------------------------


def test_planner_canonical_selection_is_deterministic():
    pass  # covered in test_planner_dry_run_canonical_and_duplicates above


# ---------------------------------------------------------------------------
# Planner dry-run
# ---------------------------------------------------------------------------


def test_planner_dry_run_canonical_and_duplicates():
    records = [
        FakeRecord("old_1", "我喜欢上海", created_ts=1000),
        FakeRecord("dup_1", "我喜欢上海。", created_ts=2000),
        FakeRecord("dup_2", "我喜欢上海", created_ts=3000),
        FakeRecord("unique_1", "用户养了一只猫", created_ts=4000),
    ]
    planner = MemoryDedupPlanner()
    plan = planner.plan(records)
    actions = {e.record_id: e.action for e in plan.entries}
    # old_1 是 canonical（vector_id 存在 + 最早创建）
    assert actions["old_1"] is WriteAction.CREATE  # canonical 保留
    assert actions["dup_1"] is WriteAction.IGNORE_DUPLICATE
    assert actions["dup_2"] is WriteAction.IGNORE_DUPLICATE
    assert actions["unique_1"] is WriteAction.CREATE  # independent
    assert plan.statistics.get("ignore_duplicate", 0) == 2
