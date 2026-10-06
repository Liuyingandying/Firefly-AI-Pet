# -*- coding: utf-8 -*-
"""M4.3 Answer Evidence Adapter 验收测试。

覆盖：正确回答产生 Evidence / 部分正确产生弱 Evidence / 错误回答无
Evidence / 空答案无 Evidence / concept_id 一致 / JSON 序列化 / 确定性,
另加阶段 3 全链（QuestionGenerator → AnswerJudge → AnswerEvidenceBuilder
→ EvidenceBridge → MasteryAdapter）。
隔离：题目/评价走 M4.1/M4.2 真实链; 不修改任何上游模块。
"""

from __future__ import annotations

import json

import pytest

from core.learning.evidence_answer import (
    KIND_ANSWER_CORRECT,
    KIND_ANSWER_PARTIAL,
    REASON_EMPTY_ANSWER,
    REASON_ERROR_ANSWER,
    AnswerEvidenceBuilder,
)
from core.learning.evidence import EvidenceBuilder
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.judge import AnswerJudge
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.question import QuestionGenerator
from core.learning.question.schema import EvaluationRule, LearningQuestion


@pytest.fixture(scope="module")
def builder():
    return AnswerEvidenceBuilder()


@pytest.fixture(scope="module")
def judge():
    return AnswerJudge()


@pytest.fixture(scope="module")
def wave_question():
    result = QuestionGenerator().generate("em-uniform-plane-wave")
    return next(
        q for q in result.questions
        if "E" in q.question_text and "正交" in q.expected_answer
    )


# ---------------------------------------------------------------------------
# 1. 正确回答产生 Evidence
# ---------------------------------------------------------------------------

