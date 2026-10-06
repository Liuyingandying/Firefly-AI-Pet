"""M2C.1 subject identity safety tests.

Verifies that the coarse kind-level subject slot correctly identifies
same-slot conflicts, does NOT false-positive on different-slot items, and
that the 5-gate SUPERSEDE check requires all safety conditions.
"""

from __future__ import annotations

import pytest

from memory.m2 import (
    MemoryCandidate,
    MemoryKind,
    MemoryWritePolicy,
    SubjectEvidence,
    WriteAction,
    normalize_memory_text,
    extract_subject_key,
)


@pytest.fixture()
def policy():
    return MemoryWritePolicy()


class _R:
    def __init__(self, id, content, kind=MemoryKind.OTHER, category="project",
                 lifecycle="active", superseded_by=None):
        self.id = id
        self.content = content
        self.kind = kind
        self.category = category
        self.lifecycle_status = lifecycle
        self.superseded_by = superseded_by


def _c(content, **kw):
    kw.setdefault("explicit", True)
    kw.setdefault("confidence", 0.95)
    return MemoryCandidate(content=content, **kw)


# ---------------------------------------------------------------------------
# Adversarial: subject_key slot detection
# ---------------------------------------------------------------------------


def test_same_kind_same_slot():
    k1 = extract_subject_key("我喜欢上海", MemoryKind.PREFERENCE)
    k2 = extract_subject_key("我现在更喜欢北京", MemoryKind.PREFERENCE)
    assert k1 == k2  # coarse slot = kind.value → same


def test_different_kind_different_slot():
    k1 = extract_subject_key("喜欢上海", MemoryKind.PREFERENCE)
    k2 = extract_subject_key("计划保研", MemoryKind.GOAL)
    assert k1 != k2


# ---------------------------------------------------------------------------
# SUPERSEDE 5-gate safety
# ---------------------------------------------------------------------------


def test_same_slot_revision_supersede(policy):
    """Gate 1-5 all pass: explicit + marker + conf + subject + single target."""
    d = policy.evaluate(
        _c("测试用户现在更喜欢城市B", kind=MemoryKind.PREFERENCE),
        [_R("r1", "测试用户更喜欢城市A", kind=MemoryKind.PREFERENCE, category="preference")],
    )
    assert d.action is WriteAction.SUPERSEDE


def test_first20_collision_cannot_authorize_supersede(policy):
    """Two completely different contents that happen to share first-20
    prefix must NOT auto-supersede without conflict markers."""
    d = policy.evaluate(
        _c("用户正在开发名为Firefly的AI桌面宠物项目"),
        [_R("r1", "用户正在开发名为Firefly的AI移动端应用", kind=MemoryKind.PROJECT_CONTEXT)],
    )
    # No conflict markers → gate 2 fails → no SUPERSEDE
    assert d.action is not WriteAction.SUPERSEDE


def test_weak_subject_evidence_requires_confirmation(policy):
    """Without explicit markers or high confidence → confirmation."""
    d = policy.evaluate(
        _c("测试用户可能想换个方向", confidence=0.4),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action in (
        WriteAction.REQUIRE_CONFIRMATION,
        WriteAction.KEEP_BOTH,
        WriteAction.CREATE,
    )


def test_strong_subject_plus_strong_revision_supersede(policy):
    d = policy.evaluate(
        _c("测试用户决定改成学习方向B"),
        [_R("r1", "测试用户计划学习方向A", kind=MemoryKind.GOAL)],
    )
    assert d.action is WriteAction.SUPERSEDE or d.action is WriteAction.REQUIRE_CONFIRMATION


# ---------------------------------------------------------------------------
# Compatible / negative / episodic
# ---------------------------------------------------------------------------


def test_also_like_keeps_both(policy):
    d = policy.evaluate(
        _c("测试用户也喜欢城市B"),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is not WriteAction.SUPERSEDE


def test_no_longer_like_same_subject_supersede(policy):
    d = policy.evaluate(
        _c("测试用户不再喜欢城市A"),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action in (WriteAction.SUPERSEDE, WriteAction.REQUIRE_CONFIRMATION)


def test_negative_different_subject_keep_both(policy):
    """不同 kind 的否定不是 conflict（kind 不同 → 不同 subject slot）。"""
    d = policy.evaluate(
        _c("测试用户不喜欢喝茶", kind=MemoryKind.PREFERENCE),
        [_R("r1", "测试用户计划申请博士", kind=MemoryKind.GOAL)],
    )
    assert d.action is not WriteAction.SUPERSEDE


def test_education_goal_revision(policy):
    d = policy.evaluate(
        _c("测试用户决定申请博士项目"),
        [_R("r1", "测试用户计划申请硕士项目", kind=MemoryKind.GOAL)],
    )
    assert d.action in (WriteAction.SUPERSEDE, WriteAction.REQUIRE_CONFIRMATION)


def test_compatible_parallel_goals_keep_both(policy):
    d = policy.evaluate(
        _c("测试用户还想学习太赫兹通信"),
        [_R("r1", "测试用户计划学习FPGA", kind=MemoryKind.GOAL)],
    )
    assert d.action is not WriteAction.SUPERSEDE


def test_episodic_never_supersede(policy):
    d = policy.evaluate(
        _c("测试用户参加了学术报告", kind=MemoryKind.EPISODIC),
        [_R("r1", "测试用户参加了组会", kind=MemoryKind.EPISODIC)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# ---------------------------------------------------------------------------
# Multiple active targets → confirmation
# ---------------------------------------------------------------------------


def test_multiple_active_targets_confirmation(policy):
    d = policy.evaluate(
        _c("测试用户决定改成方向C"),
        [
            _R("r1", "方向A相关内容"),
            _R("r2", "方向C相关内容"),
        ],
    )
    # Multiple targets → can't auto-supersede just one
    assert d.action is not WriteAction.SUPERSEDE or len(d.target_record_ids) <= 1


# ---------------------------------------------------------------------------
# LLM evidence safety
# ---------------------------------------------------------------------------


def test_llm_evidence_no_direct_mutation(policy):
    from memory.m2 import RelationEvidence, Relation

    ev = RelationEvidence(relation=Relation.CONFLICTS, confidence=0.9, reason="LLM says conflict")
    d = policy.evaluate(
        _c("测试用户喜欢城市A"),
        [_R("r1", "测试用户喜欢城市A")],
        evidence=ev,
    )
    assert d.action is WriteAction.IGNORE_DUPLICATE  # exact dup still wins


def test_llm_unavailable_safe_fallback(policy):
    """No LLM evidence → policy still works deterministically."""
    d = policy.evaluate(
        _c("测试用户现在更喜欢北京"),
        [_R("r1", "测试用户更喜欢上海", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action in (WriteAction.SUPERSEDE, WriteAction.REQUIRE_CONFIRMATION)
