# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.4 — Curriculum Planner（Recommendation → Plan Proposal）。

纯映射提案生成器（永不修改课程数据, 只产出只读 DTO）;
不修改 curriculum/Rule Engine/Recommendation/Knowledge Graph, 不接 LLM/UI。
"""

from core.learning.curriculum_planner.planner import CurriculumPlanner
from core.learning.curriculum_planner.schema import (
    PROPOSAL_ADD_EXPERIMENT,
    PROPOSAL_ADD_REVIEW,
    PROPOSAL_INSERT_PREREQUISITE,
    PROPOSAL_NO_CHANGE,
    LearningPlanProposal,
)

__all__ = [
    "CurriculumPlanner",
    "LearningPlanProposal",
    "PROPOSAL_INSERT_PREREQUISITE",
    "PROPOSAL_ADD_EXPERIMENT",
    "PROPOSAL_ADD_REVIEW",
    "PROPOSAL_NO_CHANGE",
]
