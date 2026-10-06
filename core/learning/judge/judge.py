# -*- coding: utf-8 -*-
"""AnswerJudge — 学生回答的确定性规则评价（M4.2）。

判定决策树（零 LLM、零随机、永不抛异常）::

    1. 非法输入                     → unknown + error（结构化）
    2. 空答案                       → unknown, score=0
    3. 全命中 / expected ⊆ 回答      → none,          score=1.0
    4. 部分命中（0 < hits < len）    → missing_concept, score=0.5
    5. 混淆检测（别名/对立词）        → concept_confusion, score=0
    6. 公式样回答（含 "="）零命中     → wrong_formula, score=0
    7. 其余零命中                    → missing_concept, score=0

evaluation_id 内容哈希（同题同答 → 同评价, 确定性验收）。
"""

from __future__ import annotations

import dataclasses
import hashlib

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
    LearningEvaluation,
)
from core.learning.question.schema import LearningQuestion

#: 概念别名对（混淆检测; 封闭常量, 小写匹配）
_ALIAS_PAIRS: tuple[tuple[str, str], ...] = (("te", "tm"),)

#: 别名检测要求问题侧出现的线索（避免误伤无关题）
_ALIAS_QUESTION_MARKERS: dict[str, tuple[str, ...]] = {
    "te": ("te",),
    "tm": ("tm",),
}

#: 对立表述词（与正交类关键词相反对立 → 混淆; 封闭常量）
_OPPOSITION_MARKERS: tuple[str, ...] = ("相同", "平行", "一致")


def _normalize(text) -> str:
    return str(text or "").strip().casefold()


def _keywords_of(question: LearningQuestion) -> tuple[str, ...]:
    return tuple(
        kw for kw in (question.evaluation_rule.keywords or ()) if str(kw).strip()
    )


def _question_alias(question: LearningQuestion) -> str | None:
    """问题侧命中的别名（te/tm）；无则 None。"""
    haystack = _normalize(
        f"{question.concept_id} {question.question_text} {question.expected_answer}"
    )
    for alias, markers in _ALIAS_QUESTION_MARKERS.items():
        if any(marker in haystack for marker in markers):
            return alias
    return None


def _has_confusion(question: LearningQuestion, answer: str) -> bool:
    """TE↔TM 别名混淆 或 对立表述检测。"""
    alias = _question_alias(question)
    if alias is not None:
        counterpart = next(
            (other for alias_a, other in _ALIAS_PAIRS if alias_a == alias), None
        )
        if counterpart and counterpart in answer:
            return True
    return any(marker in answer for marker in _OPPOSITION_MARKERS)


class AnswerJudge:
    """学生回答评价器（规则确定性, 永不抛异常）。"""

    def evaluate(self, question, answer) -> LearningEvaluation:
        """LearningQuestion + 学生回答 → LearningEvaluation。"""
        # ---- 1. 非法输入（结构化, 不抛异常） ---------------------------------
        if not isinstance(question, LearningQuestion):
            return self._build(
                question=None, answer="", correct=False, score=0.0,
                error_type=ERROR_UNKNOWN,
                feedback="评价失败：输入不是 LearningQuestion。",
                confidence=CONFIDENCE_EMPTY,
                error="invalid_input: question is not a LearningQuestion",
            )

        question_id = question.question_id
        concept_id = question.concept_id
        keywords = _keywords_of(question)

        if not isinstance(answer, str):
            return self._build(
                question=question, answer="", correct=False, score=0.0,
                error_type=ERROR_UNKNOWN,
                feedback="回答为空，请先复习相关概念后再作答。",
                confidence=CONFIDENCE_EMPTY,
                error="invalid_input: answer is not a string",
            )

        normalized = _normalize(answer)
        if not normalized:
            # ---- 2. 空答案 ---------------------------------------------------
            return self._build(
                question=question, answer="", correct=False, score=0.0,
                error_type=ERROR_UNKNOWN,
                feedback="回答为空，请先复习相关概念后再作答。",
                confidence=CONFIDENCE_EMPTY,
                audit={"empty": True},
            )

        hits = tuple(kw for kw in keywords if _normalize(kw) in normalized)
        expected_hit = _normalize(question.expected_answer) in normalized
        strategy = str(question.evaluation_rule.strategy or "keyword_any")
        audit = {"keywords_total": len(keywords), "keywords_hit": list(hits)}

        # ---- 3. 全命中 -------------------------------------------------------
        # keyword_any（同义词组）: 任一命中即满分; keyword_all（多点要点）: 全中满分
        full_hit = (
            expected_hit
            or (keywords and len(hits) == len(keywords))
            or (strategy == "keyword_any" and hits)
        )
        if full_hit:
            return self._build(
                question=question, answer=normalized, correct=True, score=1.0,
                error_type=ERROR_NONE,
                feedback="回答正确，继续学习下一步",
                confidence=CONFIDENCE_CORRECT,
                audit=audit,
            )

        # ---- 4. 部分命中（仅 keyword_all 多要点语义; any 同义词无部分可言） -----
        if strategy == "keyword_all" and keywords and 0 < len(hits) < len(keywords):
            return self._build(
                question=question, answer=normalized, correct=False, score=0.5,
                error_type=ERROR_MISSING_CONCEPT,
                feedback="核心方向正确，但缺少关键概念",
                confidence=CONFIDENCE_PARTIAL,
                audit=audit,
            )

        # ---- 5. 混淆检测（别名 / 对立表述） ---------------------------------------
        if _has_confusion(question, normalized):
            return self._build(
                question=question, answer=normalized, correct=False, score=0.0,
                error_type=ERROR_CONCEPT_CONFUSION,
                feedback="当前回答混淆了相关概念，需要重新复习",
                confidence=CONFIDENCE_CLASSIFIED,
                audit={**audit, "confusion": True},
            )

        # ---- 6. 公式样回答 ---------------------------------------------------------
        if "=" in normalized:
            return self._build(
                question=question, answer=normalized, correct=False, score=0.0,
                error_type=ERROR_WRONG_FORMULA,
                feedback="回答为公式形式但未命中关键要点，请核对公式后再作答。",
                confidence=CONFIDENCE_CLASSIFIED,
                audit={**audit, "formula": True},
            )

        # ---- 7. 其余零命中 -----------------------------------------------------------
        return self._build(
            question=question, answer=normalized, correct=False, score=0.0,
            error_type=ERROR_MISSING_CONCEPT,
            feedback=(
                f"回答未命中关键概念，建议重新复习"
                f"「{concept_id}」相关内容后再次作答。"
            ),
            confidence=CONFIDENCE_CLASSIFIED,
            audit=audit,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _build(
        *,
        question: LearningQuestion | None,
        answer: str,
        correct: bool,
        score: float,
        error_type: str,
        feedback: str,
        confidence: float,
        error: str = "",
        audit: dict | None = None,
    ) -> LearningEvaluation:
        question_id = question.question_id if question is not None else ""
        concept_id = question.concept_id if question is not None else ""
        digest = hashlib.sha1(
            f"{question_id}|{answer}".encode("utf-8")
        ).hexdigest()[:12]
        _ = audit  # 审计摘要当前随调用方 trace 记录; 保留形参供调试扩展
        return LearningEvaluation(
            evaluation_id=f"ev-{digest}",
            question_id=question_id,
            concept_id=concept_id,
            correct=correct,
            score=score,
            error_type=error_type,
            feedback=feedback,
            confidence=confidence,
            error=error,
        )
