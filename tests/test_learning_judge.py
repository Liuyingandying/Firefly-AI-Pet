# -*- coding: utf-8 -*-
"""M4.2 Answer Judge 验收测试。

覆盖：完全正确回答 / 关键词正确 / 部分正确 / TE/TM 概念混淆 / 空答案 /
未知输入 / JSON 序列化 / 确定性, 另加公式错误检测与闭环
（QuestionGenerator → AnswerJudge 真实案例）。
隔离：题目走 M4.1 真实生成器 + 手工构造; 零 LLM; 不接 Evidence。
"""

from __future__ import annotations

import json

import pytest

from core.learning.judge import (
    ERROR_CONCEPT_CONFUSION,
    ERROR_MISSING_CONCEPT,
    ERROR_NONE,
    ERROR_UNKNOWN,
    ERROR_WRONG_FORMULA,
    AnswerJudge,
    LearningEvaluation,
)
from core.learning.question import (
    QUESTION_SHORT_ANSWER,
    EvaluationRule,
    LearningQuestion,
    QuestionGenerator,
)


@pytest.fixture(scope="module")
def judge():
    return AnswerJudge()


@pytest.fixture(scope="module")
def wave_question():
    """M4.1 真实生成的 E/H/k 正交题（short_answer, keywords 正交/垂直）。"""
    result = QuestionGenerator().generate("em-uniform-plane-wave")
    return next(q for q in result.questions if q.question_type == QUESTION_SHORT_ANSWER)


def _question(**overrides) -> LearningQuestion:
    kwargs = dict(
        question_id="q-test0001",
        concept_id="em-te-polarization",
        question_type=QUESTION_SHORT_ANSWER,
        difficulty="easy",
        question_text="TE 极化中电场与入射面什么关系？",
        expected_answer="电场垂直于入射面",
        evaluation_rule=EvaluationRule(strategy="keyword_any",
                                       keywords=("垂直", "正交")),
    )
    kwargs.update(overrides)
    return LearningQuestion(**kwargs)


# ---------------------------------------------------------------------------
# 1. 完全正确回答
# ---------------------------------------------------------------------------

