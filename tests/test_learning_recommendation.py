# -*- coding: utf-8 -*-
"""M1.3 Recommendation Engine 验收测试。

覆盖：prerequisite recommendation / simulation recommendation /
unknown concept / weak mastery / JSON serialization / M0.4–M1.3 全链贯通。
隔离：真实 runner 仅 1 次 run_experiment（全链用例）, 其余直接构造
MasterySignal; 不触 Rule Engine/Review Scheduler/课程存储/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.knowledge_graph import KnowledgeGraph
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.mastery_adapter.schema import MasterySignal
from core.learning.recommendation import (
    ACTION_NONE,
    ACTION_PREREQUISITE,
    ACTION_REVIEW,
    ACTION_SIMULATION,
    PRIORITY_NONE,
    PRIORITY_PREREQUISITE,
    PRIORITY_REVIEW,
    PRIORITY_SIMULATION,
    LearningRecommendation,
    RecommendationEngine,
)
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def graph():
    return KnowledgeGraph()


@pytest.fixture(scope="module")
def engine(graph):
    return RecommendationEngine(graph)


def _signal(**overrides) -> MasterySignal:
    kwargs = dict(
        concept_id="em-uniform-plane-wave",
        signal_type="basic",
        source="uniform-plane-wave",
        strength=0.9,
        timestamp="2026-09-23T14:00:00+00:00",
        evidence_id="ev_run_1",
    )
    kwargs.update(overrides)
    return MasterySignal(**kwargs)


# ---------------------------------------------------------------------------
# 规则 2: prerequisite recommendation（优先级最高）
# ---------------------------------------------------------------------------

def test_prerequisite_recommendation(engine):
    sig = _signal(concept_id="em-te-polarization", strength=0.9)  # 掌握好但有前置
    rec = engine.recommend(sig)
    assert isinstance(rec, LearningRecommendation)
    assert rec.action == ACTION_PREREQUISITE
    assert rec.target_concept == "em-uniform-plane-wave"     # 指向前置概念
    assert rec.related_experiment == "uniform-plane-wave"    # 目标概念的实验
    assert rec.priority == PRIORITY_PREREQUISITE
    assert "先修" in rec.reason


def test_prerequisite_beats_weak_mastery(engine):
    """弱掌握 + 有前置 → 前置仍优先（教学顺序：先补地基）。"""
    sig = _signal(concept_id="em-tm-polarization", strength=0.2)
    rec = engine.recommend(sig)
    assert rec.action == ACTION_PREREQUISITE
    assert rec.priority == PRIORITY_PREREQUISITE


# ---------------------------------------------------------------------------
# 规则 1: weak mastery → review
# ---------------------------------------------------------------------------

def test_weak_mastery_review(engine):
    sig = _signal(strength=0.4)                    # 无前置入门概念 + 低强度
    rec = engine.recommend(sig)
    assert rec.action == ACTION_REVIEW
    assert rec.target_concept == "em-uniform-plane-wave"
    assert rec.priority == PRIORITY_REVIEW
    assert "0.40" in rec.reason and "0.60" in rec.reason  # 阈值进入理由


def test_threshold_boundary_not_weak(engine):
    sig = _signal(strength=0.6)                    # 恰好等于阈值 → 不算弱
    rec = engine.recommend(sig)
    assert rec.action == ACTION_SIMULATION


# ---------------------------------------------------------------------------
# 规则 3: simulation recommendation
# ---------------------------------------------------------------------------

def test_simulation_recommendation(engine):
    sig = _signal(strength=0.9)                    # 无前置 + 掌握好 + 有实验
    rec = engine.recommend(sig)
    assert rec.action == ACTION_SIMULATION
    assert rec.target_concept == "em-uniform-plane-wave"
    assert rec.related_experiment == "uniform-plane-wave"
    assert rec.priority == PRIORITY_SIMULATION


# ---------------------------------------------------------------------------
# 规则 4: unknown concept → 安全返回
# ---------------------------------------------------------------------------

def test_unknown_concept_safe_return(engine):
    rec = engine.recommend(_signal(concept_id="ghost-concept"))
    assert rec.action == ACTION_NONE
    assert rec.target_concept == "ghost-concept"   # 回显便于追踪
    assert rec.reason == "unknown_concept"
    assert rec.priority == PRIORITY_NONE


def test_invalid_input_safe_return(engine):
    assert engine.recommend(None).action == ACTION_NONE
    assert engine.recommend("not-signal").reason == "invalid_input"
    assert engine.recommend(_signal(concept_id="  ")).reason == "empty_concept"

    corrupted = _signal()
    object.__setattr__(corrupted, "strength", 1.5)
    assert engine.recommend(corrupted).reason == "invalid_strength"


# ---------------------------------------------------------------------------
# 规则 5: JSON serialization
# ---------------------------------------------------------------------------

def test_json_serialization(engine):
    rec = engine.recommend(_signal(concept_id="em-te-polarization"))
    d = rec.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["action"] == "prerequisite"
    assert parsed["target_concept"] == "em-uniform-plane-wave"
    assert parsed["related_experiment"] == "uniform-plane-wave"
    assert parsed["priority"] == 1

    parsed2 = json.loads(rec.to_json())
    assert "先修" in parsed2["reason"]


# ---------------------------------------------------------------------------
# 批量 + 确定性
# ---------------------------------------------------------------------------

def test_recommend_all(engine):
    signals = [
        _signal(strength=0.9),                                     # simulation
        _signal(concept_id="em-te-polarization", strength=0.9),    # prerequisite
        _signal(strength=0.3),                                     # review
        _signal(concept_id="ghost"),                               # none
        None,                                                      # none
    ]
    recs = engine.recommend_all(signals)
    assert [r.action for r in recs] == [
        ACTION_SIMULATION, ACTION_PREREQUISITE, ACTION_REVIEW, ACTION_NONE, ACTION_NONE,
    ]


def test_deterministic(engine):
    sig = _signal(concept_id="em-te-polarization")
    assert engine.recommend(sig).to_dict() == engine.recommend(sig).to_dict()


def test_custom_threshold_injection(graph):
    strict = RecommendationEngine(graph, weak_threshold=0.95)
    rec = strict.recommend(_signal(strength=0.9))
    assert rec.action == ACTION_REVIEW


# ---------------------------------------------------------------------------
# M0.4–M1.3 全链贯通
# ---------------------------------------------------------------------------

def test_full_chain_m04_to_m13(runner, graph, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 → M1.3 单链贯通。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = EvidenceBuilder().from_result(result)
    inputs = EvidenceBridge().convert(ev).inputs
    signals = MasteryAdapter().convert_all(inputs)
    recs = RecommendationEngine(graph).recommend_all(signals)

    assert len(recs) == 3
    # wave（0.7 ≥ 阈值, 无前置, 有实验）→ simulation
    wave_recs = [r for r in recs if r.action == ACTION_SIMULATION]
    assert len(wave_recs) == 1
    assert wave_recs[0].target_concept == "em-uniform-plane-wave"
    assert wave_recs[0].related_experiment == "uniform-plane-wave"
    # te/tm（有前置）→ prerequisite 指向 wave, 优先级 1
    prereq_recs = [r for r in recs if r.action == ACTION_PREREQUISITE]
    assert len(prereq_recs) == 2
    assert all(r.target_concept == "em-uniform-plane-wave" for r in prereq_recs)
    assert all(r.priority == PRIORITY_PREREQUISITE for r in prereq_recs)
