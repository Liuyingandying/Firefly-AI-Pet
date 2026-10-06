# -*- coding: utf-8 -*-
"""Firefly Learning Mode M3.1 — Agent Model Adapter（模型适配接口 + mock 后端）。

仅接口：LearningModel 协议 + 请求/响应/工具调用形状 + 固定规则 mock;
不接真实 API、不修改 ProviderRouter/ToolRegistry/Runtime 核心;
工具执行仍全部经 M2.7 白名单三层安全。
"""

from core.learning.agent.model.base import LearningModel
from core.learning.agent.model.mock import MockLearningModel
from core.learning.agent.model.schema import (
    SYSTEM_PROMPT,
    ModelRequest,
    ModelResponse,
    ToolCall,
)

__all__ = [
    "LearningModel",
    "MockLearningModel",
    "ModelRequest",
    "ModelResponse",
    "ToolCall",
    "SYSTEM_PROMPT",
]
