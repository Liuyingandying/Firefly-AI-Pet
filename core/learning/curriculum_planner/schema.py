# -*- coding: utf-8 -*-
"""Curriculum Planner schema — 学习计划提案协议（M1.4）。

设计约束：
- 输入只读消费 ``core.learning.recommendation``（M1.3）的
  ``LearningRecommendation``, 动作词表直接引用其常量（单一词表源）;
- **只生成提案, 永不修改课程数据**：本模块纯内存、无任何 store/IO 调用,
  ``LearningPlanProposal`` 是课程侧（curriculum 模块, 禁改）未来消费的
  输入 DTO, 提案的接受/落库决策完全在课程侧;
- 非法输入安全返回（no_change, 不抛异常）;
- 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 提案类型词表（M1.4 边界专属, 由 M1.3 action 双射映射）
# ---------------------------------------------------------------------------

#: 前置概念缺失 → 建议在课程中插入前置概念
PROPOSAL_INSERT_PREREQUISITE = "insert_prerequisite"
#: 掌握良好 → 建议为概念添加实验观察环节
PROPOSAL_ADD_EXPERIMENT = "add_experiment"
#: 掌握不足 → 建议为概念添加复习环节
PROPOSAL_ADD_REVIEW = "add_review"
#: 无推荐/非法输入 → 不变更课程
PROPOSAL_NO_CHANGE = "no_change"


@dataclass(frozen=True)
class LearningPlanProposal:
    """一条课程计划提案（只读 DTO, 课程侧消费）。

    规约五字段全部为上游推荐值的直接继承（零计算）——本层只做
    action → proposal_type 的词表映射与合法性校验。
    """

    proposal_type: str
    #: 目标概念（继承推荐 target_concept）
    target_concept: str
    #: 关联实验（继承, insert_prerequisite/add_experiment 时有意义）
    related_experiment: str = ""
    #: 理由（继承推荐 reason, 单一解释源, 不改写）
    reason: str = ""
    #: 优先级（继承推荐 priority, 数值越小越优先; no_change=0）
    priority: int = 0

    def to_dict(self) -> dict:
        return {
            "proposal_type": self.proposal_type,
            "target_concept": self.target_concept,
            "related_experiment": self.related_experiment,
            "reason": self.reason,
            "priority": self.priority,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
