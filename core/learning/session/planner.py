# -*- coding: utf-8 -*-
"""SessionPlanner — LearningPlanProposal → LearningSessionPlan（M1.5）。

提案层 → 会话计划层的纯模板映射::

    CurriculumPlanner(M1.4)  ──提案──▶  LearningPlanProposal
    SessionPlanner(M1.5)     ──计划──▶  LearningSessionPlan
                               │
                               └─▶ 会话执行器/教学运行时（未来消费, 本层不实现）

映射规则（M1.5 验收口径, 双射 + 安全终态）::

    insert_prerequisite → lesson      steps = introduce_concept → explain_prerequisite
                                                  → check_understanding
    add_experiment      → experiment  steps = prepare → simulate → observe → reflect
    add_review          → review      steps = recall → practice → evaluate
    no_change           → none        steps = ()
    未知 proposal_type / 非法输入 → none（reason 携带机器可读原因）

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

#: proposal_type → (session_type, steps) 封闭映射（未知提案安全落到 none）
_PROPOSAL_TO_SESSION = {
    PROPOSAL_INSERT_PREREQUISITE: (SESSION_LESSON, STEPS_LESSON),
    PROPOSAL_ADD_EXPERIMENT: (SESSION_EXPERIMENT, STEPS_EXPERIMENT),
    PROPOSAL_ADD_REVIEW: (SESSION_REVIEW, STEPS_REVIEW),
    PROPOSAL_NO_CHANGE: (SESSION_NONE, STEPS_NONE),
}


class SessionPlanner:
    """学习会话计划生成器（纯模板映射, 零数值计算）。"""

    def plan(self, proposal: LearningPlanProposal | None) -> LearningSessionPlan:
        """把一条课程提案转换为会话计划; 非法输入安全返回 none。"""
        if not isinstance(proposal, LearningPlanProposal):
            return LearningSessionPlan(
                session_type=SESSION_NONE,
                target_concept="",
                related_experiment="",
                reason="invalid_input",
            )

        mapped = _PROPOSAL_TO_SESSION.get(proposal.proposal_type)
        if mapped is None:
            # 防御：proposal_type 词表封闭, 未知值不该出现（构造层不校验）
            return LearningSessionPlan(
                session_type=SESSION_NONE,
                target_concept=proposal.target_concept,
                related_experiment=proposal.related_experiment,
                reason=f"unknown_proposal:{proposal.proposal_type}",
                priority=proposal.priority,
            )

        session_type, steps = mapped
        return LearningSessionPlan(
            session_type=session_type,
            target_concept=proposal.target_concept,
            related_experiment=proposal.related_experiment,
            steps=steps,
            reason=proposal.reason,
            priority=proposal.priority,
        )

    def plan_all(
        self,
        proposals: list[LearningPlanProposal] | tuple[LearningPlanProposal, ...],
    ) -> tuple[LearningSessionPlan, ...]:
        """批量转换（逐条安全返回, 永不抛异常）。"""
        return tuple(self.plan(p) for p in proposals or ())
