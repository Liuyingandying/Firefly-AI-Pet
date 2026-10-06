# -*- coding: utf-8 -*-
"""Agent loop schema — 多轮工具调用循环的状态与台账协议（M3.2）。

设计约束:
- 台账完整（每条工具调用有名字/参数/结果/时间戳）, 状态可序列化;
- 状态词表封闭: running / completed / max_rounds_reached /
  max_tool_calls_reached / failed;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 循环状态词表（封闭）
# ---------------------------------------------------------------------------

LOOP_RUNNING = "running"
LOOP_COMPLETED = "completed"                    # 模型给出最终回答
LOOP_MAX_ROUNDS = "max_rounds_reached"          # 轮数预算耗尽（安全停止）
LOOP_MAX_TOOL_CALLS = "max_tool_calls_reached"  # 调用数预算耗尽（安全停止）
LOOP_FAILED = "failed"                          # 结构化失败（门/输入级）

#: 预算硬顶（需求值; 构造器只允许收紧）
HARD_MAX_ROUNDS = 5
HARD_MAX_TOOL_CALLS = 10


@dataclass(frozen=True)
class ToolExecutionRecord:
    """一条工具执行台账（含被白名单拒绝的提议——result 里记录拒绝）。"""

    tool_name: str
    arguments: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)   # ToolResult.to_dict()
    timestamp: str = ""                          # UTC-ISO

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "result": dict(self.result),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class AgentLoopState:
    """多轮循环的运行状态（messages 持久演化, 台账只增）。"""

    messages: tuple[dict, ...] = ()
    tool_history: tuple[ToolExecutionRecord, ...] = ()
    step_count: int = 0                   # 已完成轮数（每轮 model.generate 计 1）
    status: str = LOOP_RUNNING

    def to_dict(self) -> dict:
        return {
            "messages": [dict(m) for m in self.messages],
            "tool_history": [r.to_dict() for r in self.tool_history],
            "step_count": self.step_count,
            "status": self.status,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class MultiTurnLoopResult:
    """一次多轮循环的完整结果（成功/预算停止/失败都是结构化结果）。"""

    success: bool
    response: str = ""                    # 模型最终回答（或回退文案）
    state: AgentLoopState | None = None
    rounds_used: int = 0
    tool_calls_used: int = 0
    warnings: tuple[str, ...] = ()
    error: str = ""                       # 门/输入级失败

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "response": self.response,
            "state": self.state.to_dict() if self.state is not None else None,
            "rounds_used": self.rounds_used,
            "tool_calls_used": self.tool_calls_used,
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
