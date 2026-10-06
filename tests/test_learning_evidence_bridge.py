# -*- coding: utf-8 -*-
"""M0.7 Evidence Bridge 验收测试。

覆盖：experiment_run→基础证据 / param_comparison→强化证据 /
失败证据拒绝转换 / 空 concept 安全丢弃 / JSON 序列化,
另加批量转换与 M0.4→M0.6→M0.7 全链集成。
隔离：真实 runner 仅 1 次 run_experiment（全链用例）,
其余直接构造 LearningEvidence; 不触 Rule Engine/Review Scheduler/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence import EvidenceBuilder, LearningEvidence
from core.learning.evidence_bridge import (
    EVIDENCE_TYPE_BASIC,
    EVIDENCE_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    BridgeOutcome,
    EvidenceBridge,
    MasteryEvidenceInput,
)
from core.learning.simulation.runner import SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def builder():
    return EvidenceBuilder()


@pytest.fixture(scope="module")
def bridge():
    return EvidenceBridge()


def _run_evidence(**overrides) -> LearningEvidence:
    """构造一条标准 experiment_run 证据。"""
    kwargs = dict(
        evidence_id="ev_run_1",
        experiment_id="uniform-plane-wave",
        concept_ids=("em-uniform-plane-wave", "em-te-polarization"),
        actions=("完成仿真运行",),
        observations=("λ ≈ 124.9 mm",),
        confidence=0.7,
        timestamp="2026-09-23T10:00:00+00:00",
        kind="experiment_run",
    )
    kwargs.update(overrides)
    return LearningEvidence(**kwargs)


# ---------------------------------------------------------------------------
# 规则 1: experiment_run → 基础学习证据
# ---------------------------------------------------------------------------

def test_experiment_run_becomes_basic_evidence(bridge):
    outcome = bridge.convert(_run_evidence())
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.ok is True
    assert len(outcome.inputs) == 2                      # 每概念一条
    for inp in outcome.inputs:
        assert isinstance(inp, MasteryEvidenceInput)
        assert inp.evidence_type == EVIDENCE_TYPE_BASIC
        assert inp.concept_id in ("em-uniform-plane-wave", "em-te-polarization")
        assert inp.source == "uniform-plane-wave"        # 溯源=实验 id
        assert inp.confidence == pytest.approx(0.7)      # 置信度透传
        assert inp.timestamp == "2026-09-23T10:00:00+00:00"
    assert {i.concept_id for i in outcome.inputs} == {"em-uniform-plane-wave", "em-te-polarization"}
    assert all(i.evidence_id == "ev_run_1" for i in outcome.inputs)  # 回链


def test_direct_run_source_fallback(bridge):
    ev = _run_evidence(experiment_id="", concept_ids=("em-uniform-plane-wave",))
    outcome = bridge.convert(ev)
    assert outcome.ok is True
    assert outcome.inputs[0].source == "direct_run"


# ---------------------------------------------------------------------------
# 规则 2: param_comparison → 强化学习证据
# ---------------------------------------------------------------------------

def test_param_comparison_becomes_reinforced_evidence(bridge):
    ev = LearningEvidence(
        evidence_id="ev_cmp_1",
        experiment_id="uniform-plane-wave",
        concept_ids=("em-uniform-plane-wave",),
        actions=("调整频率 f：2.4 → 5.8",),
        observations=("频率↑ → 波长缩短",),
        confidence=0.9,
        timestamp="2026-09-23T10:01:00+00:00",
        kind="param_comparison",
    )
    outcome = bridge.convert(ev)
    assert outcome.status == STATUS_ACCEPTED
    assert len(outcome.inputs) == 1
    assert outcome.inputs[0].evidence_type == EVIDENCE_TYPE_REINFORCED
    assert outcome.inputs[0].confidence == pytest.approx(0.9)
    assert outcome.inputs[0].source == "uniform-plane-wave"


# ---------------------------------------------------------------------------
# 规则 3: 失败证据拒绝转换
# ---------------------------------------------------------------------------

def test_failed_evidence_rejected(bridge):
    """零置信证据 = 失败/无信号记录（M0.6 保证失败不产生证据,
    本边界做防御性拒绝）。"""
    outcome = bridge.convert(_run_evidence(confidence=0.0))
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "failed_evidence"
    assert outcome.inputs == ()


def test_none_and_unknown_kind_rejected(bridge):
    assert bridge.convert(None).status == STATUS_REJECTED
    assert bridge.convert("not-evidence").status == STATUS_REJECTED

    unknown = _run_evidence(kind="mystery_kind")
    outcome = bridge.convert(unknown)
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "unknown_evidence_type"


# ---------------------------------------------------------------------------
# 规则 4: 空 concept 安全丢弃
# ---------------------------------------------------------------------------

def test_empty_concept_discarded(bridge):
    ev = _run_evidence(concept_ids=())
    outcome = bridge.convert(ev)
    assert outcome.status == STATUS_DISCARDED
    assert outcome.reason == "empty_concept"
    assert outcome.inputs == ()


def test_mixed_concepts_filtered_per_concept(bridge):
    """多概念中混有空串 → 有效概念照常接受, 空串被逐概念丢弃。"""
    ev = _run_evidence(concept_ids=("em-uniform-plane-wave", "", "  ", "em-te-polarization"))
    outcome = bridge.convert(ev)
    assert outcome.status == STATUS_ACCEPTED
    assert len(outcome.inputs) == 2
    assert set(outcome.discarded_concepts) == {"", "  "}


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_mastery_input_json_serialization(bridge):
    outcome = bridge.convert(_run_evidence())
    inp = outcome.inputs[0]

    d = inp.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["concept_id"] == inp.concept_id
    assert parsed["evidence_type"] == "basic"
    assert parsed["source"] == "uniform-plane-wave"
    assert isinstance(parsed["confidence"], float)

    parsed2 = json.loads(inp.to_json())
    assert parsed2["evidence_id"] == "ev_run_1"


def test_outcome_json_serialization(bridge):
    outcome = bridge.convert(_run_evidence(confidence=0.0))
    parsed = json.loads(outcome.to_json())
    assert parsed["status"] == "rejected"
    assert parsed["reason"] == "failed_evidence"
    assert parsed["inputs"] == []


# ---------------------------------------------------------------------------
# 批量转换 + 全链集成
# ---------------------------------------------------------------------------

def test_convert_all_flattens_accepted_only(bridge):
    batch = [
        _run_evidence(),                                    # 2 concepts → basic
        LearningEvidence(
            evidence_id="ev_cmp_2", experiment_id="uniform-plane-wave",
            concept_ids=("em-uniform-plane-wave",), actions=(), observations=(),
            confidence=0.9, timestamp="2026-09-23T10:02:00+00:00",
            kind="param_comparison",
        ),                                                  # 1 concept → reinforced
        _run_evidence(evidence_id="ev_fail", confidence=0.0),   # 拒绝
        _run_evidence(evidence_id="ev_empty", concept_ids=()),  # 丢弃
    ]
    inputs = bridge.convert_all(batch)
    assert len(inputs) == 3
    types = {i.evidence_type for i in inputs}
    assert types == {EVIDENCE_TYPE_BASIC, EVIDENCE_TYPE_REINFORCED}


def test_conversion_deterministic(bridge):
    """同一证据两次转换结果一致（无隐藏随机性）。"""
    ev = _run_evidence()
    o1, o2 = bridge.convert(ev), bridge.convert(ev)
    assert o1.to_dict() == o2.to_dict()


def test_full_chain_runner_builder_bridge(runner, builder, bridge, tmp_path):
    """M0.4 → M0.6 → M0.7 单链贯通：真实仿真 → 证据 → 掌握度输入。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = builder.from_result(result)
    assert ev is not None

    outcome = bridge.convert(ev)
    assert outcome.status == STATUS_ACCEPTED
    concept_ids = {i.concept_id for i in outcome.inputs}
    assert concept_ids == {
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }
    basic = [i for i in outcome.inputs if i.evidence_type == EVIDENCE_TYPE_BASIC]
    assert len(basic) == 3
    assert all(i.source == "uniform-plane-wave" for i in basic)
    assert all(i.evidence_id == ev.evidence_id for i in basic)
    assert all(i.confidence == pytest.approx(EvidenceBuilder.RUN_CONFIDENCE) for i in basic)
