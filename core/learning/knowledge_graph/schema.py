# -*- coding: utf-8 -*-
"""Knowledge Graph schema — 概念知识图协议（M1.2）。

设计约束：
- 纯数据知识图（ConceptNode 由人工/课程侧预置, 不由 LLM 生成）;
- ``EvidenceLinker`` 只读消费 ``core.learning.mastery_integration``（M1.1）
  的 ``RuleEvaluationInput``, 输出 ``ConceptLearningContext``;
- 未知概念**安全返回**（不抛异常）, 空 prerequisite 合法;
- 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConceptNode:
    """一个学习概念的静态知识节点。

    prerequisites          直接前置概念 id 列表（空合法——入门概念无前置）
    related_experiments    可观察该概念的实验 id 列表（对应 experiments/ 注册表）
    """

    concept_id: str
    name: str
    chapter: str = ""
    prerequisites: tuple[str, ...] = ()
    related_experiments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.concept_id:
            raise ValueError("concept_id 不能为空")
        if not self.name:
            raise ValueError(f"concept {self.concept_id!r} 缺少 name")

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "chapter": self.chapter,
            "prerequisites": list(self.prerequisites),
            "related_experiments": list(self.related_experiments),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class ConceptLearningContext:
    """EvidenceLinker 的输出：一次学习事件挂接到知识图后的上下文。

    ``known=False`` 表示未知概念/非法输入的**安全返回**——节点字段留空,
    事件回显字段在输入合法时仍然填充（事件本身不因概念未知而丢失）。
    """

    concept_id: str
    known: bool = False
    # ---- 节点侧（known=True 时填充） ----
    concept_name: str = ""
    chapter: str = ""
    prerequisites: tuple[str, ...] = ()
    related_experiments: tuple[str, ...] = ()
    #: 拓扑有序的学习路径（前置在前, 含自身）; 目标处于环上时为空
    learning_path: tuple[str, ...] = ()
    # ---- 事件回显（输入为合法 RuleEvaluationInput 时填充） ----
    event_type: str = ""
    strength: float = 0.0
    timestamp: str = ""
    evidence_id: str = ""
    #: 非法输入时给出机器可读原因（"invalid_input"/"empty_concept"）
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "known": self.known,
            "concept_name": self.concept_name,
            "chapter": self.chapter,
            "prerequisites": list(self.prerequisites),
            "related_experiments": list(self.related_experiments),
            "learning_path": list(self.learning_path),
            "event_type": self.event_type,
            "strength": self.strength,
            "timestamp": self.timestamp,
            "evidence_id": self.evidence_id,
            "reason": self.reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
