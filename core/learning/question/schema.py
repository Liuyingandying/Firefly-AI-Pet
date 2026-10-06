# -*- coding: utf-8 -*-
"""Question schema — 学习问题协议（M4.1）。

设计约束:
- ``LearningQuestion`` 是**题目提案**（结构化, 可判定）——判定（Judge）
  属下游 M4.2, 本包零评价逻辑;
- ``evaluation_rule`` 是结构化判定规则（第一版: 关键词 any-of 匹配）,
  供 Judge 消费, 不在本包执行;
- 词表封闭（题型/难度）; 确定性生成（无 LLM、无随机、id 内容哈希）;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 题型词表（封闭）
# ---------------------------------------------------------------------------

QUESTION_CLOSED_CHOICE = "closed_choice"
QUESTION_SHORT_ANSWER = "short_answer"
QUESTION_CALCULATION = "calculation"

# ---------------------------------------------------------------------------
# 难度词表（封闭）
# ---------------------------------------------------------------------------

DIFFICULTY_EASY = "easy"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HARD = "hard"

# ---------------------------------------------------------------------------
# 失败码（error 前缀, 结构化失败）
# ---------------------------------------------------------------------------

ERR_INVALID_INPUT = "invalid_input"       # concept_id 非字符串/为空
ERR_UNKNOWN_CONCEPT = "unknown_concept"   # 知识图中不存在


@dataclass(frozen=True)
class EvaluationRule:
    """判定规则（第一版: 关键词 any-of; Judge 侧消费, 本包不执行）。"""

    strategy: str = "keyword_any"           # 封闭词表: 目前仅 keyword_any
    keywords: tuple[str, ...] = ()          # 命中任一即视为答对要点

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "keywords": list(self.keywords)}


@dataclass(frozen=True)
class LearningQuestion:
    """一道学习问题（结构化提案, 确定性生成）。"""

    question_id: str
    concept_id: str
    question_type: str                      # QUESTION_* 词表
    difficulty: str                         # DIFFICULTY_* 词表
    question_text: str
    expected_answer: str
    evaluation_rule: EvaluationRule = field(default_factory=EvaluationRule)

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "concept_id": self.concept_id,
            "question_type": self.question_type,
            "difficulty": self.difficulty,
            "question_text": self.question_text,
            "expected_answer": self.expected_answer,
            "evaluation_rule": self.evaluation_rule.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class QuestionResult:
    """一次生成的结果（成功/结构化失败同型, 永不抛异常）。"""

    success: bool
    questions: tuple[LearningQuestion, ...] = ()
    error: str = ""                         # 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "questions": [q.to_dict() for q in self.questions],
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
