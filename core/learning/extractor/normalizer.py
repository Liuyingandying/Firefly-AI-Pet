# -*- coding: utf-8 -*-
"""Normalizer — ExtractedDocument → 课程侧 DocumentStructure（M2.1, M2.0 §2-L2）。

唯一的跨层映射点: 把提取层的富文档**降维**成课程侧的纯大纲
（curriculum 的 ``DocumentStructure``, 禁改）——段落/公式/图片留在提取层,
不进大纲（保持其"纯大纲语义"不变, M2.0 §2.2-L2 决策）。

兼容性保证（测试锁定）:
- level>1 的节必有 parent 且 parent level 更低（native 栈式建树保证）;
- 同域 position 唯一（兄弟计数器保证）;
- 产物通过 ``validate()`` 零问题——course 侧 ``build_draft`` 可直接消费。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.learning.curriculum.adapter.documents import (
    DocumentStructure,
    DocumentStructureError,
    Section,
)
from core.learning.extractor.schema import ExtractedDocument


def to_document_structure(
    extracted: ExtractedDocument,
    *,
    document_id: str | None = None,
    author: str = "",
) -> DocumentStructure:
    """提取文档 → 课程侧纯大纲; 结构问题抛 DocumentStructureError（显式失败）。"""
    if extracted is None or not extracted.sections:
        raise DocumentStructureError("extracted document has no sections")

    sections = tuple(
        Section(
            id=node.section_id,
            title=node.title,
            level=node.level,
            position=node.position,
            parent_id=node.parent_id,
            source_page_range=None,      # md/txt 无页概念
            concepts=(),                 # native v1 不产概念提示
        )
        for node in extracted.sections
    )

    title = (extracted.title or Path(extracted.source_path).stem or "").strip()
    if not title:
        raise DocumentStructureError("extracted document title must not be empty")

    doc_id = document_id or f"doc:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:12]}"
    document = DocumentStructure(
        document_id=doc_id,
        title=title,
        author=author,
        sections=sections,
        source_refs=(),
        page_concepts=(),
    )
    problems = document.validate()
    if problems:  # 理论不可达（构造保证）, 防御性显式失败
        raise DocumentStructureError("normalized structure invalid: " + "; ".join(problems))
    return document
