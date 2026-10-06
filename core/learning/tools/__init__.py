# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.7 — TJULLM Function Calling 适配层（原型）。

白名单工具注册表 + OpenAI function schema 投影 + 既有 Learning API 薄适配;
安全模型: 白名单即能力边界 / 文件允许根 / 封闭参数词表 / 错误即结果;
**原型**: 不接真实 TJULLM（tools 往返已在调研中实证, 见
docs/tjullm_learning_skill_integration_audit.md）。
"""

from core.learning.tools.adapters import (
    build_default_registry,
    configure_allowed_roots,
    execute_tool_call,
)
from core.learning.tools.schema import (
    ERR_EXECUTION_FAILED,
    ERR_INVALID_ARGUMENTS,
    ERR_INVALID_CALL,
    ERR_INVALID_PARAM,
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    ERR_PATH_DENIED,
    ERR_UNKNOWN_TOOL,
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
    ParsedToolCall,
    ToolResult,
    all_tool_schemas,
    parse_tool_call,
    tool_schema,
)
from core.learning.tools.tool_registry import ToolRegistry

__all__ = [
    "ToolRegistry",
    "build_default_registry",
    "configure_allowed_roots",
    "execute_tool_call",
    "ToolResult",
    "ParsedToolCall",
    "all_tool_schemas",
    "tool_schema",
    "parse_tool_call",
    "TOOL_IMPORT_TEXTBOOK",
    "TOOL_QUERY_CONCEPT",
    "TOOL_RUN_EXPERIMENT",
    "TOOL_GENERATE_LEARNING_PLAN",
    "ERR_UNKNOWN_TOOL",
    "ERR_INVALID_CALL",
    "ERR_INVALID_ARGUMENTS",
    "ERR_MISSING_PARAM",
    "ERR_INVALID_PARAM",
    "ERR_PATH_DENIED",
    "ERR_EXECUTION_FAILED",
    "ERR_NOT_FOUND",
]
