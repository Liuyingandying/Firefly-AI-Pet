# -*- coding: utf-8 -*-
"""Firefly Learning Mode M3.2 — Multi-turn Tool Calling Loop（多轮循环）。

Model → ToolCall → ToolResult → Model → … → Final Answer;
预算硬顶（5 轮 / 10 调用, 只许收紧）、白名单不变、台账完整、结构化失败;
不接 TJULLM、不修改 ToolRegistry/已有 Learning 模块/ProviderRouter。
"""

from core.learning.agent.loop.runtime_loop import MultiTurnAgentLoop
from core.learning.agent.loop.schema import (
    HARD_MAX_ROUNDS,
    HARD_MAX_TOOL_CALLS,
    LOOP_COMPLETED,
    LOOP_FAILED,
    LOOP_MAX_ROUNDS,
    LOOP_MAX_TOOL_CALLS,
    LOOP_RUNNING,
    AgentLoopState,
    MultiTurnLoopResult,
    ToolExecutionRecord,
)

__all__ = [
    "MultiTurnAgentLoop",
    "AgentLoopState",
    "ToolExecutionRecord",
    "MultiTurnLoopResult",
    "HARD_MAX_ROUNDS",
    "HARD_MAX_TOOL_CALLS",
    "LOOP_RUNNING",
    "LOOP_COMPLETED",
    "LOOP_MAX_ROUNDS",
    "LOOP_MAX_TOOL_CALLS",
    "LOOP_FAILED",
]
