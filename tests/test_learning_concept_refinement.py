# -*- coding: utf-8 -*-
"""M2.5 Concept Refinement 验收测试。

覆盖：单概念细化 / 父子关系 / 实验继承 / 置信度传递 / 空输入 /
JSON 序列化 / 不修改 KnowledgeGraph。
隔离：候选直接构造 + 一条 M2.4 真实链; 不写图、不触课程存储、无 LLM。
"""

from __future__ import annotations

import json

import pytest

from core.learning.concept_refinement import (
    ConceptProposal,
    ConceptRefiner,
    ConceptRefinementResult,
)
from core.learning.knowledge_import import ConceptCandidate
from core.learning.knowledge_graph import KnowledgeGraph


@pytest.fixture(scope="module")
def refiner():
    return ConceptRefiner()


def _chapter_candidate(**overrides) -> ConceptCandidate:
    kwargs = dict(
        concept_id="kc-abc1234567",
        name="电磁场导论",
        chapter="电磁场导论",          # 章级候选: name == chapter
        prerequisites=(),
        related_experiments=("uniform-plane-wave", "demo-te"),
        confidence=0.8,
        level=1,
        source_section_id="sec:1:0:abcd1234",
    )
    kwargs.update(overrides)
    return ConceptCandidate(**kwargs)


def _subsection_candidate() -> ConceptCandidate:
    return _chapter_candidate(
        concept_id="kc-def7654321",
        name="均匀平面波",
        chapter="电磁场导论",          # 子节候选: chapter 为所属章
        prerequisites=("kc-abc1234567",),
        confidence=0.7,
        level=2,
        source_section_id="sec:2:0:efef5678",
    )


# ---------------------------------------------------------------------------
# 1. 单概念细化
# ---------------------------------------------------------------------------

def test_single_concept_refinement(refiner):
    result = refiner.refine(_chapter_candidate())
    assert isinstance(result, ConceptRefinementResult)
    assert result.success is True
    assert result.error == ""
    assert len(result.proposals) == 1                   # 规则 3: base 恒在

    p = result.proposals[0]
    assert isinstance(p, ConceptProposal)
    assert p.concept_id == "kc-abc1234567"              # 规则 2: 沿用候选 id
    assert p.name == "电磁场导论"
    assert "电磁场导论" in p.description and "L1" in p.description
    assert p.source_candidate_id == "kc-abc1234567"     # 规则 2: 溯源三件套
    assert p.source_level == 1
    assert p.source_chapter == "电磁场导论"
    assert p.refinement_kind == "base"
    # 源候选原样回显（规则 3）
    assert result.source_candidate.concept_id == "kc-abc1234567"


# ---------------------------------------------------------------------------
# 2. 父子关系（章节标题作为 parent + hierarchy refinement）
# ---------------------------------------------------------------------------

def test_parent_concept_from_chapter_title(refiner):
    sub = refiner.refine(_subsection_candidate()).proposals[0]
    assert sub.parent_concept == "电磁场导论"            # 规则 1: 章节标题作 parent

    chapter = refiner.refine(_chapter_candidate()).proposals[0]
    assert chapter.parent_concept == ""                 # 章级候选无自指父级


def test_hierarchy_refinement_subtopics(refiner):
    result = refiner.refine(
        _chapter_candidate(),
        subtopics=("麦克斯韦方程组", "边界条件", "麦克斯韦方程组", "  ", None),
    )
    # 规则 3: base 不被删除——1 base + 2 有效子话题（重复/空白/None 过滤）
    assert len(result.proposals) == 3
    base = result.proposals[0]
    assert base.refinement_kind == "base"
    subs = result.proposals[1:]
    assert [s.name for s in subs] == ["麦克斯韦方程组", "边界条件"]
    for sub in subs:
        assert sub.parent_concept == "电磁场导论"        # 子话题父级 = 概念自身
        assert sub.prerequisites == (base.concept_id,)  # 先学父概念
        assert sub.concept_id.startswith(base.concept_id + ":sub-")
        assert sub.refinement_kind == "subtopic"
    assert any("duplicate" in w for w in result.warnings)
    assert any("empty subtopic" in w for w in result.warnings)


