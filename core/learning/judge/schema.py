# -*- coding: utf-8 -*-
"""Judge schema — 学生回答评价协议（M4.2）。

设计约束:
- ``LearningEvaluation`` 是评价结果快照（规约八字段 + error 扩展）;
- ``error_type`` 封闭词表: none / missing_concept / concept_confusion /
  wrong_formula / unknown;
- 确定性判定: evaluation_id 内容哈希（同题同答 → 同评价）;
- 全部 frozen + JSON 原生类型; 判定逻辑在 judge.py, 本模块纯形状。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# error_type 词表（封闭）
# ---------------------------------------------------------------------------

ERROR_NONE = "none"                        # 回答正确
ERROR_MISSING_CONCEPT = "missing_concept"  # 缺少关键概念（部分命中/零命中）
ERROR_CONCEPT_CONFUSION = "concept_confusion"  # 概念混淆（TE↔TM/对立表述）
ERROR_WRONG_FORMULA = "wrong_formula"      # 公式样回答但未命中
ERROR_UNKNOWN = "unknown"                  # 空答案/非法输入

# ---------------------------------------------------------------------------
# 判定置信度常数（规则版标定, 对齐 M0.6 先例）
# ---------------------------------------------------------------------------

CONFIDENCE_CORRECT = 0.9
CONFIDENCE_PARTIAL = 0.8
CONFIDENCE_CLASSIFIED = 0.7
CONFIDENCE_EMPTY = 1.0                     # "确定为空" 无歧义


@dataclass(frozen=True)
class LearningEvaluation:
    """一次学生回答的结构化评价。"""

    evaluation_id: str
    question_id: str
    concept_id: str
    correct: bool
    score: float                             # 0.0 / 0.5 / 1.0（规则版三档）
    error_type: str                          # ERROR_* 词表
    feedback: str
    confidence: float                        # 判定置信度（规则版常数标定）
    #: 扩展字段: 门/输入级失败原因（正常判定为空）
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "question_id": self.question_id,
            "concept_id": self.concept_id,
            "correct": self.correct,
            "score": self.score,
            "error_type": self.error_type,
            "feedback": self.feedback,
            "confidence": self.confidence,
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class JudgeAudit:
    """判定过程审计（命中的关键词/检测器触发, 供测试与调试）。"""

    keywords_total: int = 0
    keywords_hit: tuple[str, ...] = ()
    detectors: tuple[str, ...] = ()          # 触发的检测器名（confusion/formula…）
    normalized_answer: str = ""

    def to_dict(self) -> dict:
        return {
            "keywords_total": self.keywords_total,
            "keywords_hit": list(self.keywords_hit),
            "detectors": list(self.detectors),
            "normalized_answer": self.normalized_answer,
        }


__all__ = [
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
