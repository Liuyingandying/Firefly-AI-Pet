# -*- coding: utf-8 -*-
"""Learning tools schema — TJULLM Function Calling 工具协议（M2.7）。

设计约束:
- **白名单即边界**: 只有注册进 ``ToolRegistry`` 的工具可被调用——任意
  Python 执行/动态代码注入在结构上不可表达;
- 工具 schema = OpenAI function 格式（TJULLM 实证支持面）, 从本仓封闭
  词表机械投影;
- ``ToolResult`` 错误即结果: 成功/失败都是结果, 适配层永不抛异常;
- 全字段 JSON 原生类型（可直接作为 TJULLM tool 消息回填）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 工具名封闭词表（= 能力白名单）
# ---------------------------------------------------------------------------

TOOL_IMPORT_TEXTBOOK = "import_textbook"
TOOL_QUERY_CONCEPT = "query_concept"
TOOL_RUN_EXPERIMENT = "run_experiment"
TOOL_GENERATE_LEARNING_PLAN = "generate_learning_plan"

# ---------------------------------------------------------------------------
# 失败码封闭词表（error 前缀）
# ---------------------------------------------------------------------------

ERR_UNKNOWN_TOOL = "unknown_tool"               # 不在白名单（结构上唯一入口）
ERR_INVALID_CALL = "invalid_tool_call"          # tool call 条目形状非法
ERR_INVALID_ARGUMENTS = "invalid_arguments"     # arguments 非合法 JSON
ERR_MISSING_PARAM = "missing_parameter"
ERR_INVALID_PARAM = "invalid_parameter"
ERR_PATH_DENIED = "file_permission_denied"      # 文件越权（不在允许根内）
ERR_EXECUTION_FAILED = "execution_failed"       # 处理器异常兜底
ERR_NOT_FOUND = "not_found"                     # 资源不存在（概念/实验/文件）


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的统一结果（成功/失败都是结果, 适配层永不抛异常）。"""

    success: bool
    tool: str = ""                       # 扩展字段: 工具名（审计/回填用）
    result: dict = field(default_factory=dict)
    error: str = ""                      # 成功为 ""; 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tool": self.tool,
            "result": dict(self.result),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# 工具 schema（OpenAI function 格式, TJULLM 实证支持面）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    TOOL_IMPORT_TEXTBOOK: {
        "type": "function",
        "function": {
            "name": TOOL_IMPORT_TEXTBOOK,
            "description": (
                "解析 Markdown/纯文本教材文件并生成课程草案与教学化学习节"
                "（Firefly Learning Mode）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "教材文件路径（.md/.txt；必须位于允许目录内）",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "目标课程 ID",
                    },
                },
                "required": ["path", "course_id"],
            },
        },
    },
    TOOL_QUERY_CONCEPT: {
        "type": "function",
        "function": {
            "name": TOOL_QUERY_CONCEPT,
            "description": "查询知识图谱中的一个概念：定义、前置概念、关联实验与学习路径",
            "parameters": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "string", "description": "概念 ID"},
                },
                "required": ["concept_id"],
            },
        },
    },
    TOOL_RUN_EXPERIMENT: {
        "type": "function",
        "function": {
            "name": TOOL_RUN_EXPERIMENT,
            "description": (
                "运行一个注册的仿真实验（如 uniform-plane-wave），"
                "返回 PNG/GIF 产物与参数化结果"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string", "description": "实验 ID（注册表内）"},
                    "params": {
                        "type": "object",
                        "description": "实验参数覆盖（如 frequency/medium/polarization/amplitude）",
                    },
                },
                "required": ["experiment_id"],
            },
        },
    },
    TOOL_GENERATE_LEARNING_PLAN: {
        "type": "function",
        "function": {
            "name": TOOL_GENERATE_LEARNING_PLAN,
            "description": "为目标概念生成学习计划：前置先序的学习路径 + 每步推荐动作",
            "parameters": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "string", "description": "目标概念 ID"},
                },
                "required": ["concept_id"],
            },
        },
    },
}


def all_tool_schemas() -> list[dict]:
    """全部工具 schema（白名单投影, 顺序稳定）——直接可作为 TJULLM tools 载荷。"""
    return [TOOL_SCHEMAS[name] for name in sorted(TOOL_SCHEMAS)]


def tool_schema(name: str) -> dict | None:
    return TOOL_SCHEMAS.get(name)


# ---------------------------------------------------------------------------
# Tool call 解析（TJULLM/OpenAI tool_calls 条目 → 结构化调用）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedToolCall:
    """解析后的工具调用。"""

    call_id: str
    name: str
    arguments: dict = field(default_factory=dict)


def parse_tool_call(entry: dict | None) -> tuple[ParsedToolCall | None, str]:
    """解析一条 OpenAI/TJULLM tool_calls 条目。

    返回 ``(ParsedToolCall | None, error)``：成功 error 为空；
    失败返回 ``(None, "<错误码>: <详情>")``。永不抛异常。
    """
    if not isinstance(entry, dict):
        return None, f"{ERR_INVALID_CALL}: entry is not an object"
    call_id = str(entry.get("id", "") or "")
    function = entry.get("function")
    if entry.get("type") != "function" or not isinstance(function, dict):
        return None, f"{ERR_INVALID_CALL}: entry type must be 'function'"
    name = str(function.get("name", "") or "").strip()
    if not name:
        return None, f"{ERR_INVALID_CALL}: function.name is empty"

    raw_arguments = function.get("arguments", "{}")
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments              # 容错: 有的网关直接给对象
    elif isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return None, f"{ERR_INVALID_ARGUMENTS}: {exc}"
        if not isinstance(parsed, dict):
            return None, f"{ERR_INVALID_ARGUMENTS}: arguments must decode to an object"
        arguments = parsed
    else:
        return None, f"{ERR_INVALID_ARGUMENTS}: arguments must be a JSON string or object"

    return ParsedToolCall(call_id=call_id, name=name, arguments=arguments), ""
