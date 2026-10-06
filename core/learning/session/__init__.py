# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.5 — Session Planner（LearningPlanProposal → LearningSessionPlan）。

纯模板映射会话计划生成器（步骤为人工预置封闭词表）;
纯内存、不修改课程数据、不接模型; 不修改上游任何模块, 不接 LLM/UI。
"""

from core.learning.session.planner import SessionPlanner
from core.learning.session.schema import (
    SESSION_EXPERIMENT,
    SESSION_LESSON,
    SESSION_NONE,
    SESSION_REVIEW,
    STEPS_EXPERIMENT,
    STEPS_LESSON,
    STEPS_NONE,
    STEPS_REVIEW,
    LearningSessionPlan,
)

__all__ = [
    "SessionPlanner",
    "LearningSessionPlan",
    "SESSION_LESSON",
    "SESSION_EXPERIMENT",
    "SESSION_REVIEW",
    "SESSION_NONE",
    "STEPS_LESSON",
    "STEPS_EXPERIMENT",
    "STEPS_REVIEW",
    "STEPS_NONE",
]