def test_subsection_hierarchy_refinement(refiner):
    """子节候选也可细化: 父链两级（章 → 节 → 子话题）。"""
    result = refiner.refine(_subsection_candidate(), subtopics=("波方程",))
    base = result.proposals[0]
    sub = result.proposals[1]
    assert base.parent_concept == "电磁场导论"
    assert sub.parent_concept == "均匀平面波"
    assert sub.prerequisites == (base.concept_id,)


# ---------------------------------------------------------------------------
# 3. 实验继承 / 4. 置信度传递
# ---------------------------------------------------------------------------

def test_experiment_inheritance(refiner):
    result = refiner.refine(
        _chapter_candidate(), subtopics=("麦克斯韦方程组",)
    )
    assert all(
        p.related_experiments == ("uniform-plane-wave", "demo-te")
        for p in result.proposals
    )


def test_confidence_passthrough(refiner):
    for confidence in (0.8, 0.7, 0.5, 1.0):
        candidate = _chapter_candidate(confidence=confidence)
        result = refiner.refine(candidate, subtopics=("子话题",))
        assert all(p.confidence == pytest.approx(confidence) for p in result.proposals)


# ---------------------------------------------------------------------------
# 5. 空输入
# ---------------------------------------------------------------------------

def test_empty_input_fails(refiner):
    for bad in (None, "not-candidate", 42):
        result = refiner.refine(bad)
        assert result.success is False
        assert result.error.startswith("invalid_input")
        assert result.proposals == ()
        assert result.source_candidate is None


def test_empty_subtopics_yields_base_only(refiner):
    result = refiner.refine(_chapter_candidate(), subtopics=())
    assert result.success is True
    assert len(result.proposals) == 1
    assert result.warnings == ()


def test_refine_all(refiner):
    candidates = [_chapter_candidate(), _subsection_candidate()]
    results = refiner.refine_all(
        candidates, subtopics_by_id={"kc-abc1234567": ("子话题",)}
    )
    assert len(results) == 2
    assert len(results[0].proposals) == 2               # base + 1 子话题
    assert len(results[1].proposals) == 1


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(refiner):
    result = refiner.refine(_chapter_candidate(), subtopics=("麦克斯韦方程组",))
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert len(parsed["proposals"]) == 2
    base, sub = parsed["proposals"]
    assert base["parent_concept"] == ""
    assert sub["parent_concept"] == "电磁场导论"
    assert sub["prerequisites"] == [base["concept_id"]]
    assert sub["confidence"] == pytest.approx(0.8)
    assert sub["source_candidate_id"] == "kc-abc1234567"

    proposal_json = json.loads(result.proposals[0].to_json())
    assert proposal_json["refinement_kind"] == "base"

    failed = json.loads(
        ConceptRefinementResult(success=False, error="invalid_input: x").to_json()
    )
    assert failed["source_candidate"] is None


# ---------------------------------------------------------------------------
# 7. 不修改 KnowledgeGraph
# ---------------------------------------------------------------------------

def test_graph_never_touched(refiner):
    """细化全程: 种子知识图零变化; 输出无一成为图节点。"""
    graph = KnowledgeGraph()
    seed_before = {
        cid: graph.get_concept(cid)
        for cid in ("em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization")
    }

    candidate = _chapter_candidate()
    snapshot = candidate.to_dict()                      # 规则 3: 原概念不被修改
    result = refiner.refine(candidate, subtopics=("麦克斯韦方程组",))

    assert candidate.to_dict() == snapshot              # 候选对象原样
    assert result.source_candidate is candidate
    for cid, node in seed_before.items():
        assert graph.get_concept(cid) is node           # 图零变化
    for proposal in result.proposals:                   # 提案未成为图节点
        assert graph.get_concept(proposal.concept_id) is None
    assert graph.find_learning_path("em-te-polarization") == (
        "em-uniform-plane-wave", "em-te-polarization",
    )


def test_refiner_never_builds_nodes():
    """静态契约: refiner 不 import 知识图、不构造 ConceptNode（调用形态检查）。"""
    import core.learning.concept_refinement.refiner as refiner_mod
    import inspect

    source = inspect.getsource(refiner_mod)
    assert "knowledge_graph" not in source             # 不 import 图模块
    assert "ConceptNode(" not in source                # 不构造节点
    assert "ConceptNode" not in source.split('"""', 2)[2]  # 代码体（去 docstring）无引用
