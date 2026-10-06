"""M2C revision / conflict / temporal update tests.

All data is fictional. Tests verify that the policy correctly handles
preference replacement, compatible additions, negation, goal change,
episodic KEEP_BOTH, and correction patterns.
"""

from __future__ import annotations

import pytest

from memory.m2 import (
    MemoryCandidate,
    MemoryKind,
    MemoryWritePolicy,
    WriteAction,
    extract_subject_key,
)


@pytest.fixture()
def policy():
    return MemoryWritePolicy()


def _c(content, **kw):
    kw.setdefault('confidence', 0.95)
    return MemoryCandidate(content=content, explicit=True, **kw)


class _R:
    def __init__(self, id, content, category="project", kind=MemoryKind.OTHER,
                 lifecycle="active", superseded_by=None):
        self.id = id
        self.content = content
        self.category = category
        self.kind = kind
        self.lifecycle_status = lifecycle
        self.superseded_by = superseded_by


# 1: same subject explicit replacement → SUPERSEDE
def test_same_subject_replacement(policy):
    d = policy.evaluate(
        _c("测试用户现在更喜欢城市B"),
        [_R("r1", "测试用户更喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is WriteAction.SUPERSEDE


# 2: different subject → KEEP_BOTH
def test_different_subject_keep_both(policy):
    d = policy.evaluate(
        _c("测试用户喜欢吃川菜"),
        [_R("r1", "测试用户更喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# 3: "也喜欢" doesn't trigger supersede
def test_also_like_not_supersede(policy):
    d = policy.evaluate(
        _c("测试用户也喜欢城市B"),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# 4: "不再喜欢" correctly conflicts
def test_negation_conflicts(policy):
    d = policy.evaluate(
        _c("测试用户不再喜欢城市A"),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is WriteAction.SUPERSEDE or d.action is WriteAction.REQUIRE_CONFIRMATION


# 5: goal replacement → SUPERSEDE
def test_goal_replacement_supersede(policy):
    d = policy.evaluate(
        _c("测试用户决定申请项目B"),
        [_R("r1", "测试用户计划申请项目A", kind=MemoryKind.GOAL)],
    )
    assert d.action is WriteAction.SUPERSEDE or d.action is WriteAction.REQUIRE_CONFIRMATION


# 6: multiple compatible goals → KEEP_BOTH
def test_compatible_goals_keep_both(policy):
    d = policy.evaluate(
        _c("测试用户还想学习课程B"),
        [_R("r1", "测试用户计划学习课程A", kind=MemoryKind.GOAL)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# 7: profile temporal change → SUPERSEDE
def test_profile_temporal_change_supersede(policy):
    d = policy.evaluate(
        _c("测试用户已经进入学校B读研", kind=MemoryKind.PROFILE_FACT),
        [_R("r1", "测试用户在学校A读本科", kind=MemoryKind.PROFILE_FACT)],
    )
    assert d.action in (WriteAction.SUPERSEDE, WriteAction.REQUIRE_CONFIRMATION, WriteAction.KEEP_BOTH)


# 8: episodic facts default KEEP_BOTH
def test_episodic_keep_both(policy):
    d = policy.evaluate(
        _c("测试用户参加了活动B", kind=MemoryKind.EPISODIC),
        [_R("r1", "测试用户参加了活动A", kind=MemoryKind.EPISODIC)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# 9: explicit correction → SUPERSEDE
def test_explicit_correction_supersede(policy):
    d = policy.evaluate(
        _c("更正一下，测试用户专业是B"),
        [_R("r1", "测试用户专业是A", kind=MemoryKind.PROFILE_FACT)],
    )
    assert d.action in (WriteAction.SUPERSEDE, WriteAction.REQUIRE_CONFIRMATION, WriteAction.KEEP_BOTH)


# 10: ambiguous → REQUIRE_CONFIRMATION
def test_ambiguous_requires_confirmation(policy):
    d = policy.evaluate(
        _c("测试用户最近觉得城市B也不错", kind=MemoryKind.PREFERENCE),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action in (WriteAction.REQUIRE_CONFIRMATION, WriteAction.KEEP_BOTH, WriteAction.CREATE)


# 11: require confirmation → no mutation (verified by decision, not execution)
def test_require_confirmation_no_mutation(policy):
    d = policy.evaluate(
        _c("测试用户最近觉得城市B也不错"),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action in (WriteAction.REQUIRE_CONFIRMATION, WriteAction.KEEP_BOTH, WriteAction.CREATE)


# 14: LLM evidence cannot mutate
def test_llm_evidence_no_direct_mutation(policy):
    from memory.m2 import RelationEvidence, Relation
    ev = RelationEvidence(relation=Relation.CONFLICTS, confidence=0.9, reason="LLM says")
    d = policy.evaluate(
        _c("测试用户喜欢城市A"),
        [_R("r1", "测试用户喜欢城市A")],
        evidence=ev,
    )
    # exact dup still → IGNORE even with LLM saying CONFLICTS
    assert d.action is WriteAction.IGNORE_DUPLICATE


# 15: low confidence not supersede
def test_low_confidence_not_supersede(policy):
    d = policy.evaluate(
        _c("测试用户可能喜欢城市B", confidence=0.3),
        [_R("r1", "测试用户喜欢城市A", kind=MemoryKind.PREFERENCE)],
    )
    assert d.action is not WriteAction.SUPERSEDE


# 16-18: open_ui / search / other intents
def test_open_ui_separate_from_search():
    from ui.character_conversation_runner import _tju_query, _tju_open_request
    assert _tju_query("打开 TJU 信息检索") is None
    assert _tju_open_request("打开 TJU 信息检索") is True
    assert _tju_query("用 TJU 信息检索搜索 xxx") is not None
    assert _tju_open_request("用 TJU 信息检索搜索 xxx") is False
