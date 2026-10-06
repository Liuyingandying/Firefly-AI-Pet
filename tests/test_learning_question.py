# -*- coding: utf-8 -*-
"""M4.1 Question Generator 验收测试。

覆盖：正常生成 / 三个概念均有问题 / 未知 concept / JSON 序列化 /
确定性（两次生成完全一致）/ 非法输入, 另加多题型与批量生成。
隔离：种子知识图只读; 零 LLM、零随机; 不修改任何既有模块。
"""

from __future__ import annotations

import json

import pytest

from core.learning.question import (
    DIFFICULTY_EASY,
    DIFFICULTY_MEDIUM,
    ERR_INVALID_INPUT,
    ERR_UNKNOWN_CONCEPT,
    QUESTION_CALCULATION,
    QUESTION_CLOSED_CHOICE,
    QUESTION_SHORT_ANSWER,
    EvaluationRule,
    LearningQuestion,
    QuestionGenerator,
    QuestionResult,
)


@pytest.fixture(scope="module")
def generator():
    return QuestionGenerator()


# ---------------------------------------------------------------------------
# 1. 正常生成
# ---------------------------------------------------------------------------

def test_normal_generation(generator):
    result = generator.generate("em-uniform-plane-wave")
    assert isinstance(result, QuestionResult)
    assert result.success is True
    assert result.error == ""
    assert len(result.questions) >= 1

    first = result.questions[0]
    assert isinstance(first, LearningQuestion)
    assert first.question_id.startswith("q-")
    assert first.concept_id == "em-uniform-plane-wave"
    assert first.question_type == QUESTION_SHORT_ANSWER
    assert first.difficulty == DIFFICULTY_EASY
    assert "E" in first.question_text and "正交" in first.expected_answer
    assert "正交" in first.evaluation_rule.keywords
    assert first.evaluation_rule.strategy == "keyword_any"


# ---------------------------------------------------------------------------
# 2. 三个概念均有问题（至少一题/概念, 多题型覆盖）
# ---------------------------------------------------------------------------

def test_all_three_seed_concepts_have_questions(generator):
    concept_ids = ("em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization")
    results = generator.generate_all(concept_ids)
    assert all(r.success for r in results)
    assert all(len(r.questions) >= 1 for r in results)

    all_types = {
        q.question_type for r in results for q in r.questions
    }
    # 多题型覆盖: short_answer 必有, closed_choice / calculation 至少其一
    assert QUESTION_SHORT_ANSWER in all_types
    assert all_types & {QUESTION_CLOSED_CHOICE, QUESTION_CALCULATION}


def test_te_tm_concept_content(generator):
    te = generator.generate("em-te-polarization").questions
    assert any("垂直于入射面" in q.expected_answer for q in te)
    tm = generator.generate("em-tm-polarization").questions
    assert any("垂直于入射面" in q.expected_answer for q in tm)


def test_calculation_question_present(generator):
    result = generator.generate("em-uniform-plane-wave")
    calc = [q for q in result.questions if q.question_type == QUESTION_CALCULATION]
    assert calc and calc[0].difficulty == DIFFICULTY_MEDIUM
    assert "124.9" in calc[0].expected_answer


# ---------------------------------------------------------------------------
# 3. 未知 concept（结构化失败, 禁止异常）
# ---------------------------------------------------------------------------

def test_unknown_concept_structured_failure(generator):
    result = generator.generate("ghost-concept")
    assert result.success is False
    assert result.questions == ()
    assert result.error.startswith(ERR_UNKNOWN_CONCEPT)
    assert "ghost-concept" in result.error


def test_unknown_in_batch_does_not_break_others(generator):
    results = generator.generate_all(
        ("em-uniform-plane-wave", "ghost-concept", "em-te-polarization")
    )
    assert [r.success for r in results] == [True, False, True]
    assert results[1].error.startswith(ERR_UNKNOWN_CONCEPT)


def test_graph_concept_without_bank_gets_fallback(tmp_path):
    """图内存在但未入库的概念 → 通用回退题（简述核心要点）。"""
    from core.learning.knowledge_graph import ConceptNode, KnowledgeGraph

    tiny_graph = KnowledgeGraph([
        ConceptNode(concept_id="tiny-c", name="微小概念", chapter="测试章"),
    ])
    result = QuestionGenerator(graph=tiny_graph).generate("tiny-c")
    assert result.success is True
    question = result.questions[0]
    assert "微小概念" in question.question_text
    assert question.evaluation_rule.keywords == ("微小概念",)


# ---------------------------------------------------------------------------
# 4. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(generator):
    result = generator.generate("em-uniform-plane-wave")
    question = result.questions[0]
    d = question.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["concept_id"] == "em-uniform-plane-wave"
    assert parsed["evaluation_rule"]["strategy"] == "keyword_any"
    assert "正交" in parsed["evaluation_rule"]["keywords"]

    result_json = json.loads(result.to_json())
    assert result_json["success"] is True


# ---------------------------------------------------------------------------
# 5. 确定性（两次生成完全一致）
# ---------------------------------------------------------------------------

def test_deterministic_generation(generator):
    r1 = generator.generate("em-uniform-plane-wave")
    r2 = generator.generate("em-uniform-plane-wave")
    assert r1.to_dict() == r2.to_dict()
    assert [q.question_id for q in r1.questions] == [q.question_id for q in r2.questions]


# ---------------------------------------------------------------------------
# 6. 非法输入
# ---------------------------------------------------------------------------

def test_invalid_input_structured_failure(generator):
    for bad in (None, "", "   ", 42, ["list"]):
        result = generator.generate(bad)
        assert result.success is False
        assert result.error.startswith(ERR_INVALID_INPUT)
        assert result.questions == ()


def test_generate_all_invalid_input(generator):
    assert generator.generate_all(None) == ()                # None → 空结果
    results = generator.generate_all("em-uniform-plane-wave")   # 单字符串容错为单元素
    assert len(results) == 1 and results[0].success is True