def test_correct_answer_produces_evidence(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")
    result = builder.build(wave_question, evaluation)

    assert result.success is True
    assert result.evidence is not None
    evidence = result.evidence
    assert evidence.kind == KIND_ANSWER_CORRECT
    assert evidence.confidence == pytest.approx(0.8)          # 任务规约硬值
    assert evidence.actions == ("answer_question",)           # 任务规约动作
    assert evidence.concept_ids == ("em-uniform-plane-wave",) # concept_id 一致
    assert evidence.experiment_id == ""                       # 概念域非实验域
    # observations 三要素: 问题类型 / 得分 / 反馈
    observations = "\n".join(evidence.observations)
    assert "question_type=short_answer" in observations
    assert "score=1.0" in observations
    assert "回答正确" in observations
    assert evidence.evidence_id.startswith("ev-answer-")
    assert evidence.evidence_id in result.to_dict()["evidence"]["evidence_id"]


def test_from_evaluation_convenience(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")
    evidence = builder.from_evaluation(wave_question, evaluation)
    assert evidence is not None
    assert evidence.kind == KIND_ANSWER_CORRECT


# ---------------------------------------------------------------------------
# 2. 部分正确产生弱 Evidence
# ---------------------------------------------------------------------------

def test_partial_answer_produces_weak_evidence(builder, judge):
    question = LearningQuestion(
        question_id="q-test0001",
        concept_id="em-uniform-plane-wave",
        question_type="short_answer",
        difficulty="easy",
        question_text="E、H、k 三者关系如何？",
        expected_answer="三者相互正交且 k 沿传播方向",
        evaluation_rule=EvaluationRule(strategy="keyword_all",
                                       keywords=("正交", "传播方向")),
    )
    evaluation = judge.evaluate(question, "电场和磁场相互正交")   # keyword_all 部分命中
    assert evaluation.score == 0.5

    result = builder.build(question, evaluation)
    assert result.success is True
    assert result.evidence.kind == KIND_ANSWER_PARTIAL
    assert result.evidence.confidence == pytest.approx(0.5)   # 弱证据
    assert result.evidence.concept_ids == ("em-uniform-plane-wave",)


# ---------------------------------------------------------------------------
# 3/4. 错误回答 / 空答案无 Evidence
# ---------------------------------------------------------------------------

def test_wrong_answer_no_evidence(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "磁场和传播方向相同")   # 混淆, score=0
    result = builder.build(wave_question, evaluation)
    assert result.success is True                             # 处理成功, 但不产证据
    assert result.evidence is None                            # 错误不能增加掌握度
    assert result.reason == REASON_ERROR_ANSWER


def test_empty_answer_no_evidence(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "")
    result = builder.build(wave_question, evaluation)
    assert result.success is True
    assert result.evidence is None
    assert result.reason == REASON_EMPTY_ANSWER
    assert builder.from_evaluation(wave_question, evaluation) is None


# ---------------------------------------------------------------------------
# 非法输入（结构化失败）
# ---------------------------------------------------------------------------

def test_invalid_inputs_structured_failure(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")

    for question, evaluation_ in (
        (None, evaluation),
        (wave_question, None),
        ("not-question", evaluation),
        (wave_question, "not-evaluation"),
    ):
        result = builder.build(question, evaluation_)
        assert result.success is False
        assert result.evidence is None
        assert result.error.startswith("invalid_input")


def test_concept_mismatch_structured_failure(builder, judge):
    other = LearningQuestion(
        question_id="q-other0001",
        concept_id="em-te-polarization",                      # 与评价的概念不一致
        question_type="short_answer",
        difficulty="easy",
        question_text="TE 极化中电场与入射面什么关系？",
        expected_answer="电场垂直于入射面",
        evaluation_rule=EvaluationRule(keywords=("垂直",)),
    )
    from core.learning.judge import LearningEvaluation as _LE

    evaluation = _LE(
        evaluation_id="ev-x", question_id="q-other0001",
        concept_id="em-uniform-plane-wave",                   # 概念不一致
        correct=True, score=1.0, error_type="none",
        feedback="f", confidence=0.9,
    )
    result = builder.build(other, evaluation)
    assert result.success is False
    assert "concept mismatch" in result.error


# ---------------------------------------------------------------------------
# 5. concept_id 一致（全链贯通）
# ---------------------------------------------------------------------------

def test_concept_id_consistency_across_chain(builder, judge):
    generated = QuestionGenerator().generate("em-te-polarization")
    question = generated.questions[0]
    evaluation = judge.evaluate(question, "电场垂直于入射面")
    result = builder.build(question, evaluation)

    assert result.evidence.concept_ids == (question.concept_id,)
    assert evaluation.concept_id == question.concept_id
    assert result.input.concept_id == question.concept_id


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(builder, judge, wave_question):
    result = builder.build(
        wave_question, judge.evaluate(wave_question, "三者相互正交")
    )
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["evidence"]["kind"] == "answer_correct"
    assert parsed["evidence"]["confidence"] == pytest.approx(0.8)
    assert parsed["input"]["concept_id"] == "em-uniform-plane-wave"

    no_evidence = builder.build(
        wave_question, judge.evaluate(wave_question, "")
    )
    parsed_none = json.loads(no_evidence.to_json())
    assert parsed_none["evidence"] is None
    assert parsed_none["reason"] == REASON_EMPTY_ANSWER


# ---------------------------------------------------------------------------
# 7. 确定性
# ---------------------------------------------------------------------------

def test_deterministic(builder, judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")
    r1 = builder.build(wave_question, evaluation)
    r2 = builder.build(wave_question, evaluation)
    assert r1.evidence.evidence_id == r2.evidence.evidence_id
    assert r1.evidence.to_dict() == r2.evidence.to_dict()


# ---------------------------------------------------------------------------
# 阶段 3: 真实闭环（Question → Judge → Evidence → Bridge → MasteryAdapter）
# ---------------------------------------------------------------------------

def test_full_chain_question_to_mastery_signal(builder, judge, wave_question):
    """全链: Question → Judge → AnswerEvidence → Bridge → MasterySignal。

    已知集成边界（如实断言）: M0.7 Bridge 词表封闭（experiment_run/
    param_comparison）, answer_* kind 会被 unknown_evidence_type 拒绝——
    本测试以**显式兼容重类型化**（answer 证据 = 一次学习事件 →
    experiment_run）走通真实 Bridge/Adapter, 并单独断言该边界。
    M4.4+ 应在 Bridge 词表追加 answer 映射后移除兼容层。
    """
    import dataclasses

    from core.learning.evidence.schema import KIND_RUN
    from core.learning.evidence_bridge import EvidenceBridge
    from core.learning.mastery_adapter import MasteryAdapter

    evaluation = judge.evaluate(wave_question, "三者相互正交")
    evidence = builder.from_evaluation(wave_question, evaluation)
    assert evidence is not None

    # 边界断言: answer_* kind 被既有 Bridge 正确拒绝（防幻构机制工作）
    direct = EvidenceBridge().convert(evidence)
    assert direct.ok is False                                # rejected
    assert direct.reason == "unknown_evidence_type"

    # 兼容重类型化（不修改 Bridge, 调用方显式降维）→ 走通真实 Bridge/Adapter
    bridge_compatible = dataclasses.replace(evidence, kind=KIND_RUN)
    inputs = EvidenceBridge().convert(bridge_compatible).inputs
    assert len(inputs) == 1
    signals = MasteryAdapter().convert_all(inputs)
    assert len(signals) == 1

    signal = signals[0]
    assert signal.concept_id == "em-uniform-plane-wave"
    assert signal.signal_type == "basic"                      # normal_learning_event
    assert signal.strength == pytest.approx(0.8)              # evidence confidence 恒等透传
    assert signal.evidence_id == evidence.evidence_id
