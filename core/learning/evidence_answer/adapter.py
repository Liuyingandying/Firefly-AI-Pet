# -*- coding: utf-8 -*-
"""AnswerEvidenceBuilder — LearningEvaluation → LearningEvidence（M4.3）。

映射规则（任务规约, 确定性）::

    correct=True                        → LearningEvidence(kind=answer_correct,
                                         confidence=0.8, actions=("answer_question",))
    correct=False, score=0.5            → LearningEvidence(kind=answer_partial, confidence=0.5)
    correct=False, score=0.0（错误/空）  → None（错误不能增加掌握度）
    非法输入（类型/概念不一致）           → 结构化失败（AnswerEvidenceResult.success=False）

observations 携带三要素: 问题类型 / 得分 / 反馈（任务规约）。
evidence_id 内容哈希（evaluation_id 派生, 确定性）。纯内存, 零写入。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from core.learning.evidence_answer.schema import (
    KIND_ANSWER_CORRECT,
    KIND_ANSWER_PARTIAL,
    REASON_EMPTY_ANSWER,
    REASON_ERROR_ANSWER,
    AnswerEvidenceInput,
    AnswerEvidenceResult,
)
from core.learning.judge.schema import LearningEvaluation
from core.learning.question.schema import LearningQuestion


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _confidence_for(kind: str) -> float:
    """档位置信度（任务规约硬值: 正确 0.8 / 部分 0.5）。"""
    return 0.8 if kind == KIND_ANSWER_CORRECT else 0.5


class AnswerEvidenceBuilder:
    """学生回答评价 → 学习证据适配器（纯映射, 永不抛异常）。"""

    def from_evaluation(
        self,
        question: LearningQuestion,
        evaluation: LearningEvaluation,
    ) -> "LearningEvidence | None":
        """评价 → 证据; 错误/空答案返回 None（不产生 mastery evidence）。"""
        return self.build(question, evaluation).evidence

    def build(
        self,
        question: LearningQuestion,
        evaluation: LearningEvaluation,
    ) -> AnswerEvidenceResult:
        """结构化入口: 非法输入 → success=False; 不产证据 → success=True + reason。"""
        # ---- 非法输入 ----------------------------------------------------------
        if not isinstance(question, LearningQuestion):
            return AnswerEvidenceResult(
                success=False,
                error="invalid_input: question is not a LearningQuestion",
            )
        if not isinstance(evaluation, LearningEvaluation):
            return AnswerEvidenceResult(
                success=False,
                error="invalid_input: evaluation is not a LearningEvaluation",
            )
        if not evaluation.evaluation_id or evaluation.error:
            return AnswerEvidenceResult(
                success=False,
                error="invalid_input: evaluation is a failed/empty evaluation",
            )
        if question.concept_id != evaluation.concept_id:
            return AnswerEvidenceResult(
                success=False,
                error=(
                    f"invalid_input: concept mismatch "
                    f"(question={question.concept_id!r}, "
                    f"evaluation={evaluation.concept_id!r})"
                ),
            )

        timestamp = _now_iso()
        source_input = AnswerEvidenceInput(
            concept_id=question.concept_id,
            question_id=question.question_id,
            evaluation_id=evaluation.evaluation_id,
            correct=evaluation.correct,
            score=evaluation.score,
            error_type=evaluation.error_type,
            timestamp=timestamp,
        )

        # ---- 错误/空回答: 不产生 mastery evidence（任务规约） -------------------
        if not evaluation.correct and evaluation.score != 0.5:
            reason = (
                REASON_EMPTY_ANSWER
                if evaluation.error_type == "unknown"
                else REASON_ERROR_ANSWER
            )
            return AnswerEvidenceResult(
                success=True,
                evidence=None,
                input=source_input,
                reason=reason,
            )

        kind = KIND_ANSWER_CORRECT if evaluation.correct else KIND_ANSWER_PARTIAL
        confidence = _confidence_for(kind)

        from core.learning.evidence.schema import LearningEvidence

        evidence = LearningEvidence(
            evidence_id="ev-answer-"
                        + hashlib.sha1(evaluation.evaluation_id.encode("utf-8")).hexdigest()[:12],
            experiment_id="",                      # 答案证据属概念域, 非实验域
            concept_ids=(question.concept_id,),
            actions=("answer_question",),
            observations=(
                f"question_type={question.question_type}",
                f"score={evaluation.score}",
                f"feedback={evaluation.feedback}",
            ),
            confidence=confidence,
            timestamp=timestamp,
            kind=kind,
        )
        return AnswerEvidenceResult(
            success=True,
            evidence=evidence,
            input=source_input,
        )
