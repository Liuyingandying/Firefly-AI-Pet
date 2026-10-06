# -*- coding: utf-8 -*-
"""M2.4 Knowledge Import 验收测试。

覆盖：单章节生成 / 多章节生成 / 前置关系 / 实验关联 / 空章节 /
非法输入 / JSON 序列化 / 不修改 Graph 验证。
隔离：fixtures 走 M2.1→M2.3 真实链; 不写 KnowledgeGraph、不触课程存储、无 LLM。
"""

from __future__ import annotations

import json

import pytest

from core.learning.extractor import NativeExtractor, to_document_structure
from core.learning.curriculum.adapter.draft import build_draft
from core.learning.knowledge_graph import KnowledgeGraph
from core.learning.knowledge_import import (
    CONFIDENCE_EMPTY_CHAPTER,
    CONFIDENCE_SUBSECTION,
    ERR_CONFIRMATION_REQUIRED,
    ERR_INVALID_INPUT,
    ERR_NO_CHAPTERS,
    ConceptCandidate,
    KnowledgeCandidateResult,
    convert_approved,
    extract_candidates,
)

MD_TWO_CHAPTERS = """# 电磁场导论

引言。

## 均匀平面波

平面波正文。

## 空节

# 天线基础

天线正文。
"""


@pytest.fixture(scope="module")
def draft_and_structure(tmp_path_factory):
    base = tmp_path_factory.mktemp("m24")
    (base / "textbook.md").write_text(MD_TWO_CHAPTERS, encoding="utf-8")
    extracted = NativeExtractor().extract(base / "textbook.md").document_structure
    structure = to_document_structure(extracted)
    draft = build_draft(structure, "course-em")
    return draft, structure


@pytest.fixture(scope="module")
def result(draft_and_structure):
    draft, structure = draft_and_structure
    return extract_candidates(draft, structure)


# ---------------------------------------------------------------------------
# 1. 单章节生成
# ---------------------------------------------------------------------------

def test_single_chapter(tmp_path):
    (tmp_path / "one.md").write_text("# 唯一章\n\n正文。\n", encoding="utf-8")
    extracted = NativeExtractor().extract(tmp_path / "one.md").document_structure
    structure = to_document_structure(extracted)
    draft = build_draft(structure, "c")

    result = extract_candidates(draft, structure)
    assert result.success is True
    assert result.error == ""
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert isinstance(cand, ConceptCandidate)
    assert cand.name == "唯一章"
    assert cand.chapter == "唯一章"                  # level-1 章归属 = 自身
    assert cand.prerequisites == ()                  # level-1 无层级前置
    assert cand.confidence == pytest.approx(0.5)     # 无子节无概念 → 空章降权
    assert cand.level == 1
    assert cand.concept_id.startswith("kc-")


# ---------------------------------------------------------------------------
# 2. 多章节生成
# ---------------------------------------------------------------------------

def test_multi_chapter(result):
    assert result.success is True
    names = [c.name for c in result.candidates]
    # 两章 + 波节（"空节" 无内容也保留大纲候选——层级来自结构, 非内容）
    assert "电磁场导论" in names and "天线基础" in names
    assert "均匀平面波" in names
    chapters = [c for c in result.candidates if c.level == 1]
    assert [c.name for c in chapters] == ["电磁场导论", "天线基础"]
    # 章归属: 子节挂在章下
    wave = next(c for c in result.candidates if c.name == "均匀平面波")
    assert wave.chapter == "电磁场导论"
    assert wave.level == 2


# ---------------------------------------------------------------------------
# 3. 前置关系（heading hierarchy → prerequisite suggestion）
# ---------------------------------------------------------------------------

def test_prerequisite_suggestion(result):
    wave = next(c for c in result.candidates if c.name == "均匀平面波")
    chapter = next(c for c in result.candidates if c.name == "电磁场导论")
    assert wave.prerequisites == (chapter.concept_id,)   # 父先于子
    for cand in result.candidates:
        if cand.level == 1:
            assert cand.prerequisites == ()
    # id 确定性: 同输入两次提取结果一致
    draft, structure = None, None


def test_candidate_ids_deterministic(draft_and_structure):
    draft, structure = draft_and_structure
    r1 = extract_candidates(draft, structure)
    r2 = extract_candidates(draft, structure)
    assert [c.to_dict() for c in r1.candidates] == [c.to_dict() for c in r2.candidates]


# ---------------------------------------------------------------------------
# 4. 实验关联（experiment metadata → related_experiments）
# ---------------------------------------------------------------------------

