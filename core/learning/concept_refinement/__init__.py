# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.5 — Concept Refinement（ConceptCandidate → ConceptProposal）。

确定性概念细化（子话题由调用方显式提供, 无 LLM）;
所有输出仍是 proposal——零 ConceptNode 构造、零 KnowledgeGraph 交互;
不删除原概念（增量细化）, 置信度传递不重算。

注: 与课程侧 ``core.learning.curriculum.models.ConceptProposal``（禁改）
同名共存——字段集与语义不同, 互不引用。
"""

from core.learning.concept_refinement.refiner import ConceptRefiner
from core.learning.concept_refinement.schema import (
    ERR_INVALID_INPUT,
    ConceptProposal,
    ConceptRefinementResult,
)

__all__ = [
    "ConceptRefiner",
    "ConceptProposal",
    "ConceptRefinementResult",
    "ERR_INVALID_INPUT",
]
