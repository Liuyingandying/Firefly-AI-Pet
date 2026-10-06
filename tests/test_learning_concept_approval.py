# -*- coding: utf-8 -*-
"""M2.6 Concept Approval Bridge 验收测试。

覆盖：单节点批准 / 多节点批准 / 拒绝（自动批准）/ 未批准跳过 / 环检测 /
非法输入 / JSON 序列化 / Graph 无修改。
隔离：proposals 走 M2.5 真实链 + 手工构造; 不写图、不触课程存储、无 LLM。
"""

from __future__ import annotations

import json

import pytest

from core.learning.concept_approval import (
    ERR_EXPLICIT_APPROVAL_REQUIRED,
    ERR_INVALID_INPUT,
    REJECT_CYCLE,
    REJECT_DUPLICATE,
    REJECT_INVALID_ID,
    REJECT_NOT_APPROVED,
    ApprovalResult,
    ConceptApprover,
)
from core.learning.concept_refinement import ConceptProposal, ConceptRefiner
from core.learning.knowledge_import import ConceptCandidate
from core.learning.knowledge_graph import KnowledgeGraph


@pytest.fixture(scope="module")
def approver():
    return ConceptApprover()


@pytest.fixture(scope="module")
def proposals():
    """M2.5 真实链产出: 章候选细化出两个子话题提案。"""
    candidate = ConceptCandidate(
        concept_id="kc-abc1234567",
        name="电磁场导论",
        chapter="电磁场导论",
        prerequisites=(),
        related_experiments=("uniform-plane-wave",),
        confidence=0.8,
        level=1,
        source_section_id="sec:1:0:abcd1234",
    )
    result = ConceptRefiner().refine(
        candidate, subtopics=("麦克斯韦方程组", "边界条件")
    )
    return result.proposals


# ---------------------------------------------------------------------------
# 1. 单节点批准
# ---------------------------------------------------------------------------

def test_single_node_approval(approver, proposals):
    target = proposals[0]
    result = approver.approve(target, approved_ids={target.concept_id})
    assert isinstance(result, ApprovalResult)
    assert result.approved is True
    assert result.error == ""
    assert len(result.created_nodes) == 1
    node = result.created_nodes[0]
    assert node.concept_id == target.concept_id
    assert node.name == target.name
    assert node.chapter == target.source_chapter          # 规则 3: 溯源复用
    assert node.prerequisites == tuple(target.prerequisites)
    assert node.related_experiments == tuple(target.related_experiments)
    assert result.provenance[target.concept_id]["refinement_kind"] == target.refinement_kind


# ---------------------------------------------------------------------------
# 2. 多节点批准
# ---------------------------------------------------------------------------

def test_multi_node_approval(approver, proposals):
    ids = {p.concept_id for p in proposals}
    result = approver.approve(proposals, approved_ids=ids)
    assert result.approved is True
    assert len(result.created_nodes) == len(proposals)
    assert {n.concept_id for n in result.created_nodes} == ids
    assert result.rejected == ()
    assert set(result.provenance) == ids


# ---------------------------------------------------------------------------
# 3. 拒绝（自动批准）
# ---------------------------------------------------------------------------

def test_auto_approval_rejected(approver, proposals):
    result = approver.approve(proposals, approved_ids=())
    assert result.approved is False
    assert result.created_nodes == ()
    assert result.error.startswith(ERR_EXPLICIT_APPROVAL_REQUIRED)
    # 全部提案以 not_approved 记录（可审计）
    assert {r["reason"] for r in result.rejected} == {REJECT_NOT_APPROVED}

    assert approver.approve(proposals, approved_ids=None).approved is False


# ---------------------------------------------------------------------------
# 4. 未批准跳过
# ---------------------------------------------------------------------------

def test_unapproved_skipped(approver, proposals):
    target = proposals[0]
    result = approver.approve(proposals, approved_ids={target.concept_id})
    assert len(result.created_nodes) == 1
    unapproved = [r for r in result.rejected if r["reason"] == REJECT_NOT_APPROVED]
    assert {r["concept_id"] for r in unapproved} == {p.concept_id for p in proposals} - {target.concept_id}


# ---------------------------------------------------------------------------
# 5. 环检测（构造前检查）
# ---------------------------------------------------------------------------

def test_cycle_detection(approver):
    a = ConceptProposal(
        concept_id="kc-a", name="A", description="", parent_concept="",
        prerequisites=("kc-b",), confidence=0.8, source_chapter="章",
    )
    b = ConceptProposal(
        concept_id="kc-b", name="B", description="", parent_concept="",
        prerequisites=("kc-a",), confidence=0.8, source_chapter="章",
    )
    result = approver.approve([a, b], approved_ids={"kc-a", "kc-b"})
    assert result.approved is True                        # 门通过, 但成员被逐个拒绝
    assert result.created_nodes == ()
    assert {r["concept_id"] for r in result.rejected} == {"kc-a", "kc-b"}
    assert {r["reason"] for r in result.rejected} == {REJECT_CYCLE}
    assert any("cycle" in w for w in result.warnings)


