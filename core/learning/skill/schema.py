# -*- coding: utf-8 -*-
"""Learning Skill schema — 主动学习 Agent 的状态/决策协议（M4.6）。

设计约束:
- ``LearnerState`` 是跨源聚合的只读快照（KG + LearningStore + Memory）,
  cold_start 标记空状态（runtime 据此走 cold_start_probe, 不询问模型）;
- ``LearningAction`` 封闭词表——TJULLM 只能选择词表内动作, 词表外拒绝;
- ``SkillDecision`` 是模型决策的结构化形态, 经 validator 校验后才路由;
- 全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class LearningAction(str, Enum):
    """Skill 动作白名单（封闭; 词表外一律拒绝）。"""

    COLD_START_PROBE = "cold_start_probe"
    PROBE = "probe"
    TEACH = "teach"
    QUIZ = "quiz"
    REVIEW = "review"
    RESEARCH = "research"
    EXPERIMENT = "experiment"
    CONTINUE_PROJECT = "continue_project"


ACTION_VALUES: frozenset[str] = frozenset(a.value for a in LearningAction)


def is_valid_action(action: str) -> bool:
    return str(action) in ACTION_VALUES


# ---------------------------------------------------------------------------
# LearnerState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LearnerState:
    """跨源聚合的学习者状态快照（KG + LearningStore + Memory）。"""

    user_id: str = "default"
    learning_goals: tuple[str, ...] = ()
    active_projects: tuple[str, ...] = ()
    #: concept_id → 归一化掌握度 0.0–1.0（store mastery_level / 5）
    concept_mastery: dict[str, float] = field(default_factory=dict)
    #: [{"id": …, "reason": "wrong_answer"|"low_mastery"}]
    weak_concepts: tuple[dict, ...] = ()
    #: [{"concept_id": …, "score": …, "at": …}]
    recent_errors: tuple[dict, ...] = ()
    review_due: tuple[str, ...] = ()
    unfinished_sessions: tuple[str, ...] = ()
    #: 扩展字段: 全空状态标记（runtime 据此走 cold_start_probe, 不问模型）
    cold_start: bool = True

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "learning_goals": list(self.learning_goals),
            "active_projects": list(self.active_projects),
            "concept_mastery": dict(self.concept_mastery),
            "weak_concepts": [dict(w) for w in self.weak_concepts],
            "recent_errors": [dict(e) for e in self.recent_errors],
            "review_due": list(self.review_due),
            "unfinished_sessions": list(self.unfinished_sessions),
            "cold_start": self.cold_start,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# SkillDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillDecision:
    """模型学习决策（校验后由 runtime 路由到既有能力）。"""

    action: str                            # LearningAction 词表值
    target_concept: str = ""
    reason: str = ""
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "target_concept": self.target_concept,
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class DecisionCheck:
    """validator 的结构化结论（拒绝可区分, 不抛异常）。"""

    ok: bool
    decision: SkillDecision | None = None
    error: str = ""                        # "unknown_action" / "unknown_concept" / "invalid_confidence" / "invalid_input"


@dataclass(frozen=True)
class SkillRunResult:
    """一次 Skill 学习循环的完整结果（成功/失败都是结果, 永不抛异常）。

    ``served_question`` 为脱敏题面（probe/quiz 服务给模型的题;
    expected_answer/evaluation_rule 留在服务端, 答案隔离）。
    """

    success: bool
    decision: SkillDecision | None = None
    state: LearnerState | None = None
    tool_results: tuple[dict, ...] = ()
    response: str = ""
    warnings: tuple[str, ...] = ()
    error: str = ""
    served_question: object | None = None  # M4.1 LearningQuestion（脱敏视图外仍存原对象, 序列化时投影题面）

    def to_dict(self) -> dict:
        served = None
        if self.served_question is not None:
            served = {
                "question_id": self.served_question.question_id,
                "question_type": self.served_question.question_type,
                "difficulty": self.served_question.difficulty,
                "question_text": self.served_question.question_text,
            }
        return {
            "success": self.success,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "state": self.state.to_dict() if self.state is not None else None,
            "tool_results": [dict(t) for t in self.tool_results],
            "response": self.response,
            "warnings": list(self.warnings),
            "error": self.error,
            "served_question": served,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


__all__ = [
    "LearningAction",
    "ACTION_VALUES",
    "is_valid_action",
    "LearnerState",
    "SkillDecision",
    "DecisionCheck",
    "SkillRunResult",
]
