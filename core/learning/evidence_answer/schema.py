# -*- coding: utf-8 -*-
"""Answer evidence schema — 学生回答证据适配协议（M4.3）。

设计约束:
- ``AnswerEvidenceInput`` 是归一化输入快照; ``AnswerEvidenceResult`` 是
  结构化结果（产出/不产出/失败三态可区分）;
- 档位规则（任务规约）: 正确 → answer_correct/0.8; 部分(0.5) →
  answer_partial/0.5; score=0（错误/空）→ **不产 mastery evidence**;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 证据 kind 词表（answer 域封闭）
# ---------------------------------------------------------------------------

KIND_ANSWER_CORRECT = "answer_correct"
KIND_ANSWER_PARTIAL = "answer_partial"

# ---------------------------------------------------------------------------
# 不产证据原因词表（结构化, 可审计）
# ---------------------------------------------------------------------------

REASON_ERROR_ANSWER = "error_answer_no_evidence"      # 错误不能增加掌握度
REASON_EMPTY_ANSWER = "empty_answer_no_evidence"      # 空答案不产证据


@dataclass(frozen=True)
class AnswerEvidenceInput:
    """归一化输入快照（评价 → 证据的原始口径, 审计用）。"""

    concept_id: str
    question_id: str
    evaluation_id: str
    correct: bool
    score: float
    error_type: str
    timestamp: str = ""                      # UTC-ISO（builder 打点）

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "question_id": self.question_id,
            "evaluation_id": self.evaluation_id,
            "correct": self.correct,
            "score": self.score,
            "error_type": self.error_type,
            "timestamp": self.timestamp,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class AnswerEvidenceResult:
    """一次转换的完整结果。

    - ``success=True`` 且 ``evidence`` 非 None → 产出证据;
    - ``success=True`` 且 ``evidence`` 为 None → 不产证据（reason 说明,
      非错误——错误/空答案本来就不该产 mastery evidence）;
    - ``success=False`` → 非法输入等结构化失败。
    """

    success: bool
    evidence: object | None = None           # M0.6 LearningEvidence（对象交付）
    input: AnswerEvidenceInput | None = None
    reason: str = ""                         # 不产证据原因（词表见上）
    error: str = ""                          # 非法输入原因

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "evidence": self.evidence.to_dict() if self.evidence is not None else None,
            "input": self.input.to_dict() if self.input is not None else None,
            "reason": self.reason,
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


__all__ = [
    "AnswerEvidenceInput",
    "AnswerEvidenceResult",
    "KIND_ANSWER_CORRECT",
    "KIND_ANSWER_PARTIAL",
    "REASON_ERROR_ANSWER",
    "REASON_EMPTY_ANSWER",
]
