# -*- coding: utf-8 -*-
"""Mastery Adapter schema — MasteryEvidenceInput → MasterySignal 协议（M1.0）。

设计约束：
- 只读消费 ``core.learning.evidence_bridge``（M0.7）的 ``MasteryEvidenceInput``,
  类型词表直接复用其常量（单一词表来源, 不复制数值）;
- ``MasterySignal`` 是掌握度聚合（Mastery 0–5 / 复习调度）的**输入信号**——
  本阶段只定义并产出它, 不修改任何下游模块;
- ``strength`` 语义 = 上游 confidence 的**直接继承**（协议规定禁止二次计算,
  聚合策略属 M1 消费方）;
- 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from core.learning.evidence_bridge.schema import (
    EVIDENCE_TYPE_BASIC,
    EVIDENCE_TYPE_REINFORCED,
)

# ---------------------------------------------------------------------------
# 信号类型词表（与 M0.7 evidence_type 同源, 直接引用其常量）
# ---------------------------------------------------------------------------

SIGNAL_TYPE_BASIC = EVIDENCE_TYPE_BASIC              # "basic"
SIGNAL_TYPE_REINFORCED = EVIDENCE_TYPE_REINFORCED    # "reinforced"

# ---------------------------------------------------------------------------
# 转换终态（与 M0.7 BridgeOutcome 同构）
# ---------------------------------------------------------------------------

STATUS_ACCEPTED = "accepted"      # 产出 1 条 MasterySignal
STATUS_REJECTED = "rejected"      # 非法输入拒绝, 零产出
STATUS_DISCARDED = "discarded"    # 空 concept 安全丢弃, 零产出


@dataclass(frozen=True)
class MasterySignal:
    """掌握度聚合层的标准化输入信号。

    六个规约字段全部为上游值的直接继承（零计算）——适配层只做
    类型/词表/边界校验, 不产生任何新数值。
    """

    concept_id: str
    #: SIGNAL_TYPE_BASIC / SIGNAL_TYPE_REINFORCED（继承 evidence_type）
    signal_type: str
    #: 溯源（继承 MasteryEvidenceInput.source）
    source: str
    #: = 上游 confidence（协议规定直接继承, 禁止二次计算）
    strength: float
    #: UTC-ISO8601 字符串（继承上游时间戳）
    timestamp: str
    #: 回链 M0.6 LearningEvidence 的 id（继承 MasteryEvidenceInput.evidence_id）
    evidence_id: str = ""

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "signal_type": self.signal_type,
            "source": self.source,
            "strength": self.strength,
            "timestamp": self.timestamp,
            "evidence_id": self.evidence_id,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class AdapterOutcome:
    """一次转换的完整结果（与 M0.7 BridgeOutcome 同构）。

    - ``accepted``:  signal 非 None;
    - ``rejected``:  非法输入, reason 说明原因, signal=None;
    - ``discarded``: 空 concept, reason="empty_concept", signal=None（非错误）。
    """

    status: str
    signal: MasterySignal | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "signal": self.signal.to_dict() if self.signal is not None else None,
            "reason": self.reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
