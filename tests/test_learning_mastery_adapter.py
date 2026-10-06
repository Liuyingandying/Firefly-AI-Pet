# -*- coding: utf-8 -*-
"""M1.0 Mastery Adapter 验收测试。

覆盖：basic 转换 / reinforced 转换 / 非法输入拒绝 / 空 concept 丢弃 /
JSON 序列化 / M0.4–M1 全链贯通。
隔离：真实 runner 仅 2 次运行（全链用例：run_experiment + param_change）,
其余直接构造 MasteryEvidenceInput; 不触 Rule Engine/Review Scheduler/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.evidence_bridge.schema import MasteryEvidenceInput
from core.learning.mastery_adapter import (
    SIGNAL_TYPE_BASIC,
    SIGNAL_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    MasteryAdapter,
    MasterySignal,
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


@pytest.fixture(scope="module")
def adapter():
    return MasteryAdapter()


def _basic_input(**overrides) -> MasteryEvidenceInput:
    kwargs = dict(
        concept_id="em-uniform-plane-wave",
        evidence_type="basic",
        source="uniform-plane-wave",
        confidence=0.7,
        timestamp="2026-09-23T11:00:00+00:00",
        evidence_id="ev_run_1",
    )
    kwargs.update(overrides)
    return MasteryEvidenceInput(**kwargs)


def _corrupt(inp: MasteryEvidenceInput, attr: str, value) -> MasteryEvidenceInput:
    """模拟越界/损坏输入（frozen dataclass 绕过 __post_init__）。"""
    object.__setattr__(inp, attr, value)
    return inp


# ---------------------------------------------------------------------------
# 规则 1: basic → basic signal
# ---------------------------------------------------------------------------

def test_basic_conversion(adapter):
    outcome = adapter.convert(_basic_input())
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.ok is True
    sig = outcome.signal
    assert isinstance(sig, MasterySignal)
    assert sig.signal_type == SIGNAL_TYPE_BASIC
    assert sig.concept_id == "em-uniform-plane-wave"
    assert sig.source == "uniform-plane-wave"
    assert sig.timestamp == "2026-09-23T11:00:00+00:00"
    assert sig.evidence_id == "ev_run_1"


# ---------------------------------------------------------------------------
# 规则 2: reinforced → reinforced signal
# ---------------------------------------------------------------------------

def test_reinforced_conversion(adapter):
    inp = _basic_input(evidence_type="reinforced", confidence=0.9, evidence_id="ev_cmp_1")
    outcome = adapter.convert(inp)
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.signal.signal_type == SIGNAL_TYPE_REINFORCED
    assert outcome.signal.strength == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 规则 5: strength 直接继承 confidence（禁止二次计算）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("confidence", [0.7, 0.9, 0.05, 1.0])
def test_strength_inherits_confidence_unchanged(adapter, confidence):
    outcome = adapter.convert(_basic_input(confidence=confidence))
    assert outcome.signal.strength == pytest.approx(confidence)


# ---------------------------------------------------------------------------
# 规则 3: 非法输入拒绝
# ---------------------------------------------------------------------------

def test_invalid_input_rejected(adapter):
    assert adapter.convert(None).status == STATUS_REJECTED
    assert adapter.convert("not-input").status == STATUS_REJECTED
    assert adapter.convert(123).status == STATUS_REJECTED

    unknown = _basic_input(evidence_type="mystery")
    outcome = adapter.convert(unknown)
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "unknown_evidence_type"

    over = _corrupt(_basic_input(), "confidence", 1.5)
    outcome = adapter.convert(over)
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "invalid_confidence"


# ---------------------------------------------------------------------------
# 规则 4: 空 concept 丢弃
# ---------------------------------------------------------------------------

def test_empty_concept_discarded(adapter):
    # 纯空白 concept 可通过 M0.7 构造（仅查 falsy）→ 适配层防御性丢弃
    outcome = adapter.convert(_basic_input(concept_id="   "))
    assert outcome.status == STATUS_DISCARDED
    assert outcome.reason == "empty_concept"
    assert outcome.signal is None

    # 空 concept 本应被 M0.7 构造拒绝; 损坏注入场景下同样丢弃
    corrupted = _corrupt(_basic_input(), "concept_id", "")
    outcome = adapter.convert(corrupted)
    assert outcome.status == STATUS_DISCARDED
    assert outcome.reason == "empty_concept"


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_signal_json_serialization(adapter):
    sig = adapter.convert(_basic_input()).signal

    d = sig.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["concept_id"] == "em-uniform-plane-wave"
    assert parsed["signal_type"] == "basic"
    assert parsed["strength"] == pytest.approx(0.7)
    assert parsed["evidence_id"] == "ev_run_1"

    parsed2 = json.loads(sig.to_json())
    assert parsed2["source"] == "uniform-plane-wave"


def test_outcome_json_serialization(adapter):
    outcome = adapter.convert(None)
    parsed = json.loads(outcome.to_json())
    assert parsed["status"] == "rejected"
    assert parsed["reason"] == "invalid_input"
    assert parsed["signal"] is None


# ---------------------------------------------------------------------------
# 批量转换 + M0.4–M1 全链贯通
# ---------------------------------------------------------------------------

def test_convert_all_flattens_accepted_only(adapter):
    batch = [
        _basic_input(),                                                   # basic
        _basic_input(evidence_type="reinforced", confidence=0.9,
                     evidence_id="ev_cmp_2"),                             # reinforced
        None,                                                             # 拒绝
        _basic_input(evidence_type="mystery"),                            # 拒绝
        _basic_input(concept_id="  "),                                    # 丢弃
    ]
    signals = adapter.convert_all(batch)
    assert len(signals) == 2
    assert {s.signal_type for s in signals} == {SIGNAL_TYPE_BASIC, SIGNAL_TYPE_REINFORCED}


def test_full_chain_m04_to_m1(runner, builder, bridge, adapter, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 单链贯通（运行证据路径）。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = builder.from_result(result)
    outcome = bridge.convert(ev)
    assert outcome.ok is True

    signals = adapter.convert_all(outcome.inputs)
    assert len(signals) == 3
    assert {s.concept_id for s in signals} == {
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }
    assert all(s.signal_type == SIGNAL_TYPE_BASIC for s in signals)
    assert all(s.strength == pytest.approx(EvidenceBuilder.RUN_CONFIDENCE) for s in signals)
    assert all(s.source == "uniform-plane-wave" for s in signals)
    assert all(s.evidence_id == ev.evidence_id for s in signals)


def test_full_chain_reinforced_path(runner, builder, bridge, adapter):
    """M0.4 → M0.6（比较证据）→ M0.7 → M1.0（强化信号路径）。"""
    ev = builder.from_param_change(
        params_before={"frequency_ghz": 2.4, "medium": "vacuum"},
        params_after={"frequency_ghz": 5.8, "medium": "vacuum"},
        experiment_id="uniform-plane-wave",
    )
    inputs = bridge.convert(ev).inputs
    signals = adapter.convert_all(inputs)

    assert len(signals) == 3
    assert all(s.signal_type == SIGNAL_TYPE_REINFORCED for s in signals)
    assert all(s.strength == pytest.approx(EvidenceBuilder.COMPARISON_CONFIDENCE) for s in signals)
