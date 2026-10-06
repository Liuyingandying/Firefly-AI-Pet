# -*- coding: utf-8 -*-
"""Agent schema — 学习智能体任务/步骤/结果协议（M3.0）。

审计语义（硬约束）:
- ``AgentStep.thought_type`` 是**封闭词表的动作类别**（plan/act/observe/
  explain）, 不是模型思维——本阶段无 LLM, 未来接模型也**永不保存
  chain-of-thought**, 只保存可审计的 action trace;
- 全部 frozen + JSON 原生类型（to_dict/to_json）;
- ``AgentRunResult`` 错误即结果: 任何失败（含工具失败）都以结构化结果
  返回, runtime 永不向调用方抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"

#: AgentStep.thought_type 封闭词表（可审计动作类别, 非模型思维）
THOUGHT_PLAN = "plan"
THOUGHT_ACT = "act"
THOUGHT_OBSERVE = "observe"
THOUGHT_EXPLAIN = "explain"


@dataclass(frozen=True)
class AgentTask:
    """一次学习任务（自然语言请求 + 结构化上下文）。"""

    user_query: str
    goal: str = ""
    context: dict = field(default_factory=dict)
    status: str = TASK_PENDING

    def to_dict(self) -> dict:
        return {
            "user_query": self.user_query,
            "goal": self.goal,
            "context": dict(self.context),
            "status": self.status,
        }


@dataclass(frozen=True)
class AgentStep:
    """一个可审计的执行步骤（动作类别 + 动作 + 工具 + 结果摘要）。"""

    step_index: int
    thought_type: str                    # THOUGHT_* 封闭词表
    action: str                          # 人读动作描述
    tool: str = ""                       # 工具名；explain/规划步为空
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "thought_type": self.thought_type,
            "action": self.action,
            "tool": self.tool,
            "result": dict(self.result),
        }


@dataclass(frozen=True)
class AgentRunResult:
    """一次任务运行的完整结果（成功/失败都是结果, 永不抛异常）。"""

    success: bool
    task: AgentTask | None = None
    steps: tuple[AgentStep, ...] = ()
    response: str = ""                   # markdown 最终回复
    tools_used: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str = ""                      # 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "task": self.task.to_dict() if self.task is not None else None,
            "steps": [s.to_dict() for s in self.steps],
            "response": self.response,
            "tools_used": list(self.tools_used),
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
