# -*- coding: utf-8 -*-
"""Skill loop schema — 多轮学习循环的状态/台账/结果协议（M4.7）。

状态机封闭词表（一轮 = observing → deciding → executing → evaluating →
remembering; 终态 completed / failed）::

    created → observing → deciding → executing → evaluating → remembering
            → (回到 observing 下一轮) → completed / failed

全部 frozen + JSON 原生类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 循环状态词表（封闭）
# ---------------------------------------------------------------------------

LOOP_CREATED = "created"
LOOP_OBSERVING = "observing"
LOOP_DECIDING = "deciding"
LOOP_EXECUTING = "executing"
LOOP_EVALUATING = "evaluating"
LOOP_REMEMBERING = "remembering"
LOOP_COMPLETED = "completed"
LOOP_FAILED = "failed"


@dataclass(frozen=True)
class SkillStepRecord:
    """一轮决策-执行的审计台账。"""

    step_id: str                           # "step-1" …
    action: str                            # LearningAction 词表值
    target: str
    result: dict = field(default_factory=dict)   # 工具/判卷/证据摘要
    observation: str = ""                  # 人读观察（success/weak_point/完成状态）
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "target": self.target,
            "result": dict(self.result),
            "observation": self.observation,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class SkillLoopState:
    """多轮循环的运行状态（learner_state 随每轮 Observe 刷新）。"""

    session_id: str
    goal: str
    learner_state: object | None = None    # M4.6 LearnerState
    current_action: str = ""
    step_count: int = 0
    history: tuple[SkillStepRecord, ...] = ()
    observations: tuple[str, ...] = ()
    status: str = LOOP_CREATED
    # ---- 扩展（审计） ----
    decision: object | None = None         # 最近一轮 M4.6 SkillDecision
    status_history: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "goal": self.goal,
            "learner_state": (
                self.learner_state.to_dict() if self.learner_state is not None else None
            ),
            "current_action": self.current_action,
            "step_count": self.step_count,
            "history": [h.to_dict() for h in self.history],
            "observations": list(self.observations),
            "status": self.status,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "status_history": list(self.status_history),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class MemoryWriteRecord:
    """一次 Memory 写入的结构化记录（ok=False = 写失败, 循环不阻断）。"""

    ok: bool
    memory_id: str = ""
    content: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "memory_id": self.memory_id,
            "content": self.content,
            "error": self.error,
        }


@dataclass(frozen=True)
class SkillLoopResult:
    """一次多轮循环的结构化结果（成功/失败都是结果, 永不抛异常）。"""

    success: bool
    state: SkillLoopState | None = None
    final_message: str = ""
    warnings: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "state": self.state.to_dict() if self.state is not None else None,
            "final_message": self.final_message,
            "warnings": list(self.warnings),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