def test_exact_expected_answer(judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")
    assert evaluation.correct is True
    assert evaluation.score == 1.0
    assert evaluation.error_type == ERROR_NONE
    assert evaluation.feedback == "回答正确，继续学习下一步"
    assert evaluation.confidence == pytest.approx(0.9)
    assert evaluation.question_id == wave_question.question_id
    assert evaluation.evaluation_id.startswith("ev-")


def test_keyword_synonym_correct(judge, wave_question):
    """同义关键词（垂直）命中 → 同样满分（keyword_any 语义）。"""
    evaluation = judge.evaluate(wave_question, "三个矢量相互垂直")
    assert evaluation.correct is True
    assert evaluation.score == 1.0


def test_expected_contained_correct(judge):
    """expected_answer 包含于回答（无关键词场景）→ 正确。"""
    question = _question(evaluation_rule=EvaluationRule(keywords=()))
    evaluation = judge.evaluate(question, "电场垂直于入射面，这就是 TE 极化的定义")
    assert evaluation.correct is True and evaluation.score == 1.0


# ---------------------------------------------------------------------------
# 2. 关键词正确（构造多点关键词 → 部分/全部分界）
# ---------------------------------------------------------------------------

def test_partial_keyword_match(judge):
    question = _question(
        question_text="E、H、k 三者关系如何？",
        expected_answer="三者相互正交且 k 沿传播方向",
        evaluation_rule=EvaluationRule(strategy="keyword_all",
                                       keywords=("正交", "传播方向")),
    )
    evaluation = judge.evaluate(question, "电场和磁场相互正交")
    assert evaluation.correct is False
    assert evaluation.score == 0.5
    assert evaluation.error_type == ERROR_MISSING_CONCEPT
    assert evaluation.feedback == "核心方向正确，但缺少关键概念"
    assert evaluation.confidence == pytest.approx(0.8)


def test_keyword_all_full_match(judge):
    """keyword_all 多点要点全中 → 满分。"""
    question = _question(
        question_text="E、H、k 三者关系如何？",
        expected_answer="三者相互正交且 k 沿传播方向",
        evaluation_rule=EvaluationRule(strategy="keyword_all",
                                       keywords=("正交", "传播方向")),
    )
    evaluation = judge.evaluate(question, "三者相互正交，且 k 沿传播方向")
    assert evaluation.correct is True and evaluation.score == 1.0


# ---------------------------------------------------------------------------
# 3. TE/TM 概念混淆
# ---------------------------------------------------------------------------

def test_te_tm_confusion(judge):
    """TE 题回答 TM → concept_confusion（别名混淆检测）。"""
    evaluation = judge.evaluate(_question(), "这是 TM 极化的特征")
    assert evaluation.correct is False
    assert evaluation.score == 0.0
    assert evaluation.error_type == ERROR_CONCEPT_CONFUSION
    assert evaluation.feedback == "当前回答混淆了相关概念，需要重新复习"


def test_opposition_marker_confusion(judge, wave_question):
    """对立表述（相同/平行）→ concept_confusion（阶段 3 闭环案例）。"""
    evaluation = judge.evaluate(wave_question, "磁场和传播方向相同")
    assert evaluation.correct is False
    assert evaluation.error_type == ERROR_CONCEPT_CONFUSION
    assert evaluation.score == 0.0


# ---------------------------------------------------------------------------
# 4. 公式样错误
# ---------------------------------------------------------------------------

def test_wrong_formula_detected(judge, wave_question):
    evaluation = judge.evaluate(wave_question, "λ = c * f")
    assert evaluation.correct is False
    assert evaluation.error_type == ERROR_WRONG_FORMULA
    assert evaluation.score == 0.0


# ---------------------------------------------------------------------------
# 5. 空答案
# ---------------------------------------------------------------------------

def test_empty_answer(judge, wave_question):
    for answer in ("", "   "):
        evaluation = judge.evaluate(wave_question, answer)
        assert evaluation.correct is False
        assert evaluation.score == 0.0
        assert evaluation.error_type == ERROR_UNKNOWN
        assert evaluation.confidence == pytest.approx(1.0)
        assert "回答为空" in evaluation.feedback


def test_non_string_answer_structured(judge, wave_question):
    evaluation = judge.evaluate(wave_question, 42)
    assert evaluation.correct is False
    assert evaluation.error.startswith("invalid_input")


# ---------------------------------------------------------------------------
# 6. 未知输入（question 侧）
# ---------------------------------------------------------------------------

def test_invalid_question_structured(judge):
    for bad in (None, "not-question", 42):
        evaluation = judge.evaluate(bad, "任意回答")
        assert evaluation.correct is False
        assert evaluation.error_type == ERROR_UNKNOWN
        assert evaluation.error.startswith("invalid_input")


# ---------------------------------------------------------------------------
# 7. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(judge, wave_question):
    evaluation = judge.evaluate(wave_question, "三者相互正交")
    d = evaluation.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["correct"] is True
    assert parsed["score"] == 1.0
    assert parsed["error_type"] == "none"
    assert parsed["confidence"] == pytest.approx(0.9)

    failed = json.loads(
        judge.evaluate(wave_question, "").to_json()
    )
    assert failed["error_type"] == "unknown"


# ---------------------------------------------------------------------------
# 8. 确定性
# ---------------------------------------------------------------------------

def test_deterministic(judge, wave_question):
    e1 = judge.evaluate(wave_question, "三者相互正交")
    e2 = judge.evaluate(wave_question, "三者相互正交")
    assert e1.to_dict() == e2.to_dict()
    assert e1.evaluation_id == e2.evaluation_id


def test_different_answers_different_ids(judge, wave_question):
    e1 = judge.evaluate(wave_question, "三者相互正交")
    e2 = judge.evaluate(wave_question, "三者相互垂直")
    assert e1.evaluation_id != e2.evaluation_id


# ---------------------------------------------------------------------------
# 阶段 3 闭环：QuestionGenerator → AnswerJudge（真实案例）
# ---------------------------------------------------------------------------

def test_closed_loop_generation_to_judge(judge):
    """均匀平面波：E/H/k 问题 → 正确/混淆两种回答的完整闭环。"""
    generated = QuestionGenerator().generate("em-uniform-plane-wave")
    question = next(
        q for q in generated.questions
        if "E" in q.question_text and "正交" in q.expected_answer
    )

    correct = judge.evaluate(question, "三者互相正交")
    assert correct.correct is True
    assert correct.score == 1.0
    assert correct.error_type == ERROR_NONE

    confused = judge.evaluate(question, "磁场和传播方向相同")
    assert confused.correct is False
    assert confused.error_type == ERROR_CONCEPT_CONFUSION
    assert confused.score == 0.0
