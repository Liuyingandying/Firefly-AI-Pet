# -*- coding: utf-8 -*-
"""EvidenceBridge — LearningEvidence → MasteryEvidenceInput 适配器（M0.7）。

Learning Core 边界的**防御性转换层**::

    EvidenceBuilder(M0.6)   ──证据──▶  LearningEvidence
    EvidenceBridge(M0.7)    ──适配──▶  MasteryEvidenceInput × N（每概念一条）
                             │
                             └─▶ Rule Engine / Review Scheduler / Mastery（M1 消费,
                                 本阶段零修改零调用）

转换规则（M0.7 验收口径）:
1. ``experiment_run``  → ``basic``      基础学习证据;
2. ``param_comparison`` → ``reinforced`` 强化学习证据;
3. 失败证据 → 拒绝转换（rejected）。M0.6 builder 保证失败结果不会成为
   LearningEvidence（返回 None）, 因此失败在本边界表现为零置信/非法记录,
   桥按防御口径拒绝: confidence ≤ 0 / 非 LearningEvidence / 未知 kind;
4. 空 concept → 安全丢弃（discarded, 非错误）; 多概念证据中混有空串时
   逐概念过滤, 其余照常接受。

纯内存、无 LLM、无 UI、无数据库。
"""

from __future__ import annotations

from core.learning.evidence.schema import (
    KIND_COMPARISON,
    KIND_RUN,
    LearningEvidence,
)
from core.learning.evidence_bridge.schema import (
    EVIDENCE_TYPE_BASIC,
    EVIDENCE_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    BridgeOutcome,
    MasteryEvidenceInput,
)

#: kind → evidence_type 映射（封闭词表, 未知 kind 拒绝）
_KIND_TO_EVIDENCE_TYPE = {
    KIND_RUN: EVIDENCE_TYPE_BASIC,
    KIND_COMPARISON: EVIDENCE_TYPE_REINFORCED,
}

#: 直连运行（无 experiment_id）时的溯源占位
_DIRECT_RUN_SOURCE = "direct_run"


class EvidenceBridge:
    """LearningEvidence → Learning Core 适配层。"""

    def convert(self, evidence: LearningEvidence | None) -> BridgeOutcome:
        """转换一条证据; 拒绝/丢弃均返回零产出的结构化结果（不抛异常）。"""
        # ---- 规则 3（防御部分）: 非法/失败证据拒绝 ------------------------
        if not isinstance(evidence, LearningEvidence):
            return BridgeOutcome(status=STATUS_REJECTED, reason="invalid_evidence")
        if evidence.confidence <= 0.0:
            return BridgeOutcome(status=STATUS_REJECTED, reason="failed_evidence")
        evidence_type = _KIND_TO_EVIDENCE_TYPE.get(evidence.kind)
        if evidence_type is None:
            return BridgeOutcome(status=STATUS_REJECTED, reason="unknown_evidence_type")

        # ---- 规则 4: 空 concept 安全丢弃（逐概念过滤） ---------------------
        valid_concepts = tuple(
            c for c in evidence.concept_ids if isinstance(c, str) and c.strip()
        )
        discarded = tuple(
            c for c in evidence.concept_ids
            if not (isinstance(c, str) and c.strip())
        )
        if not valid_concepts:
            return BridgeOutcome(
                status=STATUS_DISCARDED,
                reason="empty_concept",
                discarded_concepts=discarded,
            )

        # ---- 规则 1/2: 按类型生成输入（每概念一条） ------------------------
        source = evidence.experiment_id or _DIRECT_RUN_SOURCE
        inputs = tuple(
            MasteryEvidenceInput(
                concept_id=concept,
                evidence_type=evidence_type,
                source=source,
                confidence=evidence.confidence,
                timestamp=evidence.timestamp,
                evidence_id=evidence.evidence_id,
            )
            for concept in valid_concepts
        )
        return BridgeOutcome(
            status=STATUS_ACCEPTED,
            inputs=inputs,
            discarded_concepts=discarded,
        )

    def convert_all(
        self, evidences: list[LearningEvidence] | tuple[LearningEvidence, ...]
    ) -> tuple[MasteryEvidenceInput, ...]:
        """批量转换, 仅拼接被接受的输入（拒绝/丢弃静默跳过）。

        逐条审计用 :meth:`convert`（带 BridgeOutcome）。
        """
        inputs: list[MasteryEvidenceInput] = []
        for evidence in evidences or ():
            outcome = self.convert(evidence)
            inputs.extend(outcome.inputs)
        return tuple(inputs)
