# -*- coding: utf-8 -*-
"""Firefly Learning Mode M4.7 — Multi-turn Skill Loop（持续学习循环）。

MultiTurnSkillLoop：Observe→Decide→Act→Evaluate→Remember→Next 的多轮循环;
双预算（max_steps / max_tool_calls）; 结构化失败（禁止异常逃逸）;
不修改 Memory/Store/KG 核心。
"""

from core.learning.skill.loop.memory_writer import LearningMemoryWriter
from core.learning.skill.loop.runtime import MultiTurnSkillLoop
from core.learning.skill.loop.schema import (
    MemoryWriteRecord,
    SkillLoopResult,
    SkillLoopState,
    SkillStepRecord,
)

__all__ = [
    "MultiTurnSkillLoop",
    "SkillLoopState",
    "SkillStepRecord",
    "SkillLoopResult",
    "MemoryWriteRecord",
    "LearningMemoryWriter",
]
