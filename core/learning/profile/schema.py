# -*- coding: utf-8 -*-
"""Learner Profile schema — 长期学习画像数据结构（M4.9）。

分四组嵌套 frozen dataclass, 全部 JSON 原生类型。
不保存聊天全文, 不保存 CoT。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 知识掌握
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KnowledgeSnapshot:
    """概念掌握快照（来源: LearningStore + KnowledgeGraph）。"""

    concept_mastery: dict[str, float] = field(default_factory=dict)   # cid → 0–1
    strong_concepts: tuple[str, ...] = ()      # mastery ≥ 0.6
    weak_concepts: tuple[str, ...] = ()        # mastery < 0.3 或有错误模式

    def to_dict(self) -> dict:
        return {
            "concept_mastery": dict(self.concept_mastery),
            "strong_concepts": list(self.strong_concepts),
            "weak_concepts": list(self.weak_concepts),
        }


# ---------------------------------------------------------------------------
# 学习行为
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearningBehavior:
    """学习行为与错误模式（来源: Memory + Judge 错误聚合）。"""

    learning_preferences: tuple[str, ...] = ()         # Memory preference 事实
    common_error_patterns: tuple[str, ...] = ()        # 聚合后的长期弱点描述
    preferred_explanation_style: str = ""               # Memory preference 推断

    def to_dict(self) -> dict:
        return {
            "learning_preferences": list(self.learning_preferences),
            "common_error_patterns": list(self.common_error_patterns),
            "preferred_explanation_style": self.preferred_explanation_style,
        }


# ---------------------------------------------------------------------------
# 活动快照
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActivitySnapshot:
    """近期学习活动（来源: LearningStore sessions + review_items）。"""

    recent_learning_events: tuple[str, ...] = ()       # 最近学习事件摘要
    last_studied_concepts: tuple[str, ...] = ()
    review_due: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "recent_learning_events": list(self.recent_learning_events),
            "last_studied_concepts": list(self.last_studied_concepts),
            "review_due": list(self.review_due),
        }


# ---------------------------------------------------------------------------
# 项目上下文
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectSnapshot:
    """项目上下文（来源: Memory project_context + KG related_experiments）。"""

    active_projects: tuple[str, ...] = ()
    research_topics: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "active_projects": list(self.active_projects),
            "research_topics": list(self.research_topics),
        }


# ---------------------------------------------------------------------------
# LearnerProfile（顶层聚合）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearnerProfile:
    """长期学习画像（跨源聚合只读快照）。"""

    user_id: str = "default"
    knowledge: KnowledgeSnapshot = field(default_factory=KnowledgeSnapshot)
    learning_behavior: LearningBehavior = field(default_factory=LearningBehavior)
    activity: ActivitySnapshot = field(default_factory=ActivitySnapshot)
    projects: ProjectSnapshot = field(default_factory=ProjectSnapshot)
    metadata: dict = field(default_factory=dict)        # generated_at / source_summary

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "knowledge": self.knowledge.to_dict(),
            "learning_behavior": self.learning_behavior.to_dict(),
            "activity": self.activity.to_dict(),
            "projects": self.projects.to_dict(),
            "metadata": dict(self.metadata),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def render_for_brain(self) -> str:
        """渲染为 Brain system prompt 可读的学习画像摘要。"""
        k, b, a, p = self.knowledge, self.learning_behavior, self.activity, self.projects
        lines = ["[Learner Profile]"]
        if k.strong_concepts:
            lines.append(f"已掌握概念: {', '.join(k.strong_concepts)}")
        if k.weak_concepts:
            lines.append(f"薄弱概念: {', '.join(k.weak_concepts)}")
        if b.common_error_patterns:
            lines.append(f"常见错误模式: {'; '.join(b.common_error_patterns)}")
        if b.learning_preferences:
            lines.append(f"学习偏好: {', '.join(b.learning_preferences)}")
        if a.review_due:
            lines.append(f"到期复习: {', '.join(a.review_due)}")
        if a.last_studied_concepts:
            lines.append(f"最近学习: {', '.join(a.last_studied_concepts)}")
        if p.active_projects:
            lines.append(f"活跃项目: {', '.join(p.active_projects)}")
        return "\n".join(lines)


__all__ = [
    "KnowledgeSnapshot",
    "LearningBehavior",
    "ActivitySnapshot",
    "ProjectSnapshot",
    "LearnerProfile",
]
