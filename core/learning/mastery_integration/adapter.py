# -*- coding: utf-8 -*-
"""RuleEngineAdapter — MasterySignal → RuleEvaluationInput 适配器（M1.1）。

Learning 信号侧 → 规则引擎输入侧的最后一段适配::

    MasteryAdapter(M1.0)      ──适配──▶  MasterySignal
    RuleEngineAdapter(M1.1)   ──适配──▶  RuleEvaluationInput（学习事件）
                                │
                                └─▶ Rule Engine / Review Scheduler（M1 消费,
                                    本阶段零修改零调用）

转换规则（M1.1 验收口径）:
1. ``basic`` 信号      → ``normal_learning_event`` 普通学习事件;
2. ``reinforced`` 信号 → ``strong_learning_event`` 强学习事件;
3. ``strength`` 原值保持, 不重新计算——本层零数值运算;
4. 非法输入 → 拒绝（rejected）: None / 非 MasterySignal /
   未知 signal_type / strength 越界或 NaN;
5. 空 concept → 安全丢弃（discarded, 非错误）。

纯内存、无 LLM、无 UI、无数据库、不触课程存储。
"""

from __future__ import annotations

from core.learning.mastery_adapter.schema import (
    SIGNAL_TYPE_BASIC,
    SIGNAL_TYPE_REINFORCED,
    MasterySignal,
)
from core.learning.mastery_integration.schema import (
    EVENT_TYPE_NORMAL,
    EVENT_TYPE_STRONG,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    RuleEvaluationInput,
)

#: signal_type → 事件类型双射映射（封闭词表, 未知信号拒绝）
_SIGNAL_TO_EVENT = {
    SIGNAL_TYPE_BASIC: EVENT_TYPE_NORMAL,
    SIGNAL_TYPE_REINFORCED: EVENT_TYPE_STRONG,
}


class RuleEngineAdapter:
    """MasterySignal → RuleEvaluationInput 适配层（零数值计算）。"""

    def convert(self, signal: MasterySignal | None) -> AdapterOutcome:
        """转换一条掌握度信号; 拒绝/丢弃均返回零产出结构化结果。"""
        # ---- 规则 4: 非法信号拒绝 ------------------------------------------
        if not isinstance(signal, MasterySignal):
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_signal")
        event_type = _SIGNAL_TO_EVENT.get(signal.signal_type)
        if event_type is None:
            return AdapterOutcome(status=STATUS_REJECTED, reason="unknown_signal_type")
        try:
            strength = float(signal.strength)
        except (TypeError, ValueError):
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_strength")
        if not (0.0 <= strength <= 1.0) or strength != strength:  # 越界 / NaN
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_strength")

        # ---- 规则 5: 空 concept 安全丢弃 -----------------------------------
        concept_id = signal.concept_id
        if not isinstance(concept_id, str) or not concept_id.strip():
            return AdapterOutcome(status=STATUS_DISCARDED, reason="empty_concept")

        # ---- 规则 1/2/3: 事件映射 + strength 原值保持 -----------------------
        event = RuleEvaluationInput(
            concept_id=concept_id,
            signal_type=event_type,
            evidence_source=signal.source,
            strength=strength,
            timestamp=signal.timestamp,
            evidence_id=signal.evidence_id,
        )
        return AdapterOutcome(status=STATUS_ACCEPTED, event=event)

    def convert_all(
        self, signals: list[MasterySignal] | tuple[MasterySignal, ...]
    ) -> tuple[RuleEvaluationInput, ...]:
        """批量转换, 仅拼接被接受的事件（拒绝/丢弃静默跳过）。

        逐条审计用 :meth:`convert`（带 AdapterOutcome）。
        """
        events: list[RuleEvaluationInput] = []
        for signal in signals or ():
            outcome = self.convert(signal)
            if outcome.event is not None:
                events.append(outcome.event)
        return tuple(events)
