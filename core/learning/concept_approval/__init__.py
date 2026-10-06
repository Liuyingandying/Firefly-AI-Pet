# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.6 — Concept Approval Bridge（ConceptProposal → ConceptNode）。

显式确认转换桥: 零自动批准、构造前三检查（id/环/实验存在）、对象交付;
**永不写入 KnowledgeGraph**（注入是调用方的构造注入）;
不修改 KnowledgeGraph/ConceptNode, 无 LLM。
"""

from core.learning.concept_approval.approver import ConceptApprover
from core.learning.concept_approval.schema import (
    ERR_EXPLICIT_APPROVAL_REQUIRED,
    ERR_INVALID_INPUT,
    REJECT_CYCLE,
    REJECT_DUPLICATE,
    REJECT_INVALID_ID,
    REJECT_NOT_APPROVED,
    ApprovalResult,
    is_valid_concept_id,
)

__all__ = [
    "ConceptApprover",
    "ApprovalResult",
    "is_valid_concept_id",
    "ERR_EXPLICIT_APPROVAL_REQUIRED",
    "ERR_INVALID_INPUT",
    "REJECT_NOT_APPROVED",
    "REJECT_INVALID_ID",
    "REJECT_DUPLICATE",
    "REJECT_CYCLE",
]
