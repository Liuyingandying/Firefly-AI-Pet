# -*- coding: utf-8 -*-
"""Tool selector — Agent 工具白名单（M3.0）。

**禁动态工具发现**: 允许集合是编译期常量（与 M2.7 工具词表 import 断言
同源锁定, 词表漂移在 import 时即失败）, 筛选只是常量的子集运算。
runtime 执行前二次校验（防御纵深——即便 planner 产出差值, registry 仍
会以 unknown_tool 拒绝）。
"""

from __future__ import annotations

from core.learning.tools.schema import (
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
)

#: Agent 允许的工具白名单（封闭常量, 与 tools 词表 import 锁定）
ALLOWED_TOOLS = frozenset({
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
    TOOL_GENERATE_LEARNING_PLAN,
})

#: 意图关键词 → 工具（用于按任务筛选候选子集）
_INTENT_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("教材", "导入", "解析课程", "生成课程"), TOOL_IMPORT_TEXTBOOK),
    (("实验", "仿真", "运行", "演示", "观察"), TOOL_RUN_EXPERIMENT),
    (("计划", "规划", "怎么学", "学习路径"), TOOL_GENERATE_LEARNING_PLAN),
    (("概念", "查询", "是什么", "解释", "区别", "理解", "TE", "TM", "极化", "波"), TOOL_QUERY_CONCEPT),
)


class ToolDeniedError(PermissionError):
    """词表外工具被拒绝（runtime 兜底为结构化失败）。"""


def select_allowed_tools(user_query: str) -> tuple[str, ...]:
    """按任务意图返回允许工具子集（常量白名单的子集, 稳定排序）。

    零命中时回退 = query_concept + generate_learning_plan（保守默认）。
    """
    query = str(user_query or "")
    selected: list[str] = []
    for keywords, tool in _INTENT_TOOLS:
        if tool in selected:
            continue
        if any(kw in query for kw in keywords):
            selected.append(tool)
    if not selected:
        selected = [TOOL_QUERY_CONCEPT, TOOL_GENERATE_LEARNING_PLAN]
    return tuple(sorted(selected))


def assert_allowed(tool: str) -> None:
    """执行前白名单校验；词表外抛 ToolDeniedError（runtime 兜底）。"""
    if tool not in ALLOWED_TOOLS:
        raise ToolDeniedError(f"tool {tool!r} is not in the agent allowlist")
