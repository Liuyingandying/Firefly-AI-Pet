# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.3 — Recommendation Engine（KnowledgeGraph + MasterySignal 驱动）。

确定性规则推荐（prerequisite > review > simulation）, 安全返回语义;
不修改上游任何模块, 不接 LLM/UI/数据库。

注意: 本包的 ``LearningRecommendation`` 与规则引擎决策层
（``core.learning.decision.models``）同名类刻意共存——后者是冻结的
UI 下一步动作词表, 本类是仿真驱动的学习推荐, 互不引用互不修改。
"""

from core.learning.recommendation.engine import RecommendationEngine
from core.learning.recommendation.schema import (
    ACTION_NONE,
    ACTION_PREREQUISITE,
    ACTION_REVIEW,
    ACTION_SIMULATION,
    PRIORITY_NONE,
    PRIORITY_PREREQUISITE,
    PRIORITY_REVIEW,
    PRIORITY_SIMULATION,
    LearningRecommendation,
)

__all__ = [
    "RecommendationEngine",
    "LearningRecommendation",
    "ACTION_NONE",
    "ACTION_PREREQUISITE",
    "ACTION_REVIEW",
    "ACTION_SIMULATION",
    "PRIORITY_NONE",
    "PRIORITY_PREREQUISITE",
    "PRIORITY_REVIEW",
    "PRIORITY_SIMULATION",
]
