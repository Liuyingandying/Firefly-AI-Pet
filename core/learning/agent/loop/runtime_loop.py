# -*- coding: utf-8 -*-
"""MultiTurnAgentLoop — 多轮工具调用循环（M3.2）。

    while 轮数 < MAX_ROUNDS 且 调用数 < MAX_TOOL_CALLS:
        response = model.generate(messages + tools)
        if 无 tool_calls: → 最终回答, 结束（completed）
        for call in tool_calls:
            白名单检查（拒绝 → 记台账 + 警告, 不执行）
            execute_tool_call（M2.7 三层安全原样）
            台账追加 + role:"tool" 消息回填（模型下一轮可见）
        轮数 +1
    超预算 → 安全停止（max_rounds_reached / max_tool_calls_reached）

安全（需求硬值, 构造器只许收紧）:
- 最大 5 轮 / 最大 10 次工具调用;
- 工具白名单不变（tool_selector + M2.7 registry 双层, 拒绝的提议也计预算,
  防无限拒绝循环）;
- 模型不能直接执行代码/访问文件系统——唯一副作用面 = 白名单工具;
- 失败全部结构化返回, 永不抛异常。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

from core.learning.agent.model.schema import ModelRequest, ToolCall
from core.learning.agent.tool_selector import ToolDeniedError, assert_allowed
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
from core.learning.tools.adapters import execute_tool_call
from core.learning.tools.schema import all_tool_schemas
from core.learning.tools.tool_registry import ToolRegistry

_EMPTY_FALLBACK = (
    "（模型在预算内未给出最终回答）请换个问法，"
    "例如：帮我理解均匀平面波中的TE和TM模式。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MultiTurnAgentLoop:
    """多轮工具调用循环（预算硬顶 + 台账 + 结构化失败）。"""

    def __init__(
        self,
        model,
        *,
        registry: ToolRegistry | None = None,
        max_rounds: int = HARD_MAX_ROUNDS,
        max_tool_calls: int = HARD_MAX_TOOL_CALLS,
    ):
        if not hasattr(model, "generate"):
            raise ValueError("model must implement generate(request) -> ModelResponse")
        self._model = model
        self._registry = registry if registry is not None else _default_registry()
        # 硬顶: 构造器只允许收紧, 不允许放宽
        self._max_rounds = max(1, min(int(max_rounds), HARD_MAX_ROUNDS))
        self._max_tool_calls = max(1, min(int(max_tool_calls), HARD_MAX_TOOL_CALLS))

    def run(
        self,
        user_query: str,
        *,
        context: dict | None = None,
        tools: tuple[dict, ...] | list[dict] | None = None,
    ) -> MultiTurnLoopResult:
        """跑一次多轮循环; 任何失败都以结构化结果返回, 永不抛异常。"""
        query = str(user_query or "").strip()
        if not query:
            return MultiTurnLoopResult(
                success=False, error="empty_query: user_query is empty",
            )

        state = AgentLoopState(
            messages=(
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": query},
            ),
            tool_history=(),
            step_count=0,
            status=LOOP_RUNNING,
        )
        context = context if isinstance(context, dict) else {}
        tool_schemas = tuple(tools) if tools else tuple(all_tool_schemas())

        warnings: list[str] = []
        rounds = 0
        tool_calls_used = 0
        final_response = ""
        status = LOOP_RUNNING

        try:
            while state.step_count < self._max_rounds and tool_calls_used < self._max_tool_calls:
                # ---- 1. 模型生成 ------------------------------------------------
                request = ModelRequest(
                    messages=state.messages,
                    tools=tool_schemas,
                    context=context,
                )
                response = self._model.generate(request)
                rounds += 1
                state = dataclasses.replace(
                    state, step_count=rounds, messages=_append_message(state.messages, {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": [
                            {
                                "id": f"call_r{rounds}_{i}",
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for i, call in enumerate(response.tool_calls)
                        ],
                    }),
                )

                # ---- 2. 无调用 → 最终回答，结束 ----------------------------------
                if not response.tool_calls:
                    status = LOOP_COMPLETED
                    final_response = response.content.strip() or _EMPTY_FALLBACK
                    if not response.content.strip():
                        warnings.append("model returned empty final answer")
                    break

                # ---- 3-5. 逐提议：白名单 → 执行 → 台账 → 回填 ---------------------
                state, warnings, tool_calls_used, stop_status = self._execute_proposals(
                    state,
                    response,
                    rounds,
                    tool_calls_used,
                    warnings,
                    context,
                    tool_schemas,
                )
                if stop_status is not None:
                    status = stop_status
                    break

            # ---- 预算收口 --------------------------------------------------------
            if status == LOOP_RUNNING:
                if state.step_count >= self._max_rounds:
                    status = LOOP_MAX_ROUNDS
                    warnings.append(f"stopped after max rounds ({self._max_rounds})")
                else:
                    status = LOOP_MAX_TOOL_CALLS
                    warnings.append(f"stopped after max tool calls ({self._max_tool_calls})")
                final_response = _EMPTY_FALLBACK
            elif status == LOOP_MAX_TOOL_CALLS:
                # 轮中间触顶（_execute_proposals 内 break）: 补警告与回退文案
                warnings.append(f"stopped after max tool calls ({self._max_tool_calls})")
                final_response = _EMPTY_FALLBACK

            failed_calls = sum(
                1 for r in state.tool_history
                if not r.result.get("success", False)
            )
            if failed_calls:
                warnings.append(f"{failed_calls} tool call(s) failed during the loop")

            return MultiTurnLoopResult(
                success=True,
                response=final_response,
                state=dataclasses.replace(state, status=status),
                rounds_used=state.step_count,
                tool_calls_used=tool_calls_used,
                warnings=tuple(warnings),
            )
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            return MultiTurnLoopResult(
                success=False,
                state=dataclasses.replace(state, status=LOOP_FAILED),
                rounds_used=rounds,
                tool_calls_used=tool_calls_used,
                warnings=tuple(warnings),
                error=f"execution_failed: {type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _execute_proposals(
        self,
        state: AgentLoopState,
        response,
        round_index: int,
        tool_calls_used: int,
        warnings: list[str],
        context: dict,
        tool_schemas: tuple,
    ):
        """执行一轮的全部工具提议；返回 (state, warnings, used, stop_status)。"""
        stop_status = None
        for i, call in enumerate(response.tool_calls):
            # ---- 调用数预算（拒绝的提议同样计入, 防无限拒绝循环） ----------------
            if tool_calls_used >= self._max_tool_calls:
                stop_status = LOOP_MAX_TOOL_CALLS
                break
            tool_calls_used += 1

            call_obj = call if isinstance(call, ToolCall) else ToolCall(
                name=str(getattr(call, "name", "")),
                arguments=dict(getattr(call, "arguments", {}) or {}),
            )

            # ---- 白名单（拒绝 → 台账, 不执行） ---------------------------------
            try:
                assert_allowed(call_obj.name)
            except ToolDeniedError as exc:
                warnings.append(f"tool_denied: {exc}")
                record = ToolExecutionRecord(
                    tool_name=call_obj.name,
                    arguments=dict(call_obj.arguments),
                    result={"success": False, "error": f"tool_denied: {exc}"},
                    timestamp=_now_iso(),
                )
                state = dataclasses.replace(
                    state, tool_history=state.tool_history + (record,),
                )
                continue

            # ---- 执行（M2.7 三层安全原样） --------------------------------------
            entry = {
                "id": f"call_r{round_index}_{i}",
                "type": "function",
                "function": {
                    "name": call_obj.name,
                    "arguments": json.dumps(call_obj.arguments, ensure_ascii=False),
                },
            }
            tool_result = execute_tool_call(entry, registry=self._registry)
            record = ToolExecutionRecord(
                tool_name=call_obj.name,
                arguments=dict(call_obj.arguments),
                result=tool_result.to_dict(),
                timestamp=_now_iso(),
            )
            state = dataclasses.replace(
                state,
                tool_history=state.tool_history + (record,),
                messages=_append_tool_result(state.messages, f"call_r{round_index}_{i}", tool_result),
            )

            # ---- 预算触顶检查（执行后立即收口） -----------------------------------
            if tool_calls_used >= self._max_tool_calls:
                stop_status = LOOP_MAX_TOOL_CALLS
                break

        return state, warnings, tool_calls_used, stop_status


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------

def _append_message(messages: tuple[dict, ...], message: dict) -> tuple[dict, ...]:
    return messages + (dict(message),)


def _append_tool_result(messages: tuple[dict, ...], call_id: str, tool_result) -> tuple[dict, ...]:
    """OpenAI 约定: 工具结果以 role=tool 消息回填（模型下一轮可见）。"""
    return _append_message(messages, {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(tool_result.to_dict(), ensure_ascii=False),
    })


def _system_prompt() -> str:
    return (
        "你是 Firefly Learning Mode 的学习规划助手。"
        "只能通过提供的工具完成任务；每轮可以提议一个或多个工具调用；"
        "获得足够信息后，请不再调用工具并直接给出最终中文回答。"
    )


def _default_registry() -> ToolRegistry:
    from core.learning.tools.adapters import build_default_registry

    return build_default_registry()
