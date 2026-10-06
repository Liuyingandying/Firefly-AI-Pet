# -*- coding: utf-8 -*-
"""ToolRegistry — 工具白名单注册表 + 参数校验执行器（M2.7）。

安全模型（本层是唯一调用入口）:
- **白名单即能力边界**: ``execute(name, …)`` 只接受注册过的工具名——
  任意 Python 执行 / 动态代码注入在结构上不可表达;
- 参数按 schema 校验（required 存在 + JSON 类型匹配）后才调用处理器;
- 处理器异常兜底为 ``execution_failed`` 结果——适配层永不抛异常;
- 处理器本身承担资源级安全（如文件允许根）, 见 adapters。

本模块不 import 任何 learning 处理器（保持通用层）; 默认注册表在
``adapters.build_default_registry()`` 组装。
"""

from __future__ import annotations

from typing import Callable

from core.learning.tools.schema import (
    ERR_EXECUTION_FAILED,
    ERR_INVALID_PARAM,
    ERR_MISSING_PARAM,
    ERR_UNKNOWN_TOOL,
    TOOL_SCHEMAS,
    ToolResult,
)

#: JSON schema type → python 类型谓词
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


class ToolRegistry:
    """白名单工具注册表（schema + 处理器成对注册）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    def register(self, tool_name: str, handler: Callable[..., ToolResult]) -> None:
        """注册一个工具（名字必须在 TOOL_SCHEMAS 词表内——schema 与白名单同源）。"""
        if tool_name not in TOOL_SCHEMAS:
            raise ValueError(f"unknown tool name: {tool_name!r}（词表外工具不可注册）")
        self._handlers[tool_name] = handler

    def registered_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def execute(self, name: str, arguments: dict | None = None) -> ToolResult:
        """执行一个白名单工具; 任何失败都以 ToolResult 返回, 永不抛异常。"""
        tool_name = str(name or "")
        # ---- 白名单: 词表外的名字在结构上不可调用 ---------------------------
        if tool_name not in TOOL_SCHEMAS:
            return self._fail(tool_name, ERR_UNKNOWN_TOOL, f"{tool_name!r} is not a registered tool")
        handler = self._handlers.get(tool_name)
        if handler is None:
            return self._fail(tool_name, ERR_UNKNOWN_TOOL, f"{tool_name!r} has no registered handler")

        arguments = arguments if isinstance(arguments, dict) else {}

        # ---- 参数校验（required 存在 + JSON 类型匹配） -----------------------
        parameters = TOOL_SCHEMAS[tool_name]["function"]["parameters"]
        for required in parameters.get("required", ()):
            if required not in arguments:
                return self._fail(tool_name, ERR_MISSING_PARAM, f"missing required parameter: {required}")
        for key, value in arguments.items():
            spec = parameters.get("properties", {}).get(key)
            if spec is None:
                return self._fail(tool_name, ERR_INVALID_PARAM, f"unknown parameter: {key}")
            expected = spec.get("type")
            check = _TYPE_CHECKS.get(expected)
            if check is not None and not isinstance(value, check):
                return self._fail(
                    tool_name, ERR_INVALID_PARAM,
                    f"parameter {key!r} must be {expected}, got {type(value).__name__}",
                )
            # bool 是 int 子类——显式声明 boolean 的参数拒绝 int 冒充
            if expected == "boolean" and not isinstance(value, bool):
                return self._fail(tool_name, ERR_INVALID_PARAM, f"parameter {key!r} must be boolean")

        # ---- 执行（异常兜底为结果） -------------------------------------------
        try:
            return handler(**arguments)
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            return self._fail(tool_name, ERR_EXECUTION_FAILED, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _fail(tool: str, code: str, detail: str) -> ToolResult:
        return ToolResult(success=False, tool=tool, error=f"{code}: {detail}")
