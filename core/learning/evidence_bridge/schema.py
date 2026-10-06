# -*- coding: utf-8 -*-
"""Evidence Bridge schema — LearningEvidence → Learning Core 适配协议（M0.7）。

设计约束：
- 只读消费 ``core.learning.evidence``（M0.6）的 ``LearningEvidence``;
- ``MasteryEvidenceInput`` 是 Learning Core（掌握度评估/规则引擎/复习调度）的
  **输入 DTO**——本阶段只定义并产出它, 不修改任何下游模块;
- 全字段 JSON 原生类型（tuple→list 由 to_dict 转换）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 证据类型词表（Learning Core 消费口径）
# ---------------------------------------------------------------------------

#: 基础学习证据：单次成功实验运行
EVIDENCE_TYPE_BASIC = "basic"
#: 强化学习证据：受控参数变化比较（因果链更明确）
EVIDENCE_TYPE_REINFORCED = "reinforced"

# ---------------------------------------------------------------------------
# 转换终态
# ---------------------------------------------------------------------------

STATUS_ACCEPTED = "accepted"      # 产出 ≥1 条 MasteryEvidenceInput
STATUS_REJECTED = "rejected"      # 拒绝转换（失败证据/非法输入）, 零产出
STATUS_DISCARDED = "discarded"    # 安全丢弃（空 concept）, 零产出


@dataclass(frozen=True)
class MasteryEvidenceInput:
    """掌握度评估的标准化证据输入（Learning Core 消费契约）。

    规约五字段（M0.7 验收口径）+ 一个溯源扩展字段 ``evidence_id``
    （回链 M0.6 LearningEvidence, 供 M1 幂等去重/审计）。
    """

    concept_id: str
    #: EVIDENCE_TYPE_BASIC / EVIDENCE_TYPE_REINFORCED
    evidence_type: str
    #: 溯源（实验 id; 直连运行为 "direct_run"）
    source: str
    confidence: float
    #: UTC-ISO8601 字符串（沿用上游证据时间戳）
    timestamp: str
    #: 扩展字段: 来源 LearningEvidence 的 id
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if not self.concept_id:
            raise ValueError("concept_id 不能为空")
        conf = float(self.confidence)
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence 必须在 [0, 1], 得到 {self.confidence}")
        object.__setattr__(self, "confidence", conf)

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "evidence_type": self.evidence_type,
            "source": self.source,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "evidence_id": self.evidence_id,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class BridgeOutcome:
    """一次转换的完整结果（拒绝/丢弃与接受可区分, 便于验收与审计）。

    - ``accepted``:  inputs 非空;
    - ``rejected``:  零产出, reason 说明拒绝原因（failed_evidence 等）;
    - ``discarded``: 零产出, reason="empty_concept"（安全丢弃, 非错误）。
    """

    status: str
    inputs: tuple[MasteryEvidenceInput, ...] = ()
    reason: str = ""
    #: accepted 时也可能有逐概念丢弃（空字符串 concept 被过滤）
    discarded_concepts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "inputs": [i.to_dict() for i in self.inputs],
            "reason": self.reason,
            "discarded_concepts": list(self.discarded_concepts),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
