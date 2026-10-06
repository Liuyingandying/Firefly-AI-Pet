# -*- coding: utf-8 -*-
"""MasteryAdapter — MasteryEvidenceInput → MasterySignal 适配器（M1.0）。

掌握度聚合的最后一个适配层（Learning Core 输入侧边界）::

    EvidenceBridge(M0.7)     ──适配──▶  MasteryEvidenceInput
    MasteryAdapter(M1.0)     ──适配──▶  MasterySignal
                              │
                              └─▶ Mastery 聚合 / Review Scheduler（M1 消费,
                                  本阶段零修改零调用）

转换规则（M1.0 验收口径）:
1. ``basic`` 证据  → ``basic`` 信号;
2. ``reinforced`` 证据 → ``reinforced`` 信号;
3. 非法输入 → 拒绝（rejected）: None / 非 MasteryEvidenceInput /
   未知 evidence_type / confidence 越界;
4. 空 concept → 安全丢弃（discarded, 非错误）;
5. ``strength`` = 上游 confidence **直接继承, 禁止二次计算**——适配层
   是纯类型/词表/边界校验器, 零数值运算。

纯内存、无 LLM、无 UI、无数据库。
"""

from __future__ import annotations

from core.learning.evidence_bridge.schema import MasteryEvidenceInput
from core.learning.mastery_adapter.schema import (
    SIGNAL_TYPE_BASIC,
    SIGNAL_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    AdapterOutcome,
    MasterySignal,
)

#: evidence_type → signal_type 校验词表（封闭; 未知类型拒绝）
_KNOWN_EVIDENCE_TYPES = frozenset({SIGNAL_TYPE_BASIC, SIGNAL_TYPE_REINFORCED})


class MasteryAdapter:
    """MasteryEvidenceInput → MasterySignal 适配层（零数值计算）。"""

    def convert(self, evidence_input: MasteryEvidenceInput | None) -> AdapterOutcome:
        """转换一条掌握度证据输入; 拒绝/丢弃均返回零产出结构化结果。"""
        # ---- 规则 3: 非法输入拒绝 ------------------------------------------
        if not isinstance(evidence_input, MasteryEvidenceInput):
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_input")
        if evidence_input.evidence_type not in _KNOWN_EVIDENCE_TYPES:
            return AdapterOutcome(
                status=STATUS_REJECTED, reason="unknown_evidence_type"
            )
        try:
            strength = float(evidence_input.confidence)
        except (TypeError, ValueError):
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_confidence")
        if not (0.0 <= strength <= 1.0) or strength != strength:  # 越界 / NaN
            return AdapterOutcome(status=STATUS_REJECTED, reason="invalid_confidence")

        # ---- 规则 4: 空 concept 安全丢弃 -----------------------------------
        concept_id = evidence_input.concept_id
        if not isinstance(concept_id, str) or not concept_id.strip():
            return AdapterOutcome(status=STATUS_DISCARDED, reason="empty_concept")

        # ---- 规则 1/2/5: 类型映射 + 全字段直接继承 --------------------------
        signal = MasterySignal(
            concept_id=concept_id,
            signal_type=evidence_input.evidence_type,
            source=evidence_input.source,
            strength=strength,
            timestamp=evidence_input.timestamp,
            evidence_id=evidence_input.evidence_id,
        )
        return AdapterOutcome(status=STATUS_ACCEPTED, signal=signal)

    def convert_all(
        self,
        evidence_inputs: list[MasteryEvidenceInput] | tuple[MasteryEvidenceInput, ...],
    ) -> tuple[MasterySignal, ...]:
        """批量转换, 仅拼接被接受的信号（拒绝/丢弃静默跳过）。

        逐条审计用 :meth:`convert`（带 AdapterOutcome）。
        """
        signals: list[MasterySignal] = []
        for evidence_input in evidence_inputs or ():
            outcome = self.convert(evidence_input)
            if outcome.signal is not None:
                signals.append(outcome.signal)
        return tuple(signals)
