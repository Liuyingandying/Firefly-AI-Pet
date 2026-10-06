# -*- coding: utf-8 -*-
"""Mastery Integration schema — MasterySignal → RuleEvaluationInput 协议（M1.1）。

设计约束：
- 只读消费 ``core.learning.mastery_adapter``（M1.0）的 ``MasterySignal``;
- ``RuleEvaluationInput`` 是规则引擎（Rule Engine）的**输入事件**——本阶段
  只定义并产出它, 不修改/不调用 Rule Engine 与 Review Scheduler;
- 词表换轨：Learning 侧的 ``basic``/``reinforced`` 信号类型在本边界翻译为
  规则引擎的事件词表 ``normal_learning_event``/``strong_learning_event``
  （双射映射, 无信息损失）;
- ``strength`` 语义 = 上游值的**原值保持**（协议规定禁止重新计算,
  事件权重策略属规则引擎侧）;
- 全字段 JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 规则引擎事件词表（M1.1 边界专属）
# ---------------------------------------------------------------------------

#: 基础学习信号 → 普通学习事件
EVENT_TYPE_NORMAL = "normal_learning_event"
#: 强化学习信号 → 强学习事件
EVENT_TYPE_STRONG = "strong_learning_event"

# ---------------------------------------------------------------------------
# 转换终态（与 M0.7/M1.0 同构）
# ---------------------------------------------------------------------------

STATUS_ACCEPTED = "accepted"      # 产出 1 条 RuleEvaluationInput
STATUS_REJECTED = "rejected"      # 非法信号拒绝, 零产出
STATUS_DISCARDED = "discarded"    # 空 concept 安全丢弃, 零产出


@dataclass(frozen=True)
class RuleEvaluationInput:
    """规则引擎的标准化学习事件输入。

    规约六字段。注意 ``signal_type`` 在本边界承载**规则引擎事件词表**
    （normal_learning_event / strong_learning_event）——这是任务规约的
    字段命名, 语义为"该信号翻译成的学习事件类型"。
    """

    concept_id: str
    #: EVENT_TYPE_NORMAL / EVENT_TYPE_STRONG（由上游 signal_type 双射映射）
    signal_type: str
    #: 证据溯源（继承上游 source, 字段改名以匹配规则引擎消费口径）
    evidence_source: str
    #: = 上游 strength（原值保持, 禁止重新计算）
    strength: float
    #: UTC-ISO8601 字符串（全链单一时间源：M0.6 打点, 逐层透传）
    timestamp: str
    #: 回链 M0.6 LearningEvidence 的 id（继承上游回链）
    evidence_id: str = ""

    def to_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "signal_type": self.signal_type,
            "evidence_source": self.evidence_source,
            "strength": self.strength,
            "timestamp": self.timestamp,
            "evidence_id": self.evidence_id,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class AdapterOutcome:
    """一次转换的完整结果（与 M0.7 BridgeOutcome / M1.0 AdapterOutcome 同构）。

    - ``accepted``:  event 非 None;
    - ``rejected``:  非法信号, reason 说明原因, event=None;
    - ``discarded``: 空 concept, reason="empty_concept", event=None（非错误）。
    """

    status: str
    event: RuleEvaluationInput | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "event": self.event.to_dict() if self.event is not None else None,
            "reason": self.reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
