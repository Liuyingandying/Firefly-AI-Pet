# -*- coding: utf-8 -*-
"""Knowledge import schema — 知识图谱候选节点协议（M2.4）。

设计约束:
- **候选 ≠ 节点**: ``ConceptCandidate`` 是待审提案, 永不自动写入
  ``KnowledgeGraph``（M1.2 禁改）——正式 ``ConceptNode`` 只在
  **显式 confirm**（injector 的 approved_ids 门）后构造, 且以返回值
  交给调用方经构造注入（``KnowledgeGraph(nodes=...)``）, 本包零图写入;
- 输入只读消费 ``CurriculumDraft``（课程侧, 禁改）与课程侧
  ``DocumentStructure``（大纲层级来源）;
- 全字段 JSON 原生类型; 错误即结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 置信度常数（M0 风格: 固定标定, 上层可演进）
# ---------------------------------------------------------------------------

CONFIDENCE_CHAPTER = 0.8          # 章（level-1）候选
CONFIDENCE_SUBSECTION = 0.7       # 节（level≥2, 有父级层级依据）
CONFIDENCE_EMPTY_CHAPTER = 0.5    # 无子节且无概念提案的空章（降权 + 告警）

# ---------------------------------------------------------------------------
# 失败码封闭词表
# ---------------------------------------------------------------------------

ERR_INVALID_INPUT = "invalid_input"       # draft 非 CurriculumDraft / 缺 chapters
ERR_NO_CHAPTERS = "no_chapters"           # 草案没有任何章 → 无候选可提
ERR_CONFIRMATION_REQUIRED = "confirmation_required"  # 注入门: 未提供 approved_ids
ERR_CYCLE = "prerequisite_cycle"          # 前置闭环（防御, 父链构造下不可达）


@dataclass(frozen=True)
class ConceptCandidate:
    """一个待审知识概念提案（确认前绝不进入知识图）。"""

    concept_id: str                     # 候选 id（确定性, 确认后即节点 id）
    name: str
    chapter: str                        # 所属章标题（level-1 即自身）
    prerequisites: tuple[str, ...] = () # 父候选 id（标题层级 → 前置建议）
    related_experiments: tuple[str, ...] = ()  # 实验元数据（显式注入映射）
    confidence: float = CONFIDENCE_CHAPTER
    level: int = 1                      # 来源大纲层级（审计用）
    source_section_id: str = ""         # 来源大纲节 id（回链, 审计用）

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "chapter": self.chapter,
            "prerequisites": list(self.prerequisites),
            "related_experiments": list(self.related_experiments),
            "confidence": self.confidence,
            "level": self.level,
            "source_section_id": self.source_section_id,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class KnowledgeCandidateResult:
    """一次候选提取的完整结果（成功与失败都是结果, 不抛异常）。"""

    success: bool
    candidates: tuple[ConceptCandidate, ...] = ()
    source_document: dict = field(default_factory=dict)  # 来源摘要（JSON 安全）
    warnings: tuple[str, ...] = ()
    error: str = ""                      # 成功为 ""; 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "candidates": [c.to_dict() for c in self.candidates],
            "source_document": dict(self.source_document),
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class KnowledgeInjectionResult:
    """显式 confirm 后的节点构造结果（**不含任何图实例**——规则 4）。

    ``nodes`` 是已构造的 ``ConceptNode``（M1.2 类型, 只构造不写入）;
    注入方式 = 调用方以 ``KnowledgeGraph(nodes=...)`` 构造注入。
    """

    success: bool
    nodes: tuple = ()                    # tuple[ConceptNode, ...]
    approved_count: int = 0
    skipped: tuple[str, ...] = ()        # 未批准/前置未批准的候选 id
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "nodes": [node.to_dict() for node in self.nodes],
            "approved_count": self.approved_count,
            "skipped": list(self.skipped),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
