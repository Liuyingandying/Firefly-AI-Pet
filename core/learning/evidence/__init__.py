# -*- coding: utf-8 -*-
"""Firefly Learning Mode M0.6 — Evidence Adapter（SimulationResult → LearningEvidence）。

只读消费 simulation（M0.4）与 explanation（M0.5）, 不修改任何上游模块;
纯内存, 不接数据库/LLM/UI。
"""

from core.learning.evidence.evidence_builder import EvidenceBuilder
from core.learning.evidence.schema import (
    KIND_COMPARISON,
    KIND_RUN,
    LearningEvidence,
)

__all__ = ["EvidenceBuilder", "LearningEvidence", "KIND_RUN", "KIND_COMPARISON"]
