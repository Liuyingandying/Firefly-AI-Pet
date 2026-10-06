# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.4 — Knowledge Import（草案 → 知识图候选 → 显式 confirm）。

候选提取 + 显式 confirm 注入门;
**永不写入 KnowledgeGraph**（节点以返回值交付, 注入是调用方的构造注入）;
不修改 KnowledgeGraph 核心/CurriculumDraft/Rule Engine, 无 LLM。
"""

from core.learning.knowledge_import.extractor import extract_candidates
from core.learning.knowledge_import.injector import convert_approved
from core.learning.knowledge_import.schema import (
    CONFIDENCE_CHAPTER,
    CONFIDENCE_EMPTY_CHAPTER,
    CONFIDENCE_SUBSECTION,
    ERR_CONFIRMATION_REQUIRED,
    ERR_CYCLE,
    ERR_INVALID_INPUT,
    ERR_NO_CHAPTERS,
    ConceptCandidate,
    KnowledgeCandidateResult,
    KnowledgeInjectionResult,
)

__all__ = [
    "extract_candidates",
    "convert_approved",
    "ConceptCandidate",
    "KnowledgeCandidateResult",
    "KnowledgeInjectionResult",
    "CONFIDENCE_CHAPTER",
    "CONFIDENCE_SUBSECTION",
    "CONFIDENCE_EMPTY_CHAPTER",
    "ERR_INVALID_INPUT",
    "ERR_NO_CHAPTERS",
    "ERR_CONFIRMATION_REQUIRED",
    "ERR_CYCLE",
]
