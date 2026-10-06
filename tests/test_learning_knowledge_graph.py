# -*- coding: utf-8 -*-
"""M1.2 Knowledge Graph 验收测试。

覆盖：concept query / prerequisite query / experiment relation /
evidence link / unknown concept / cycle detection / JSON serialization /
M0.4–M1.2 全链贯通。
隔离：真实 runner 仅 1 次 run_experiment（全链用例）, 其余直接构造事件;
不触 Rule Engine/Review Scheduler/课程存储/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.knowledge_graph import (
    ConceptLearningContext,
    ConceptNode,
    EvidenceLinker,
    KnowledgeGraph,
)
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.mastery_integration import RuleEngineAdapter
from core.learning.mastery_integration.schema import RuleEvaluationInput
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def graph():
    return KnowledgeGraph()          # 种子图：EM 三概念


@pytest.fixture(scope="module")
def linker(graph):
    return EvidenceLinker(graph)


def _event(**overrides) -> RuleEvaluationInput:
    kwargs = dict(
        concept_id="em-te-polarization",
        signal_type="normal_learning_event",
        evidence_source="uniform-plane-wave",
        strength=0.7,
        timestamp="2026-09-23T13:00:00+00:00",
        evidence_id="ev_run_1",
    )
    kwargs.update(overrides)
    return RuleEvaluationInput(**kwargs)


# ---------------------------------------------------------------------------
# 1. concept query
# ---------------------------------------------------------------------------

def test_concept_query(graph):
    node = graph.get_concept("em-uniform-plane-wave")
    assert node is not None
    assert node.name == "均匀平面波传播"
    assert node.chapter.startswith("电磁场与电磁波")
    assert node.prerequisites == ()               # 入门概念无前置（合法）


def test_concept_query_node_immutability(graph):
    node = graph.get_concept("em-te-polarization")
    with pytest.raises(Exception):
        node.name = "x"                           # frozen dataclass


# ---------------------------------------------------------------------------
# 2. prerequisite query
# ---------------------------------------------------------------------------

def test_prerequisite_query(graph):
    assert graph.get_prerequisites("em-te-polarization") == ("em-uniform-plane-wave",)
    assert graph.get_prerequisites("em-uniform-plane-wave") == ()
    assert graph.get_prerequisites("ghost") == ()          # 未知 → 安全空


def test_learning_path_prereq_first(graph):
    path = graph.find_learning_path("em-te-polarization")
    assert path == ("em-uniform-plane-wave", "em-te-polarization")
    assert graph.find_learning_path("em-uniform-plane-wave") == ("em-uniform-plane-wave",)


# ---------------------------------------------------------------------------
# 3. experiment relation
# ---------------------------------------------------------------------------

def test_experiment_relation(graph):
    assert graph.get_related_experiments("em-te-polarization") == ("uniform-plane-wave",)
    assert graph.get_related_experiments("ghost") == ()


# ---------------------------------------------------------------------------
# 4. evidence link
# ---------------------------------------------------------------------------

def test_evidence_link(linker):
    ctx = linker.link(_event())
    assert isinstance(ctx, ConceptLearningContext)
    assert ctx.known is True
    assert ctx.concept_id == "em-te-polarization"
    assert ctx.concept_name == "TE 极化"
    assert ctx.prerequisites == ("em-uniform-plane-wave",)
    assert ctx.related_experiments == ("uniform-plane-wave",)
    assert ctx.learning_path == ("em-uniform-plane-wave", "em-te-polarization")
    # 事件回显完整
    assert ctx.event_type == "normal_learning_event"
    assert ctx.strength == pytest.approx(0.7)
    assert ctx.timestamp == "2026-09-23T13:00:00+00:00"
    assert ctx.evidence_id == "ev_run_1"


def test_evidence_link_empty_prerequisite_concept(linker):
    """空 prerequisite 合法：入门概念的上下文路径就是自身。"""
    ctx = linker.link(_event(concept_id="em-uniform-plane-wave"))
    assert ctx.known is True
    assert ctx.prerequisites == ()
    assert ctx.learning_path == ("em-uniform-plane-wave",)


# ---------------------------------------------------------------------------
# 5. unknown concept（安全返回）
# ---------------------------------------------------------------------------

def test_unknown_concept_safe_return(linker):
    ctx = linker.link(_event(concept_id="ghost-concept"))
    assert ctx.known is False
    assert ctx.concept_name == ""
    assert ctx.learning_path == ()
    # 事件回显不丢
    assert ctx.event_type == "normal_learning_event"
    assert ctx.strength == pytest.approx(0.7)
    assert ctx.evidence_id == "ev_run_1"


def test_invalid_and_empty_concept_safe_return(linker):
    ctx = linker.link(None)
    assert ctx.known is False and ctx.reason == "invalid_input"
    ctx = linker.link("not-event")
    assert ctx.known is False and ctx.reason == "invalid_input"

    corrupted = _event()
    object.__setattr__(corrupted, "concept_id", "")
    ctx = linker.link(corrupted)
    assert ctx.known is False and ctx.reason == "empty_concept"


# ---------------------------------------------------------------------------
# 6. cycle detection
# ---------------------------------------------------------------------------

def test_cycle_detection():
    cyclic = KnowledgeGraph([
        ConceptNode(concept_id="a", name="A", prerequisites=("c",)),
        ConceptNode(concept_id="b", name="B", prerequisites=("a",)),
        ConceptNode(concept_id="c", name="C", prerequisites=("b",)),
    ])
    cycle = cyclic.find_cycle()
    assert len(cycle) == 3                       # a→c→b→... 回边被检出
    assert set(cycle) == {"a", "b", "c"}
    # 环上目标：节点存在但路径安全拒绝
    assert cyclic.find_learning_path("a") == ()
    linker = EvidenceLinker(cyclic)
    ctx = linker.link(_event(concept_id="a"))
    assert ctx.known is True                     # 节点存在
    assert ctx.learning_path == ()               # 路径拒绝
    # 无环图
    assert KnowledgeGraph().find_cycle() == ()


def test_diamond_dependency_path_order():
    """菱形依赖：路径含全部前置且前置先于目标（拓扑序）。"""
    g = KnowledgeGraph([
        ConceptNode(concept_id="base", name="基"),
        ConceptNode(concept_id="l", name="左", prerequisites=("base",)),
        ConceptNode(concept_id="r", name="右", prerequisites=("base",)),
        ConceptNode(concept_id="top", name="顶", prerequisites=("l", "r")),
    ])
    path = g.find_learning_path("top")
    assert path[0] == "base"
    assert path[-1] == "top"
    assert set(path) == {"base", "l", "r", "top"}
    assert path.index("l") < path.index("top")
    assert path.index("r") < path.index("top")


# ---------------------------------------------------------------------------
# 7. JSON serialization
# ---------------------------------------------------------------------------

def test_json_serialization(linker):
    ctx = linker.link(_event())
    d = ctx.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["known"] is True
    assert parsed["concept_name"] == "TE 极化"
    assert parsed["prerequisites"] == ["em-uniform-plane-wave"]
    assert parsed["learning_path"] == ["em-uniform-plane-wave", "em-te-polarization"]
    assert parsed["strength"] == pytest.approx(0.7)

    parsed2 = json.loads(ctx.to_json())
    assert parsed2["event_type"] == "normal_learning_event"

    node_json = json.loads(graph_node_json())
    assert node_json["concept_id"] == "em-uniform-plane-wave"


def graph_node_json() -> str:
    from core.learning.knowledge_graph import KnowledgeGraph as _KG
    return _KG().get_concept("em-uniform-plane-wave").to_json()


# ---------------------------------------------------------------------------
# 8. M0.4–M1.2 全链贯通
# ---------------------------------------------------------------------------

def test_full_chain_m04_to_m12(runner, linker, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 → M1.1 → M1.2 单链贯通。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = EvidenceBuilder().from_result(result)
    inputs = EvidenceBridge().convert(ev).inputs
    signals = MasteryAdapter().convert_all(inputs)
    events = RuleEngineAdapter().convert_all(signals)
    contexts = linker.link_all(events)

    assert len(contexts) == 3
    by_id = {c.concept_id: c for c in contexts}
    assert set(by_id) == {
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }
    assert all(c.known for c in contexts)        # 种子图覆盖全部注册表概念

    wave = by_id["em-uniform-plane-wave"]
    assert wave.learning_path == ("em-uniform-plane-wave",)
    assert wave.event_type == "normal_learning_event"
    assert wave.strength == pytest.approx(EvidenceBuilder.RUN_CONFIDENCE)
    assert wave.evidence_id == ev.evidence_id

    te = by_id["em-te-polarization"]
    assert te.learning_path == ("em-uniform-plane-wave", "em-te-polarization")
    assert te.related_experiments == ("uniform-plane-wave",)
