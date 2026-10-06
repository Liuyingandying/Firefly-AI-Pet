# -*- coding: utf-8 -*-
"""LearningModel 协议 — Agent 模型适配接口（M3.1）。

**仅接口, 零真实 API**：协议与 OpenAI Chat Completions 的
tools/tool_calls 形状同构, 未来接 TJULLM 时实现一个 ``LearningModel``
（内部走 Provider 层 tools 透传——独立演进项, 见 M3.0 审计）即可,
Runtime 与工具层零改动。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.learning.agent.model.schema import ModelRequest, ModelResponse


@runtime_checkable
class LearningModel(Protocol):
    """学习智能体的模型后端协议（结构化鸭子协议）。"""

    name: str

    def generate(self, request: ModelRequest) -> ModelResponse:
        """请求 → 响应。实现方必须：永不抛异常、永不执行工具（只提议调用）。"""
        ...  # pragma: no cover
