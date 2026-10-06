# -*- coding: utf-8 -*-
"""ContextBuilder — KnowledgeGraph + ExperimentRegistry → LearningContext（M3.5）。

数据来源（全部只读, 零上游改动）::

    KnowledgeGraph.list_concepts()        → available_concepts（M3.5 新增只读枚举）
    graph.get_concept(id).name            → 接地文本中的概念 display_name
    ExperimentRegistry.list_experiments() → available_experiments（M0.2 既有接口）
    meta.display_name                     → 实验显示名

``render_grounding`` 产出模型可读文本（runtime 注入首条 system 消息）::

    [Learning Environment]
    Available concepts:
    - em-uniform-plane-wave (均匀平面波传播)
    ...
    Available experiments:
    - uniform-plane-wave (均匀平面波传播)
    ...

graph/registry 均可注入（测试隔离）; 两者不可用时降级为空词表
（grounding 文本随之退化为提示语, 永不抛异常）。
"""

from __future__ import annotations

from core.learning.agent.context.schema import ContextGrounding, LearningContext
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph


class ContextBuilder:
    """学习环境快照构建器（只读消费 KnowledgeGraph + ExperimentRegistry）。"""

    def __init__(
        self,
        *,
        graph: KnowledgeGraph | None = None,
        registry=None,                      # M0.2 ExperimentRegistry（可选注入）
    ):
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)
        self._registry = registry

    def build(self, *, learning_goal: str = "", hints: tuple[str, ...] | list[str] = ()) -> LearningContext:
        """构建当前环境快照; 任何来源故障降级为空词表（永不抛异常）。"""
        concepts: list[str] = []
        try:
            concepts = list(self._graph.list_concepts())
        except Exception:  # noqa: BLE001 - 降级为空词表
            concepts = []

        experiments: list[str] = []
        try:
            registry = self._registry
            if registry is None:
                from core.learning.simulation.registry import ExperimentRegistry

                registry = ExperimentRegistry()
            experiments = [
                meta.experiment_id for meta in registry.list_experiments()
            ]
        except Exception:  # noqa: BLE001
            experiments = []

        return LearningContext(
            available_concepts=tuple(concepts),
            available_experiments=tuple(experiments),
            learning_goal=str(learning_goal or ""),
            hints=tuple(str(h) for h in hints if str(h).strip()),
        )

    def render_grounding(
        self,
        context: LearningContext | None = None,
        *,
        learning_goal: str = "",
        hints: tuple[str, ...] | list[str] = (),
    ) -> ContextGrounding:
        """把上下文渲染为模型可读的接地文本（context 缺省时先 build）。"""
        ctx = context if context is not None else self.build(
            learning_goal=learning_goal, hints=hints
        )
        lines: list[str] = ["[Learning Environment]"]

        lines.append("Available concepts:")
        if ctx.available_concepts:
            for cid in ctx.available_concepts:
                node = None
                try:
                    node = self._graph.get_concept(cid)
                except Exception:  # noqa: BLE001
                    node = None
                name = getattr(node, "name", "") if node is not None else ""
                lines.append(f"- {cid}" + (f" ({name})" if name else ""))
        else:
            lines.append("- (none)")

        lines.append("Available experiments:")
        if ctx.available_experiments:
            for eid in ctx.available_experiments:
                lines.append(f"- {eid}")
        else:
            lines.append("- (none)")

        lines.append(
            "Use ONLY the concept ids and experiment ids listed above when "
            "calling tools; do not invent ids."
        )
        if ctx.learning_goal:
            lines.append(f"Learning goal: {ctx.learning_goal}")
        for hint in ctx.hints:
            lines.append(f"Hint: {hint}")

        return ContextGrounding(text="\n".join(lines), context=ctx)