def test_experiment_relation(draft_and_structure):
    draft, structure = draft_and_structure
    result = extract_candidates(
        draft,
        structure,
        experiment_metadata={
            "均匀平面波": ("uniform-plane-wave",),
            "电磁场导论": ("uniform-plane-wave", "demo-te"),
        },
    )
    by_name = {c.name: c for c in result.candidates}
    assert by_name["均匀平面波"].related_experiments == (
        "uniform-plane-wave", "demo-te",
    )   # 节级 + 章级继承（保序去重）
    antenna = by_name["天线基础"]
    assert antenna.related_experiments == ()          # 无元数据 → 空
    # 无元数据调用 → 全空
    plain = extract_candidates(draft, structure)
    assert all(c.related_experiments == () for c in plain.candidates)


# ---------------------------------------------------------------------------
# 5. 空章节
# ---------------------------------------------------------------------------

def test_empty_chapter_downgraded(result):
    """无子节且无概念提案的章 → 降权 + 告警（候选仍生成）。"""
    antenna = next(c for c in result.candidates if c.name == "天线基础")
    assert antenna.confidence == pytest.approx(CONFIDENCE_EMPTY_CHAPTER)
    assert any("empty chapter" in w and "天线基础" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 6. 非法输入
# ---------------------------------------------------------------------------

def test_invalid_input():
    for bad in (None, "not-draft", 42):
        result = extract_candidates(bad)
        assert result.success is False
        assert result.error.startswith(ERR_INVALID_INPUT)
        assert result.candidates == ()


def test_no_chapters_fails():
    from core.learning.curriculum.models import CurriculumDraft

    empty_draft = CurriculumDraft(id="d0", course_id="c", title="空草案")
    result = extract_candidates(empty_draft)
    assert result.success is False
    assert result.error.startswith(ERR_NO_CHAPTERS)


# ---------------------------------------------------------------------------
# 7. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(result):
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["source_document"]["chapter_count"] == 2
    cand = parsed["candidates"][0]
    assert cand["concept_id"].startswith("kc-")
    assert cand["confidence"] == pytest.approx(0.8)

    cand_json = json.loads(result.candidates[0].to_json())
    assert cand_json["level"] == 1

    # 注入门拒绝结果同样可序列化
    rejected = convert_approved(result, approved_ids=())
    parsed_reject = json.loads(rejected.to_json())
    assert parsed_reject["error"].startswith(ERR_CONFIRMATION_REQUIRED)


# ---------------------------------------------------------------------------
# 8. 不修改 Graph 验证（规则 4/5）
# ---------------------------------------------------------------------------

def test_graph_never_modified(result):
    """提取 + 显式 confirm 全程: 种子知识图零变化。"""
    graph = KnowledgeGraph()                       # M1.2 种子图（EM 三概念）
    seed_before = {
        cid: graph.get_concept(cid)
        for cid in ("em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization")
    }
    path_before = graph.find_learning_path("em-te-polarization")

    # 门 1: 无批准 → 拒绝
    rejected = convert_approved(result, approved_ids=())
    assert rejected.success is False
    assert rejected.nodes == ()

    # 门 2: 显式批准 → 构造节点（返回值, 不入图）
    approved = {c.concept_id for c in result.candidates}
    injection = convert_approved(result, approved_ids=approved, confirmed_by="user-1")
    assert injection.success is True
    assert injection.approved_count == len(result.candidates)
    assert len(injection.nodes) == len(result.candidates)

    # 图零变化: 种子节点原样、候选 id 不在图中、路径不变
    for cid, node in seed_before.items():
        assert graph.get_concept(cid) is node
    for cand in result.candidates:
        assert graph.get_concept(cand.concept_id) is None
    assert graph.find_cycle() == ()
    assert graph.find_learning_path("em-te-polarization") == path_before

    # 构造出的节点形状符合 M1.2 契约（可被 KnowledgeGraph(nodes=...) 消费）
    node = injection.nodes[0]
    assert node.name and isinstance(node.prerequisites, tuple)


def test_injection_requires_explicit_approval(result):
    """规则 5: 未批准的候选被跳过; 前置指向未批准者 → 边丢弃且记录。"""
    candidates = list(result.candidates)
    chapter = next(c for c in candidates if c.level == 1)
    wave = next(c for c in candidates if c.name == "均匀平面波")

    # 只批准子节（不批准其父章）→ 前置边丢弃 + skipped 记录
    injection = convert_approved(result, approved_ids={wave.concept_id})
    assert injection.success is True
    assert injection.approved_count == 1
    assert injection.nodes[0].prerequisites == ()          # 父未批准 → 边丢弃
    assert f"{wave.concept_id}:prerequisite-dropped" in injection.skipped
    assert chapter.concept_id in injection.skipped

    # 全批准 → 父子前置边保留
    full = convert_approved(result, approved_ids={c.concept_id for c in candidates})
    full_wave = next(n for n in full.nodes if n.concept_id == wave.concept_id)
    assert full_wave.prerequisites == (chapter.concept_id,)
