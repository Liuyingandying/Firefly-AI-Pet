# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.3 — Answer Evidence Adapter（LearningEvaluation → LearningEvidence）。

档位映射: 正确→answer_correct/0.8; 部分(0.5)→answer_partial/0.5;
错误/空 → 不产 mastery evidence（None）;
不修改 EvidenceBuilder/EvidenceBridge/Mastery Adapter/Rule Engine/Question/Judge。
"""

from core.learning.evidence_answer.adapter import AnswerEvidenceBuilder
from core.learning.evidence_answer.schema import (
    KIND_ANSWER_CORRECT,
    KIND_ANSWER_PARTIAL,
    REASON_EMPTY_ANSWER,
    REASON_ERROR_ANSWER,
    AnswerEvidenceInput,
    AnswerEvidenceResult,
)

__all__ = [
    "AnswerEvidenceBuilder",
    "AnswerEvidenceInput",
    "AnswerEvidenceResult",
    "KIND_ANSWER_CORRECT",
    "KIND_ANSWER_PARTIAL",
    "REASON_ERROR_ANSWER",
    "REASON_EMPTY_ANSWER",
]
