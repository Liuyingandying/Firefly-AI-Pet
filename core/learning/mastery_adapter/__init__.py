# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.0 — Mastery Adapter（MasteryEvidenceInput → MasterySignal）。

只读消费 evidence_bridge（M0.7）, 产出掌握度聚合输入信号;
strength 直接继承 confidence（禁止二次计算）;
不修改 Rule Engine / Review Scheduler, 不接 LLM/UI/数据库。
"""

from core.learning.mastery_adapter.adapter import MasteryAdapter
from core.learning.mastery_adapter.schema import (
    SIGNAL_TYPE_BASIC,
    SIGNAL_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    MasterySignal,
)

__all__ = [
    "MasteryAdapter",
    "MasterySignal",
    "AdapterOutcome",
    "SIGNAL_TYPE_BASIC",
    "SIGNAL_TYPE_REINFORCED",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "STATUS_DISCARDED",
]
