# -*- coding: utf-8 -*-
"""Firefly Learning Mode M0.7 — Evidence Bridge（LearningEvidence → Learning Core）。

只读消费 evidence（M0.6）, 产出掌握度评估输入 DTO;
不修改 Rule Engine / Review Scheduler, 不接 LLM/UI/数据库。
"""

from core.learning.evidence_bridge.bridge import EvidenceBridge
from core.learning.evidence_bridge.schema import (
    EVIDENCE_TYPE_BASIC,
    EVIDENCE_TYPE_REINFORCED,
    STATUS_ACCEPTED,
    STATUS_DISCARDED,
    STATUS_REJECTED,
    BridgeOutcome,
    MasteryEvidenceInput,
)

__all__ = [
    "EvidenceBridge",
    "MasteryEvidenceInput",
    "BridgeOutcome",
    "EVIDENCE_TYPE_BASIC",
    "EVIDENCE_TYPE_REINFORCED",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "STATUS_DISCARDED",
]
