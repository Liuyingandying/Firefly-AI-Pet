# -*- coding: utf-8 -*-
"""Planner — 规则规划器：用户目标 → 有序 PlannedStep（M3.0, 无 LLM）。

确定性关键词意图规划（词表为模块常量）；命中可叠加、顺序 = 依赖序：
导入（若提供素材） → 逐概念查询 → 实验 → 学习计划 → explain 终步。

示例（任务书）: "解释TE和TM区别"
→ [query_concept(em-te-polarization), query_concept(em-tm-polarization),
   run_experiment(uniform-plane-wave), explain]
"""

from __future__ import annotations

from dataclasses import dataclass

from core.learning.tools.schema import (
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
)

#: 概念关键词 → 知识图 concept_id（封闭映射）
_CONCEPT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("TE", "te极化", "te 极化", "水平极化"), "em-te-polarization"),
    (("TM", "tm极化", "tm 极化", "垂直极化"), "em-tm-polarization"),
    (("均匀平面波", "平面波", "plane wave"), "em-uniform-plane-wave"),
)
_DEFAULT_CONCEPT = "em-uniform-plane-wave"

_EXPERIMENT_ID = "uniform-plane-wave"

_EXPLAIN_KEYWORDS = ("解释", "理解", "区别", "是什么", "为什么", "看看", "帮我", "演示")
_PLAN_KEYWORDS = ("计划", "规划", "怎么学", "学习路径", "安排")
_IMPORT_KEYWORDS = ("教材", "导入")


@dataclass(frozen=True)
class PlannedStep:
    """一个规划步骤（tool=None 表示 explain 终步）。"""

    action: str
    tool: str | None
    arguments: dict


def _match_concepts(query: str) -> list[str]:
    matched: list[str] = []
    for keywords, concept_id in _CONCEPT_KEYWORDS:
        if any(kw in query for kw in keywords) and concept_id not in matched:
            matched.append(concept_id)
    return matched


def plan(task) -> tuple[PlannedStep, ...]:
    """AgentTask（或任意带 user_query/context 的对象）→ 有序步骤。永不抛异常。"""
    query = str(getattr(task, "user_query", "") or "")
    context = getattr(task, "context", None) or {}
    if not isinstance(context, dict):
        context = {}
    steps: list[PlannedStep] = []

    # ---- 1. 教材导入（仅当意图明确且素材齐备；最先执行） --------------------
    if any(kw in query for kw in _IMPORT_KEYWORDS):
        path = context.get("textbook_path") or context.get("path")
        course_id = context.get("course_id")
        if path and course_id:
            steps.append(PlannedStep(
                action=f"导入教材 {path} 并生成课程 {course_id}",
                tool=TOOL_IMPORT_TEXTBOOK,
                arguments={"path": str(path), "course_id": str(course_id)},
            ))
        else:
            steps.append(PlannedStep(
                action="跳过教材导入（缺少 textbook_path/course_id 上下文）",
                tool=None,
                arguments={"skipped": True},
            ))

    # ---- 2. 概念查询（逐命中概念；零命中回退默认概念） -----------------------
    concepts = _match_concepts(query)
    if not concepts:
        concepts = [_DEFAULT_CONCEPT]
    for concept_id in concepts:
        steps.append(PlannedStep(
            action=f"查询概念 {concept_id}",
            tool=TOOL_QUERY_CONCEPT,
            arguments={"concept_id": concept_id},
        ))

    # ---- 3. 仿真实验（显式实验意图, 或概念解释类请求的观察环节） -------------
    wants_experiment = (
        any(kw in query for kw in ("实验", "仿真", "演示", "观察"))
        or any(kw in query for kw in _EXPLAIN_KEYWORDS)
    )
    if wants_experiment:
        params = context.get("experiment_params")
        arguments = {"experiment_id": _EXPERIMENT_ID}
        if isinstance(params, dict) and params:
            arguments["params"] = params
        steps.append(PlannedStep(
            action=f"运行仿真实验 {_EXPERIMENT_ID}",
            tool=TOOL_RUN_EXPERIMENT,
            arguments=arguments,
        ))

    # ---- 4. 学习计划 ---------------------------------------------------------
    if any(kw in query for kw in _PLAN_KEYWORDS):
        steps.append(PlannedStep(
            action=f"生成学习计划（目标概念：{concepts[-1]}）",
            tool=TOOL_GENERATE_LEARNING_PLAN,
            arguments={"concept_id": concepts[-1]},
        ))

    # ---- 5. explain 终步（消费前序结果, 由 runtime 的解释层执行） ------------
    steps.append(PlannedStep(
        action=f"综合解释并给出学习建议（焦点概念：{', '.join(concepts)}）",
        tool=None,
        arguments={"focus_concepts": list(concepts)},
    ))
    return tuple(steps)
