# -*- coding: utf-8 -*-
"""Recommendation schema — 学习推荐协议（M1.3）。

设计约束：
- 输入只读消费 ``core.learning.mastery_adapter``（M1.0）的 ``MasterySignal``
  与 ``core.learning.knowledge_graph``（M1.2）的 ``KnowledgeGraph``;
- **与规则引擎决策层的 ``LearningRecommendation``（core.learning.decision.models,
  冻结的 UI 下一步动作词表）刻意共存、互不修改**：本类是仿真驱动的学习推荐
  （字段 action/target_concept/reason/related_experiment/priority）, 词表独立;
- 未知概念/非法输入**安全返回**（action="none", 不抛异常）;
- 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 推荐动作词表（M1.3 边界专属）
# ---------------------------------------------------------------------------

#: 无推荐（未知概念 / 非法输入 / 无可推荐项）——安全终态
ACTION_NONE = "none"
#: 推荐先修前置概念（target_concept 指向前置）
ACTION_PREREQUISITE = "prerequisite"
#: 复习当前概念（低强度信号）
ACTION_REVIEW = "review"
#: 前往关联实验做仿真观察
ACTION_SIMULATION = "simulation"

# priority 约定：数值越小优先级越高；0 = 无优先级（action=none）
PRIORITY_NONE = 0
PRIORITY_PREREQUISITE = 1
PRIORITY_REVIEW = 2
PRIORITY_SIMULATION = 3


@dataclass(frozen=True)
class LearningRecommendation:
    """一条学习推荐（KnowledgeGraph + MasterySignal 驱动）。

    action 词表: none / prerequisite / review / simulation
    priority:    prerequisite=1 > review=2 > simulation=3；none=0
    """

    action: str
    #: 推荐目标概念（prerequisite 动作时为前置概念 id；none 时回显原概念或空）
    target_concept: str
    #: 人类可读的推荐理由
    reason: str
    #: 关联实验 id（simulation 动作时填充目标概念的第一个实验）
    related_experiment: str = ""
    priority: int = PRIORITY_NONE

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "target_concept": self.target_concept,
            "reason": self.reason,
            "related_experiment": self.related_experiment,
            "priority": self.priority,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
