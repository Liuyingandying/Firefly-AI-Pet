# -*- coding: utf-8 -*-
"""M1.4 Curriculum Planner 验收测试。

覆盖：prerequisite/simulation/review/none 四类提案 / 非法输入 /
JSON 序列化 / M0.4–M1.4 全链贯通。
隔离：真实 runner 仅 1 次 run_experiment（全链用例）, 其余直接构造推荐;
不触 curriculum 存储/Rule Engine/Review Scheduler/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.curriculum_planner import (
    PROPOSAL_ADD_EXPERIMENT,
    PROPOSAL_ADD_REVIEW,
    PROPOSAL_INSERT_PREREQUISITE,
    PROPOSAL_NO_CHANGE,
    CurriculumPlanner,
    LearningPlanProposal,
)
from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.knowledge_graph import KnowledgeGraph
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.recommendation import RecommendationEngine
from core.learning.recommendation.schema import LearningRecommendation
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def planner():
    return CurriculumPlanner()


def _rec(**overrides) -> LearningRecommendation:
    kwargs = dict(
        action="prerequisite",
        target_concept="em-uniform-plane-wave",
        reason="先修前置概念「均匀平面波传播」后再学「TE 极化」",
        related_experiment="uniform-plane-wave",
        priority=1,
    )
    kwargs.update(overrides)
    return LearningRecommendation(**kwargs)


# ---------------------------------------------------------------------------
# 规则 1-4: action → proposal_type 映射
# ---------------------------------------------------------------------------

def test_prerequisite_proposal(planner):
    p = planner.propose(_rec())
    assert isinstance(p, LearningPlanProposal)
    assert p.proposal_type == PROPOSAL_INSERT_PREREQUISITE
    assert p.target_concept == "em-uniform-plane-wave"
    assert p.related_experiment == "uniform-plane-wave"
    assert p.reason == "先修前置概念「均匀平面波传播」后再学「TE 极化」"   # 原值继承
    assert p.priority == 1                                                # 原值继承


def test_simulation_proposal(planner):
    p = planner.propose(_rec(
        action="simulation", target_concept="em-uniform-plane-wave",
        reason="掌握良好，建议在实验中观察", priority=3,
    ))
    assert p.proposal_type == PROPOSAL_ADD_EXPERIMENT
    assert p.target_concept == "em-uniform-plane-wave"
    assert p.priority == 3


def test_review_proposal(planner):
    p = planner.propose(_rec(
        action="review", target_concept="em-uniform-plane-wave",
        reason="掌握强度 0.40 低于阈值 0.60", priority=2,
    ))
    assert p.proposal_type == PROPOSAL_ADD_REVIEW
    assert p.priority == 2


def test_none_proposal(planner):
    p = planner.propose(_rec(
        action="none", target_concept="ghost-concept",
        reason="unknown_concept", related_experiment="", priority=0,
    ))
    assert p.proposal_type == PROPOSAL_NO_CHANGE
    assert p.target_concept == "ghost-concept"    # 回显保留
    assert p.reason == "unknown_concept"
    assert p.priority == 0


# ---------------------------------------------------------------------------
# 非法输入安全返回
# ---------------------------------------------------------------------------

def test_invalid_input_safe_return(planner):
    p = planner.propose(None)
    assert p.proposal_type == PROPOSAL_NO_CHANGE
    assert p.reason == "invalid_input"
    assert p.target_concept == ""

    assert planner.propose("not-rec").reason == "invalid_input"
    assert planner.propose(123).proposal_type == PROPOSAL_NO_CHANGE


def test_unknown_action_safe_return(planner):
    """action 词表封闭, 但构造层不校验——防御性落到 no_change。"""
    p = planner.propose(_rec(action="mystery"))
    assert p.proposal_type == PROPOSAL_NO_CHANGE
    assert p.reason == "unknown_action:mystery"
    assert p.target_concept == "em-uniform-plane-wave"    # 其余字段仍回显


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(planner):
    p = planner.propose(_rec())
    d = p.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["proposal_type"] == "insert_prerequisite"
    assert parsed["target_concept"] == "em-uniform-plane-wave"
    assert parsed["related_experiment"] == "uniform-plane-wave"
    assert parsed["priority"] == 1

    parsed2 = json.loads(p.to_json())
    assert "先修" in parsed2["reason"]


# ---------------------------------------------------------------------------
# 批量 + 确定性 + 永不写数据
# ---------------------------------------------------------------------------

def test_propose_all(planner):
    recs = [
        _rec(),                                                            # insert_prerequisite
        _rec(action="simulation", priority=3),                             # add_experiment
        _rec(action="review", priority=2),                                 # add_review
        _rec(action="none", reason="unknown_concept", priority=0),         # no_change
        None,                                                              # no_change/invalid
    ]
    types = [p.proposal_type for p in planner.propose_all(recs)]
    assert types == [
        PROPOSAL_INSERT_PREREQUISITE, PROPOSAL_ADD_EXPERIMENT,
        PROPOSAL_ADD_REVIEW, PROPOSAL_NO_CHANGE, PROPOSAL_NO_CHANGE,
    ]


def test_deterministic(planner):
    rec = _rec()
    assert planner.propose(rec).to_dict() == planner.propose(rec).to_dict()


def test_planner_never_touches_curriculum_store(planner):
    """纯内存契约：模块不 import 课程存储（防回归的静态断言）。"""
    import core.learning.curriculum_planner.planner as planner_mod
    import inspect

    source = inspect.getsource(planner_mod)
    assert "curriculum.store" not in source
    assert "Store(" not in source
    assert "open(" not in source


# ---------------------------------------------------------------------------
# M0.4–M1.4 全链贯通
# ---------------------------------------------------------------------------

def test_full_chain_m04_to_m14(runner, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 → M1.3 → M1.4 单链贯通。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = EvidenceBuilder().from_result(result)
    inputs = EvidenceBridge().convert(ev).inputs
    signals = MasteryAdapter().convert_all(inputs)
    recs = RecommendationEngine(KnowledgeGraph()).recommend_all(signals)
    proposals = CurriculumPlanner().propose_all(recs)

    assert len(proposals) == 3
    types = [p.proposal_type for p in proposals]
    assert types.count(PROPOSAL_ADD_EXPERIMENT) == 1            # wave
    assert types.count(PROPOSAL_INSERT_PREREQUISITE) == 2       # te/tm

    exp = next(p for p in proposals if p.proposal_type == PROPOSAL_ADD_EXPERIMENT)
    assert exp.target_concept == "em-uniform-plane-wave"
    assert exp.related_experiment == "uniform-plane-wave"
    assert exp.priority == 3

    ins = [p for p in proposals if p.proposal_type == PROPOSAL_INSERT_PREREQUISITE]
    assert all(p.target_concept == "em-uniform-plane-wave" for p in ins)
    assert all(p.priority == 1 for p in ins)
    assert all(p.related_experiment == "uniform-plane-wave" for p in ins)
