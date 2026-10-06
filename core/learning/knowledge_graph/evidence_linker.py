# -*- coding: utf-8 -*-
"""EvidenceLinker — RuleEvaluationInput → ConceptLearningContext（M1.2）。

把规则引擎输入事件挂接到知识图：事件本身携带"发生了什么学习"，
知识图补充"这个概念是什么/先学什么/去哪个实验观察"。

安全语义（M1.2 验收口径）:
- **永不抛异常**——未知概念/空 concept/非法输入都返回
  ``ConceptLearningContext(known=False)`` 的安全结果;
- 事件回显在输入合法时始终保留（概念未知不丢事件）;
- 目标概念处于环上时 ``known=True`` 但 ``learning_path=()``（节点存在,
  路径拒绝）。

纯内存、无 LLM、无 UI、不写课程存储。
"""

from __future__ import annotations

from core.learning.knowledge_graph.graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.knowledge_graph.schema import ConceptLearningContext
from core.learning.mastery_integration.schema import RuleEvaluationInput


class EvidenceLinker:
    """学习事件 → 概念学习上下文。"""

    def __init__(self, graph: KnowledgeGraph | None = None):
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)

    def link(self, event: RuleEvaluationInput | None) -> ConceptLearningContext:
        """挂接一次学习事件; 永不抛异常。"""
        # ---- 非法输入：全空安全上下文 ---------------------------------------
        if not isinstance(event, RuleEvaluationInput):
            return ConceptLearningContext(concept_id="", reason="invalid_input")

        concept_id = event.concept_id
        if not isinstance(concept_id, str) or not concept_id.strip():
            return ConceptLearningContext(
                concept_id=concept_id if isinstance(concept_id, str) else "",
                event_type=event.signal_type,
                strength=event.strength,
                timestamp=event.timestamp,
                evidence_id=event.evidence_id,
                reason="empty_concept",
            )

        # ---- 事件回显（合法输入始终保留） -----------------------------------
        echo = dict(
            event_type=event.signal_type,
            strength=event.strength,
            timestamp=event.timestamp,
            evidence_id=event.evidence_id,
        )

        # ---- 未知概念：known=False 安全返回, 事件不丢 ------------------------
        node = self._graph.get_concept(concept_id)
        if node is None:
            return ConceptLearningContext(concept_id=concept_id, known=False, **echo)

        # ---- 已知概念：节点数据 + 路径（环上拒绝路径但保留节点） -------------
        if self._graph._closure_has_cycle(concept_id):  # noqa: SLF001 - 同包边界
            return ConceptLearningContext(
                concept_id=concept_id,
                known=True,
                concept_name=node.name,
                chapter=node.chapter,
                prerequisites=node.prerequisites,
                related_experiments=node.related_experiments,
                learning_path=(),
                **echo,
            )

        return ConceptLearningContext(
            concept_id=concept_id,
            known=True,
            concept_name=node.name,
            chapter=node.chapter,
            prerequisites=self._graph.get_prerequisites(concept_id),
            related_experiments=self._graph.get_related_experiments(concept_id),
            learning_path=self._graph.find_learning_path(concept_id),
            **echo,
        )

    def link_all(
        self, events: list[RuleEvaluationInput] | tuple[RuleEvaluationInput, ...]
    ) -> tuple[ConceptLearningContext, ...]:
        """批量挂接（逐条安全返回, 永不抛异常）。"""
        return tuple(self.link(event) for event in events or ())
