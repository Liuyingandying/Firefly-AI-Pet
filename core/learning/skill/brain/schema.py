# -*- coding: utf-8 -*-
"""Learning Brain schema — TJULLM 学习决策大脑的协议（M4.8 阶段 1）。

设计约束:
- ``LearnerProfile`` 是跨源聚合的只读画像（store mastery + memory + KG）;
- ``SkillBrainRequest`` 是 Brain 的输入（状态+词表+目标）;
- ``SkillBrainResponse`` 是 Brain 的输出（decision + 简短理由, **不存 CoT**）;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.learning.skill.schema import SkillDecision


# ---------------------------------------------------------------------------
# LearnerProfile（Brain 视角的学习者画像）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearnerProfile:
    """跨源聚合的学习者画像（只读, Brain 决策依据）。"""

    user_id: str = "default"
    concept_mastery: dict[str, float] = field(default_factory=dict)  # cid → 0–1
    weak_concepts: tuple[str, ...] = ()
    recent_errors: tuple[dict, ...] = ()       # [{"concept_id", "error_type", "at"}]
    review_due: tuple[str, ...] = ()
    active_projects: tuple[str, ...] = ()
    learning_preferences: tuple[str, ...] = ()  # ["实践偏好", "可视化学习"] 等
    available_concepts: tuple[str, ...] = ()    # KG 全量概念词表

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "concept_mastery": dict(self.concept_mastery),
            "weak_concepts": list(self.weak_concepts),
            "recent_errors": [dict(e) for e in self.recent_errors],
            "review_due": list(self.review_due),
            "active_projects": list(self.active_projects),
            "learning_preferences": list(self.learning_preferences),
            "available_concepts": list(self.available_concepts),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# SkillBrainRequest / SkillBrainResponse
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillBrainRequest:
    """Brain 的一次决策请求。"""

    profile: LearnerProfile = field(default_factory=LearnerProfile)
    goal: str = ""
    grounding_text: str = ""                    # M3.5 词表接地文本
    user_query: str = ""

    def to_dict(self) -> dict:
        return {
            "profile": self.profile.to_dict(),
            "goal": self.goal,
            "grounding_text": self.grounding_text,
            "user_query": self.user_query,
        }


@dataclass(frozen=True)
class SkillBrainResponse:
    """Brain 的一次决策响应（不保存 CoT, 只存简短教学理由）。"""

    decision: SkillDecision
    reasoning_summary: str = ""                 # 简短教学原因（非 CoT）
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.to_dict(),
            "reasoning_summary": self.reasoning_summary,
            "confidence": self.confidence,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
