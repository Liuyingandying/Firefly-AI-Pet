# -*- coding: utf-8 -*-
"""Firefly Learning Mode M3.0 — TJULLM 端到端学习智能体原型。

规则规划（无 LLM）+ 静态白名单工具（M2.7）+ 解释层组装（M0.5）；
可审计 action trace（无 CoT）；结构化错误（永不异常退出）;
零修改 M0.4–M2.7 任何模块; 本地 Runtime, 不接真实 TJULLM API。
"""

from core.learning.agent.planner import PlannedStep, plan
from core.learning.agent.runtime import LearningAgentRuntime
from core.learning.agent.schema import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    THOUGHT_ACT,
    THOUGHT_EXPLAIN,
    THOUGHT_OBSERVE,
    THOUGHT_PLAN,
    AgentRunResult,
    AgentStep,
    AgentTask,
)
from core.learning.agent.tool_selector import (
    ALLOWED_TOOLS,
    ToolDeniedError,
    assert_allowed,
    select_allowed_tools,
)

__all__ = [
    "LearningAgentRuntime",
    "AgentTask",
    "AgentStep",
    "AgentRunResult",
    "PlannedStep",
    "plan",
    "ALLOWED_TOOLS",
    "select_allowed_tools",
    "assert_allowed",
    "ToolDeniedError",
    "TASK_PENDING",
    "TASK_RUNNING",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "THOUGHT_PLAN",
    "THOUGHT_ACT",
    "THOUGHT_OBSERVE",
    "THOUGHT_EXPLAIN",
]
