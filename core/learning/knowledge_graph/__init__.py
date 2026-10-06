# -*- coding: utf-8 -*-
"""Firefly Learning Mode M1.2 — Knowledge Graph（概念知识图 + 证据挂接）。

纯数据概念图（人工预置种子）+ 只读查询接口 + 学习事件挂接;
不修改上游任何模块, 不接 LLM/UI/数据库。
"""

from core.learning.knowledge_graph.evidence_linker import EvidenceLinker
from core.learning.knowledge_graph.graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.knowledge_graph.schema import ConceptLearningContext, ConceptNode

__all__ = [
    "ConceptNode",
    "ConceptLearningContext",
    "KnowledgeGraph",
    "EvidenceLinker",
    "DEFAULT_NODES",
]
