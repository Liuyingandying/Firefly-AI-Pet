# -*- coding: utf-8 -*-
"""M0.6 Evidence Adapter 验收测试。

覆盖：success 实验生成证据 / 参数变化生成比较证据 /
failed·timeout 不生成错误掌握记录（builder 返回 None）/ JSON 序列化。
隔离：真实 runner 仅 3 次运行（run_experiment / 直连 run / param_error）,
其余直接构造 SimulationResult; 不触数据库/UI/LLM。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from core.learning.evidence import EvidenceBuilder, LearningEvidence
from core.learning.simulation.result import SimulationStatus
from core.learning.simulation.runner import SimulationParams, SimulationResult, SimulationRunner


@pytest.fixture(scope="module")
def runner():
    return SimulationRunner(timeout_s=20)


@pytest.fixture(scope="module")
def builder():
    return EvidenceBuilder()


# ---------------------------------------------------------------------------
# 1. success 实验生成证据
# ---------------------------------------------------------------------------

def test_success_experiment_creates_evidence(runner, builder, tmp_path):
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    assert result.ok is True

    ev = builder.from_result(result)
    assert isinstance(ev, LearningEvidence)
    assert ev.evidence_id                                   # 非空 id
    assert ev.experiment_id == "uniform-plane-wave"
    # concept_ids 来自注册表只读查找（yaml 声明的 3 个概念）
    assert set(ev.concept_ids) == {
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }
    assert ev.actions and ev.observations
    assert any("2.4" in str(a) for a in ev.actions)          # 参数回读进动作
    assert any("λ" in str(o) for o in ev.observations)       # 物理量进观察
    assert ev.confidence == pytest.approx(EvidenceBuilder.RUN_CONFIDENCE)
    assert ev.kind == "experiment_run"
    # timestamp 为可解析的 UTC-ISO 字符串
    ts = datetime.fromisoformat(ev.timestamp)
    assert ts.tzinfo is not None


def test_direct_run_falls_back_to_default_concepts(runner, builder, tmp_path):
    """直连 run() 无 experiment_id → 使用构造缺省概念。"""
    result = runner.run(SimulationParams(), out_dir=tmp_path)
    ev = builder.from_result(result)
    assert ev is not None
    assert ev.experiment_id == ""
    assert ev.concept_ids == ("em-uniform-plane-wave",)


def test_explicit_concept_ids_override(builder):
    result = SimulationResult(ok=True, status="completed", experiment_id="exp-x")
    ev = builder.from_result(result, concept_ids=("custom-concept",))
    assert ev.concept_ids == ("custom-concept",)


def test_evidence_ids_unique(builder):
    r1 = SimulationResult(ok=True, status="completed", experiment_id="exp-x")
    r2 = SimulationResult(ok=True, status="completed", experiment_id="exp-x")
    ev1, ev2 = builder.from_result(r1), builder.from_result(r2)
    assert ev1.evidence_id != ev2.evidence_id


# ---------------------------------------------------------------------------
# 2. 参数变化生成比较证据
# ---------------------------------------------------------------------------

def test_param_change_creates_comparison_evidence(builder):
    before = {"frequency_ghz": 2.4, "medium": "vacuum", "polarization_deg": 0.0, "amplitude": 0.8}
    after = {"frequency_ghz": 5.8, "medium": "vacuum", "polarization_deg": 0.0, "amplitude": 0.8}

    ev = builder.from_param_change(params_before=before, params_after=after,
                                   experiment_id="uniform-plane-wave")
    assert isinstance(ev, LearningEvidence)
    assert ev.kind == "param_comparison"
    assert ev.experiment_id == "uniform-plane-wave"
    assert ev.concept_ids == ("em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization")
    # 动作记录变化本身, 观察记录量化规则（复用 M0.5 解释层）
    assert any("2.4" in a and "5.8" in a for a in ev.actions)
    assert any("缩短" in o for o in ev.observations)
    assert ev.confidence == pytest.approx(EvidenceBuilder.COMPARISON_CONFIDENCE)
    assert ev.confidence > EvidenceBuilder.RUN_CONFIDENCE    # 受控差异证据更强


def test_param_change_schema_keys_accepted(builder):
    before = {"frequency": 2.4, "medium": "vacuum", "polarization": 0.0, "amplitude": 0.8}
    after = {"frequency": 2.4, "medium": "glass", "polarization": 0.0, "amplitude": 0.8}
    ev = builder.from_param_change(params_before=before, params_after=after)
    assert ev is not None
    assert any("glass" in a for a in ev.actions)


def test_param_change_no_diff_returns_none(builder):
    same = {"frequency": 2.4, "medium": "vacuum", "polarization": 0.0, "amplitude": 0.8}
    assert builder.from_param_change(params_before=same, params_after=dict(same)) is None


def test_param_change_empty_params_returns_none(builder):
    assert builder.from_param_change(params_before={}, params_after={"frequency": 2.4}) is None
    assert builder.from_param_change(params_before=None, params_after=None) is None


# ---------------------------------------------------------------------------
# 3. failed/timeout 不生成错误掌握记录
# ---------------------------------------------------------------------------

def test_param_error_run_produces_no_evidence(runner, builder, tmp_path):
    result = runner.run(SimulationParams(frequency=999), out_dir=tmp_path)
    assert result.ok is False and result.status == "param_error"
    assert builder.from_result(result) is None               # 核心规则


def test_failed_statuses_produce_no_evidence(builder):
    for status in (
        SimulationStatus.TIMEOUT.value,
        SimulationStatus.EXECUTION_FAILED.value,
        SimulationStatus.SECURITY_REJECTED.value,
    ):
        result = SimulationResult(ok=False, status=status, reason="x")
        assert builder.from_result(result) is None, f"status={status} 不应生成证据"


def test_none_and_mismatched_status_produce_no_evidence(builder):
    assert builder.from_result(None) is None
    # ok=True 但 status 非 completed → 拒绝（防御性口径）
    weird = SimulationResult(ok=True, status="timeout")
    assert builder.from_result(weird) is None


# ---------------------------------------------------------------------------
# 4. JSON 序列化
# ---------------------------------------------------------------------------

def test_evidence_json_serialization(runner, builder, tmp_path):
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = builder.from_result(result)

    d = ev.to_dict()
    assert isinstance(d, dict)
    text = json.dumps(d, ensure_ascii=False)                 # tuple/datetime 泄漏会在此抛错
    parsed = json.loads(text)
    assert parsed["evidence_id"] == ev.evidence_id
    assert parsed["concept_ids"] == list(ev.concept_ids)     # tuple → list
    assert isinstance(parsed["confidence"], float)
    assert 0.0 <= parsed["confidence"] <= 1.0
    datetime.fromisoformat(parsed["timestamp"])              # ISO 可解析

    parsed2 = json.loads(ev.to_json())
    assert parsed2["kind"] == "experiment_run"


def test_comparison_evidence_json_roundtrip(builder):
    ev = builder.from_param_change(
        params_before={"frequency": 2.4, "medium": "vacuum"},
        params_after={"frequency": 5.8, "medium": "vacuum"},
    )
    parsed = json.loads(ev.to_json())
    assert parsed["kind"] == "param_comparison"
    assert parsed["actions"] and parsed["observations"]


# ---------------------------------------------------------------------------
# 5. schema 校验
# ---------------------------------------------------------------------------

def test_confidence_bounds_enforced():
    with pytest.raises(ValueError, match="confidence"):
        LearningEvidence(evidence_id="e1", confidence=1.5)
    with pytest.raises(ValueError, match="evidence_id"):
        LearningEvidence(evidence_id="")
