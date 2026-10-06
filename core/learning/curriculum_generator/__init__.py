# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.3 — Curriculum Generator（DocumentStructure → CurriculumDraft）。

既有 build_draft 的只读包装 + 教学化节记录;
不修改 build_draft/CurriculumDraft/KnowledgeGraph, 不落库, 不自动激活;
无 LLM、无 Docling/MinerU、无 UI。
"""

from core.learning.curriculum_generator.generator import CurriculumGenerator
from core.learning.curriculum_generator.schema import (
    ERR_DRAFT_BUILD,
    ERR_EMPTY_DOCUMENT,
    ERR_INVALID_INPUT,
    ERR_INVALID_STRUCTURE,
    ERR_MISSING_COURSE_ID,
    CurriculumGenerationResult,
    GeneratedSection,
)

__all__ = [
    "CurriculumGenerator",
    "CurriculumGenerationResult",
    "GeneratedSection",
    "ERR_INVALID_INPUT",
    "ERR_EMPTY_DOCUMENT",
    "ERR_MISSING_COURSE_ID",
    "ERR_INVALID_STRUCTURE",
    "ERR_DRAFT_BUILD",
]
