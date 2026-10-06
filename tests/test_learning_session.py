# -*- coding: utf-8 -*-
"""M1.5 Session Planner 验收测试。

覆盖：lesson/experiment/review/none 四类会话 / 非法输入 /
JSON 序列化 / M0.4–M1.5 全链贯通。
隔离：真实 runner 仅 1 次 run_experiment（全链用例）, 其余直接构造提案;
纯内存, 不触课程存储/Rule Engine/DB/UI/模型。
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
from core.learning.session import (
    SESSION_EXPERIMENT,
    SESSION_LESSON,
    SESSION_NONE,
    SESSION_REVIEW,
    STEPS_EXPERIMENT,
    STEPS_LESSON,
    STEPS_NONE,
    STEPS_REVIEW,
    LearningSessionPlan,
    SessionPlanner,
)
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def session_planner():
    return SessionPlanner()


def _proposal(**overrides) -> LearningPlanProposal:
    kwargs = dict(
        proposal_type=PROPOSAL_INSERT_PREREQUISITE,
        target_concept="em-uniform-plane-wave",
        related_experiment="uniform-plane-wave",
        reason="先修前置概念「均匀平面波传播」后再学「TE 极化」",
        priority=1,
    )
    kwargs.update(overrides)
    return LearningPlanProposal(**kwargs)


# ---------------------------------------------------------------------------
# 规则 1: insert_prerequisite → lesson 会话
# ---------------------------------------------------------------------------

def test_lesson_session(session_planner):
    plan = session_planner.plan(_proposal())
    assert isinstance(plan, LearningSessionPlan)
    assert plan.session_type == SESSION_LESSON
    assert plan.steps == (
        "introduce_concept", "explain_prerequisite", "check_understanding",
    )
    assert plan.steps == STEPS_LESSON
    assert plan.target_concept == "em-uniform-plane-wave"
    assert plan.related_experiment == "uniform-plane-wave"
    assert plan.reason == "先修前置概念「均匀平面波传播」后再学「TE 极化」"   # 原值继承
    assert plan.priority == 1


# ---------------------------------------------------------------------------
# 规则 2: add_experiment → experiment 会话
# ---------------------------------------------------------------------------

def test_experiment_session(session_planner):
    plan = session_planner.plan(_proposal(
        proposal_type=PROPOSAL_ADD_EXPERIMENT,
        reason="掌握良好，建议在实验中观察", priority=3,
    ))
    assert plan.session_type == SESSION_EXPERIMENT
    assert plan.steps == ("prepare", "simulate", "observe", "reflect")
    assert plan.steps == STEPS_EXPERIMENT
    assert plan.priority == 3


# ---------------------------------------------------------------------------
# 规则 3: add_review → review 会话
# ---------------------------------------------------------------------------

def test_review_session(session_planner):
    plan = session_planner.plan(_proposal(
        proposal_type=PROPOSAL_ADD_REVIEW,
        reason="掌握强度 0.40 低于阈值 0.60", priority=2,
    ))
    assert plan.session_type == SESSION_REVIEW
    assert plan.steps == ("recall", "practice", "evaluate")
    assert plan.steps == STEPS_REVIEW
    assert plan.priority == 2


# ---------------------------------------------------------------------------
# 规则 4: no_change → none 会话
# ---------------------------------------------------------------------------

def test_none_session(session_planner):
    plan = session_planner.plan(_proposal(
        proposal_type=PROPOSAL_NO_CHANGE,
        target_concept="ghost-concept",
        reason="unknown_concept", related_experiment="", priority=0,
    ))
    assert plan.session_type == SESSION_NONE
    assert plan.steps == STEPS_NONE
    assert plan.target_concept == "ghost-concept"    # 回显保留
    assert plan.reason == "unknown_concept"
    assert plan.priority == 0


# ---------------------------------------------------------------------------
# 非法输入安全返回
# ---------------------------------------------------------------------------

def test_invalid_input_safe_return(session_planner):
    p = session_planner.plan(None)
    assert p.session_type == SESSION_NONE
    assert p.reason == "invalid_input"
    assert p.target_concept == ""

    assert session_planner.plan("not-proposal").reason == "invalid_input"
    assert session_planner.plan(42).session_type == SESSION_NONE


def test_unknown_proposal_type_safe_return(session_planner):
    """proposal_type 词表封闭, 但构造层不校验——防御性落到 none。"""
    plan = session_planner.plan(_proposal(proposal_type="mystery"))
    assert plan.session_type == SESSION_NONE
    assert plan.reason == "unknown_proposal:mystery"
    assert plan.target_concept == "em-uniform-plane-wave"   # 其余字段仍回显
    assert plan.steps == STEPS_NONE


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(session_planner):
    plan = session_planner.plan(_proposal(
        proposal_type=PROPOSAL_ADD_EXPERIMENT, priority=3,
    ))
    d = plan.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["session_type"] == "experiment"
    assert parsed["steps"] == ["prepare", "simulate", "observe", "reflect"]  # tuple→list
    assert parsed["target_concept"] == "em-uniform-plane-wave"
    assert parsed["priority"] == 3

    parsed2 = json.loads(plan.to_json())
    assert parsed2["related_experiment"] == "uniform-plane-wave"


# ---------------------------------------------------------------------------
# 批量 + 确定性 + 纯内存契约
# ---------------------------------------------------------------------------

def test_plan_all(session_planner):
    proposals = [
        _proposal(),                                                        # lesson
        _proposal(proposal_type=PROPOSAL_ADD_EXPERIMENT, priority=3),       # experiment
        _proposal(proposal_type=PROPOSAL_ADD_REVIEW, priority=2),           # review
        _proposal(proposal_type=PROPOSAL_NO_CHANGE, priority=0),            # none
        None,                                                               # none/invalid
    ]
    types = [p.session_type for p in session_planner.plan_all(proposals)]
    assert types == [SESSION_LESSON, SESSION_EXPERIMENT, SESSION_REVIEW, SESSION_NONE, SESSION_NONE]


def test_deterministic(session_planner):
    proposal = _proposal()
    assert session_planner.plan(proposal).to_dict() == session_planner.plan(proposal).to_dict()


def test_session_planner_never_touches_store(session_planner):
    """纯内存契约：模块不 import 课程存储/文件 IO（防回归的静态断言）。"""
    import core.learning.session.planner as planner_mod
    import inspect

    source = inspect.getsource(planner_mod)
    assert "curriculum.store" not in source
    assert "Store(" not in source
    assert "open(" not in source


# ---------------------------------------------------------------------------
# M0.4–M1.5 全链贯通
# ---------------------------------------------------------------------------

def test_full_chain_m04_to_m15(runner, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 → M1.3 → M1.4 → M1.5 单链贯通。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = EvidenceBuilder().from_result(result)
    inputs = EvidenceBridge().convert(ev).inputs
    signals = MasteryAdapter().convert_all(inputs)
    recs = RecommendationEngine(KnowledgeGraph()).recommend_all(signals)
    proposals = CurriculumPlanner().propose_all(recs)
    plans = SessionPlanner().plan_all(proposals)

    assert len(plans) == 3
    types = [p.session_type for p in plans]
    assert types.count(SESSION_EXPERIMENT) == 1                 # wave
    assert types.count(SESSION_LESSON) == 2                     # te/tm

    exp = next(p for p in plans if p.session_type == SESSION_EXPERIMENT)
    assert exp.target_concept == "em-uniform-plane-wave"
    assert exp.related_experiment == "uniform-plane-wave"
    assert exp.steps == STEPS_EXPERIMENT
    assert exp.priority == 3

    lessons = [p for p in plans if p.session_type == SESSION_LESSON]
    assert all(p.target_concept == "em-uniform-plane-wave" for p in lessons)
    assert all(p.steps == STEPS_LESSON for p in lessons)
    assert all(p.priority == 1 for p in lessons)
