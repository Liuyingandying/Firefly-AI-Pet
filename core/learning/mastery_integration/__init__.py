# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.1 — Mastery Integration（MasterySignal → RuleEvaluationInput）。

只读消费 mastery_adapter（M1.0）, 产出规则引擎输入事件;
strength 原值保持（禁止重新计算）;
不修改 Rule Engine / Review Scheduler / 课程存储, 不接 LLM/UI/数据库。
"""

from core.learning.mastery_integration.adapter import RuleEngineAdapter
from core.learning.mastery_integration.schema import (
    EVENT_TYPE_NORMAL,
    EVENT_TYPE_STRONG,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    RuleEvaluationInput,
)

__all__ = [
    "RuleEngineAdapter",
    "RuleEvaluationInput",
    "AdapterOutcome",
    "EVENT_TYPE_NORMAL",
    "EVENT_TYPE_STRONG",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "STATUS_DISCARDED",
]