def test_cycle_isolated_from_valid(approver, proposals):
    """环成员被拒, 合法提案照常批准（不互相阻断）。"""
    a = ConceptProposal(
        concept_id="kc-x", name="X", description="", parent_concept="",
        prerequisites=("kc-y",), confidence=0.8, source_chapter="章",
    )
    y = ConceptProposal(
        concept_id="kc-y", name="Y", description="", parent_concept="",
        prerequisites=("kc-x",), confidence=0.8, source_chapter="章",
    )
    result = approver.approve(
        [*proposals, a, y], approved_ids={p.concept_id for p in proposals} | {"kc-x", "kc-y"}
    )
    assert len(result.created_nodes) == len(proposals)     # 原有两节点不受牵连
    assert {r["concept_id"] for r in result.rejected} == {"kc-x", "kc-y"}


# ---------------------------------------------------------------------------
# 6. 非法输入 + 构造前三检查
# ---------------------------------------------------------------------------

def test_invalid_input(approver, proposals):
    for bad in (None, "not-proposal", 42, [None, proposals[0]]):
        result = approver.approve(bad, approved_ids={p.concept_id for p in proposals})
        assert result.approved is False
        assert result.error.startswith(ERR_INVALID_INPUT)
        assert result.created_nodes == ()


def test_invalid_id_rejected(approver):
    bad = ConceptProposal(
        concept_id="bad id with spaces", name="X", description="",
        parent_concept="", confidence=0.8, source_chapter="章",
    )
    empty = ConceptProposal(
        concept_id="", name="Y", description="",
        parent_concept="", confidence=0.8, source_chapter="章",
    )
    good = ConceptProposal(
        concept_id="kc-ok:sub-1a2b3c", name="Z", description="",
        parent_concept="", confidence=0.8, source_chapter="章",
    )
    result = approver.approve(
        [bad, empty, good, good], approved_ids={"bad id with spaces", "", "kc-ok:sub-1a2b3c"}
    )
    reasons = {r["concept_id"]: r["reason"] for r in result.rejected}
    assert reasons["bad id with spaces"] == REJECT_INVALID_ID
    # 空 id 无法被显式批准（门归一化滤掉空串）→ 门优先记 not_approved
    assert reasons[""] == REJECT_NOT_APPROVED
    assert reasons["kc-ok:sub-1a2b3c"] == REJECT_DUPLICATE        # 同批重复
    assert [n.concept_id for n in result.created_nodes] == ["kc-ok:sub-1a2b3c"]


def test_unknown_experiment_stripped(approver, proposals):
    """规则 4 实验存在检查: 白名单外的实验引用被剥离并告警, 节点保留。"""
    target = proposals[0]
    result = approver.approve(
        target, approved_ids={target.concept_id},
        experiment_ids={"uniform-plane-wave"},
    )
    node = result.created_nodes[0]
    assert node.related_experiments == tuple(target.related_experiments)  # 白名单内全保留

    strict = approver.approve(
        target, approved_ids={target.concept_id}, experiment_ids={"other-exp"}
    )
    assert strict.created_nodes[0].related_experiments == ()
    assert any("unknown experiment" in w for w in strict.warnings)


def test_unresolved_prerequisite_dropped(approver, proposals):
    """可选 known_concept_ids: 未解析前置边被丢弃并告警。"""
    wave = ConceptProposal(
        concept_id="kc-wave", name="均匀平面波", description="",
        parent_concept="电磁场导论", prerequisites=("kc-abc1234567", "em-te-polarization"),
        confidence=0.7, source_candidate_id="kc-def7654321", source_chapter="电磁场导论",
    )
    result = approver.approve(
        wave, approved_ids={"kc-wave"},
        known_concept_ids={"em-te-polarization"},      # 已有图概念
    )
    node = result.created_nodes[0]
    assert node.prerequisites == ("em-te-polarization",)
    assert any("unresolved prerequisite" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 7. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(approver, proposals):
    result = approver.approve(proposals, approved_ids={p.concept_id for p in proposals})
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["approved"] is True
    assert len(parsed["created_nodes"]) == len(proposals)
    assert parsed["created_nodes"][0]["concept_id"] == proposals[0].concept_id
    assert set(parsed["provenance"]) == {p.concept_id for p in proposals}

    rejected_parsed = json.loads(
        approver.approve(proposals, approved_ids=()).to_json()
    )
    assert rejected_parsed["approved"] is False
    assert rejected_parsed["created_nodes"] == []


# ---------------------------------------------------------------------------
# 8. Graph 无修改
# ---------------------------------------------------------------------------

def test_graph_never_modified(approver, proposals):
    graph = KnowledgeGraph()
    seed_before = {
        cid: graph.get_concept(cid)
        for cid in ("em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization")
    }
    path_before = graph.find_learning_path("em-te-polarization")

    ids = {p.concept_id for p in proposals}
    snapshot = {p.concept_id: p.to_dict() for p in proposals}   # 规则 3: proposal 不被修改
    result = approver.approve(proposals, approved_ids=ids)

    assert result.approved is True
    for p in proposals:
        assert p.to_dict() == snapshot[p.concept_id]           # 源提案原样
    for cid, node in seed_before.items():
        assert graph.get_concept(cid) is node                  # 图零变化
    for p in proposals:
        assert graph.get_concept(p.concept_id) is None         # 批准 ≠ 入图
    assert graph.find_learning_path("em-te-polarization") == path_before


def test_approver_never_touches_graph_instance():
    """静态契约: approver 不构造图、不调用图方法（只 import ConceptNode 类型）。"""
    import core.learning.concept_approval.approver as approver_mod
    import inspect

    source = inspect.getsource(approver_mod)
    assert "KnowledgeGraph(" not in source
    assert "find_learning_path" not in source
    assert "get_concept" not in source
