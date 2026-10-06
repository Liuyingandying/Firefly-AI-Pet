# -*- coding: utf-8 -*-
"""Session Planner schema — 学习会话计划协议（M1.5）。

设计约束：
- 输入只读消费 ``core.learning.curriculum_planner``（M1.4）的
  ``LearningPlanProposal``, 提案类型词表直接引用其常量（单一词表源）;
- 步骤词表（steps）是**人工预置的封闭词表**（教学模板, 不由 LLM 生成）;
- 纯内存转换, 不修改课程数据, 不接模型;
- 非法输入安全返回（session_type=none, 不抛异常）;
- 全字段 JSON 原生类型（steps tuple → list 由 to_dict 转换）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 会话类型词表（M1.5 边界专属, 由 M1.4 proposal_type 双射映射）
# ---------------------------------------------------------------------------

SESSION_LESSON = "lesson"          # 新概念教学（含前置讲解）
SESSION_EXPERIMENT = "experiment"  # 仿真实验观察
SESSION_REVIEW = "review"          # 复习巩固
SESSION_NONE = "none"              # 无会话（no_change/非法输入的安全终态）

# ---------------------------------------------------------------------------
# 步骤模板（人工预置封闭词表, 每种会话类型一套）
# ---------------------------------------------------------------------------

STEPS_LESSON: tuple[str, ...] = (
    "introduce_concept",
    "explain_prerequisite",
    "check_understanding",
)
STEPS_EXPERIMENT: tuple[str, ...] = (
    "prepare",
    "simulate",
    "observe",
    "reflect",
)
STEPS_REVIEW: tuple[str, ...] = (
    "recall",
    "practice",
    "evaluate",
)
STEPS_NONE: tuple[str, ...] = ()


@dataclass(frozen=True)
class LearningSessionPlan:
    """一次学习会话的可执行计划（只读 DTO）。

    规约六字段; target_concept/related_experiment/reason/priority 为上游
    提案值的直接继承（零计算）, session_type/steps 由提案类型按封闭模板映射。
    """

    session_type: str
    #: 目标概念（继承提案 target_concept）
    target_concept: str
    #: 关联实验（继承; experiment 会话时为执行入口）
    related_experiment: str
    #: 有序步骤（封闭词表, 见 STEPS_*）
    steps: tuple[str, ...] = ()
    #: 理由（继承提案 reason 原文, 单一解释源不改写）
    reason: str = ""
    #: 优先级（继承提案 priority, 数值越小越优先; none=0）
    priority: int = 0

    def to_dict(self) -> dict:
        return {
            "session_type": self.session_type,
            "target_concept": self.target_concept,
            "related_experiment": self.related_experiment,
            "steps": list(self.steps),
            "reason": self.reason,
            "priority": self.priority,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
