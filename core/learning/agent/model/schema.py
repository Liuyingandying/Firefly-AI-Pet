# -*- coding: utf-8 -*-
"""Model adapter schema — 学习智能体模型协议形状（M3.1）。

设计约束:
- 与 OpenAI/TJULLM 消息形状同构（messages/tools/finish_reason/tool_calls）,
  但**不绑定任何真实 API**——本阶段唯一实现是 mock;
- ``ToolCall.arguments`` 为 dict（模型侧结构化）; 落到 M2.7 执行层时由
  runtime 序列化为 JSON 字符串条目——白名单/校验/兜底三层安全原样生效;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: 模型请求的系统提示骨架（文档化常量; 真实后端按需实例化）
SYSTEM_PROMPT = (
    "你是 Firefly Learning Mode 的学习规划助手。"
    "只能通过提供的工具完成任务；工具参数必须使用结构化 JSON。"
)


@dataclass(frozen=True)
class ModelRequest:
    """一次模型调用的请求。"""

    messages: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()            # OpenAI function schema（M2.7 投影）
    context: dict = field(default_factory=dict)

    @classmethod
    def from_task(cls, task, tools: tuple[dict, ...] = ()) -> "ModelRequest":
        """从 AgentTask 构造请求（user_query 为用户消息, context 透传）。"""
        return cls(
            messages=(
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": str(getattr(task, "user_query", "") or "")},
            ),
            tools=tuple(tools),
            context=dict(getattr(task, "context", {}) or {}),
        )

    def to_dict(self) -> dict:
        return {
            "messages": [dict(m) for m in self.messages],
            "tools": [dict(t) for t in self.tools],
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class ToolCall:
    """模型请求的工具调用（结构化参数）。"""

    name: str
    arguments: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class ModelResponse:
    """一次模型调用的响应（content 与 tool_calls 至少有一处信息）。"""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: 封闭词表: "tool_calls"（有调用） / "stop"（纯文本结束）
    finish_reason: str = "stop"

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "finish_reason": self.finish_reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
