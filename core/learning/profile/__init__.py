# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.9 — Learner Profile + Memory Consolidation。

长期学习画像聚合（Memory/Store/KG 只读）+ 错误模式聚合器;
不修改 Memory 核心 / LearningStore 核心 / KnowledgeGraph 数据结构 /
TJULLM Adapter / SkillLoop 核心。
"""

from core.learning.profile.builder import LearnerProfileBuilder
from core.learning.profile.consolidator import (
    ConsolidatedWeakness,
    LearningMemoryConsolidator,
)
from core.learning.profile.schema import (
    ActivitySnapshot,
    KnowledgeSnapshot,
    LearnerProfile,
    LearningBehavior,
    ProjectSnapshot,
)

__all__ = [
    "LearnerProfileBuilder",
    "LearningMemoryConsolidator",
    "LearnerProfile",
    "KnowledgeSnapshot",
    "LearningBehavior",
    "ActivitySnapshot",
    "ProjectSnapshot",
    "ConsolidatedWeakness",
]
