# -*- coding: utf-8 -*-
"""Candidate extractor — CurriculumDraft + DocumentStructure → ConceptCandidate（M2.4）。

规则映射（M2.4 验收口径）:
1. **Chapter → ConceptCandidate**: 候选从草案章生成（课程侧是命名权威）,
   大纲树用于补层级/父子关系;
2. **Heading hierarchy → prerequisite suggestion**: level≥2 的节 → 候选,
   其前置建议 = 父节候选（父先于子的教材顺序）; level-1 章无前置;
3. **experiment metadata → related_experiments**: 显式注入的
   ``experiment_metadata`` 映射（节标题/章标题 → 实验 id）——这是
   M2.3 add_experiment 提案 / 实验注册表对齐的未来接线点;
4. 永不写 KnowledgeGraph（本模块甚至不 import 图类型, 除 injector 外）;
5. confirm 门在 injector——本模块只产候选。

失败码: invalid_input（draft 非法/缺 chapters）/ no_chapters（零章）。
"""

from __future__ import annotations

import hashlib

from core.learning.knowledge_import.schema import (
    CONFIDENCE_CHAPTER,
    CONFIDENCE_EMPTY_CHAPTER,
    CONFIDENCE_SUBSECTION,
    ERR_INVALID_INPUT,
    ERR_NO_CHAPTERS,
    ConceptCandidate,
    KnowledgeCandidateResult,
)


def _candidate_id(level: int, title: str, parent_key: str) -> str:
    digest = hashlib.sha1(f"{level}|{title}|{parent_key}".encode("utf-8")).hexdigest()[:10]
    return f"kc-{digest}"


def extract_candidates(
    draft,
    document_structure=None,
    *,
    experiment_metadata: dict | None = None,
    include_subsections: bool = True,
) -> KnowledgeCandidateResult:
    """草案 + 大纲 → 知识概念候选; 非法输入返回失败结果, 不抛异常。"""
    # ---- 非法输入 -----------------------------------------------------------
    chapters = getattr(draft, "chapters", None)
    if chapters is None or not isinstance(chapters, tuple):
        return _fail(ERR_INVALID_INPUT, "draft is not a CurriculumDraft")
    ordered_chapters = sorted(chapters, key=lambda c: (getattr(c, "position", 0), getattr(c, "id", "")))
    if not ordered_chapters:
        return _fail(ERR_NO_CHAPTERS, "draft has no chapters")

    metadata = experiment_metadata or {}

    # ---- 大纲树（可选: 无树则只有章候选, 无层级前置） -------------------------
    sections = list(getattr(document_structure, "sections", ()) or ()) \
        if document_structure is not None else []
    by_title: dict[str, list] = {}
    for section in sections:
        by_title.setdefault(getattr(section, "title", ""), []).append(section)
    section_by_id = {getattr(s, "id", ""): s for s in sections}

    warnings: list[str] = []
    candidates: list[ConceptCandidate] = []
    candidate_by_section: dict[str, str] = {}   # section id → candidate id
    draft_concept_counts = _draft_concept_counts(ordered_chapters)

    source_summary = {
        "draft_id": getattr(draft, "id", ""),
        "draft_title": getattr(draft, "title", ""),
        "chapter_count": len(ordered_chapters),
        "document_id": getattr(document_structure, "document_id", "") if document_structure is not None else "",
        "document_title": getattr(document_structure, "title", "") if document_structure is not None else "",
        "section_count": len(sections),
    }

    # ---- 规则 1: Chapter → ConceptCandidate ---------------------------------
    for chapter in ordered_chapters:
        title = getattr(chapter, "title", "")
        matches = by_title.get(title, [])
        root = next((s for s in matches if int(getattr(s, "level", 1)) == 1), None)
        if root is None and matches:
            root = matches[0]
        root_id = getattr(root, "id", "") if root is not None else ""

        subsections = _subtree_sections(root, section_by_id) if root is not None else []
        concept_count = draft_concept_counts.get(getattr(chapter, "id", ""), 0)
        confidence = CONFIDENCE_CHAPTER
        if not subsections and concept_count == 0:
            confidence = CONFIDENCE_EMPTY_CHAPTER
            warnings.append(
                f"empty chapter (no subsections, no concept proposals): {title}"
            )
        if root is None:
            warnings.append(f"chapter not found in document structure: {title}")

        concept_id = _candidate_id(1, title, "root")
        candidate_by_section[root_id or f"title:{title}"] = concept_id
        candidates.append(
            ConceptCandidate(
                concept_id=concept_id,
                name=title,
                chapter=title,
                prerequisites=(),            # level-1 无层级前置
                related_experiments=_experiments_for(title, title, metadata),
                confidence=confidence,
                level=1,
                source_section_id=root_id,
            )
        )

        # ---- 规则 2: 子节 → 候选, 前置建议 = 父候选 --------------------------
        if include_subsections:
            for section in subsections:
                sec_title = getattr(section, "title", "")
                sec_level = int(getattr(section, "level", 2))
                parent_id = getattr(section, "parent_id", None) or ""
                parent_candidate = candidate_by_section.get(parent_id, "")
                concept_id = _candidate_id(
                    sec_level, sec_title, parent_id or "root"
                )
                candidate_by_section[getattr(section, "id", "")] = concept_id
                candidates.append(
                    ConceptCandidate(
                        concept_id=concept_id,
                        name=sec_title,
                        chapter=title,
                        prerequisites=(parent_candidate,) if parent_candidate else (),
                        related_experiments=_experiments_for(sec_title, title, metadata),
                        confidence=CONFIDENCE_SUBSECTION,
                        level=sec_level,
                        source_section_id=getattr(section, "id", ""),
                    )
                )

    return KnowledgeCandidateResult(
        success=True,
        candidates=tuple(candidates),
        source_document=source_summary,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _draft_concept_counts(ordered_chapters: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chapter in ordered_chapters:
        concepts = getattr(chapter, "concepts", ()) or ()
        counts[getattr(chapter, "id", "")] = len(concepts)
    return counts


def _subtree_sections(root, section_by_id: dict) -> list:
    """root 的全部 level≥2 后代（文档序: 父先子后）。"""
    if root is None:
        return []
    children_map: dict[str, list] = {}
    for section in section_by_id.values():
        parent = getattr(section, "parent_id", None)
        if parent:
            children_map.setdefault(parent, []).append(section)

    ordered: list = []
    stack = list(children_map.get(getattr(root, "id", ""), []))
    while stack:
        section = stack.pop(0)
        ordered.append(section)
        stack[:0] = children_map.get(getattr(section, "id", ""), [])
    return ordered


def _experiments_for(title: str, chapter_title: str, metadata: dict) -> tuple[str, ...]:
    """节级实验元数据 + 章级（继承）, 保序去重。"""
    merged: list[str] = []
    for key in (title, chapter_title):
        for exp in metadata.get(key, ()) or ():
            exp = str(exp)
            if exp and exp not in merged:
                merged.append(exp)
    return tuple(merged)


def _fail(code: str, detail: str) -> KnowledgeCandidateResult:
    return KnowledgeCandidateResult(success=False, error=f"{code}: {detail}")
