"""学习状态引擎：掌握度折算、可提取性、状态派生。

设计参考（自研最小实现，非复制）：
- Inno Agent L1 state-engine：证据加权掌握度、可提取性 R = 0.9^(elapsed_days / stability)
- LearningMAP forgetting：稳定性随复习次数增长

约定：
- 时间统一为 UTC ISO-8601 字符串（naive），本模块不感知时区。
- 掌握度权威状态只存于 LearnerProfile.knowledge，图谱上的掌握度只是快照。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Outcome = Literal["correct", "partial", "incorrect"]

# 证据折算档位（参考 Inno Agent evidence bands）
EVIDENCE_BANDS: dict[Outcome, float] = {"correct": 0.85, "partial": 0.5, "incorrect": 0.15}

# 可提取性低于该值判定"复习到期"
REVIEW_RETRIEVABILITY_THRESHOLD = 0.7

# 稳定性基础天数与每次成功复习的增长步长（天）
STABILITY_BASE_DAYS = 7.0
STABILITY_GROWTH_DAYS = 7.0
STABILITY_MAX_DAYS = 365.0


def now_iso() -> str:
    """当前 UTC 时间，ISO-8601 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 ISO 字符串为 naive UTC datetime。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def days_between(start_iso: str, end_iso: str) -> float:
    """两个 ISO 时刻之间的天数差（end - start），允许负值。"""
    return (parse_iso(end_iso) - parse_iso(start_iso)).total_seconds() / 86400.0


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass
class KnowledgeState:
    """单个知识节点的学习状态（持久化于 profile.knowledge[node_id]）。"""

    node_id: str
    mastery: float = 0.0
    confidence: float = 0.0
    stability_days: float = STABILITY_BASE_DAYS
    last_seen_at: str | None = None
    review_due_at: str | None = None
    session_count: int = 0
    # 历史错误描述（来自评估证据）；"误解" 的权威记录在 LearnerProfile.misconceptions
    errors: list[str] = field(default_factory=list)
    # 派生状态标签：unknown / learning / fragile / review_due / stable / misconception
    status: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "mastery": round(self.mastery, 3),
            "confidence": round(self.confidence, 3),
            "stability_days": round(self.stability_days, 3),
            "last_seen_at": self.last_seen_at,
            "review_due_at": self.review_due_at,
            "session_count": self.session_count,
            "errors": list(self.errors),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeState":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Misconception:
    """一条误解记录。resolved_at 为空表示尚未解决（用于教学门控）。"""

    node_id: str
    description: str
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    source: str = "evaluation"

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "description": self.description,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Misconception":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class StateEngine:
    """纯函数状态引擎：证据 -> 掌握度/置信度/稳定性/复习时间。"""

    def retrievability(self, last_seen_at: str, stability_days: float, as_of: str) -> float:
        """可提取性 R = 0.9^(elapsed_days / stability)。"""
        elapsed = max(0.0, days_between(last_seen_at, as_of))
        return clamp(0.9 ** (elapsed / max(0.25, stability_days)))

    def days_until_review(self, stability_days: float) -> float:
        """按 0.7 可提取性阈值反推下次复习所需天数。"""
        return (
            max(0.0, stability_days)
            * math.log(REVIEW_RETRIEVABILITY_THRESHOLD)
            / math.log(0.9)
        )

    def apply_evidence(self, state: KnowledgeState, outcome: Outcome, at: str) -> KnowledgeState:
        """把一条作答证据折算进知识状态（原地更新并返回）。"""
        band = EVIDENCE_BANDS[outcome]
        alpha = 0.6  # 平滑系数：新证据占 60% 权重
        state.mastery = clamp(state.mastery + alpha * (band - state.mastery))
        state.session_count += 1
        state.last_seen_at = at

        # 置信度：答对升、答错降、部分答对小升
        confidence_delta = {"correct": 0.4, "partial": 0.1, "incorrect": -0.3}[outcome]
        state.confidence = clamp(state.confidence + confidence_delta)

        # 稳定性：成功复习增长，失败回缩（参考艾宾浩斯稳定性增长）
        if outcome == "correct":
            state.stability_days = min(
                STABILITY_MAX_DAYS,
                state.stability_days + STABILITY_GROWTH_DAYS,
            )
        elif outcome == "incorrect":
            state.stability_days = max(STABILITY_BASE_DAYS, state.stability_days * 0.6)

        # 复习时间：按当前稳定性反推下次到期点
        from datetime import timedelta

        base_dt = parse_iso(at)
        state.review_due_at = (
            base_dt + timedelta(days=self.days_until_review(state.stability_days))
        ).isoformat(timespec="seconds")

        state.status = self.derive_status(state, at)
        return state

    def derive_status(self, state: KnowledgeState, as_of: str) -> str:
        """派生状态标签（不含误解门控，误解由 profile 层判断）。"""
        if state.session_count == 0 or state.last_seen_at is None:
            return "unknown"
        if state.review_due_at and state.review_due_at <= as_of:
            return "review_due"
        if state.mastery >= 0.8 and state.confidence >= 0.7:
            return "stable"
        if state.mastery >= 0.6:
            return "learning"
        return "fragile"
