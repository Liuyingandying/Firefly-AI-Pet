# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.2 — Answer Judge（学生回答确定性规则评价）。

规则判定（keyword_any 全/部分命中 + 混淆/公式检测表）, 零 LLM、零随机、
永不抛异常; 不修改旧 AssessmentService/QuizGrading/Rule Engine/Agent Runtime。
"""

from core.learning.judge.judge import AnswerJudge
from core.learning.judge.schema import (
    CONFIDENCE_CLASSIFIED,
    CONFIDENCE_CORRECT,
    CONFIDENCE_EMPTY,
    CONFIDENCE_PARTIAL,
    ERROR_CONCEPT_CONFUSION,
    ERROR_MISSING_CONCEPT,
    ERROR_NONE,
    ERROR_UNKNOWN,
    ERROR_WRONG_FORMULA,
    JudgeAudit,
    LearningEvaluation,
)

__all__ = [
    "AnswerJudge",
    "LearningEvaluation",
    "JudgeAudit",
    "ERROR_NONE",
    "ERROR_MISSING_CONCEPT",
    "ERROR_CONCEPT_CONFUSION",
    "ERROR_WRONG_FORMULA",
    "ERROR_UNKNOWN",
    "CONFIDENCE_CORRECT",
    "CONFIDENCE_PARTIAL",
    "CONFIDENCE_CLASSIFIED",
    "CONFIDENCE_EMPTY",
]
