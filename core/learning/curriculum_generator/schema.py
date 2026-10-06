# -*- coding: utf-8 -*-
"""Curriculum generator schema — 教材课程生成协议（M2.3）。

设计约束:
- 输入只读消费 M2.1 ``ExtractedDocument`` 与 M2.2 ``DocumentPackage``;
- ``curriculum_draft`` 由**既有** ``build_draft``（禁改）产出——本层是只读
  包装器, 零课程数据结构改动、零落库、零激活（confirm 门保持课程侧唯一）;
- 规约六字段全部 JSON 原生类型; ``CurriculumDraft`` 本体是课程侧活对象,
  JSON 中以摘要投影出现（完整序列化属课程侧职责）;
- 错误即结果: ``CurriculumGenerationResult`` 承载成功与失败, 不抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 失败码封闭词表
# ---------------------------------------------------------------------------

ERR_INVALID_INPUT = "invalid_input"          # 输入非提取文档/缺 sections
ERR_EMPTY_DOCUMENT = "empty_document"        # 提取文档没有任何节
ERR_MISSING_COURSE_ID = "missing_course_id"  # course_id 为空
ERR_INVALID_STRUCTURE = "invalid_structure"  # 归一化大纲校验失败
ERR_DRAFT_BUILD = "draft_build_failed"       # 既有 build_draft 抛错（防御）


@dataclass(frozen=True)
class GeneratedSection:
    """一个非空节的教学化记录（规则 1-4 的落点）。

    - ``lesson_path``: 层级编号（"1"、"1.1"、"1.1.1"）——标题层级 → 课程层级;
    - ``formulas``:    公式元数据（latex + display）, 只记录不改写;
    - ``image_refs`` / ``attachment_ids``: 图片引用与（可选包解析后的）
      附件 id——引用先于解析, 附件缺失只告警不阻断生成。
    """

    section_id: str
    title: str
    level: int
    lesson_path: str
    chapter_title: str                     # 所属章（最近 level-1 祖先）
    paragraph_count: int
    formulas: tuple[dict, ...] = ()        # {"latex": str, "display": bool}
    image_refs: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "level": self.level,
            "lesson_path": self.lesson_path,
            "chapter_title": self.chapter_title,
            "paragraph_count": self.paragraph_count,
            "formulas": [dict(f) for f in self.formulas],
            "image_refs": list(self.image_refs),
            "attachment_ids": list(self.attachment_ids),
        }


@dataclass(frozen=True)
class CurriculumGenerationResult:
    """一次生成的完整结果（成功与失败都是结果, 不抛异常）。"""

    success: bool
    curriculum_draft: object | None = None     # CurriculumDraft（既有类型, 零改动）
    source_document: object | None = None      # M2.1 ExtractedDocument 回显
    generated_sections: tuple[GeneratedSection, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str = ""                            # 成功为 ""; 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "curriculum_draft": _draft_summary(self.curriculum_draft),
            "source_document": (
                self.source_document.to_dict()
                if self.source_document is not None
                else None
            ),
            "generated_sections": [s.to_dict() for s in self.generated_sections],
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _draft_summary(draft: object | None) -> dict | None:
    """CurriculumDraft 的 JSON 摘要投影（本体不序列化, 课程侧职责）。"""
    if draft is None:
        return None
    chapters = getattr(draft, "chapters", ()) or ()
    ordered = sorted(chapters, key=lambda c: (getattr(c, "position", 0), getattr(c, "id", "")))
    return {
        "draft_id": getattr(draft, "id", ""),
        "course_id": getattr(draft, "course_id", ""),
        "title": getattr(draft, "title", ""),
        "status": getattr(draft, "status", ""),
        "is_confirmed": bool(getattr(draft, "is_confirmed", False)),
        "chapter_count": len(chapters),
        "chapter_titles": [getattr(c, "title", "") for c in ordered],
        "concept_proposal_count": len(getattr(draft, "concept_candidates", ()) or ()),
    }
