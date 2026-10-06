# -*- coding: utf-8 -*-
"""SkillDecisionValidator — TJULLM 决策输出校验（M4.6）。

校验项（全部拒绝为结构化结果, 不抛异常）:
1. payload 必须是含 ``action`` 的对象        → invalid_input
2. action ∈ LearningAction 白名单           → unknown_action
3. target_concept 必填（cold_start 除外）
   且 ∈ available_concepts（提供词表时）     → unknown_concept
4. confidence 缺省 0.5; 数值 0–1            → invalid_confidence

安全语义: validator 是模型输出的**唯一信任边界**——词表外动作/编造概念
/越界置信度全部在此拒绝, runtime 回退安全决策（probe/cold_start）。
"""

from __future__ import annotations

from core.learning.skill.schema import (
    ACTION_VALUES,
    DecisionCheck,
    LearningAction,
    SkillDecision,
)


class SkillDecisionValidator:
    """模型决策校验器（白名单边界, 永不抛异常）。"""

    def __init__(self, *, default_confidence: float = 0.5):
        self._default_confidence = float(default_confidence)

    def validate(
        self,
        payload,
        *,
        available_concepts: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
    ) -> DecisionCheck:
        """校验一条模型决策 JSON; 非法返回 ok=False + 机器可读 error。"""
        if not isinstance(payload, dict):
            return DecisionCheck(ok=False, error="invalid_input: payload must be an object")
        if "action" not in payload:
            return DecisionCheck(ok=False, error="invalid_input: action is required")

        action = str(payload.get("action", "") or "").strip()
        if action not in ACTION_VALUES:
            return DecisionCheck(ok=False, error=f"unknown_action: {action!r}")

        target = str(payload.get("target_concept", "") or "").strip()
        requires_target = action != LearningAction.COLD_START_PROBE.value
        if requires_target and not target:
            return DecisionCheck(
                ok=False, error=f"unknown_concept: action {action!r} requires target_concept",
            )
        if requires_target and available_concepts is not None:
            available = {str(c) for c in available_concepts}
            if target not in available:
                return DecisionCheck(
                    ok=False,
                    error=f"unknown_concept: {target!r} is not in the available concepts",
                )

        raw_confidence = payload.get("confidence", self._default_confidence)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            return DecisionCheck(ok=False, error="invalid_confidence: not a number")
        if not (0.0 <= confidence <= 1.0):
            return DecisionCheck(
                ok=False, error=f"invalid_confidence: {confidence} out of [0, 1]",
            )

        metadata = payload.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        reason = str(payload.get("reason", "") or "")

        return DecisionCheck(
            ok=True,
            decision=SkillDecision(
                action=action,
                target_concept=target,
                reason=reason,
                confidence=round(confidence, 4),
                metadata=metadata,
            ),
        )
