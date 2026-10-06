# -*- coding: utf-8 -*-
"""Concept refinement schema — 概念细化协议（M2.5）。

设计约束:
- 输入只读消费 M2.4 ``ConceptCandidate``; 输出**仍是 proposal**（规则 5）——
  本包不构造 ``ConceptNode``、不 import 知识图类型、不触碰任何图实例;
- **与课程侧 ``core.learning.curriculum.models.ConceptProposal``（禁改）
  同名共存**: 字段集与语义不同（学习概念细化提案 vs 课程章内概念草案）,
  互不引用;
- 规则 3（不删除原概念）: 细化是**增量**的——细化结果同时包含
  原概念提案与子概念提案, 源候选对象原样保留;
- 置信度**传递**不重算（对齐 M1.0 strength 语义）; 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 失败码封闭词表
# ---------------------------------------------------------------------------

ERR_INVALID_INPUT = "invalid_input"      # 输入非 ConceptCandidate


@dataclass(frozen=True)
class ConceptProposal:
    """细粒度学习概念提案（仍是提案——确认与节点化属下游职责）。

    规约七字段 + 溯源扩展三字段（规则 2: 保留原始 candidate 溯源）。
    """

    concept_id: str
    name: str
    description: str
    #: 父概念引用（章节标题; 章级候选 name==chapter → 空串 = 顶层概念）
    parent_concept: str = ""
    prerequisites: tuple[str, ...] = ()
    related_experiments: tuple[str, ...] = ()
    confidence: float = 0.0
    # ---- 溯源扩展（规则 2） ----
    source_candidate_id: str = ""       # 来源 M2.4 候选 id
    source_level: int = 0               # 来源大纲层级
    source_chapter: str = ""            # 来源章标题
    #: 层级细化（规则 4）: "base" = 概念本身, "subtopic" = 细化子概念
    refinement_kind: str = "base"

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "description": self.description,
            "parent_concept": self.parent_concept,
            "prerequisites": list(self.prerequisites),
            "related_experiments": list(self.related_experiments),
            "confidence": self.confidence,
            "source_candidate_id": self.source_candidate_id,
            "source_level": self.source_level,
            "source_chapter": self.source_chapter,
            "refinement_kind": self.refinement_kind,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class ConceptRefinementResult:
    """一次细化的完整结果（成功与失败都是结果, 不抛异常）。

    ``source_candidate`` 原样回显（规则 3: 不删除/不修改原概念）;
    ``proposals`` 至少含 base 一条（规则 3: 增量细化）。
    """

    success: bool
    proposals: tuple[ConceptProposal, ...] = ()
    source_candidate: object | None = None   # M2.4 ConceptCandidate 原样回显
    warnings: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "proposals": [p.to_dict() for p in self.proposals],
            "source_candidate": (
                self.source_candidate.to_dict()
                if self.source_candidate is not None
                else None
            ),
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
