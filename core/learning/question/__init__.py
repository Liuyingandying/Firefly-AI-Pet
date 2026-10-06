# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.1 — Question Generator（ConceptNode → LearningQuestion）。

确定性模板出题（零 LLM、零随机）; 结构化失败（未知概念/非法输入不抛异常）;
不修改 Rule Engine/AssessmentService/QuizGrading/Session Planner/Agent Runtime。
"""

from core.learning.question.generator import QuestionGenerator
from core.learning.question.schema import (
    DIFFICULTY_EASY,
    DIFFICULTY_HARD,
    DIFFICULTY_MEDIUM,
    ERR_INVALID_INPUT,
    ERR_UNKNOWN_CONCEPT,
    QUESTION_CALCULATION,
    QUESTION_CLOSED_CHOICE,
    QUESTION_SHORT_ANSWER,
    EvaluationRule,
    LearningQuestion,
    QuestionResult,
)

__all__ = [
    "QuestionGenerator",
    "LearningQuestion",
    "QuestionResult",
    "EvaluationRule",
    "QUESTION_CLOSED_CHOICE",
    "QUESTION_SHORT_ANSWER",
    "QUESTION_CALCULATION",
    "DIFFICULTY_EASY",
    "DIFFICULTY_MEDIUM",
    "DIFFICULTY_HARD",
    "ERR_INVALID_INPUT",
    "ERR_UNKNOWN_CONCEPT",
]
