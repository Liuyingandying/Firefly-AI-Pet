# -*- coding: utf-8 -*-
"""CurriculumPlanner — LearningRecommendation → LearningPlanProposal（M1.4）。

推荐层 → 课程计划提案层的纯映射转换::

    RecommendationEngine(M1.3)  ──推荐──▶  LearningRecommendation
    CurriculumPlanner(M1.4)     ──提案──▶  LearningPlanProposal
                                  │
                                  └─▶ 课程侧（curriculum 模块, 禁改）未来消费——
                                      接受/落库决策完全在课程侧, 本层永不写数据

映射规则（M1.4 验收口径, 双射 + 安全终态）::

    action=prerequisite → insert_prerequisite   建议课程插入前置概念
    action=simulation   → add_experiment        建议添加实验观察环节
    action=review       → add_review            建议添加复习环节
    action=none         → no_change             不变更课程
    未知 action / 非法输入 → no_change（reason 携带机器可读原因）

纯内存、零 IO、零 LLM、零 UI; target_concept/related_experiment/reason/
priority 全部原值继承, 零计算。
"""

from __future__ import annotations

from core.learning.curriculum_planner.schema import (
    PROPOSAL_ADD_EXPERIMENT,
    PROPOSAL_ADD_REVIEW,
    PROPOSAL_INSERT_PREREQUISITE,
    PROPOSAL_NO_CHANGE,
    LearningPlanProposal,
)
from core.learning.recommendation.schema import (
    ACTION_NONE,
    ACTION_PREREQUISITE,
    ACTION_REVIEW,
    ACTION_SIMULATION,
    LearningRecommendation,
)

#: action → proposal_type 双射映射（封闭词表, 未知 action 安全落到 no_change）
_ACTION_TO_PROPOSAL = {
    ACTION_PREREQUISITE: PROPOSAL_INSERT_PREREQUISITE,
    ACTION_SIMULATION: PROPOSAL_ADD_EXPERIMENT,
    ACTION_REVIEW: PROPOSAL_ADD_REVIEW,
    ACTION_NONE: PROPOSAL_NO_CHANGE,
}


class CurriculumPlanner:
    """学习计划提案生成器（纯映射, 永不修改课程数据）。"""

    def propose(self, recommendation: LearningRecommendation | None) -> LearningPlanProposal:
        """把一条推荐转换为课程提案; 非法输入安全返回 no_change。"""
        if not isinstance(recommendation, LearningRecommendation):
            return LearningPlanProposal(
                proposal_type=PROPOSAL_NO_CHANGE,
                target_concept="",
                reason="invalid_input",
            )

        proposal_type = _ACTION_TO_PROPOSAL.get(recommendation.action)
        if proposal_type is None:
            # 防御：action 词表封闭, 未知值不该出现（构造层不校验）
            return LearningPlanProposal(
                proposal_type=PROPOSAL_NO_CHANGE,
                target_concept=recommendation.target_concept,
                reason=f"unknown_action:{recommendation.action}",
                priority=recommendation.priority,
            )

        return LearningPlanProposal(
            proposal_type=proposal_type,
            target_concept=recommendation.target_concept,
            related_experiment=recommendation.related_experiment,
            reason=recommendation.reason,
            priority=recommendation.priority,
        )

    def propose_all(
        self,
        recommendations: list[LearningRecommendation]
        | tuple[LearningRecommendation, ...],
    ) -> tuple[LearningPlanProposal, ...]:
        """批量转换（逐条安全返回, 永不抛异常）。"""
        return tuple(self.propose(rec) for rec in recommendations or ())
