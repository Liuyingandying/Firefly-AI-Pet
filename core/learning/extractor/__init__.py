# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.1 — Native Document Extractor（M2.0 §3/§4 首个落地）。

Markdown/TXT 提取器 + 提取协议 + 注册表 + 大纲归一化;
不接 Docling/MinerU/LLM, 不修改 CurriculumDraft/KnowledgeGraph/编排器。
"""

from core.learning.extractor.base import DocumentExtractor, ExtractorRegistry
from core.learning.extractor.native import NativeExtractor
from core.learning.extractor.normalizer import to_document_structure
from core.learning.extractor.schema import (
    ERR_DECODE,
    ERR_EMPTY,
    ERR_NOT_FOUND,
    ERR_NOT_SUPPORTED,
    ERR_READ,
    EXTRACTOR_NATIVE,
    DocumentExtractionResult,
    ExtractedDocument,
    ExtractedSection,
    FormulaBlock,
    ImageBlock,
    ParagraphBlock,
)

__all__ = [
    "DocumentExtractor",
    "ExtractorRegistry",
    "NativeExtractor",
    "to_document_structure",
    "DocumentExtractionResult",
    "ExtractedDocument",
    "ExtractedSection",
    "ParagraphBlock",
    "FormulaBlock",
    "ImageBlock",
    "EXTRACTOR_NATIVE",
    "ERR_NOT_FOUND",
    "ERR_NOT_SUPPORTED",
    "ERR_DECODE",
    "ERR_EMPTY",
    "ERR_READ",
]
