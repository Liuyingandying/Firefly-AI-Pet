# -*- coding: utf-8 -*-
"""M1.1 Rule Engine Adapter 验收测试。

覆盖：basic→normal 事件 / reinforced→strong 事件 / 非法信号拒绝 /
空 concept 丢弃 / JSON 序列化 / M0.4–M1.1 全链贯通。
隔离：真实 runner 仅 2 次运行（全链用例）,
其余直接构造 MasterySignal; 不触 Rule Engine/Review Scheduler/课程存储/DB/UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.mastery_adapter.schema import MasterySignal
from core.learning.mastery_integration import (
    EVENT_TYPE_NORMAL,
    EVENT_TYPE_STRONG,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    RuleEngineAdapter,
    RuleEvaluationInput,
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
def signal_adapter():
    return MasteryAdapter()


@pytest.fixture(scope="module")
def adapter():
    return RuleEngineAdapter()


def _basic_signal(**overrides) -> MasterySignal:
    kwargs = dict(
        concept_id="em-uniform-plane-wave",
        signal_type="basic",
        source="uniform-plane-wave",
        strength=0.7,
        timestamp="2026-09-23T12:00:00+00:00",
        evidence_id="ev_run_1",
    )
    kwargs.update(overrides)
    return MasterySignal(**kwargs)


def _corrupt(sig: MasterySignal, attr: str, value) -> MasterySignal:
    """模拟越界/损坏输入（frozen dataclass 绕过构造校验）。"""
    object.__setattr__(sig, attr, value)
    return sig


# ---------------------------------------------------------------------------
# 规则 1: basic → normal_learning_event
# ---------------------------------------------------------------------------

def test_basic_conversion(adapter):
    outcome = adapter.convert(_basic_signal())
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.ok is True
    ev = outcome.event
    assert isinstance(ev, RuleEvaluationInput)
    assert ev.signal_type == EVENT_TYPE_NORMAL
    assert ev.concept_id == "em-uniform-plane-wave"
    assert ev.evidence_source == "uniform-plane-wave"       # source 改名透传
    assert ev.timestamp == "2026-09-23T12:00:00+00:00"
    assert ev.evidence_id == "ev_run_1"


# ---------------------------------------------------------------------------
# 规则 2: reinforced → strong_learning_event
# ---------------------------------------------------------------------------

def test_reinforced_conversion(adapter):
    sig = _basic_signal(signal_type="reinforced", strength=0.9, evidence_id="ev_cmp_1")
    outcome = adapter.convert(sig)
    assert outcome.status == STATUS_ACCEPTED
    assert outcome.event.signal_type == EVENT_TYPE_STRONG
    assert outcome.event.strength == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 规则 3: strength 原值保持（禁止重新计算）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("strength", [0.7, 0.9, 0.05, 1.0])
def test_strength_kept_unchanged(adapter, strength):
    outcome = adapter.convert(_basic_signal(strength=strength))
    assert outcome.event.strength == pytest.approx(strength)


# ---------------------------------------------------------------------------
# 规则 4: 非法信号拒绝
# ---------------------------------------------------------------------------

def test_invalid_signal_rejected(adapter):
    assert adapter.convert(None).status == STATUS_REJECTED
    assert adapter.convert("not-signal").status == STATUS_REJECTED
    assert adapter.convert(123).status == STATUS_REJECTED

    unknown = _basic_signal(signal_type="mystery")
    outcome = adapter.convert(unknown)
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "unknown_signal_type"

    over = _corrupt(_basic_signal(), "strength", 1.5)
    outcome = adapter.convert(over)
    assert outcome.status == STATUS_REJECTED
    assert outcome.reason == "invalid_strength"


# ---------------------------------------------------------------------------
# 规则 5: 空 concept 丢弃
# ---------------------------------------------------------------------------

def test_empty_concept_discarded(adapter):
    # 纯空白 concept 可通过 M1.0 构造（仅查 falsy）→ 适配层防御性丢弃
    outcome = adapter.convert(_basic_signal(concept_id="   "))
    assert outcome.status == STATUS_DISCARDED
    assert outcome.reason == "empty_concept"
    assert outcome.event is None

    # 空 concept 本应被 M1.0 构造拒绝; 损坏注入场景下同样丢弃
    corrupted = _corrupt(_basic_signal(), "concept_id", "")
    outcome = adapter.convert(corrupted)
    assert outcome.status == STATUS_DISCARDED
    assert outcome.reason == "empty_concept"


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_event_json_serialization(adapter):
    ev = adapter.convert(_basic_signal()).event

    d = ev.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["concept_id"] == "em-uniform-plane-wave"
    assert parsed["signal_type"] == "normal_learning_event"
    assert parsed["evidence_source"] == "uniform-plane-wave"
    assert parsed["strength"] == pytest.approx(0.7)
    assert parsed["evidence_id"] == "ev_run_1"

    parsed2 = json.loads(ev.to_json())
    assert parsed2["timestamp"] == "2026-09-23T12:00:00+00:00"


def test_outcome_json_serialization(adapter):
    outcome = adapter.convert(None)
    parsed = json.loads(outcome.to_json())
    assert parsed["status"] == "rejected"
    assert parsed["reason"] == "invalid_signal"
    assert parsed["event"] is None


# ---------------------------------------------------------------------------
# 批量转换 + M0.4–M1.1 全链贯通
# ---------------------------------------------------------------------------

def test_convert_all_flattens_accepted_only(adapter):
    batch = [
        _basic_signal(),                                                   # normal
        _basic_signal(signal_type="reinforced", strength=0.9,
                      evidence_id="ev_cmp_2"),                             # strong
        None,                                                              # 拒绝
        _basic_signal(signal_type="mystery"),                              # 拒绝
        _basic_signal(concept_id="  "),                                    # 丢弃
    ]
    events = adapter.convert_all(batch)
    assert len(events) == 2
    assert {e.signal_type for e in events} == {EVENT_TYPE_NORMAL, EVENT_TYPE_STRONG}


def test_full_chain_m04_to_m11(runner, builder, bridge, signal_adapter, adapter, tmp_path):
    """M0.4 → M0.6 → M0.7 → M1.0 → M1.1 单链贯通（运行证据路径）。"""
    result = runner.run_experiment("uniform-plane-wave", out_dir=tmp_path)
    ev = builder.from_result(result)
    inputs = bridge.convert(ev).inputs
    signals = signal_adapter.convert_all(inputs)
    events = adapter.convert_all(signals)

    assert len(events) == 3
    assert {e.concept_id for e in events} == {
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }
    assert all(e.signal_type == EVENT_TYPE_NORMAL for e in events)
    assert all(e.strength == pytest.approx(EvidenceBuilder.RUN_CONFIDENCE) for e in events)
    assert all(e.evidence_source == "uniform-plane-wave" for e in events)
    assert all(e.evidence_id == ev.evidence_id for e in events)


def test_full_chain_reinforced_path(builder, bridge, signal_adapter, adapter):
    """比较证据路径 → strong_learning_event。"""
    ev = builder.from_param_change(
        params_before={"frequency_ghz": 2.4, "medium": "vacuum"},
        params_after={"frequency_ghz": 5.8, "medium": "vacuum"},
        experiment_id="uniform-plane-wave",
    )
    inputs = bridge.convert(ev).inputs
    signals = signal_adapter.convert_all(inputs)
    events = adapter.convert_all(signals)

    assert len(events) == 3
    assert all(e.signal_type == EVENT_TYPE_STRONG for e in events)
    assert all(e.strength == pytest.approx(EvidenceBuilder.COMPARISON_CONFIDENCE) for e in events)
